"""Bug #32-B regression: orphaned fills must persist to ClickHouse.

Symptom: today's R47_MAKER_TMF traded 53 round-trips at broker (per Shioaji
list_profit_loss) but only 35 round-trips landed in hft.fills (platform's CH
table). 36 fills were lost. Root cause = engine restarted 5+ times during
trading; in-flight fill callbacks died with the process. AND:

When a fill DOES arrive but `strategy_id == "UNKNOWN"` (callback couldn't
resolve to a known order_key), the router routes it to the orphaned-fill DLQ
and ``continue``s — bypassing the recorder_queue write. The fill is invisible
to forensic queries on hft.fills.

Fix: even orphaned fills must be sent to recorder_queue (with strategy_id
preserved as "UNKNOWN") so CH has a complete record.
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
    fill_id: str = "FILL_ORPHAN_1"
    order_id: str = "BROKER_v002UNK"
    strategy_id: str = "UNKNOWN"  # ← the orphan condition
    symbol: str = "TMFE6"
    account_id: str = "acct_F002002_6117"
    ingest_ts_ns: int = 1_000_000_000
    decision_price: int = 0
    arrival_price: int = 0
    side: str = "BUY"
    qty: int = 1
    price_scaled: int = 37500_0000


def _make_router_with_recorder(metrics: MagicMock) -> tuple[ExecutionRouter, asyncio.Queue]:
    recorder_q: asyncio.Queue = asyncio.Queue(maxsize=100)
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    position_store = MagicMock()
    position_store.positions = {}
    position_store.on_fill = MagicMock(return_value=MagicMock(realized_pnl=0))

    sym_meta = MagicMock()
    router = ExecutionRouter(
        bus=bus,
        raw_queue=asyncio.Queue(maxsize=100),
        order_id_map={},  # empty → forces UNKNOWN if normalizer doesn't resolve
        position_store=position_store,
        terminal_handler=MagicMock(),
        risk_engine=None,
        recorder_queue=recorder_q,
        symbol_metadata=sym_meta,
    )
    router.metrics = metrics
    router._phantom_resolver = None  # force DLQ path (no phantom resolution)
    return router, recorder_q


@pytest.mark.asyncio
async def test_orphaned_fill_is_recorded_to_clickhouse_queue() -> None:
    """An orphaned fill (strategy_id=UNKNOWN) must still reach recorder_queue
    so it lands in hft.fills with strategy_id='UNKNOWN' for forensic visibility."""
    metrics = _stub_metrics()
    router, recorder_q = _make_router_with_recorder(metrics)

    fill = _StubFillEvent()
    raw = RawExecEvent(
        topic="deal",
        data={"ordno": fill.order_id, "code": fill.symbol},
        ingest_ts_ns=1_000_000_000,
    )

    with (
        patch.object(router.normalizer, "normalize_fill", return_value=fill),
        patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
        patch(
            "hft_platform.recorder.mapper.map_event_to_record",
            return_value=("hft.fills", {"strategy_id": "UNKNOWN", "fill_id": fill.fill_id}),
        ),
    ):
        mock_dlq.return_value = MagicMock(add=MagicMock())
        await router.raw_queue.put(raw)

        async def _run_briefly():
            router.running = True
            await asyncio.wait_for(
                router._run_loop_iteration() if hasattr(router, "_run_loop_iteration") else router.run(), timeout=0.5
            )

        # Cleaner: drive one loop iteration manually
        try:
            router.running = True
            task = asyncio.create_task(router.run())
            await asyncio.sleep(0.1)
            router.running = False
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        except (RuntimeError, asyncio.CancelledError):
            pass

    # The orphan fill MUST have been queued to recorder
    assert not recorder_q.empty(), (
        "Orphaned fill (strategy_id=UNKNOWN) was NOT written to recorder_queue. "
        "This is the bug — fills disappear from hft.fills when order_key is unresolved."
    )
    item = recorder_q.get_nowait()
    assert item["topic"] == "hft.fills"
    assert item["data"]["strategy_id"] == "UNKNOWN"
    assert item["data"]["fill_id"] == fill.fill_id

    # AND the existing DLQ behavior is preserved
    mock_dlq.return_value.add.assert_called_once()
    metrics.orphaned_fill_total.inc.assert_called()
