"""Tests for ExecutionRouter.stop() graceful shutdown drain."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.execution import FillEvent, Side
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
def router_parts():
    """Create ExecutionRouter with mocked dependencies."""
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
    normalizer.normalize_fill.return_value = fill
    raw_queue.put_nowait(_make_deal_raw("F001"))

    router.running = True
    drained = await router.stop(drain_timeout_s=1.0)

    assert drained == 1
    position_store.on_fill.assert_called_once_with(fill)


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
