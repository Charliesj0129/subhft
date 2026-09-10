"""A recovered fill is not an orphaned fill.

``orphaned_fill_total`` is declared as "Orphaned fills routed to DLQ" and
``OrphanedFillDetected`` pages on ``increase(orphaned_fill_total[5m]) > 0``.
The main execution loop bumped it on **both** branches of the phantom check --
the one that routes an unattributable fill to the DLQ, and the one that
successfully resolves the fill back to its strategy. So a working recovery
raised the same warning as a lost fill.

Measured on THESHOW 2026-09-10, in the four hours after the order path was
restored: ``orphaned_fill_total`` 4, ``phantom_fill_reconciled_total`` 2. Half
the warnings that alert sent were reporting the recovery mechanism working
correctly.

The shutdown-drain path twenty lines below implements the same decision and
never had this bug, so the two paths disagreed with each other about what the
counter meant. These tests pin both.

Where the fills come from: ``place_order`` occasionally exceeds
``HFT_API_TIMEOUT_S`` (3.0 s, against a measured p99 of 695 ms). The mutating
guard then refuses to retry, correctly, because the call may still land at the
broker. When it does land, the fill arrives with no matching order and the
phantom resolver is what attributes it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter


@pytest.fixture(autouse=True)
def _isolate_fill_dedup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    for name in (
        "execution_router_alive",
        "execution_router_heartbeat_ts",
        "execution_router_lag_ns",
        "execution_router_errors_total",
        "execution_events_total",
        "orphaned_fill_total",
        "phantom_fill_reconciled_total",
        "position_pnl_realized",
        "e2e_order_latency_ns",
        "fills_total",
        "exec_overflow_drained_total",
        "recorder_exec_drops_total",
        "duplicate_fill_total",
        "dlq_retry_resolved_total",
        "fill_normalization_failed_total",
    ):
        setattr(m, name, MagicMock())
    return m


@dataclasses.dataclass
class _StubFillEvent:
    fill_id: str = "FILL_PHANTOM_1"
    order_id: str = "BROKER_v002PHANTOM"
    strategy_id: str = "UNKNOWN"  # the condition that sends it down the phantom path
    symbol: str = "TMFI6"
    account_id: str = "acct_test"
    ingest_ts_ns: int = 1_000_000_000
    decision_price: int = 0
    arrival_price: int = 0
    side: str = "BUY"
    qty: int = 1
    price_scaled: int = 37500_0000


def _make_router(metrics: MagicMock, resolver) -> tuple[ExecutionRouter, asyncio.Queue]:
    recorder_q: asyncio.Queue = asyncio.Queue(maxsize=100)
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    position_store = MagicMock()
    position_store.positions = {}
    position_store.on_fill = MagicMock(return_value=MagicMock(realized_pnl=0))
    router = ExecutionRouter(
        bus=bus,
        raw_queue=asyncio.Queue(maxsize=100),
        order_id_map={},
        position_store=position_store,
        terminal_handler=MagicMock(),
        risk_engine=None,
        recorder_queue=recorder_q,
        symbol_metadata=MagicMock(),
    )
    router.metrics = metrics
    router._phantom_resolver = resolver
    return router, recorder_q


async def _drive_one_fill(router: ExecutionRouter, recorder_q: asyncio.Queue, fill) -> None:
    """Run the router until it has handled the queued event.

    Polls the recorder queue instead of sleeping a fixed interval: the loop
    normally finishes in well under a millisecond, and a fixed sleep would
    either be wasteful or flaky depending on the machine.
    """
    raw = RawExecEvent(topic="deal", data={"ordno": fill.order_id, "code": fill.symbol}, ingest_ts_ns=1_000_000_000)
    await router.raw_queue.put(raw)
    router.running = True
    task = asyncio.create_task(router.run())
    try:
        for _ in range(200):
            if not recorder_q.empty():
                break
            await asyncio.sleep(0.001)
    finally:
        router.running = False
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


class TestARecoveredFillIsNotCountedAsOrphaned:
    @pytest.mark.asyncio
    async def test_a_reconciled_fill_does_not_bump_the_orphan_counter(self) -> None:
        """This increment is what fired OrphanedFillDetected on a success."""
        metrics = _stub_metrics()
        router, recorder_q = _make_router(metrics, resolver=lambda _fill: "R47_MAKER_TMF")
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "R47_MAKER_TMF", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            await _drive_one_fill(router, recorder_q, fill)

            metrics.orphaned_fill_total.inc.assert_not_called()
            mock_dlq.return_value.add.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_reconciled_fill_is_counted_as_a_reconciliation(self) -> None:
        """A recovery that leaves no trace looks like a fill that never arrived."""
        metrics = _stub_metrics()
        router, recorder_q = _make_router(metrics, resolver=lambda _fill: "R47_MAKER_TMF")
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "R47_MAKER_TMF", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            await _drive_one_fill(router, recorder_q, fill)

            metrics.phantom_fill_reconciled_total.inc.assert_called_once()


class TestAGenuinelyLostFillIsStillCounted:
    @pytest.mark.asyncio
    async def test_an_unresolvable_fill_still_bumps_the_orphan_counter(self) -> None:
        """The alert must keep firing on the case it exists for."""
        metrics = _stub_metrics()
        router, recorder_q = _make_router(metrics, resolver=None)
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "UNKNOWN", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            await _drive_one_fill(router, recorder_q, fill)

            metrics.orphaned_fill_total.inc.assert_called()
            mock_dlq.return_value.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_resolver_that_returns_unknown_is_not_a_resolution(self) -> None:
        """ "UNKNOWN" back from the resolver means it could not attribute it."""
        metrics = _stub_metrics()
        router, recorder_q = _make_router(metrics, resolver=lambda _fill: "UNKNOWN")
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "UNKNOWN", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            await _drive_one_fill(router, recorder_q, fill)

            metrics.orphaned_fill_total.inc.assert_called()
            metrics.phantom_fill_reconciled_total.inc.assert_not_called()


class TestTheShutdownDrainAgreesWithTheMainLoop:
    """The two paths implement the same decision and must report it the same way."""

    @pytest.mark.asyncio
    async def test_a_reconciliation_during_shutdown_is_counted(self) -> None:
        metrics = _stub_metrics()
        router, _recorder_q = _make_router(metrics, resolver=lambda _fill: "R47_MAKER_TMF")
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "R47_MAKER_TMF", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            router.raw_queue.put_nowait(
                RawExecEvent(topic="deal", data={"ordno": fill.order_id, "code": fill.symbol}, ingest_ts_ns=1)
            )
            await router.stop(drain_timeout_s=1.0)

            metrics.phantom_fill_reconciled_total.inc.assert_called_once()
            metrics.orphaned_fill_total.inc.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_unresolvable_fill_during_shutdown_still_counts_as_orphaned(self) -> None:
        metrics = _stub_metrics()
        router, _recorder_q = _make_router(metrics, resolver=None)
        fill = _StubFillEvent()

        with (
            patch.object(router.normalizer, "normalize_fill", return_value=fill),
            patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
            patch(
                "hft_platform.recorder.mapper.map_event_to_record",
                return_value=("hft.fills", {"strategy_id": "UNKNOWN", "fill_id": fill.fill_id}),
            ),
        ):
            mock_dlq.return_value = MagicMock(add=MagicMock())
            router.raw_queue.put_nowait(
                RawExecEvent(topic="deal", data={"ordno": fill.order_id, "code": fill.symbol}, ingest_ts_ns=1)
            )
            await router.stop(drain_timeout_s=1.0)

            metrics.orphaned_fill_total.inc.assert_called()
            metrics.phantom_fill_reconciled_total.inc.assert_not_called()
