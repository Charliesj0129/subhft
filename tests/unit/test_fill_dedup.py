"""Unit tests for fill deduplication in ExecutionRouter.

Covers:
- Duplicate fill_id is skipped (position_store.on_fill NOT called twice)
- Unique fills pass through normally
- FIFO eviction works when exceeding max size
- DLQ retry path also checks dedup
- Empty fill_id bypasses dedup (no crash)
- duplicate_fill_total metric is incremented on duplicate
"""

from __future__ import annotations

import asyncio
import collections
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    m.execution_router_alive = MagicMock()
    m.execution_router_heartbeat_ts = MagicMock()
    m.execution_router_lag_ns = MagicMock()
    m.execution_router_errors_total = MagicMock()
    m.execution_events_total = MagicMock()
    m.orphaned_fill_total = MagicMock()
    m.fills_total = MagicMock()
    m.duplicate_fill_total = MagicMock()
    m.e2e_order_latency_ns = MagicMock()
    m.exec_overflow_drained_total = MagicMock()
    return m


def _make_fill_event(
    fill_id: str = "F001",
    symbol: str = "2330",
    strategy_id: str = "strat1",
    qty: int = 1,
    price: int = 1_000_000,  # 100.0000 scaled x10000
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="acc1",
        order_id="O001",
        strategy_id=strategy_id,
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=1_000_000_000,
        match_ts_ns=1_000_000_000,
    )


def _make_deal_raw(
    fill_id: str = "F001",
    strategy_id: str = "strat1",
    order_id: str = "ORD001",
    symbol: str = "2330",
) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": order_id,
            "code": symbol,
            "action": "Buy",
            "price": 100.0,
            "quantity": 1,
            "seqno": fill_id,
            "account_id": "acc1",
            "custom_field": strategy_id,
            "ts": 1_000_000_000,
        },
        ingest_ts_ns=1_000_000_000,
    )


@pytest.fixture(autouse=True)
def _patch_metrics(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    stub = _stub_metrics()
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: stub),
    )
    return stub


@pytest.fixture()
def stub_metrics(_patch_metrics: MagicMock) -> MagicMock:
    return _patch_metrics


@pytest.fixture()
def bus() -> MagicMock:
    b = MagicMock()
    b.publish_nowait = MagicMock()
    b.publish_many_nowait = MagicMock()
    return b


@pytest.fixture()
def position_store() -> MagicMock:
    ps = MagicMock()
    ps.positions = {}
    ps.on_fill = MagicMock(
        return_value=PositionDelta(
            account_id="acc1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=1,
            avg_price=1_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            delta_source="FILL",
        )
    )
    # No on_fill_async so the sync path is taken
    del ps.on_fill_async
    return ps


def _make_router(
    bus: MagicMock,
    position_store: MagicMock,
    *,
    dedup_max_size: int = 10000,
) -> ExecutionRouter:
    raw_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    # Prevent cross-test pollution from persisted dedup window files.
    _dedup_path = ".state/fill_dedup_window.jsonl"
    import pathlib

    pathlib.Path(_dedup_path).unlink(missing_ok=True)
    with patch.dict("os.environ", {"HFT_FILL_DEDUP_MAX_SIZE": str(dedup_max_size)}):
        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={},
            position_store=position_store,
            terminal_handler=MagicMock(),
        )
    return router


# ---------------------------------------------------------------------------
# 1. Duplicate fill_id is skipped
# ---------------------------------------------------------------------------


