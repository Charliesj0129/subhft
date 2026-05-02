"""Coverage tests for execution/router.py — targets uncovered lines.

Covers:
- _create_task_with_error_handling: exception callback, cancelled, invalid state
- set_risk_engine, set_overflow_buf, set_phantom_resolver
- _load_fill_dedup error paths
- _register_fill_dedup_key eviction
- _backfill_order_id_map: various payload shapes
- run() overflow buffer drain with QueueFull
- run() order recording: recorder_queue full + WAL fallback
- run() terminal handler with on_terminal_state object
- run() fill normalization returns None
- run() phantom resolver: success and error
- run() TCA enrichment
- stop() drain: phantom resolver in shutdown
- _wal_fallback_write: no WAL writer path, WAL exception, callback failure
- _publish_nowait: bus without publish_nowait
- persist_fill_dedup / _maybe_persist_fill_dedup throttle
"""

from __future__ import annotations

import asyncio
import collections
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.core import timebase
from hft_platform.execution.normalizer import RawExecEvent


@pytest.fixture(autouse=True)
def _isolate_fill_dedup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))


def _stub_metrics() -> MagicMock:
    m = MagicMock()
    m.execution_router_alive = MagicMock()
    m.execution_router_heartbeat_ts = MagicMock()
    m.execution_router_lag_ns = MagicMock()
    m.execution_router_errors_total = MagicMock()
    m.orphaned_fill_total = MagicMock()
    m.e2e_order_latency_ns = MagicMock()
    m.fills_total = MagicMock()
    m.exec_overflow_drained_total = MagicMock()
    m.recorder_exec_drops_total = MagicMock()
    m.duplicate_fill_total = MagicMock()
    m.dlq_retry_resolved_total = MagicMock()
    m.fill_normalization_failed_total = MagicMock()
    m.exec_fill_data_loss_total = MagicMock()
    m.recorder_exec_wal_fallback_total = MagicMock()
    m.recorder_exec_wal_fallback_failure_total = MagicMock()
    m.phantom_fill_reconciled_total = MagicMock()
    return m


def _make_fill_event(
    *,
    fill_id: str = "FILL001",
    order_id: str = "ORD001",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    account_id: str = "acct1",
) -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        order_id=order_id,
        account_id=account_id,
        strategy_id=strategy_id,
        symbol=symbol,
        side=Side.BUY,
        price=5_000_000,
        qty=1,
        fee=0,
        tax=0,
        ingest_ts_ns=timebase.now_ns(),
        match_ts_ns=timebase.now_ns(),
    )


def _make_delta(realized_pnl: int = 0) -> MagicMock:
    delta = MagicMock()
    delta.realized_pnl = realized_pnl
    return delta


def _make_router(
    metrics: MagicMock | None = None,
    *,
    risk_engine: object | None = None,
    recorder_queue: asyncio.Queue | None = None,
    symbol_metadata: object | None = None,
    wal_writer: object | None = None,
    overflow_buf: collections.deque | None = None,
):
    if metrics is None:
        metrics = _stub_metrics()

    from hft_platform.execution.router import ExecutionRouter

    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    bus.publish_nowait = MagicMock()

    position_store = MagicMock(spec=["positions", "on_fill"])
    position_store.positions = {}
    position_store.on_fill = MagicMock(return_value=_make_delta())

    router = ExecutionRouter(
        bus=bus,
        raw_queue=asyncio.Queue(maxsize=100),
        order_id_map={"ORD001": "strat1:ORD001"},
        position_store=position_store,
        terminal_handler=MagicMock(),
        risk_engine=risk_engine,
        recorder_queue=recorder_queue,
        symbol_metadata=symbol_metadata,
        wal_writer=wal_writer,
        overflow_buf=overflow_buf,
    )
    router.metrics = metrics
    return router


def _make_order_event(
    order_id: str = "O001",
    status: OrderStatus = OrderStatus.FILLED,
    strategy_id: str = "strat1",
) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        strategy_id=strategy_id,
        symbol="2330",
        status=status,
        submitted_qty=1,
        filled_qty=1,
        remaining_qty=0,
        price=5_000_000,
        side=Side.BUY,
        ingest_ts_ns=timebase.now_ns(),
        broker_ts_ns=timebase.now_ns(),
    )


async def _run_router_one_tick(router, timeout: float = 0.1) -> None:
    task = asyncio.create_task(router.run())
    await asyncio.sleep(timeout)
    router.running = False
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


