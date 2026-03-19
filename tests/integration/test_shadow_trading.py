"""WU-19: Integration tests for shadow trading runner."""

from __future__ import annotations

from typing import Any, Sequence

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.core import timebase
from hft_platform.events import MetaData, TickEvent
from hft_platform.testing.shadow_runner import RecordingOrderAdapter, ShadowResult, run

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tick(symbol: str = "2330", price: int = 100_0000, volume: int = 5) -> TickEvent:
    ts = timebase.now_ns()
    return TickEvent(
        meta=MetaData(seq=1, source_ts=ts, local_ts=ts),
        symbol=symbol,
        price=price,
        volume=volume,
    )


def _simple_strategy(event: Any) -> Sequence[OrderIntent] | None:
    """Emit one BUY intent per tick."""
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_normal_shadow_run() -> None:
    """Shadow run produces commands from strategy callback."""
    events = [_tick() for _ in range(3)]
    result: ShadowResult = await run(events, _simple_strategy)

    assert len(result.commands) == 3
    assert result.duration_ns > 0
    assert result.precision_violations == []


@pytest.mark.asyncio
async def test_precision_violation_detected() -> None:
    """Precision law violations are captured in ShadowResult."""

    def bad_strategy(event: Any) -> Sequence[OrderIntent] | None:
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
        # Forcefully inject a float to simulate a violation
        object.__setattr__(intent, "price", 100.5)  # type: ignore[arg-type]
        return [intent]

    result = await run([_tick()], bad_strategy)
    assert len(result.precision_violations) > 0
    assert "price" in result.precision_violations[0]


@pytest.mark.asyncio
async def test_no_broker_calls() -> None:
    """RecordingOrderAdapter never touches a real broker."""
    adapter = RecordingOrderAdapter()
    # No broker attributes
    assert not hasattr(adapter, "client")
    assert not hasattr(adapter, "place_order")

    events = [_tick()]
    result = await run(events, _simple_strategy)
    # Commands exist but no side effects
    assert len(result.commands) == 1


@pytest.mark.asyncio
async def test_command_count_matches_intents() -> None:
    """Each intent from the strategy maps to exactly one command."""

    def multi_intent(event: Any) -> Sequence[OrderIntent] | None:
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

    events = [_tick(), _tick()]
    result = await run(events, multi_intent)
    assert len(result.commands) == 8  # 4 intents * 2 events


@pytest.mark.asyncio
async def test_empty_events() -> None:
    """Empty event list yields zero commands and no errors."""
    result = await run([], _simple_strategy)
    assert len(result.commands) == 0
    assert result.precision_violations == []
    assert result.duration_ns >= 0
