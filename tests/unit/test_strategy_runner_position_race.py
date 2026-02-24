"""Tests for position snapshot race-condition safety in StrategyRunner (S1, S2).

Verifies that _build_positions_by_strategy snapshots the positions dict before
iteration so that concurrent mutation (from broker callback threads) cannot
cause a RuntimeError or silent data corruption.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


def _make_runner(position_store=None):
    from hft_platform.strategy.runner import StrategyRunner

    risk_q = asyncio.Queue()
    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(MagicMock(), risk_q, config_path="dummy", position_store=position_store)
    return runner


class _FakePosition:
    def __init__(self, strategy_id, symbol, net_qty):
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.net_qty = net_qty


class _PositionStore:
    def __init__(self, initial: dict):
        self.positions = initial


# ─── Unit: _build_positions_by_strategy ──────────────────────────────────────

def test_empty_position_store():
    runner = _make_runner(_PositionStore({}))
    result = runner._build_positions_by_strategy()
    assert result == {}


def test_structured_position_objects():
    store = _PositionStore({
        "pos:s1:2330": _FakePosition("s1", "2330", 3),
        "pos:s2:2317": _FakePosition("s2", "2317", -1),
    })
    runner = _make_runner(store)
    result = runner._build_positions_by_strategy()
    # Structured objects go through strategy_id/symbol branch
    assert result.get("s1", {}).get("2330") == 3
    assert result.get("s2", {}).get("2317") == -1


def test_string_key_parsing():
    store = _PositionStore({
        "pos:alpha1:TSM": 5,
        "pos:alpha2:AAPL": -2,
    })
    runner = _make_runner(store)
    result = runner._build_positions_by_strategy()
    assert result["alpha1"]["TSM"] == 5
    assert result["alpha2"]["AAPL"] == -2


def test_position_key_cache_populated():
    """Parsed key tuples should be cached on first call."""
    store = _PositionStore({"pos:s1:2330": 10})
    runner = _make_runner(store)
    assert len(runner._position_key_cache) == 0
    runner._build_positions_by_strategy()
    assert "pos:s1:2330" in runner._position_key_cache
    assert runner._position_key_cache["pos:s1:2330"] == ("s1", "2330")


def test_position_key_cache_reused():
    """Second call should reuse the cached tuple (not split again)."""
    store = _PositionStore({"pos:s1:2330": 10})
    runner = _make_runner(store)
    runner._build_positions_by_strategy()
    # Manually poison the key in cache — should NOT be overwritten on 2nd call
    runner._position_key_cache["pos:s1:2330"] = ("s1", "POISON")
    result = runner._build_positions_by_strategy()
    # Cache entry is reused (not re-parsed)
    assert result.get("s1", {}).get("POISON") == 10


def test_fallback_for_non_string_key():
    """Non-string keys without colon should go to the fallback '*' bucket."""
    store = _PositionStore({"some_key": 7})
    runner = _make_runner(store)
    result = runner._build_positions_by_strategy()
    assert result.get("*", {}).get("some_key") == 7


def test_no_position_store_returns_empty():
    runner = _make_runner(None)
    result = runner._build_positions_by_strategy()
    assert result == {}


# ─── Race condition: dict mutation during iteration ──────────────────────────

def test_concurrent_dict_mutation_does_not_raise():
    """Simulates a broker thread mutating positions while iteration is in progress.

    Without the dict(raw) snapshot, this would raise RuntimeError:
    "dictionary changed size during iteration".
    With the snapshot it must complete without error.
    """
    shared_positions: dict = {}
    for i in range(100):
        shared_positions[f"pos:s1:SYM{i}"] = i

    store = _PositionStore(shared_positions)
    runner = _make_runner(store)

    errors: list[Exception] = []
    stop_event = threading.Event()

    def mutate_dict():
        """Mutates the positions dict concurrently from another thread."""
        i = 200
        while not stop_event.is_set():
            key = f"pos:s1:SYM{i}"
            shared_positions[key] = i
            time.sleep(0)  # yield
            shared_positions.pop(key, None)
            i += 1

    mutator = threading.Thread(target=mutate_dict, daemon=True)
    mutator.start()

    try:
        for _ in range(500):
            try:
                runner._build_positions_by_strategy()
            except Exception as e:
                errors.append(e)
                break
    finally:
        stop_event.set()
        mutator.join(timeout=2.0)

    assert not errors, f"Got exception during concurrent dict access: {errors[0]}"


# ─── Circuit breaker 3-state FSM ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_normal_to_degraded():
    """After threshold/2 consecutive failures, state transitions to 'degraded'."""
    from hft_platform.events import MetaData, TickEvent
    from hft_platform.strategy.base import BaseStrategy

    class _BadStrategy(BaseStrategy):
        def on_tick(self, event):
            raise RuntimeError("always fails")

    runner = _make_runner()
    runner._circuit_threshold = 6
    runner._circuit_recovery_threshold = 3
    strat = _BadStrategy("bad", symbols=["2330"])
    runner.register(strat)

    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="2330", price=100_000, volume=1, total_volume=1,
        bid_side_total_vol=0, ask_side_total_vol=0,
        is_simtrade=False, is_odd_lot=False,
    )

    # 3 failures → degraded (threshold // 2 = 3)
    for _ in range(3):
        await runner.process_event(event)

    assert runner._circuit_states.get("bad") == "degraded"
    assert strat.enabled  # still enabled in degraded


@pytest.mark.asyncio
async def test_circuit_breaker_degraded_to_halted():
    """After threshold failures, state transitions to 'halted' and strategy is disabled."""
    from hft_platform.events import MetaData, TickEvent
    from hft_platform.strategy.base import BaseStrategy

    class _BadStrategy(BaseStrategy):
        def on_tick(self, event):
            raise RuntimeError("always fails")

    runner = _make_runner()
    runner._circuit_threshold = 4
    strat = _BadStrategy("bad2", symbols=["2330"])
    runner.register(strat)

    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="2330", price=100_000, volume=1, total_volume=1,
        bid_side_total_vol=0, ask_side_total_vol=0,
        is_simtrade=False, is_odd_lot=False,
    )

    for _ in range(4):
        await runner.process_event(event)

    assert runner._circuit_states.get("bad2") == "halted"
    assert not strat.enabled


@pytest.mark.asyncio
async def test_circuit_breaker_degraded_recovery():
    """N consecutive successes in degraded state should recover to 'normal'."""
    from hft_platform.events import MetaData, TickEvent
    from hft_platform.strategy.base import BaseStrategy

    class _GoodStrategy(BaseStrategy):
        def on_tick(self, event):
            return []

    runner = _make_runner()
    runner._circuit_threshold = 6
    runner._circuit_recovery_threshold = 3
    strat = _GoodStrategy("good", symbols=["2330"])
    runner.register(strat)

    # Manually put it in degraded state
    sid = strat.strategy_id
    runner._circuit_states[sid] = "degraded"
    runner._failure_counts[sid] = 3
    runner._circuit_success_counts[sid] = 0

    event = TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol="2330", price=100_000, volume=1, total_volume=1,
        bid_side_total_vol=0, ask_side_total_vol=0,
        is_simtrade=False, is_odd_lot=False,
    )

    # 3 successes should recover to normal
    for _ in range(3):
        await runner.process_event(event)

    assert runner._circuit_states.get(sid) == "normal"
    assert runner._failure_counts[sid] == 0