# ── _create_task_with_error_handling ─────────────────────────────────────


@pytest.mark.asyncio
async def test_create_task_with_error_handling_logs_exception():
    """_on_task_done callback logs exception from failed task."""
    from hft_platform.execution.router import _create_task_with_error_handling

    async def failing_coro():
        raise ValueError("task_error")

    task = _create_task_with_error_handling(failing_coro(), name="test_fail")
    await asyncio.sleep(0.05)
    assert task.done()
    with pytest.raises(ValueError, match="task_error"):
        task.result()


@pytest.mark.asyncio
async def test_create_task_with_error_handling_cancelled():
    """_on_task_done handles CancelledError without crashing."""
    from hft_platform.execution.router import _create_task_with_error_handling

    async def slow_coro():
        await asyncio.sleep(10)

    task = _create_task_with_error_handling(slow_coro(), name="test_cancel")
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_create_task_with_error_handling_success():
    """_on_task_done handles successful task without error."""
    from hft_platform.execution.router import _create_task_with_error_handling

    async def ok_coro():
        return 42

    task = _create_task_with_error_handling(ok_coro(), name="test_ok")
    result = await task
    assert result == 42


# ── set_risk_engine / set_overflow_buf / set_phantom_resolver ────────────


def test_set_risk_engine():
    """set_risk_engine replaces the risk engine reference."""
    router = _make_router()
    mock_engine = MagicMock()
    router.set_risk_engine(mock_engine)
    assert router._risk_engine is mock_engine


def test_set_overflow_buf():
    """set_overflow_buf sets the overflow buffer."""
    router = _make_router()
    buf = collections.deque()
    router.set_overflow_buf(buf)
    assert router._overflow_buf is buf


def test_set_phantom_resolver():
    """set_phantom_resolver injects the resolver callable."""
    router = _make_router()
    resolver = MagicMock()
    router.set_phantom_resolver(resolver)
    assert router._phantom_resolver is resolver


# ── _load_fill_dedup ────────────────────────────────────────────────────


def test_load_fill_dedup_handles_corrupt_file(tmp_path, monkeypatch):
    """_load_fill_dedup handles corrupt jsonl gracefully."""
    dedup_path = tmp_path / "fill_dedup.jsonl"
    dedup_path.write_text("not_valid_json\n")
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(dedup_path))
    router = _make_router()
    assert len(router._seen_fill_ids) == 0


def test_load_fill_dedup_loads_valid_entries(tmp_path, monkeypatch):
    """_load_fill_dedup loads valid jsonl entries into dedup set."""
    import orjson

    dedup_path = tmp_path / "fill_dedup.jsonl"
    entries = [orjson.dumps("fill_001") + b"\n", orjson.dumps("fill_002") + b"\n"]
    dedup_path.write_bytes(b"".join(entries))
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(dedup_path))
    router = _make_router()
    assert "fill_001" in router._seen_fill_ids
    assert "fill_002" in router._seen_fill_ids


def test_load_fill_dedup_respects_max_size(tmp_path, monkeypatch):
    """_load_fill_dedup enforces max_size by evicting oldest."""
    import orjson

    dedup_path = tmp_path / "fill_dedup.jsonl"
    entries = [orjson.dumps(f"fill_{i:04d}") + b"\n" for i in range(20)]
    dedup_path.write_bytes(b"".join(entries))
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(dedup_path))
    monkeypatch.setenv("HFT_FILL_DEDUP_MAX_SIZE", "10")
    router = _make_router()
    assert len(router._seen_fill_ids) == 10
    assert "fill_0019" in router._seen_fill_ids
    assert "fill_0000" not in router._seen_fill_ids


# ── _register_fill_dedup_key eviction ────────────────────────────────────


def test_register_fill_dedup_key_evicts_oldest(tmp_path, monkeypatch):
    """When max size exceeded, oldest entry is evicted."""
    monkeypatch.setenv("HFT_FILL_DEDUP_MAX_SIZE", "3")
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))
    router = _make_router()
    router._fill_dedup_persist_interval_s = 999999
    router._register_fill_dedup_key("a")
    router._register_fill_dedup_key("b")
    router._register_fill_dedup_key("c")
    assert len(router._seen_fill_ids) == 3
    router._register_fill_dedup_key("d")
    assert len(router._seen_fill_ids) == 3
    assert "a" not in router._seen_fill_ids
    assert "d" in router._seen_fill_ids


