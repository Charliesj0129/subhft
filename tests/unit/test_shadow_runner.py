"""Tests for hft_platform.testing.shadow_runner (WU-19)."""

from __future__ import annotations

import asyncio
from typing import Any, Sequence

import pytest

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.testing.shadow_runner import (
    RecordingOrderAdapter,
    ShadowResult,
    _check_precision,
    run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_intent(
    price: Any = 150_000,
    qty: Any = 10,
    side: Side = Side.BUY,
    intent_id: int = 1,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="test_strategy",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=side,
        price=price,
        qty=qty,
    )


# ---------------------------------------------------------------------------
# ShadowResult dataclass
# ---------------------------------------------------------------------------

def test_shadow_result_default_values():
    result = ShadowResult()
    assert result.commands == []
    assert result.precision_violations == []
    assert result.duration_ns == 0


def test_shadow_result_fields_are_independent():
    r1 = ShadowResult()
    r2 = ShadowResult()
    r1.commands.append(object())  # type: ignore[arg-type]
    assert r2.commands == [], "ShadowResult instances must not share list references"


# ---------------------------------------------------------------------------
# RecordingOrderAdapter
# ---------------------------------------------------------------------------

def test_recording_adapter_submit_stores_command():
    adapter = RecordingOrderAdapter()
    intent = _make_intent()
    cmd = OrderCommand(
        cmd_id=1, intent=intent, deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL, created_ns=0
    )
    adapter.submit(cmd)
    assert len(adapter.commands) == 1
    assert adapter.commands[0] is cmd


def test_recording_adapter_submit_accumulates_multiple():
    adapter = RecordingOrderAdapter()
    for i in range(5):
        intent = _make_intent(intent_id=i)
        cmd = OrderCommand(
            cmd_id=i, intent=intent, deadline_ns=0,
            storm_guard_state=StormGuardState.NORMAL
        )
        adapter.submit(cmd)
    assert len(adapter.commands) == 5


@pytest.mark.asyncio
async def test_recording_adapter_execute_stores_command():
    adapter = RecordingOrderAdapter()
    intent = _make_intent()
    cmd = OrderCommand(
        cmd_id=99, intent=intent, deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL
    )
    await adapter.execute(cmd)
    assert len(adapter.commands) == 1
    assert adapter.commands[0].cmd_id == 99


@pytest.mark.asyncio
async def test_recording_adapter_execute_and_submit_share_list():
    adapter = RecordingOrderAdapter()
    intent = _make_intent()
    cmd1 = OrderCommand(cmd_id=1, intent=intent, deadline_ns=0, storm_guard_state=StormGuardState.NORMAL)
    cmd2 = OrderCommand(cmd_id=2, intent=intent, deadline_ns=0, storm_guard_state=StormGuardState.NORMAL)
    adapter.submit(cmd1)
    await adapter.execute(cmd2)
    assert len(adapter.commands) == 2


# ---------------------------------------------------------------------------
# _check_precision
# ---------------------------------------------------------------------------

def test_check_precision_valid_int_price_and_qty():
    intent = _make_intent(price=150_000, qty=10)
    violations = _check_precision(intent)
    assert violations == []


def test_check_precision_float_price_detected():
    intent = _make_intent(price=150.0)
    violations = _check_precision(intent)
    assert len(violations) == 1
    assert "price" in violations[0]
    assert "float" in violations[0]


def test_check_precision_float_qty_detected():
    intent = _make_intent(qty=10.5)
    violations = _check_precision(intent)
    assert len(violations) == 1
    assert "qty" in violations[0]


def test_check_precision_both_float_returns_two_violations():
    intent = _make_intent(price=100.0, qty=5.0)
    violations = _check_precision(intent)
    assert len(violations) == 2


def test_check_precision_string_price_detected():
    intent = _make_intent(price="150000")
    violations = _check_precision(intent)
    assert len(violations) >= 1
    assert "price" in violations[0]


# ---------------------------------------------------------------------------
# run() — main async function
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_empty_events_returns_empty_result():
    result = await run([], lambda e: None)
    assert result.commands == []
    assert result.precision_violations == []
    assert result.duration_ns >= 0


@pytest.mark.asyncio
async def test_run_strategy_returning_none_produces_no_commands():
    events = [object(), object(), object()]
    result = await run(events, lambda e: None)
    assert result.commands == []


@pytest.mark.asyncio
async def test_run_strategy_returning_empty_list_produces_no_commands():
    events = ["tick1", "tick2"]
    result = await run(events, lambda e: [])
    assert result.commands == []


@pytest.mark.asyncio
async def test_run_collects_commands_from_strategy():
    events = ["tick1", "tick2"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent()]

    result = await run(events, strategy)
    assert len(result.commands) == 2


@pytest.mark.asyncio
async def test_run_cmd_ids_are_sequential():
    events = ["e1", "e2", "e3"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent()]

    result = await run(events, strategy)
    ids = [cmd.cmd_id for cmd in result.commands]
    assert ids == [1, 2, 3]


@pytest.mark.asyncio
async def test_run_multiple_intents_per_event():
    events = ["tick"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent(intent_id=1), _make_intent(intent_id=2)]

    result = await run(events, strategy)
    assert len(result.commands) == 2
    assert result.commands[0].cmd_id == 1
    assert result.commands[1].cmd_id == 2


@pytest.mark.asyncio
async def test_run_detects_float_price_violation():
    events = ["tick"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent(price=150.5)]

    result = await run(events, strategy)
    assert len(result.precision_violations) == 1
    assert "price" in result.precision_violations[0]


@pytest.mark.asyncio
async def test_run_detects_float_qty_violation():
    events = ["tick"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent(qty=3.7)]

    result = await run(events, strategy)
    assert len(result.precision_violations) == 1
    assert "qty" in result.precision_violations[0]


@pytest.mark.asyncio
async def test_run_accumulates_violations_across_events():
    events = ["t1", "t2", "t3"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent(price=9.9)]

    result = await run(events, strategy)
    assert len(result.precision_violations) == 3


@pytest.mark.asyncio
async def test_run_valid_intents_produce_no_violations():
    events = ["t1", "t2"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent(price=150_000, qty=10)]

    result = await run(events, strategy)
    assert result.precision_violations == []


@pytest.mark.asyncio
async def test_run_commands_have_storm_guard_normal():
    events = ["tick"]

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent()]

    result = await run(events, strategy)
    assert result.commands[0].storm_guard_state == StormGuardState.NORMAL


@pytest.mark.asyncio
async def test_run_commands_contain_original_intent():
    events = ["tick"]
    intent = _make_intent(price=200_000, qty=5, side=Side.SELL)

    def strategy(event: Any) -> list[OrderIntent]:
        return [intent]

    result = await run(events, strategy)
    assert result.commands[0].intent is intent


@pytest.mark.asyncio
async def test_run_duration_ns_is_positive():
    events = list(range(100))

    def strategy(event: Any) -> list[OrderIntent]:
        return [_make_intent()]

    result = await run(events, strategy)
    assert result.duration_ns > 0


@pytest.mark.asyncio
async def test_run_strategy_events_are_passed_through():
    received = []

    def strategy(event: Any) -> None:
        received.append(event)
        return None

    events = ["a", "b", "c"]
    await run(events, strategy)
    assert received == events
