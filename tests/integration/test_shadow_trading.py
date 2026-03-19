"""WU-19: Integration tests for shadow trading runner."""

from __future__ import annotations

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.events import MetaData, TickEvent
from hft_platform.testing.shadow_runner import RecordingOrderAdapter, run


def _tick(symbol="2330", price=100_0000, volume=5):
    ts = timebase.now_ns()
    return TickEvent(meta=MetaData(seq=1, source_ts=ts, local_ts=ts), symbol=symbol, price=price, volume=volume)


def _simple_strategy(event):
    if not isinstance(event, TickEvent):
        return None
    return [
        OrderIntent(
            intent_id=1,
            strategy_id="shadow_test",
            symbol=event.symbol,
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=event.price,
            qty=1,
            tif=TIF.LIMIT,
            timestamp_ns=timebase.now_ns(),
        )
    ]


@pytest.mark.asyncio
async def test_normal_shadow_run():
    result = await run([_tick() for _ in range(3)], _simple_strategy)
    assert len(result.commands) == 3
    assert result.duration_ns > 0
    assert result.precision_violations == []


@pytest.mark.asyncio
async def test_precision_violation_detected():
    def bad_strategy(event):
        intent = OrderIntent(
            intent_id=1,
            strategy_id="bad",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=100_0000,
            qty=1,
            tif=TIF.LIMIT,
            timestamp_ns=timebase.now_ns(),
        )
        object.__setattr__(intent, "price", 100.5)
        return [intent]

    result = await run([_tick()], bad_strategy)
    assert len(result.precision_violations) > 0
    assert "price" in result.precision_violations[0]


@pytest.mark.asyncio
async def test_no_broker_calls():
    adapter = RecordingOrderAdapter()
    assert not hasattr(adapter, "client")
    assert not hasattr(adapter, "place_order")
    result = await run([_tick()], _simple_strategy)
    assert len(result.commands) == 1


@pytest.mark.asyncio
async def test_command_count_matches_intents():
    def multi_intent(event):
        return [
            OrderIntent(
                intent_id=i,
                strategy_id="multi",
                symbol="2330",
                intent_type=IntentType.NEW,
                side=Side.BUY,
                price=100_0000,
                qty=1,
                tif=TIF.LIMIT,
                timestamp_ns=timebase.now_ns(),
            )
            for i in range(4)
        ]

    result = await run([_tick(), _tick()], multi_intent)
    assert len(result.commands) == 8


@pytest.mark.asyncio
async def test_empty_events():
    result = await run([], _simple_strategy)
    assert len(result.commands) == 0
    assert result.precision_violations == []
    assert result.duration_ns >= 0