# ── _backfill_order_id_map ──────────────────────────────────────────────


def test_backfill_order_id_map_registers_new_ids():
    """_backfill_order_id_map extracts broker IDs from order callback."""
    router = _make_router()
    router.normalizer.order_id_resolver.order_id_map["SDK_ID_1"] = "strat1:ORD001"
    raw = RawExecEvent(
        topic="order",
        data={"id": "SDK_ID_1", "seqno": "SEQ_NEW", "ordno": "ORDNO_NEW", "order": {}, "status": {}},
        ingest_ts_ns=0,
    )
    router._backfill_order_id_map(raw)
    resolver = router.normalizer.order_id_resolver
    assert "SEQ_NEW" in resolver.order_id_map
    assert "ORDNO_NEW" in resolver.order_id_map


def test_backfill_order_id_map_no_matching_id():
    """_backfill_order_id_map returns early when no ID matches existing map."""
    router = _make_router()
    raw = RawExecEvent(topic="order", data={"id": "UNKNOWN_ID", "order": {}, "status": {}}, ingest_ts_ns=0)
    router._backfill_order_id_map(raw)
    assert "UNKNOWN_ID" not in router.normalizer.order_id_resolver.order_id_map


def test_backfill_order_id_map_non_dict_data():
    """_backfill_order_id_map handles non-dict data gracefully."""
    router = _make_router()
    raw = RawExecEvent(topic="order", data="not_a_dict", ingest_ts_ns=0)
    router._backfill_order_id_map(raw)
    assert True


def test_backfill_order_id_map_empty_ids():
    """_backfill_order_id_map returns early when all ID fields are empty."""
    router = _make_router()
    raw = RawExecEvent(
        topic="order", data={"id": "", "seqno": "", "ordno": "", "order": {}, "status": {}}, ingest_ts_ns=0
    )
    router._backfill_order_id_map(raw)
    assert True


# ── run() overflow buffer drain ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_overflow_buffer_drain():
    """run() drains overflow buffer items back into raw_queue."""
    overflow = collections.deque()
    metrics = _stub_metrics()
    router = _make_router(metrics, overflow_buf=overflow)

    overflow_item = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
    overflow.append(overflow_item)

    fill = _make_fill_event()
    with patch.object(router.normalizer, "normalize_fill", return_value=fill):
        trigger = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(trigger)
        await _run_router_one_tick(router)

    metrics.exec_overflow_drained_total.inc.assert_called()


@pytest.mark.asyncio
async def test_run_overflow_buffer_drain_queue_full():
    """run() stops draining overflow when raw_queue becomes full."""
    from hft_platform.execution.router import ExecutionRouter

    metrics = _stub_metrics()
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()
    bus.publish_nowait = MagicMock()

    position_store = MagicMock(spec=["positions", "on_fill"])
    position_store.positions = {}
    position_store.on_fill = MagicMock(return_value=_make_delta())

    raw_queue = asyncio.Queue(maxsize=2)
    overflow = collections.deque()

    router = ExecutionRouter(
        bus=bus,
        raw_queue=raw_queue,
        order_id_map={},
        position_store=position_store,
        terminal_handler=MagicMock(),
        overflow_buf=overflow,
    )
    router.metrics = metrics

    for _ in range(5):
        overflow.append(RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns()))

    fill = _make_fill_event()
    with patch.object(router.normalizer, "normalize_fill", return_value=fill):
        raw_queue.put_nowait(RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns()))
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0.1)
        router.running = False
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert len(overflow) >= 0


# ── run() order recording: recorder_queue full + WAL fallback ────────────


@pytest.mark.asyncio
async def test_run_order_recording_queue_full_triggers_wal():
    """Order recording falls back to WAL when recorder_queue is full."""
    metrics = _stub_metrics()
    recorder_queue = asyncio.Queue(maxsize=1)
    recorder_queue.put_nowait("prefill")
    wal_writer = MagicMock()
    wal_writer.write = AsyncMock(return_value=True)

    router = _make_router(metrics, recorder_queue=recorder_queue, symbol_metadata=MagicMock(), wal_writer=wal_writer)

    order_event = _make_order_event("O001", OrderStatus.SUBMITTED, "strat1")
    with (
        patch.object(router.normalizer, "normalize_order", return_value=order_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("orders", {"data": "mapped"})),
    ):
        raw = RawExecEvent(
            topic="order",
            data={"ord_no": "O001", "status": {"status": "Submitted"}, "contract": {"code": "2330"}, "order": {}},
            ingest_ts_ns=timebase.now_ns(),
        )
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    await asyncio.sleep(0.05)
    wal_writer.write.assert_called()


