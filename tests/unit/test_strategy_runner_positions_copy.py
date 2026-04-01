"""Tests that StrategyRunner passes a shallow copy of positions to each strategy context.

Verifies that:
1. Mutating ctx.positions in one strategy does NOT affect ctx.positions seen by a
   subsequent strategy in the same event cycle.
2. The underlying _positions_cache is not corrupted by a strategy mutation.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


def _make_runner(position_store=None):
    from hft_platform.strategy.runner import StrategyRunner

    risk_q = asyncio.Queue()
    with patch("hft_platform.strategy.runner.StrategyRegistry") as mock_reg:
        mock_reg.return_value.instantiate.return_value = []
        runner = StrategyRunner(
            MagicMock(),
            risk_q,
            config_path="dummy",
            position_store=position_store,
        )
    return runner


class _FakePosition:
    def __init__(self, strategy_id: str, symbol: str, net_qty: int) -> None:
        self.strategy_id = strategy_id
        self.symbol = symbol
        self.net_qty = net_qty


class _PositionStore:
    def __init__(self, initial: dict) -> None:
        self.positions = initial


def _make_tick_event(symbol: str = "2330") -> object:
    """Minimal tick-like event that process_event accepts."""
    from hft_platform.events import MetaData, TickEvent

    return TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=1, local_ts=1),
        symbol=symbol,
        price=550_000,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )


# ---------------------------------------------------------------------------
# Strategy stubs that capture and optionally mutate ctx.positions
# ---------------------------------------------------------------------------


class _CapturingStrategy:
    """Records the positions dict seen at handle_event call time."""

    def __init__(self, sid: str, symbol: str = "2330") -> None:
        self.strategy_id = sid
        self.symbols = {symbol}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self.seen_positions: list[dict] = []

    def handle_event(self, ctx, event):  # noqa: ANN001
        # Snapshot value at call time (dict copy so we can compare later)
        self.seen_positions.append(dict(ctx.positions))
        return []


class _MutatingStrategy:
    """Mutates ctx.positions by adding a fake entry, then records what was there."""

    def __init__(self, sid: str, symbol: str = "2330") -> None:
        self.strategy_id = sid
        self.symbols = {symbol}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self.seen_positions: list[dict] = []

    def handle_event(self, ctx, event):  # noqa: ANN001
        self.seen_positions.append(dict(ctx.positions))
        # Mutate: add a fake key that should NOT propagate to other strategies
        ctx.positions["__injected__"] = 9999
        return []


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mutation_in_first_strategy_does_not_affect_second():
    """Mutating ctx.positions in strategy A must not alter ctx.positions for strategy B."""
    store = _PositionStore(
        {
            "pos:*:2330": 5,
        }
    )
    runner = _make_runner(store)

    mutator = _MutatingStrategy("strat_a")
    observer = _CapturingStrategy("strat_b")

    runner.register(mutator)
    runner.register(observer)

    event = _make_tick_event("2330")
    await runner.process_event(event)

    assert len(mutator.seen_positions) == 1, "mutator must have been called"
    assert len(observer.seen_positions) == 1, "observer must have been called"

    # The observer should NOT see the key injected by the mutator
    assert "__injected__" not in observer.seen_positions[0], (
        "observer saw mutator-injected key — positions dict was shared, not copied"
    )


@pytest.mark.asyncio
async def test_mutation_does_not_corrupt_positions_cache():
    """After a strategy mutates ctx.positions, the internal cache must remain clean."""
    store = _PositionStore(
        {
            "pos:*:2330": 10,
        }
    )
    runner = _make_runner(store)

    mutator = _MutatingStrategy("strat_mut")
    runner.register(mutator)

    event = _make_tick_event("2330")
    await runner.process_event(event)

    # The strategy mutated ctx.positions — the cache should still be pristine
    cached = runner._positions_cache
    wildcard_positions = cached.get("*", {})
    assert "__injected__" not in wildcard_positions, (
        "_positions_cache was corrupted by strategy mutation"
    )
    assert wildcard_positions.get("2330") == 10, (
        "cache lost original position value after strategy mutation"
    )


@pytest.mark.asyncio
async def test_each_strategy_receives_independent_copy():
    """Three strategies, each mutating ctx.positions, must all start from same clean state."""
    store = _PositionStore(
        {
            "pos:*:2330": 7,
        }
    )
    runner = _make_runner(store)

    strategies = [_MutatingStrategy(f"strat_{i}") for i in range(3)]
    for s in strategies:
        runner.register(s)

    event = _make_tick_event("2330")
    await runner.process_event(event)

    for i, strat in enumerate(strategies):
        assert len(strat.seen_positions) == 1, f"strat_{i} not called"
        positions_at_entry = strat.seen_positions[0]
        assert "__injected__" not in positions_at_entry, (
            f"strat_{i} saw a key injected by a previous strategy"
        )
        assert positions_at_entry.get("2330") == 7, (
            f"strat_{i} did not see the original position value"
        )
