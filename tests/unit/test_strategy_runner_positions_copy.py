"""Tests that StrategyRunner passes a read-only view of positions to each strategy context.

Verifies that:
1. `ctx.positions` is a `types.MappingProxyType` — attempting mutation raises TypeError.
2. Each strategy sees the same per-strategy positions (no accidental cross-strategy leak
   because each strategy_id owns a distinct underlying dict in `positions_by_strategy`).
3. The underlying `_positions_cache` is never corrupted (read-only enforces this).

Prior contract allowed `ctx.positions = dict(positions)` defensive copy. That allocated a
fresh dict per strategy per event. The runner now hands out a MappingProxyType view over
the already-cached per-strategy dict — zero allocation, mutation fails loud.
"""

from __future__ import annotations

import asyncio
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest


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


class _PositionStore:
    def __init__(self, initial: dict) -> None:
        self.positions = initial


def _make_tick_event(symbol: str = "2330") -> object:
    from hft_platform.events import MetaData, TickEvent

    return TickEvent(
        meta=MetaData(seq=1, topic="tick", source_ts=0, local_ts=0),
        symbol=symbol,
        price=550_000,
        volume=1,
        total_volume=1,
        bid_side_total_vol=0,
        ask_side_total_vol=0,
        is_simtrade=False,
        is_odd_lot=False,
    )


class _CapturingStrategy:
    def __init__(self, sid: str, symbol: str = "2330") -> None:
        self.strategy_id = sid
        self.symbols = {symbol}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self.seen_positions: list[dict] = []
        self.seen_type: list[type] = []

    def handle_event(self, ctx, event):  # noqa: ANN001
        self.seen_positions.append(dict(ctx.positions))
        self.seen_type.append(type(ctx.positions))
        return []


class _MutatingStrategy:
    """Attempts to mutate ctx.positions — must raise TypeError under read-only contract."""

    def __init__(self, sid: str, symbol: str = "2330") -> None:
        self.strategy_id = sid
        self.symbols = {symbol}
        self.enabled = True
        self.required_features = []
        self.required_feature_profile = None
        self.mutation_error: Exception | None = None
        self.seen_positions: list[dict] = []

    def handle_event(self, ctx, event):  # noqa: ANN001
        self.seen_positions.append(dict(ctx.positions))
        try:
            ctx.positions["__injected__"] = 9999
        except TypeError as exc:
            self.mutation_error = exc
        return []


@pytest.mark.asyncio
async def test_ctx_positions_is_readonly_view():
    """ctx.positions must be MappingProxyType; direct mutation must raise TypeError."""
    store = _PositionStore({"pos:*:2330": 5})
    runner = _make_runner(store)

    mutator = _MutatingStrategy("strat_a")
    runner.register(mutator)

    await runner.process_event(_make_tick_event("2330"))

    assert mutator.mutation_error is not None, "mutation must have raised"
    assert isinstance(mutator.mutation_error, TypeError)


@pytest.mark.asyncio
async def test_mutation_attempt_does_not_corrupt_positions_cache():
    """Failed mutation must leave _positions_cache pristine."""
    store = _PositionStore({"pos:*:2330": 10})
    runner = _make_runner(store)

    mutator = _MutatingStrategy("strat_mut")
    runner.register(mutator)

    await runner.process_event(_make_tick_event("2330"))

    cached = runner._positions_cache
    wildcard_positions = cached.get("*", {})
    assert "__injected__" not in wildcard_positions
    assert wildcard_positions.get("2330") == 10


@pytest.mark.asyncio
async def test_each_strategy_receives_readonly_view():
    """All strategies see MappingProxyType. Observer sees clean state despite mutator attempt."""
    store = _PositionStore({"pos:*:2330": 7})
    runner = _make_runner(store)

    mutator = _MutatingStrategy("strat_a")
    observer = _CapturingStrategy("strat_b")
    runner.register(mutator)
    runner.register(observer)

    await runner.process_event(_make_tick_event("2330"))

    assert mutator.mutation_error is not None
    assert observer.seen_type == [MappingProxyType]
    assert "__injected__" not in observer.seen_positions[0]
    assert observer.seen_positions[0].get("2330") == 7