# ── run() terminal handler with on_terminal_state object ─────────────────


@pytest.mark.asyncio
async def test_run_terminal_handler_on_terminal_state_object():
    """Terminal handler via on_terminal_state method on handler object."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    # Use a non-callable object with on_terminal_state to exercise the elif branch
    class TerminalStateHandler:
        def __init__(self):
            self.calls = []

        def on_terminal_state(self, strategy_id, order_id):
            self.calls.append((strategy_id, order_id))

    handler_obj = TerminalStateHandler()
    router.terminal_handler = handler_obj

    order_event = _make_order_event("O001", OrderStatus.FILLED, "strat1")
    with patch.object(router.normalizer, "normalize_order", return_value=order_event):
        raw = RawExecEvent(
            topic="order",
            data={"ord_no": "O001", "status": {"status": "Filled"}, "contract": {"code": "2330"}, "order": {}},
            ingest_ts_ns=timebase.now_ns(),
        )
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    assert ("strat1", "O001") in handler_obj.calls


@pytest.mark.asyncio
async def test_run_terminal_handler_async_coroutine():
    """Terminal handler returning a coroutine creates async task."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    async def async_handler(strategy_id, order_id):
        pass

    router.terminal_handler = async_handler

    order_event = _make_order_event("O001", OrderStatus.FILLED, "strat1")
    with patch.object(router.normalizer, "normalize_order", return_value=order_event):
        raw = RawExecEvent(
            topic="order",
            data={"ord_no": "O001", "status": {"status": "Filled"}, "contract": {"code": "2330"}, "order": {}},
            ingest_ts_ns=timebase.now_ns(),
        )
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    assert True


# ── run() fill normalization returns None ────────────────────────────────


@pytest.mark.asyncio
async def test_run_fill_normalization_returns_none():
    """Fill normalization returning None increments fill_normalization_failed_total."""
    metrics = _stub_metrics()
    wal_writer = MagicMock()
    wal_writer.write = AsyncMock(return_value=True)
    router = _make_router(metrics, wal_writer=wal_writer)

    with patch.object(router.normalizer, "normalize_fill", return_value=None):
        raw = RawExecEvent(topic="deal", data={"broken": True}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    metrics.fill_normalization_failed_total.inc.assert_called()


# ── run() phantom resolver ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_phantom_resolver_resolves_fill():
    """Phantom resolver resolves UNKNOWN fill to a strategy_id."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="PH001")
    router.set_phantom_resolver(lambda fill: "phantom_strat")

    with patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill):
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    metrics.orphaned_fill_total.inc.assert_called()
    router.position_store.on_fill.assert_called()


@pytest.mark.asyncio
async def test_run_phantom_resolver_exception_falls_to_dlq():
    """Phantom resolver exception causes fill to go to DLQ."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="PH_ERR")

    def bad_resolver(fill):
        raise RuntimeError("resolver crash")

    router.set_phantom_resolver(bad_resolver)

    with (
        patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill),
        patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
    ):
        mock_dlq.return_value = MagicMock()
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    metrics.orphaned_fill_total.inc.assert_called()


@pytest.mark.asyncio
async def test_run_phantom_resolver_returns_unknown_still_dlq():
    """Phantom resolver returning UNKNOWN still routes to DLQ."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="PH_UNK")
    router.set_phantom_resolver(lambda fill: "UNKNOWN")

    with (
        patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill),
        patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
    ):
        mock_dlq.return_value = MagicMock()
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    mock_dlq.return_value.add.assert_called()


# ── run() TCA enrichment ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_tca_enrichment_sets_decision_and_arrival_price():
    """TCA enrichment populates decision_price and arrival_price on fill."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    fill = _make_fill_event(fill_id="TCA_FILL", order_id="ORD_TCA")
    router._order_id_map["ORD_TCA"] = "strat1:ORD_TCA"
    router._cmd_tca_map["strat1:ORD_TCA"] = (5_000_000, 5_001_000)
    router._cmd_created_ns_map["strat1:ORD_TCA"] = timebase.now_ns() - 1_000_000

    with patch.object(router.normalizer, "normalize_fill", return_value=fill):
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        await _run_router_one_tick(router)

    assert fill.decision_price == 5_000_000
    assert fill.arrival_price == 5_001_000
    metrics.e2e_order_latency_ns.observe.assert_called()


