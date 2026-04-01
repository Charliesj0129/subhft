"""Unit tests for recorder queue drop logging and metrics in ExecutionRouter."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

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
    m.position_pnl_realized = MagicMock()
    m.e2e_order_latency_ns = MagicMock()
    m.fills_total = MagicMock()
    m.exec_overflow_drained_total = MagicMock()
    m.recorder_exec_drops_total = MagicMock()
    return m


def _make_order_raw(
    *,
    strategy_id: str = "strat1",
    order_id: str = "ORD001",
    symbol: str = "2330",
) -> RawExecEvent:
    return RawExecEvent(
        topic="order",
        data={
            "order": {
                "ordno": order_id,
                "action": "Buy",
                "price": 100,
                "quantity": 1,
                "custom_field": strategy_id,
            },
            "status": {"status": "Submitted"},
            "contract": {"code": symbol},
        },
        ingest_ts_ns=1_000_000_000,
    )


def _make_deal_raw(
    *,
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
            "price": 500.0,
            "quantity": 1,
            "seqno": "FILL001",
            "account_id": "acct1",
            "custom_field": strategy_id,
            "ts": 1_000_000_000,
        },
        ingest_ts_ns=1_000_000_000,
    )


def _make_router(metrics: MagicMock, recorder_queue: asyncio.Queue) -> ExecutionRouter:
    bus = MagicMock()
    bus.publish_many_nowait = MagicMock()

    position_store = MagicMock()
    position_store.on_fill = MagicMock(return_value=MagicMock())

    symbol_metadata = MagicMock()

    router = ExecutionRouter(
        bus=bus,
        raw_queue=asyncio.Queue(maxsize=100),
        order_id_map={"ORD001": "strat1:ORD001"},
        position_store=position_store,
        terminal_handler=MagicMock(),
        recorder_queue=recorder_queue,
        symbol_metadata=symbol_metadata,
    )
    router.metrics = metrics
    return router


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _stub_metrics()
    monkeypatch.setattr(
        "hft_platform.execution.router.MetricsRegistry.get",
        lambda: stub,
    )


@pytest.mark.asyncio
async def test_counter_increments_on_order_recorder_queue_full() -> None:
    """recorder_exec_drops_total with topic='orders' increments when recorder queue is full."""
    metrics = _stub_metrics()
    recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    recorder_queue.put_nowait("prefill")  # fill it so subsequent puts raise QueueFull
    router = _make_router(metrics, recorder_queue)

    order_event = MagicMock()
    order_event.strategy_id = "strat1"
    order_event.order_id = "ORD001"
    order_event.status = 1

    mapped = ("orders", {"data": "payload"})
    with (
        patch.object(router.normalizer, "normalize_order", return_value=order_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=mapped),
    ):
        raw = _make_order_raw()
        router.raw_queue.put_nowait(raw)

        # Run one iteration of the router loop
        router.running = True
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    metrics.recorder_exec_drops_total.labels.assert_called_with(topic="orders")
    metrics.recorder_exec_drops_total.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_counter_increments_on_fill_recorder_queue_full() -> None:
    """recorder_exec_drops_total with topic='fills' increments when recorder queue is full on fill path."""
    metrics = _stub_metrics()
    recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=1)
    recorder_queue.put_nowait("prefill")  # fill it so subsequent puts raise QueueFull
    router = _make_router(metrics, recorder_queue)

    fill_event = MagicMock()
    fill_event.strategy_id = "strat1"
    fill_event.order_id = "ORD001"
    fill_event.symbol = "2330"
    fill_event.account_id = "acct1"
    fill_event.ingest_ts_ns = 1_000_000_000
    fill_event.decision_price = 0
    fill_event.arrival_price = 0

    delta = MagicMock()
    router.position_store.on_fill = MagicMock(return_value=delta)
    router.position_store.on_fill_async = AsyncMock(return_value=delta)

    mapped = ("fills", {"data": "payload"})
    with (
        patch.object(router.normalizer, "normalize_fill", return_value=fill_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=mapped),
    ):
        raw = _make_deal_raw()
        router.raw_queue.put_nowait(raw)

        router.running = True
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    metrics.recorder_exec_drops_total.labels.assert_called_with(topic="fills")
    metrics.recorder_exec_drops_total.labels.return_value.inc.assert_called()


@pytest.mark.asyncio
async def test_no_drop_counter_when_recorder_queue_has_space() -> None:
    """recorder_exec_drops_total is NOT incremented when the recorder queue has room."""
    metrics = _stub_metrics()
    recorder_queue: asyncio.Queue = asyncio.Queue(maxsize=100)  # not full — plenty of space
    router = _make_router(metrics, recorder_queue)

    order_event = MagicMock()
    order_event.strategy_id = "strat1"
    order_event.order_id = "ORD001"
    order_event.status = 1

    mapped = ("orders", {"data": "payload"})
    with (
        patch.object(router.normalizer, "normalize_order", return_value=order_event),
        patch("hft_platform.recorder.mapper.map_event_to_record", return_value=mapped),
    ):
        raw = _make_order_raw()
        router.raw_queue.put_nowait(raw)

        router.running = True
        task = asyncio.create_task(router.run())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # labels() should never have been called with topic="orders" for a drop
    for call in metrics.recorder_exec_drops_total.labels.call_args_list:
        assert call != ((), {"topic": "orders"}), "Drop counter was unexpectedly incremented"

    assert recorder_queue.qsize() == 1  # the item was successfully enqueued
