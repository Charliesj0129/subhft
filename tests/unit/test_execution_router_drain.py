"""Tests for ExecutionRouter.stop() graceful shutdown drain."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.core import timebase
from hft_platform.execution.normalizer import RawExecEvent


def _make_deal_raw(fill_id: str = "F001", symbol: str = "2330") -> RawExecEvent:
    return RawExecEvent(
        topic="deal",
        data={
            "seqno": fill_id,
            "symbol": symbol,
            "price": 5_000_000,
            "quantity": 1,
            "action": "Buy",
        },
        ingest_ts_ns=timebase.now_ns(),
    )


def _make_fill(fill_id: str = "F001", symbol: str = "2330") -> FillEvent:
    return FillEvent(
        fill_id=fill_id,
        order_id="ORD001",
        account_id="A1",
        strategy_id="s1",
        symbol=symbol,
        side=Side.BUY,
        price=5_000_000,
        qty=1,
        fee=0,
        tax=0,
        ingest_ts_ns=timebase.now_ns(),
        match_ts_ns=timebase.now_ns(),
    )


@pytest.fixture
def router_parts(tmp_path, monkeypatch):
    """Create ExecutionRouter with mocked dependencies."""
    monkeypatch.setenv("HFT_FILL_DEDUP_PERSIST_PATH", str(tmp_path / "fill_dedup.jsonl"))
    with patch("hft_platform.execution.router.MetricsRegistry") as mm:
        metrics_mock = MagicMock()
        mm.get.return_value = metrics_mock

        from hft_platform.execution.router import ExecutionRouter

        raw_queue = asyncio.Queue(maxsize=1024)
        normalizer = MagicMock()
        bus = MagicMock()
        position_store = MagicMock()
        position_store.on_fill.return_value = MagicMock()

        router = ExecutionRouter(
            bus=bus,
            raw_queue=raw_queue,
            order_id_map={},
            position_store=position_store,
            terminal_handler=MagicMock(),
        )
        router.normalizer = normalizer
        yield router, raw_queue, normalizer, position_store


@pytest.mark.asyncio
async def test_stop_drains_remaining_fills(router_parts):
    """stop() processes remaining fill events from the queue."""
    router, raw_queue, normalizer, position_store = router_parts

    fill = _make_fill("F001")
    delta = MagicMock()
    position_store.on_fill.return_value = delta
    normalizer.normalize_fill.return_value = fill
    raw_queue.put_nowait(_make_deal_raw("F001"))

    router.running = True
    drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    position_store.on_fill.assert_called_once_with(fill)
    router.bus.publish_many_nowait.assert_called_once_with([delta, fill])


@pytest.mark.asyncio
async def test_stop_skips_duplicate_fills_during_drain(router_parts):
    """stop() respects fill dedup during drain."""
    router, raw_queue, normalizer, position_store = router_parts

    fill = _make_fill("F001")
    normalizer.normalize_fill.return_value = fill
    # Pre-register the fill_id as already seen
    router._seen_fill_ids["F001"] = None
    raw_queue.put_nowait(_make_deal_raw("F001"))

    drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 0
    position_store.on_fill.assert_not_called()


@pytest.mark.asyncio
async def test_stop_returns_zero_on_empty_queue(router_parts):
    """stop() returns 0 when queue is already empty."""
    router, raw_queue, normalizer, position_store = router_parts

    drained = await router.stop(drain_timeout_s=0.1)

    assert drained == 0
    position_store.on_fill.assert_not_called()


@pytest.mark.asyncio
async def test_stop_sets_running_false(router_parts):
    """stop() sets running to False."""
    router, *_ = router_parts
    router.running = True

    await router.stop()

    assert router.running is False


# ---------------------------------------------------------------------------
# M7: order events processed during shutdown drain
# ---------------------------------------------------------------------------


def _make_order_raw(order_id: str = "O001", status: str = "Submitted") -> RawExecEvent:
    return RawExecEvent(
        topic="order",
        data={
            "ord_no": order_id,
            "status": {"status": status},
            "contract": {"code": "2330"},
            "order": {"action": "Buy", "price": 5_000_000, "quantity": 1},
        },
        ingest_ts_ns=timebase.now_ns(),
    )


def _make_order_event(order_id: str = "O001", status: OrderStatus = OrderStatus.SUBMITTED) -> OrderEvent:
    return OrderEvent(
        order_id=order_id,
        strategy_id="s1",
        symbol="2330",
        status=status,
        submitted_qty=1,
        filled_qty=0,
        remaining_qty=1,
        price=5_000_000,
        side=Side.BUY,
        ingest_ts_ns=timebase.now_ns(),
        broker_ts_ns=timebase.now_ns(),
    )


@pytest.mark.asyncio
async def test_stop_drains_order_events(router_parts):
    """stop() processes order events remaining in the queue during drain."""
    router, raw_queue, normalizer, position_store = router_parts

    order_event = _make_order_event("O001", OrderStatus.SUBMITTED)
    normalizer.normalize_order.return_value = order_event
    raw_queue.put_nowait(_make_order_raw("O001", "Submitted"))

    drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    router.bus.publish_nowait.assert_called_once_with(order_event)


@pytest.mark.asyncio
async def test_stop_drain_order_terminal_calls_terminal_handler(router_parts):
    """stop() calls terminal_handler for terminal-state orders during drain."""
    router, raw_queue, normalizer, _ = router_parts

    terminal_event = _make_order_event("O002", OrderStatus.FILLED)
    normalizer.normalize_order.return_value = terminal_event
    raw_queue.put_nowait(_make_order_raw("O002", "Filled"))

    await router.stop(drain_timeout_s=1.0)

    router.terminal_handler.assert_called_once_with(terminal_event.strategy_id, terminal_event.order_id)


@pytest.mark.asyncio
async def test_stop_drain_order_non_terminal_skips_terminal_handler(router_parts):
    """stop() does NOT call terminal_handler for non-terminal orders during drain."""
    router, raw_queue, normalizer, _ = router_parts

    submitted_event = _make_order_event("O003", OrderStatus.SUBMITTED)
    normalizer.normalize_order.return_value = submitted_event
    raw_queue.put_nowait(_make_order_raw("O003", "Submitted"))

    await router.stop(drain_timeout_s=1.0)

    router.terminal_handler.assert_not_called()


@pytest.mark.asyncio
async def test_stop_drain_mixed_order_and_fill_events(router_parts):
    """stop() correctly drains a queue with both order and fill events."""
    router, raw_queue, normalizer, position_store = router_parts

    order_event = _make_order_event("O004", OrderStatus.SUBMITTED)
    fill_event = _make_fill("F004")
    normalizer.normalize_order.return_value = order_event
    normalizer.normalize_fill.return_value = fill_event

    raw_queue.put_nowait(_make_order_raw("O004", "Submitted"))
    raw_queue.put_nowait(_make_deal_raw("F004"))

    drained = await router.stop(drain_timeout_s=1.0)

    # Both events count as drained
    assert drained == 2
    position_store.on_fill.assert_called_once_with(fill_event)


@pytest.mark.asyncio
async def test_stop_drains_fill_with_publish_fallback_when_bus_has_no_batch_api(router_parts):
    """stop() publishes delta + fill individually when bus lacks publish_many_nowait."""
    router, raw_queue, normalizer, position_store = router_parts

    publish_nowait = MagicMock()
    publish = MagicMock()
    router.bus = SimpleNamespace(publish_nowait=publish_nowait, publish=publish)
    delta = MagicMock()
    position_store.on_fill.return_value = delta
    fill_event = _make_fill("F005")
    normalizer.normalize_fill.return_value = fill_event
    raw_queue.put_nowait(_make_deal_raw("F005"))

    drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    assert publish_nowait.call_args_list == [((delta,),), ((fill_event,),)]