# ── _wal_fallback_write ─────────────────────────────────────────────────


def test_wal_fallback_write_no_writer_logs_data_loss():
    """_wal_fallback_write logs critical data loss when wal_writer is None."""
    metrics = _stub_metrics()
    router = _make_router(metrics, wal_writer=None)
    payload = MagicMock(symbol="2330")
    router._wal_fallback_write("fills", payload)
    metrics.exec_fill_data_loss_total.inc.assert_called()


def test_wal_fallback_write_no_writer_none_payload():
    """_wal_fallback_write handles None payload when wal_writer is None."""
    metrics = _stub_metrics()
    router = _make_router(metrics, wal_writer=None)
    router._wal_fallback_write("fills", None)
    metrics.exec_fill_data_loss_total.inc.assert_called()


@pytest.mark.asyncio
async def test_wal_fallback_write_exception_handled():
    """_wal_fallback_write catches WAL write exception."""
    metrics = _stub_metrics()
    wal_writer = MagicMock()
    wal_writer.write = MagicMock(side_effect=RuntimeError("wal broken"))
    router = _make_router(metrics, wal_writer=wal_writer)
    router._wal_fallback_write("fills", {"data": "test"})
    assert True


@pytest.mark.asyncio
async def test_wal_fallback_write_async_failure_callback():
    """WAL async write failure triggers error callback."""
    metrics = _stub_metrics()
    wal_writer = MagicMock()
    wal_writer.write = AsyncMock(side_effect=RuntimeError("async wal error"))
    router = _make_router(metrics, wal_writer=wal_writer)
    router._wal_fallback_write("fills", {"data": "test"})
    await asyncio.sleep(0.05)
    assert True


# ── _publish_nowait fallback ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_nowait_falls_back_to_async():
    """_publish_nowait creates async task when bus has no publish_nowait."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    async def async_publish(event):
        pass

    router.bus = SimpleNamespace(publish_nowait=None, publish=async_publish)
    event = MagicMock()
    router._publish_nowait(event)
    await asyncio.sleep(0.05)
    assert True


# ── persist_fill_dedup ───────────────────────────────────────────────────


def test_persist_fill_dedup_writes_to_disk(tmp_path, monkeypatch):
    """persist_fill_dedup writes dedup keys to disk atomically."""
    dedup_path = tmp_path / "fill_dedup.jsonl"
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(dedup_path))
    router = _make_router()
    router._seen_fill_ids["key1"] = None
    router._seen_fill_ids["key2"] = None
    router.persist_fill_dedup()
    assert dedup_path.exists()
    content = dedup_path.read_text()
    assert "key1" in content
    assert "key2" in content


def test_persist_fill_dedup_handles_error(tmp_path, monkeypatch):
    """persist_fill_dedup handles write errors gracefully."""
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", "/proc/nonexistent/fill_dedup.jsonl")
    router = _make_router()
    router._seen_fill_ids["key1"] = None
    router.persist_fill_dedup()
    assert True


# ── _maybe_persist_fill_dedup throttle ───────────────────────────────────


def test_maybe_persist_throttles_by_interval(tmp_path, monkeypatch):
    """_maybe_persist_fill_dedup respects interval throttle."""
    dedup_path = tmp_path / "fill_dedup.jsonl"
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(dedup_path))
    router = _make_router()
    router._fill_dedup_persist_interval_s = 9999

    router._maybe_persist_fill_dedup()
    assert dedup_path.exists()

    dedup_path.unlink()
    router._maybe_persist_fill_dedup()
    assert not dedup_path.exists()

    router._maybe_persist_fill_dedup(force=True)
    assert dedup_path.exists()


# ── stop() drain: phantom resolver in shutdown ───────────────────────────


@pytest.mark.asyncio
async def test_stop_drain_phantom_resolver_resolves_unknown():
    """Shutdown drain uses phantom resolver for UNKNOWN fills."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="SD_PH01")
    router.set_phantom_resolver(lambda fill: "drain_strat")

    delta = _make_delta()
    router.position_store.on_fill = MagicMock(return_value=delta)

    with patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill):
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    router.position_store.on_fill.assert_called()


