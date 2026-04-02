"""Tests for empty fill_id dedup synthesis in ExecutionRouter and normalizer.

Covers:
- Empty fill_id gets synthetic dedup key and dedup works
- Normal fill_id dedup unchanged
- Duplicate fills with empty fill_id are caught
- Normalizer generates synthetic fill_id when seqno is absent
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.router import ExecutionRouter, _synthesize_dedup_key

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
    m.synthetic_fill_id_total = MagicMock()
    m.e2e_order_latency_ns = MagicMock()
    m.exec_overflow_drained_total = MagicMock()
    m.recorder_exec_drops_total = MagicMock()
    return m


def _make_fill(
    *,
    fill_id: str = "FILL001",
    symbol: str = "2330",
    side: Side = Side.BUY,
    price: int = 1_000_000,
    qty: int = 1,
    match_ts_ns: int = 1_000_000_000,
    strategy_id: str = "strat1",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        account_id="acct1",
        order_id="ORD001",
        strategy_id=strategy_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        fee=0,
        tax=0,
        ingest_ts_ns=match_ts_ns - 100,
        match_ts_ns=match_ts_ns,
    )


def _make_deal_raw(
    *,
    strategy_id: str = "strat1",
    order_id: str = "ORD001",
    symbol: str = "2330",
    price: float = 100.0,
    qty: int = 1,
    seqno: str = "FILL001",
    ingest_ts_ns: int = 1_000_000_000,
) -> RawExecEvent:
    data: dict = {
        "ordno": order_id,
        "code": symbol,
        "action": "Buy",
        "price": price,
        "quantity": qty,
        "account_id": "acct1",
        "custom_field": strategy_id,
        "ts": 1_000_000_000,
    }
    if seqno:
        data["seqno"] = seqno
    return RawExecEvent(topic="deal", data=data, ingest_ts_ns=ingest_ts_ns)


@pytest.fixture(autouse=True)
def _patch_metrics(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    stub = _stub_metrics()
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: stub),
    )
    return stub


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
            account_id="acct1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=1,
            avg_price=1_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            delta_source="FILL",
        )
    )
    ps.on_fill_async = AsyncMock(
        return_value=PositionDelta(
            account_id="acct1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=1,
            avg_price=1_000_000,
            realized_pnl=0,
            unrealized_pnl=0,
            delta_source="FILL",
        )
    )
    return ps


@pytest.fixture()
def terminal_handler() -> MagicMock:
    return MagicMock()


# ===========================================================================
# 1. _synthesize_dedup_key unit tests
# ===========================================================================


class TestSynthesizeDedupKey:
    """Unit tests for _synthesize_dedup_key helper."""

    def test_produces_deterministic_key(self) -> None:
        fill = _make_fill(fill_id="", symbol="TMFD6", side=Side.BUY, price=500_000, qty=1, match_ts_ns=999)
        key = _synthesize_dedup_key(fill)
        assert key == f"TMFD6|ORD001|{Side.BUY}|500000|1|999"

    def test_same_fill_produces_same_key(self) -> None:
        f1 = _make_fill(fill_id="", symbol="2330", side=Side.SELL, price=100, qty=5, match_ts_ns=123)
        f2 = _make_fill(fill_id="", symbol="2330", side=Side.SELL, price=100, qty=5, match_ts_ns=123)
        assert _synthesize_dedup_key(f1) == _synthesize_dedup_key(f2)

    def test_different_fields_produce_different_keys(self) -> None:
        f1 = _make_fill(fill_id="", symbol="2330", side=Side.BUY, price=100, qty=5, match_ts_ns=123)
        f2 = _make_fill(fill_id="", symbol="2330", side=Side.SELL, price=100, qty=5, match_ts_ns=123)
        assert _synthesize_dedup_key(f1) != _synthesize_dedup_key(f2)


# ===========================================================================
# 2. Router dedup with empty fill_id
# ===========================================================================


class TestRouterEmptyFillIdDedup:
    """Ensure empty fill_id fills are deduplicated via synthetic key."""

    @pytest.mark.asyncio()
    async def test_empty_fill_id_duplicate_is_skipped(
        self, bus: MagicMock, position_store: MagicMock, terminal_handler: MagicMock
    ) -> None:
        """Two identical fills with empty fill_id: second should be skipped."""
        raw_queue: asyncio.Queue = asyncio.Queue()
        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={"ORD001": "strat1:intent1"},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )
        # Patch normalizer to return fills with empty fill_id
        fill = _make_fill(fill_id="", symbol="2330", side=Side.BUY, price=1_000_000, qty=1, match_ts_ns=1_000_000_000)

        call_count = 0
        original_normalize = router.normalizer.normalize_fill

        def _mock_normalize(raw: RawExecEvent) -> FillEvent:
            nonlocal call_count
            call_count += 1
            return fill

        router.normalizer.normalize_fill = _mock_normalize  # type: ignore[method-assign]

        # Enqueue two identical deal events
        raw1 = _make_deal_raw(seqno="")
        raw2 = _make_deal_raw(seqno="")
        await raw_queue.put(raw1)
        await raw_queue.put(raw2)

        # Process first fill
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0.05)
        router.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # position_store.on_fill_async should be called only once (second is deduped)
        assert position_store.on_fill_async.call_count == 1
        # duplicate_fill_total should be incremented once
        router.metrics.duplicate_fill_total.inc.assert_called()

    @pytest.mark.asyncio()
    async def test_normal_fill_id_dedup_still_works(
        self, bus: MagicMock, position_store: MagicMock, terminal_handler: MagicMock
    ) -> None:
        """Normal (non-empty) fill_id dedup continues to work."""
        raw_queue: asyncio.Queue = asyncio.Queue()
        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={"ORD001": "strat1:intent1"},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )
        fill = _make_fill(fill_id="BROKER_SEQ_42")

        def _mock_normalize(raw: RawExecEvent) -> FillEvent:
            return fill

        router.normalizer.normalize_fill = _mock_normalize  # type: ignore[method-assign]

        raw1 = _make_deal_raw(seqno="BROKER_SEQ_42")
        raw2 = _make_deal_raw(seqno="BROKER_SEQ_42")
        await raw_queue.put(raw1)
        await raw_queue.put(raw2)

        task = asyncio.create_task(router.run())
        await asyncio.sleep(0.05)
        router.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert position_store.on_fill_async.call_count == 1

    @pytest.mark.asyncio()
    async def test_different_empty_fill_ids_are_not_deduped(
        self, bus: MagicMock, position_store: MagicMock, terminal_handler: MagicMock
    ) -> None:
        """Two fills with empty fill_id but different fields should both process."""
        raw_queue: asyncio.Queue = asyncio.Queue()
        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={"ORD001": "strat1:intent1"},
            position_store=position_store,
            terminal_handler=terminal_handler,
        )

        fills = [
            _make_fill(fill_id="", symbol="2330", side=Side.BUY, price=1_000_000, qty=1, match_ts_ns=100),
            _make_fill(fill_id="", symbol="2330", side=Side.BUY, price=1_000_000, qty=1, match_ts_ns=200),
        ]
        fill_iter = iter(fills)

        def _mock_normalize(raw: RawExecEvent) -> FillEvent:
            return next(fill_iter)

        router.normalizer.normalize_fill = _mock_normalize  # type: ignore[method-assign]

        await raw_queue.put(_make_deal_raw(seqno=""))
        await raw_queue.put(_make_deal_raw(seqno=""))

        task = asyncio.create_task(router.run())
        await asyncio.sleep(0.05)
        router.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Both fills have different match_ts_ns so both should process
        assert position_store.on_fill_async.call_count == 2


# ===========================================================================
# 3. Normalizer synthetic fill_id generation
# ===========================================================================


class TestNormalizerSyntheticFillId:
    """Test that normalize_fill generates synthetic fill_id when seqno is absent."""

    def test_missing_seqno_produces_synthetic_fill_id(self) -> None:
        normalizer = ExecutionNormalizer()
        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.0,
                "quantity": 1,
                "account_id": "acct1",
                "custom_field": "strat1",
                "ts": 1_000_000_000,
                # No seqno or seq_no
            },
            ingest_ts_ns=1_000_000_000,
        )
        fill = normalizer.normalize_fill(raw)
        assert fill is not None
        assert fill.fill_id.startswith("synth_")
        assert "2330" in fill.fill_id
        assert "BUY" in fill.fill_id
        normalizer.metrics.synthetic_fill_id_total.inc.assert_called_once()

    def test_present_seqno_uses_broker_fill_id(self) -> None:
        normalizer = ExecutionNormalizer()
        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "2330",
                "action": "Buy",
                "price": 100.0,
                "quantity": 1,
                "seqno": "BROKER_42",
                "account_id": "acct1",
                "custom_field": "strat1",
                "ts": 1_000_000_000,
            },
            ingest_ts_ns=1_000_000_000,
        )
        fill = normalizer.normalize_fill(raw)
        assert fill is not None
        assert fill.fill_id == "BROKER_42"

    def test_empty_string_seqno_produces_synthetic(self) -> None:
        normalizer = ExecutionNormalizer()
        raw = RawExecEvent(
            topic="deal",
            data={
                "code": "TMFD6",
                "action": "Sell",
                "price": 50.0,
                "quantity": 2,
                "seqno": "",
                "account_id": "acct1",
                "custom_field": "strat1",
                "ts": 2_000_000_000,
            },
            ingest_ts_ns=2_000_000_000,
        )
        fill = normalizer.normalize_fill(raw)
        assert fill is not None
        assert fill.fill_id.startswith("synth_")
        assert "TMFD6" in fill.fill_id
        assert "SELL" in fill.fill_id

    def test_synthetic_fill_id_deterministic(self) -> None:
        """Same input produces same synthetic fill_id for dedup consistency."""
        normalizer = ExecutionNormalizer()
        data = {
            "code": "2330",
            "action": "Buy",
            "price": 100.0,
            "quantity": 1,
            "account_id": "acct1",
            "custom_field": "strat1",
            "ts": 1_000_000_000,
        }
        raw1 = RawExecEvent(topic="deal", data=dict(data), ingest_ts_ns=1_000_000_000)
        raw2 = RawExecEvent(topic="deal", data=dict(data), ingest_ts_ns=1_000_000_000)
        fill1 = normalizer.normalize_fill(raw1)
        fill2 = normalizer.normalize_fill(raw2)
        assert fill1 is not None and fill2 is not None
        assert fill1.fill_id == fill2.fill_id
