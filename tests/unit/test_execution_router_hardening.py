"""Unit tests for ExecutionRouter hardening fixes X-C1, X-H2, X-H3, X-M2."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter, _synthesize_dedup_key


@pytest.fixture(autouse=True)
def _isolate_fill_dedup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))


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
    m.position_pnl_realized = MagicMock()
    m.e2e_order_latency_ns = MagicMock()
    m.fills_total = MagicMock()
    m.exec_overflow_drained_total = MagicMock()
    m.recorder_exec_drops_total = MagicMock()
    m.duplicate_fill_total = MagicMock()
    m.dlq_retry_resolved_total = MagicMock()
    return m


def _make_fill_event(
    *,
    fill_id: str = "FILL001",
    order_id: str = "ORD001",
    strategy_id: str = "strat1",
    symbol: str = "2330",
    account_id: str = "acct1",
    realized_pnl: int = 500,
) -> MagicMock:
    fill = MagicMock()
    fill.fill_id = fill_id
    fill.order_id = order_id
    fill.strategy_id = strategy_id
    fill.symbol = symbol
    fill.account_id = account_id
    fill.ingest_ts_ns = 1_000_000_000
    fill.decision_price = 0
    fill.arrival_price = 0
    return fill


def _make_delta(realized_pnl: int = 500) -> MagicMock:
    delta = MagicMock()
    delta.realized_pnl = realized_pnl
    return delta


def _make_router(
    metrics: MagicMock,
    *,
    risk_engine: object | None = None,
    recorder_queue: asyncio.Queue | None = None,
    symbol_metadata: object | None = None,
    wal_writer: object | None = None,
) -> ExecutionRouter:
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()

    position_store = MagicMock()
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
        symbol_metadata=symbol_metadata or MagicMock(),
        wal_writer=wal_writer,
    )
    router.metrics = metrics
    return router


def _make_deal_raw(
    order_id: str = "ORD001",
    strategy_id: str = "strat1",
    symbol: str = "2330",
) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": order_id,
            "code": symbol,
            "action": "Buy",
            "price": 500.0,
            "quantity": 1,
            "seqno": "FILL001",
            "account_id": "acct1",
            "custom_field": strategy_id,
            "ts": 1_000_000_000,
        },
        ingest_ts_ns=1_000_000_000,
    )


# ---------------------------------------------------------------------------
# Fix X-H3: _synthesize_dedup_key includes order_id
# ---------------------------------------------------------------------------


def test_synthesize_dedup_key_includes_order_id() -> None:
    """Two fills with different order_id but same price/qty/ts produce different keys."""
    fill_a = MagicMock()
    fill_a.symbol = "2330"
    fill_a.order_id = "ORD001"
    fill_a.side = "Buy"
    fill_a.price = 5000000
    fill_a.qty = 1
    fill_a.match_ts_ns = 1_000_000_000

    fill_b = MagicMock()
    fill_b.symbol = "2330"
    fill_b.order_id = "ORD002"
    fill_b.side = "Buy"
    fill_b.price = 5000000
    fill_b.qty = 1
    fill_b.match_ts_ns = 1_000_000_000

    key_a = _synthesize_dedup_key(fill_a)
    key_b = _synthesize_dedup_key(fill_b)

    assert key_a != key_b
    assert "ORD001" in key_a
    assert "ORD002" in key_b


def test_synthesize_dedup_key_same_order_is_deterministic() -> None:
    """Same fill fields produce identical key (idempotency)."""
    fill = MagicMock()
    fill.symbol = "2330"
    fill.order_id = "ORD001"
    fill.side = "Buy"
    fill.price = 5000000
    fill.qty = 1
    fill.match_ts_ns = 1_000_000_000

    assert _synthesize_dedup_key(fill) == _synthesize_dedup_key(fill)


# ---------------------------------------------------------------------------
# Fix X-C1: shutdown drain uses PositionDelta.realized_pnl via delta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drain_notify_fill_pnl_uses_delta() -> None:
    """Shutdown drain calls notify_fill_pnl with PositionDelta.realized_pnl, not fill attribute."""
    metrics = _stub_metrics()
    risk_engine = MagicMock()
    risk_engine.notify_fill_pnl = MagicMock()

    router = _make_router(metrics, risk_engine=risk_engine)

    delta = _make_delta(realized_pnl=750)
    router.position_store.on_fill = MagicMock(return_value=delta)

    fill_event = _make_fill_event(fill_id="FILL001", realized_pnl=999)

    with patch.object(router.normalizer, "normalize_fill", return_value=fill_event):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        await router.stop(drain_timeout_s=1.0)

    risk_engine.notify_fill_pnl.assert_called_once_with("strat1", 750)


@pytest.mark.asyncio
async def test_shutdown_drain_no_notify_when_pnl_delta_is_zero() -> None:
    """Shutdown drain does NOT call notify_fill_pnl when realized_pnl delta is zero."""
    metrics = _stub_metrics()
    risk_engine = MagicMock()
    risk_engine.notify_fill_pnl = MagicMock()

    router = _make_router(metrics, risk_engine=risk_engine)

    # Position store has existing realized pnl == delta.realized_pnl → delta is 0
    existing_pos = MagicMock()
    existing_pos.realized_pnl_scaled = 750
    router.position_store.positions = {"acct1:strat1:2330": existing_pos}

    delta = _make_delta(realized_pnl=750)
    router.position_store.on_fill = MagicMock(return_value=delta)

    fill_event = _make_fill_event(fill_id="FILL001")

    with patch.object(router.normalizer, "normalize_fill", return_value=fill_event):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        await router.stop(drain_timeout_s=1.0)

    risk_engine.notify_fill_pnl.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_drain_no_notify_when_no_risk_engine() -> None:
    """Shutdown drain skips PnL notification when risk_engine is None."""
    metrics = _stub_metrics()
    router = _make_router(metrics, risk_engine=None)

    delta = _make_delta(realized_pnl=500)
    router.position_store.on_fill = MagicMock(return_value=delta)

    fill_event = _make_fill_event(fill_id="FILL002")

    with patch.object(router.normalizer, "normalize_fill", return_value=fill_event):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1


# ---------------------------------------------------------------------------
# Fix X-M2: shutdown drain uses map_event_to_record for WAL writes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_drain_uses_mapped_recorder_queue() -> None:
    """Shutdown drain writes to recorder_queue via map_event_to_record, not raw FillEvent."""
    metrics = _stub_metrics()
    recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
    router = _make_router(metrics, recorder_queue=recorder_queue)

    fill_event = _make_fill_event(fill_id="FILL003")
    delta = _make_delta(realized_pnl=0)
    router.position_store.on_fill = MagicMock(return_value=delta)

    mapped_payload = {"fill_data": "mapped_record"}
    with (
        patch.object(router.normalizer, "normalize_fill", return_value=fill_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("fills", mapped_payload)),
    ):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        await router.stop(drain_timeout_s=1.0)

    assert recorder_queue.qsize() == 1
    item = recorder_queue.get_nowait()
    assert item == {"topic": "fills", "data": mapped_payload}


@pytest.mark.asyncio
async def test_shutdown_drain_wal_fallback_when_recorder_queue_full() -> None:
    """Shutdown drain falls back to WAL when recorder_queue is full."""
    metrics = _stub_metrics()
    recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    recorder_queue.put_nowait("prefill")  # fill the queue

    wal_writer = MagicMock()
    wal_writer.write = AsyncMock(return_value=True)

    router = _make_router(metrics, recorder_queue=recorder_queue, wal_writer=wal_writer)

    fill_event = _make_fill_event(fill_id="FILL004")
    delta = _make_delta(realized_pnl=0)
    router.position_store.on_fill = MagicMock(return_value=delta)

    mapped_payload = {"fill_data": "mapped_record"}
    with (
        patch.object(router.normalizer, "normalize_fill", return_value=fill_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("fills", mapped_payload)),
    ):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        await router.stop(drain_timeout_s=1.0)

    # Let the WAL async write complete
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    wal_writer.write.assert_awaited_once_with("fills", [mapped_payload])


@pytest.mark.asyncio
async def test_shutdown_drain_wal_fallback_when_no_recorder_queue() -> None:
    """Shutdown drain falls back to WAL when recorder_queue is None (legacy path)."""
    metrics = _stub_metrics()
    wal_writer = MagicMock()
    wal_writer.write = AsyncMock(return_value=True)

    router = _make_router(metrics, recorder_queue=None, wal_writer=wal_writer)
    # Override symbol_metadata to None so the fallback branch is hit
    router._symbol_metadata = None

    fill_event = _make_fill_event(fill_id="FILL005")
    delta = _make_delta(realized_pnl=0)
    router.position_store.on_fill = MagicMock(return_value=delta)

    with patch.object(router.normalizer, "normalize_fill", return_value=fill_event):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)
        await router.stop(drain_timeout_s=1.0)

    await asyncio.sleep(0)
    await asyncio.sleep(0)

    wal_writer.write.assert_awaited_once()
    call_args = wal_writer.write.await_args
    assert call_args[0][0] == "fills"


# ---------------------------------------------------------------------------
# Fix X-H2: orphaned fill retry calls notify_fill_pnl
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_orphaned_fills_calls_notify_fill_pnl() -> None:
    """DLQ retry notifies risk engine with delta.realized_pnl when non-zero."""
    metrics = _stub_metrics()
    risk_engine = MagicMock()
    risk_engine.notify_fill_pnl = MagicMock()

    router = _make_router(metrics, risk_engine=risk_engine)

    fill = _make_fill_event(fill_id="FILL006", strategy_id="strat1")
    delta = _make_delta(realized_pnl=300)
    router.position_store.on_fill = MagicMock(return_value=delta)
    # Remove on_fill_async so synchronous path is used
    del router.position_store.on_fill_async

    with patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_get_dlq:
        mock_dlq = MagicMock()
        mock_dlq.count = 1
        mock_dlq.retry = MagicMock(return_value=([fill], []))
        mock_get_dlq.return_value = mock_dlq
        await router._retry_orphaned_fills()

    risk_engine.notify_fill_pnl.assert_called_once_with("strat1", 300)


@pytest.mark.asyncio
async def test_retry_orphaned_fills_no_notify_when_pnl_zero() -> None:
    """DLQ retry does NOT call notify_fill_pnl when delta.realized_pnl is zero."""
    metrics = _stub_metrics()
    risk_engine = MagicMock()
    risk_engine.notify_fill_pnl = MagicMock()

    router = _make_router(metrics, risk_engine=risk_engine)

    fill = _make_fill_event(fill_id="FILL007", strategy_id="strat1")
    delta = _make_delta(realized_pnl=0)
    router.position_store.on_fill = MagicMock(return_value=delta)
    del router.position_store.on_fill_async

    with patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_get_dlq:
        mock_dlq = MagicMock()
        mock_dlq.count = 1
        mock_dlq.retry = MagicMock(return_value=([fill], []))
        mock_get_dlq.return_value = mock_dlq
        await router._retry_orphaned_fills()

    risk_engine.notify_fill_pnl.assert_not_called()


@pytest.mark.asyncio
async def test_retry_orphaned_fills_no_notify_when_no_risk_engine() -> None:
    """DLQ retry skips PnL notification when risk_engine is None."""
    metrics = _stub_metrics()
    router = _make_router(metrics, risk_engine=None)

    fill = _make_fill_event(fill_id="FILL008", strategy_id="strat1")
    delta = _make_delta(realized_pnl=100)
    router.position_store.on_fill = MagicMock(return_value=delta)
    del router.position_store.on_fill_async

    with patch("hft_platform.execution.fill_dlq.get_orphaned_fill_dlq") as mock_get_dlq:
        mock_dlq = MagicMock()
        mock_dlq.count = 1
        mock_dlq.retry = MagicMock(return_value=([fill], []))
        mock_get_dlq.return_value = mock_dlq
        # Should not raise
        await router._retry_orphaned_fills()

    assert True  # Reached here without AttributeError
