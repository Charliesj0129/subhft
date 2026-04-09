"""Async unit tests for ExecutionRouter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hft_platform.contracts.execution import OrderEvent, PositionDelta
from hft_platform.execution.normalizer import RawExecEvent
from hft_platform.execution.router import ExecutionRouter, _create_task_with_error_handling

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
    return m


def _make_order_raw(
    *,
    status: str = "Filled",
    strategy_id: str = "strat1",
    order_id: str = "ORD001",
    symbol: str = "2330",
    ingest_ts_ns: int = 1_000_000_000,
) -> RawExecEvent:
    return RawExecEvent(
        topic="order",
        data={
            "order": {"ordno": order_id, "action": "Buy", "price": 100, "quantity": 1, "custom_field": strategy_id},
            "status": {"status": status},
            "contract": {"code": symbol},
        },
        ingest_ts_ns=ingest_ts_ns,
    )


def _make_deal_raw(
    *,
    strategy_id: str = "strat1",
    order_id: str = "ORD001",
    symbol: str = "2330",
    price: float = 100.0,
    qty: int = 1,
    ingest_ts_ns: int = 1_000_000_000,
) -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "ordno": order_id,
            "code": symbol,
            "action": "Buy",
            "price": price,
            "quantity": qty,
            "seqno": "FILL001",
            "account_id": "acct1",
            "custom_field": strategy_id,
            "ts": 1_000_000_000,
        },
        ingest_ts_ns=ingest_ts_ns,
    )


@pytest.fixture(autouse=True)
def _patch_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub_metrics()
    monkeypatch.setattr(
        "hft_platform.observability.metrics.MetricsRegistry.get",
        staticmethod(lambda: stub),
    )


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
def router(bus: MagicMock, position_store: MagicMock) -> ExecutionRouter:
    q: asyncio.Queue = asyncio.Queue()
    order_id_map: dict[str, str] = {"ORD001": "strat1"}
    handler = MagicMock()
    r = ExecutionRouter(bus, q, order_id_map, position_store, handler)
    return r


# ---------------------------------------------------------------------------
# Order event normalization + publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_event_normalized_and_published(router: ExecutionRouter, bus: MagicMock) -> None:
    raw = _make_order_raw(status="Submitted")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    bus.publish_nowait.assert_called()
    event = bus.publish_nowait.call_args[0][0]
    assert isinstance(event, OrderEvent)


# ---------------------------------------------------------------------------
# Terminal state handlers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminal_sync_handler_called(router: ExecutionRouter) -> None:
    handler = MagicMock()
    router.terminal_handler = handler

    raw = _make_order_raw(status="Filled")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    handler.assert_called_once()


@pytest.mark.asyncio
async def test_terminal_async_handler_called(router: ExecutionRouter) -> None:
    handler = AsyncMock()
    router.terminal_handler = handler

    raw = _make_order_raw(status="Filled")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    # Allow spawned tasks to run
    await asyncio.sleep(0.01)
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    handler.assert_awaited_once()
    assert handler.await_count == 1


@pytest.mark.asyncio
async def test_terminal_object_handler(router: ExecutionRouter) -> None:
    class _Handler:
        def __init__(self) -> None:
            self.called = False
            self.args: tuple = ()

        def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
            self.called = True
            self.args = (strategy_id, order_id)

    obj = _Handler()
    router.terminal_handler = obj

    raw = _make_order_raw(status="Cancelled")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert obj.called


# ---------------------------------------------------------------------------
# Fill -> position -> PnL flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_updates_position(router: ExecutionRouter, position_store: MagicMock, bus: MagicMock) -> None:
    raw = _make_deal_raw()
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    position_store.on_fill_async.assert_awaited_once()
    bus.publish_many_nowait.assert_called()


# ---------------------------------------------------------------------------
# Non-terminal order skips handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_terminal_order_skips_handler(router: ExecutionRouter) -> None:
    handler = MagicMock()
    router.terminal_handler = handler

    raw = _make_order_raw(status="Submitted")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    handler.assert_not_called()


# ---------------------------------------------------------------------------
# Error recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_does_not_stop_loop(router: ExecutionRouter, bus: MagicMock) -> None:
    # First event causes normalizer to throw, second should still process
    bad_raw = RawExecEvent(topic="order", data="NOT_A_DICT", ingest_ts_ns=1000)
    good_raw = _make_order_raw(status="Submitted")

    await router.raw_queue.put(bad_raw)
    await router.raw_queue.put(good_raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The good event should have been published
    assert bus.publish_nowait.call_count >= 1


# ---------------------------------------------------------------------------
# Lifecycle: running flag, alive metric, heartbeat, lag metric
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_running_flag_lifecycle(router: ExecutionRouter) -> None:
    assert router.running is False

    raw = _make_order_raw(status="Submitted")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()

    assert router.running is True
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_alive_metric_set_on_start(router: ExecutionRouter) -> None:
    raw = _make_order_raw(status="Submitted")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    router.metrics.execution_router_alive.set.assert_called()


@pytest.mark.asyncio
async def test_heartbeat_updated(router: ExecutionRouter) -> None:
    raw = _make_order_raw(status="Submitted")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert router.metrics.execution_router_heartbeat_ts.set.call_count >= 2  # init + loop


@pytest.mark.asyncio
async def test_lag_metric_recorded(router: ExecutionRouter) -> None:
    raw = _make_order_raw(ingest_ts_ns=1_000_000)
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    router.metrics.execution_router_lag_ns.observe.assert_called_once()


# ---------------------------------------------------------------------------
# Queue drain and cancellation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_stops_loop(router: ExecutionRouter) -> None:
    task = asyncio.create_task(router.run())
    await asyncio.sleep(0.01)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # alive should be set to 0 after exit
    router.metrics.execution_router_alive.set.assert_called_with(0)


# ---------------------------------------------------------------------------
# Normalizer returns None
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normalizer_returns_none_no_publish(router: ExecutionRouter, bus: MagicMock) -> None:
    router.normalizer.normalize_order = MagicMock(return_value=None)

    raw = _make_order_raw()
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    bus.publish_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# Fill with risk engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_with_risk_engine_notifies_pnl(router: ExecutionRouter, position_store: MagicMock) -> None:
    risk_engine = MagicMock()
    risk_engine.notify_fill_pnl = MagicMock()
    router._risk_engine = risk_engine

    # Set up pre-position with realized_pnl
    pre_pos = SimpleNamespace(realized_pnl_scaled=100_000)
    position_store.positions = {"acct1:strat1:2330": pre_pos}
    position_store.on_fill_async = AsyncMock(
        return_value=PositionDelta(
            account_id="acct1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=0,
            avg_price=0,
            realized_pnl=150_000,
            unrealized_pnl=0,
            delta_source="FILL",
        )
    )

    raw = _make_deal_raw()
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    risk_engine.notify_fill_pnl.assert_called_once_with("strat1", 50_000)


# ---------------------------------------------------------------------------
# Fill without risk engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_without_risk_engine(router: ExecutionRouter, bus: MagicMock) -> None:
    router._risk_engine = None

    raw = _make_deal_raw()
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    bus.publish_many_nowait.assert_called()


# ---------------------------------------------------------------------------
# Handler exception isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash(router: ExecutionRouter, bus: MagicMock) -> None:
    handler = MagicMock(side_effect=RuntimeError("handler error"))
    router.terminal_handler = handler

    raw = _make_order_raw(status="Failed")
    await router.raw_queue.put(raw)

    task = asyncio.create_task(router.run())
    await router.raw_queue.join()
    router.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # The error metric should have been recorded
    router.metrics.execution_router_errors_total.inc.assert_called()


# ---------------------------------------------------------------------------
# _create_task_with_error_handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_task_with_error_handling_logs_exception() -> None:
    async def fail():
        raise ValueError("test failure")

    task = _create_task_with_error_handling(fail(), name="test-fail")
    # Let task run and fail
    await asyncio.sleep(0.01)
    assert task.done()
    with pytest.raises(ValueError):
        task.result()


@pytest.mark.asyncio
async def test_create_task_with_error_handling_success() -> None:
    async def ok():
        return 42

    task = _create_task_with_error_handling(ok(), name="test-ok")
    result = await task
    assert result == 42


# ---------------------------------------------------------------------------
# E2E order-to-fill latency metric (SLO-2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_latency_observed_when_cmd_created_ns_known(bus: MagicMock, position_store: MagicMock) -> None:
    """When cmd_created_ns_map contains the order_key for a fill, latency is observed."""
    q: asyncio.Queue = asyncio.Queue()
    order_id_map: dict[str, str] = {"ORD001": "strat1:42"}
    cmd_created_ns_map: dict[str, int] = {"strat1:42": 1_000_000_000}
    handler = MagicMock()
    r = ExecutionRouter(bus, q, order_id_map, position_store, handler, cmd_created_ns_map=cmd_created_ns_map)

    # fill arrives 5ms after the command was dispatched
    raw = _make_deal_raw(order_id="ORD001", strategy_id="strat1", ingest_ts_ns=1_005_000_000)
    await r.raw_queue.put(raw)

    task = asyncio.create_task(r.run())
    await r.raw_queue.join()
    r.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    r.metrics.e2e_order_latency_ns.observe.assert_called_once_with(5_000_000)


@pytest.mark.asyncio
async def test_e2e_latency_not_observed_when_order_key_missing(bus: MagicMock, position_store: MagicMock) -> None:
    """When order_id_map has no entry for fill.order_id, no latency is observed."""
    q: asyncio.Queue = asyncio.Queue()
    order_id_map: dict[str, str] = {}  # no mapping
    cmd_created_ns_map: dict[str, int] = {}
    handler = MagicMock()
    r = ExecutionRouter(bus, q, order_id_map, position_store, handler, cmd_created_ns_map=cmd_created_ns_map)

    raw = _make_deal_raw(order_id="ORD001", strategy_id="strat1", ingest_ts_ns=1_005_000_000)
    await r.raw_queue.put(raw)

    task = asyncio.create_task(r.run())
    await r.raw_queue.join()
    r.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    r.metrics.e2e_order_latency_ns.observe.assert_not_called()


# ---------------------------------------------------------------------------
# Direct order recording safety net (H5 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_event_recorded_directly_to_recorder_queue(bus: MagicMock, position_store: MagicMock) -> None:
    """OrderEvents should be written directly to recorder_queue to survive ring buffer overwrite."""
    from unittest.mock import patch

    recorder_q: asyncio.Queue = asyncio.Queue(maxsize=100)
    symbol_meta = MagicMock()
    price_codec = MagicMock()

    q: asyncio.Queue = asyncio.Queue()
    order_id_map: dict[str, str] = {}
    handler = MagicMock()
    r = ExecutionRouter(bus, q, order_id_map, position_store, handler)
    r._recorder_queue = recorder_q
    r._symbol_metadata = symbol_meta
    r._price_codec = price_codec

    raw = _make_order_raw(status="Submitted")
    await r.raw_queue.put(raw)

    with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("orders", {"id": "O1"})):
        task = asyncio.create_task(r.run())
        await r.raw_queue.join()
        r.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert not recorder_q.empty(), "OrderEvent should be recorded directly to recorder_queue"
    item = recorder_q.get_nowait()
    assert item["topic"] == "orders"


# ---------------------------------------------------------------------------
# DLQ retry uses on_fill_async (H2 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dlq_retry_uses_on_fill_async(router: ExecutionRouter, position_store: MagicMock, bus: MagicMock) -> None:
    """_retry_orphaned_fills must use on_fill_async, not blocking on_fill."""
    from unittest.mock import patch

    from hft_platform.contracts.execution import FillEvent

    fake_fill = FillEvent(
        fill_id="F001",
        account_id="acct1",
        order_id="ORD001",
        strategy_id="strat1",
        symbol="2330",
        side=1,
        qty=1,
        price=1_000_000,
        fee=0,
        tax=0,
        ingest_ts_ns=1_000_000_000,
        match_ts_ns=1_000_000_000,
    )

    # Mock the DLQ to return one resolved fill
    mock_dlq = MagicMock()
    mock_dlq.count = 1
    mock_dlq.retry = MagicMock(return_value=([fake_fill], []))

    with patch(
        "hft_platform.execution.fill_dlq.get_orphaned_fill_dlq",
        return_value=mock_dlq,
    ):
        await router._retry_orphaned_fills()

    # on_fill_async should be called, NOT on_fill
    position_store.on_fill_async.assert_awaited_once_with(fake_fill)
    position_store.on_fill.assert_not_called()


@pytest.mark.asyncio
async def test_e2e_latency_not_observed_when_created_ns_zero(bus: MagicMock, position_store: MagicMock) -> None:
    """When cmd_created_ns is 0 (unset), no latency is observed."""
    q: asyncio.Queue = asyncio.Queue()
    order_id_map: dict[str, str] = {"ORD001": "strat1:42"}
    cmd_created_ns_map: dict[str, int] = {"strat1:42": 0}
    handler = MagicMock()
    r = ExecutionRouter(bus, q, order_id_map, position_store, handler, cmd_created_ns_map=cmd_created_ns_map)

    raw = _make_deal_raw(order_id="ORD001", strategy_id="strat1", ingest_ts_ns=1_005_000_000)
    await r.raw_queue.put(raw)

    task = asyncio.create_task(r.run())
    await r.raw_queue.join()
    r.running = False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    r.metrics.e2e_order_latency_ns.observe.assert_not_called()


# ---------------------------------------------------------------------------
# DLQ retry TCA enrichment (M4 fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dlq_retry_enriches_fill_with_tca_prices(
    router: ExecutionRouter, position_store: MagicMock, bus: MagicMock
) -> None:
    """DLQ-resolved fills should be enriched with TCA decision/arrival prices."""
    from unittest.mock import patch

    from hft_platform.contracts.execution import FillEvent

    fake_fill = FillEvent(
        fill_id="F001",
        account_id="acct1",
        order_id="ORD001",
        strategy_id="strat1",
        symbol="2330",
        side=1,
        qty=1,
        price=1_000_000,
        fee=0,
        tax=0,
        ingest_ts_ns=1_000_000_000,
        match_ts_ns=1_000_000_000,
    )

    # Seed order_id_map and TCA map
    router._order_id_map["ORD001"] = "strat1:42"
    router._cmd_tca_map["strat1:42"] = (5_000_000, 5_010_000)

    mock_dlq = MagicMock()
    mock_dlq.count = 1
    mock_dlq.retry = MagicMock(return_value=([fake_fill], []))

    with patch(
        "hft_platform.execution.fill_dlq.get_orphaned_fill_dlq",
        return_value=mock_dlq,
    ):
        await router._retry_orphaned_fills()

    # TCA prices should be enriched on the fill
    assert fake_fill.decision_price == 5_000_000
    assert fake_fill.arrival_price == 5_010_000