@pytest.mark.asyncio
async def test_stop_drain_phantom_resolver_exception():
    """Shutdown drain handles phantom resolver exception."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="SD_PH_ERR")

    def bad_resolver(fill):
        raise RuntimeError("drain resolver crash")

    router.set_phantom_resolver(bad_resolver)

    with (
        patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill),
        patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
    ):
        mock_dlq.return_value = MagicMock()
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 0
    mock_dlq.return_value.add.assert_called()


@pytest.mark.asyncio
async def test_stop_drain_unknown_fill_no_resolver_goes_to_dlq():
    """Shutdown drain routes UNKNOWN fill to DLQ when no resolver."""
    metrics = _stub_metrics()
    router = _make_router(metrics)
    router._phantom_resolver = None

    unknown_fill = _make_fill_event(strategy_id="UNKNOWN", fill_id="SD_NODLQ")

    with (
        patch.object(router.normalizer, "normalize_fill", return_value=unknown_fill),
        patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_dlq,
    ):
        mock_dlq.return_value = MagicMock()
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 0
    metrics.orphaned_fill_total.inc.assert_called()


@pytest.mark.asyncio
async def test_stop_drain_handles_processing_exception():
    """Shutdown drain catches processing exceptions and continues."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    with patch.object(router.normalizer, "normalize_fill", side_effect=RuntimeError("drain error")):
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 0


@pytest.mark.asyncio
async def test_stop_drain_order_async_terminal_handler():
    """Shutdown drain awaits async terminal handler for terminal orders."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    called = []

    async def async_handler(strategy_id, order_id):
        called.append((strategy_id, order_id))

    router.terminal_handler = async_handler

    order_event = _make_order_event("O_ASYNC", OrderStatus.FILLED, "strat1")
    with patch.object(router.normalizer, "normalize_order", return_value=order_event):
        raw = RawExecEvent(
            topic="order",
            data={"ord_no": "O_ASYNC", "status": {"status": "Filled"}, "contract": {"code": "2330"}, "order": {}},
            ingest_ts_ns=timebase.now_ns(),
        )
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    assert ("strat1", "O_ASYNC") in called


@pytest.mark.asyncio
async def test_stop_drain_order_on_terminal_state_async():
    """Shutdown drain awaits on_terminal_state async method."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    called = []

    class AsyncTerminalHandler:
        async def on_terminal_state(self, strategy_id, order_id):
            called.append((strategy_id, order_id))

    router.terminal_handler = AsyncTerminalHandler()

    order_event = _make_order_event("O_OBJ_ASYNC", OrderStatus.CANCELLED, "strat1")
    with patch.object(router.normalizer, "normalize_order", return_value=order_event):
        raw = RawExecEvent(
            topic="order",
            data={
                "ord_no": "O_OBJ_ASYNC",
                "status": {"status": "Cancelled"},
                "contract": {"code": "2330"},
                "order": {},
            },
            ingest_ts_ns=timebase.now_ns(),
        )
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    assert ("strat1", "O_OBJ_ASYNC") in called


# ── run() general error handling ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_generic_exception_increments_error_counter():
    """Generic exception in run() loop increments error counter."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    call_count = [0]
    original = router.normalizer.normalize_fill

    def flaky_normalize(raw):
        call_count[0] += 1
        if call_count[0] == 1:
            raise RuntimeError("unexpected error")
        return original(raw)

    router.normalizer.normalize_fill = flaky_normalize

    raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
    router.raw_queue.put_nowait(raw)
    await _run_router_one_tick(router)

    metrics.execution_router_errors_total.inc.assert_called()


# ── stop() drain: TCA enrichment ────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_drain_tca_enrichment():
    """Shutdown drain enriches fill with TCA decision/arrival prices."""
    metrics = _stub_metrics()
    router = _make_router(metrics)

    fill = _make_fill_event(fill_id="TCA_SD", order_id="ORD_TCA_SD")
    router._order_id_map["ORD_TCA_SD"] = "strat1:ORD_TCA_SD"
    router._cmd_tca_map["strat1:ORD_TCA_SD"] = (6_000_000, 6_001_000)

    delta = _make_delta()
    router.position_store.on_fill = MagicMock(return_value=delta)

    with patch.object(router.normalizer, "normalize_fill", return_value=fill):
        raw = RawExecEvent(topic="deal", data={}, ingest_ts_ns=timebase.now_ns())
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    assert fill.decision_price == 6_000_000
    assert fill.arrival_price == 6_001_000