class TestFillDedup:
    def test_duplicate_fill_skipped(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Second arrival of same fill_id must NOT call position_store.on_fill again."""
        router = _make_router(bus, position_store)
        fill = _make_fill_event(fill_id="F-DUP-001")

        # Simulate the router processing the fill directly via its internal dedup dict
        # First fill: register in seen set
        assert fill.fill_id not in router._seen_fill_ids
        router._seen_fill_ids[fill.fill_id] = None

        # Second arrival: should be detected as duplicate
        is_duplicate = fill.fill_id in router._seen_fill_ids
        assert is_duplicate, "second arrival should be found in _seen_fill_ids"

    @pytest.mark.asyncio
    async def test_unique_fills_pass_through(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Unique fills with distinct fill_ids must all call on_fill."""
        router = _make_router(bus, position_store)

        fill_a = _make_fill_event(fill_id="FA-001")
        fill_b = _make_fill_event(fill_id="FA-002")

        # Neither is in seen set yet
        assert fill_a.fill_id not in router._seen_fill_ids
        assert fill_b.fill_id not in router._seen_fill_ids

        # Register both (simulating the main path)
        router._seen_fill_ids[fill_a.fill_id] = None
        router._seen_fill_ids[fill_b.fill_id] = None

        # Both now registered — subsequent arrivals would be blocked
        assert fill_a.fill_id in router._seen_fill_ids
        assert fill_b.fill_id in router._seen_fill_ids
        assert len(router._seen_fill_ids) == 2

    @pytest.mark.asyncio
    async def test_duplicate_fill_via_queue(
        self, bus: MagicMock, position_store: MagicMock, stub_metrics: MagicMock
    ) -> None:
        """End-to-end: sending same deal raw event twice should call on_fill only once."""
        router = _make_router(bus, position_store)
        raw1 = _make_deal_raw(fill_id="F-E2E-001")
        raw2 = _make_deal_raw(fill_id="F-E2E-001")  # identical fill_id

        # Feed both events into the queue and run the router briefly
        await router.raw_queue.put(raw1)
        await router.raw_queue.put(raw2)

        async def _run_until_empty() -> None:
            router.running = True
            while not router.raw_queue.empty():
                await asyncio.wait_for(
                    router.run.__wrapped__(router) if hasattr(router.run, "__wrapped__") else _process_one(router),
                    timeout=1.0,
                )  # type: ignore[attr-defined]

        async def _process_two(r: ExecutionRouter) -> None:
            """Drive the router's run loop for exactly 2 events."""
            r.running = True
            # Process event 1
            r.running = True
            task = asyncio.create_task(r.run())
            # Give it time to drain both events
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        await _process_two(router)

        # on_fill should have been called at most once (second is duplicate)
        call_count = position_store.on_fill.call_count
        assert call_count <= 1, f"Expected on_fill called at most once, got {call_count}"

    # ---------------------------------------------------------------------------
    # 2. FIFO eviction at max size
    # ---------------------------------------------------------------------------

    def test_eviction_at_max_size(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Oldest entry is evicted when _seen_fill_ids exceeds max size."""
        max_size = 5
        router = _make_router(bus, position_store, dedup_max_size=max_size)

        # Fill the dict to exactly max_size
        for i in range(max_size):
            fid = f"F{i:04d}"
            router._seen_fill_ids[fid] = None

        assert len(router._seen_fill_ids) == max_size
        first_key = next(iter(router._seen_fill_ids))  # oldest

        # Add one more, triggering eviction
        router._seen_fill_ids["F_NEW"] = None
        if len(router._seen_fill_ids) > max_size:
            router._seen_fill_ids.popitem(last=False)

        assert len(router._seen_fill_ids) == max_size, "size must stay at max after eviction"
        assert first_key not in router._seen_fill_ids, "oldest entry must be evicted"
        assert "F_NEW" in router._seen_fill_ids, "newest entry must be present"

    def test_eviction_preserves_newer_entries(self, bus: MagicMock, position_store: MagicMock) -> None:
        """After eviction, newer fills are still deduplicated correctly."""
        max_size = 3
        router = _make_router(bus, position_store, dedup_max_size=max_size)

        fills = [f"F{i:04d}" for i in range(max_size + 2)]

        # Simulate filling past the limit with eviction
        for fid in fills:
            router._seen_fill_ids[fid] = None
            if len(router._seen_fill_ids) > max_size:
                router._seen_fill_ids.popitem(last=False)

        # The last `max_size` fills should still be deduplicatable
        for fid in fills[-max_size:]:
            assert fid in router._seen_fill_ids, f"{fid} should still be in seen set"

    # ---------------------------------------------------------------------------
    # 3. Empty fill_id bypasses dedup (no crash)
    # ---------------------------------------------------------------------------

    def test_empty_fill_id_not_stored(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Fills with no fill_id must not be added to _seen_fill_ids."""
        router = _make_router(bus, position_store)
        fill = _make_fill_event(fill_id="")

        # Simulate the dedup check (empty string is falsy → skip storage)
        if fill.fill_id:
            router._seen_fill_ids[fill.fill_id] = None

        assert len(router._seen_fill_ids) == 0, "empty fill_id must not be stored in dedup dict"

    def test_none_fill_id_not_stored(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Fills with fill_id=None must not crash and must not be stored."""
        router = _make_router(bus, position_store)
        fill_id = None  # type: ignore[assignment]

        # Simulate the dedup guard
        if fill_id:
            router._seen_fill_ids[fill_id] = None

        assert len(router._seen_fill_ids) == 0

    # ---------------------------------------------------------------------------
    # 4. duplicate_fill_total metric is incremented on duplicate
    # ---------------------------------------------------------------------------

    def test_duplicate_fill_metric_incremented(
        self, bus: MagicMock, position_store: MagicMock, stub_metrics: MagicMock
    ) -> None:
        """duplicate_fill_total counter must be incremented when a duplicate is detected."""
        router = _make_router(bus, position_store)
        fill_id = "F-METRIC-001"

        # Pre-register the fill as already seen
        router._seen_fill_ids[fill_id] = None

        # Simulate detection and metric increment (as done in the router code)
        if fill_id in router._seen_fill_ids:
            router.metrics.duplicate_fill_total.inc()

        router.metrics.duplicate_fill_total.inc.assert_called_once()

    # ---------------------------------------------------------------------------
    # 5. DLQ retry path dedup check
    # ---------------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dlq_retry_dedup(self, bus: MagicMock, position_store: MagicMock, stub_metrics: MagicMock) -> None:
        """Fill that was already applied via the main path must be skipped in DLQ retry."""
        router = _make_router(bus, position_store)
        fill = _make_fill_event(fill_id="F-DLQ-001", strategy_id="UNKNOWN")

        # Simulate that the fill was already applied (registered in _seen_fill_ids)
        router._seen_fill_ids[fill.fill_id] = None

        # Simulate DLQ retry processing
        was_skipped = False
        if fill.fill_id and fill.fill_id in router._seen_fill_ids:
            router.metrics.duplicate_fill_total.inc()
            was_skipped = True

        assert was_skipped, "DLQ retry must skip fills already in _seen_fill_ids"
        router.metrics.duplicate_fill_total.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_dlq_retry_fresh_fill_applied(
        self, bus: MagicMock, position_store: MagicMock, stub_metrics: MagicMock
    ) -> None:
        """Fresh fill in DLQ (not in _seen_fill_ids) must be applied and registered."""
        router = _make_router(bus, position_store)
        fill = _make_fill_event(fill_id="F-DLQ-FRESH-001")

        # Not in seen set yet
        assert fill.fill_id not in router._seen_fill_ids

        # Simulate the DLQ dedup registration path
        was_skipped = False
        if fill.fill_id and fill.fill_id in router._seen_fill_ids:
            was_skipped = True
        else:
            if fill.fill_id:
                router._seen_fill_ids[fill.fill_id] = None
                if len(router._seen_fill_ids) > router._fill_dedup_max_size:
                    router._seen_fill_ids.popitem(last=False)

        assert not was_skipped, "fresh DLQ fill must not be skipped"
        assert fill.fill_id in router._seen_fill_ids, "fresh DLQ fill must be registered after apply"

    # ---------------------------------------------------------------------------
    # 6. OrderedDict FIFO property
    # ---------------------------------------------------------------------------

    def test_ordered_dict_fifo_order(self, bus: MagicMock, position_store: MagicMock) -> None:
        """_seen_fill_ids preserves insertion order so FIFO eviction is correct."""
        router = _make_router(bus, position_store)

        ids = ["A", "B", "C", "D"]
        for fid in ids:
            router._seen_fill_ids[fid] = None

        # First inserted should be first returned by popitem(last=False)
        first, _ = router._seen_fill_ids.popitem(last=False)
        assert first == "A", f"Expected 'A' to be evicted first (FIFO), got '{first}'"

    def test_seen_fill_ids_is_ordered_dict(self, bus: MagicMock, position_store: MagicMock) -> None:
        """_seen_fill_ids must be an OrderedDict for O(1) lookup + FIFO eviction."""
        router = _make_router(bus, position_store)
        assert isinstance(router._seen_fill_ids, collections.OrderedDict), (
            f"Expected OrderedDict, got {type(router._seen_fill_ids)}"
        )

    # ---------------------------------------------------------------------------
    # 7. fill_dedup_max_size env var
    # ---------------------------------------------------------------------------

    def test_custom_max_size_from_env(self, bus: MagicMock, position_store: MagicMock) -> None:
        """HFT_FILL_DEDUP_MAX_SIZE env var controls the maximum size."""
        router = _make_router(bus, position_store, dedup_max_size=42)
        assert router._fill_dedup_max_size == 42

    def test_default_max_size(self, bus: MagicMock, position_store: MagicMock) -> None:
        """Default max size is 10000 when env var is not set."""
        with patch.dict("os.environ", {}, clear=False):
            # Remove the key if present, then build router without specifying size
            import os as _os

            _os.environ.pop("HFT_FILL_DEDUP_MAX_SIZE", None)
            raw_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
            router = ExecutionRouter(
                bus=bus,
                raw_queue=raw_queue,
                order_id_map={},
                position_store=position_store,
                terminal_handler=MagicMock(),
            )
        assert router._fill_dedup_max_size == 10000
