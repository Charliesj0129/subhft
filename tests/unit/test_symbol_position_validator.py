"""Tests for SymbolPositionLimitValidator (WU-09)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.execution.positions import Position, PositionStore
from hft_platform.risk.symbol_position_validator import SymbolPositionLimitValidator


def _make_intent(
    symbol: str = "2330",
    side: Side = Side.BUY,
    qty: int = 10,
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "strat_a",
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=1000_0000,  # scaled int
        qty=qty,
        tif=TIF.LIMIT,
        timestamp_ns=0,
    )


def _make_store_with_positions(**symbol_nets: int) -> PositionStore:
    """Return a mock PositionStore with positions keyed by symbol."""
    store = MagicMock(spec=PositionStore)
    positions: dict[str, Position] = {}
    for i, (sym, net) in enumerate(symbol_nets.items()):
        key = f"acc:strat:{sym}"
        pos = Position(account_id="acc", strategy_id="strat", symbol=sym, net_qty=net)
        positions[key] = pos
    store.positions = positions
    return store


class TestSymbolPositionLimitValidator:
    """Core acceptance tests."""

    def test_within_limit_approved(self) -> None:
        store = _make_store_with_positions(**{"2330": 50})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.BUY, qty=40))
        assert ok is True
        assert reason == "OK"

    def test_exceeds_limit_rejected(self) -> None:
        store = _make_store_with_positions(**{"2330": 90})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.BUY, qty=20))
        assert ok is False
        assert "SYMBOL_POSITION_LIMIT" in reason

    def test_per_symbol_override(self) -> None:
        config = {
            "default_max_position_lots": 1000,
            "symbol_limits": {"2330": {"max_position_lots": 50}},
        }
        store = _make_store_with_positions(**{"2330": 40})
        v = SymbolPositionLimitValidator(config, position_store=store)
        # 40 + 20 = 60 > 50 per-symbol override
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.BUY, qty=20))
        assert ok is False
        assert "SYMBOL_POSITION_LIMIT" in reason

    def test_cancel_always_passes(self) -> None:
        store = _make_store_with_positions(**{"2330": 9999})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 10}, position_store=store)
        ok, reason = v.check(_make_intent(intent_type=IntentType.CANCEL))
        assert ok is True

    def test_amend_always_passes(self) -> None:
        store = _make_store_with_positions(**{"2330": 9999})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 10}, position_store=store)
        ok, reason = v.check(_make_intent(intent_type=IntentType.AMEND))
        assert ok is True

    def test_no_store_graceful(self) -> None:
        """Without a PositionStore, current_net is 0 so order passes."""
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=None)
        ok, reason = v.check(_make_intent(qty=50))
        assert ok is True

    def test_sell_reduces_position(self) -> None:
        store = _make_store_with_positions(**{"2330": 80})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        # Selling 30: projected = abs(80 - 30) = 50, within limit
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.SELL, qty=30))
        assert ok is True

    def test_sell_creates_short_over_limit(self) -> None:
        store = _make_store_with_positions(**{"2330": 10})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        # Selling 120: projected = abs(10 - 120) = 110 > 100
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.SELL, qty=120))
        assert ok is False
        assert "SYMBOL_POSITION_LIMIT" in reason

    def test_aggregates_across_strategies(self) -> None:
        """Multiple positions with the same symbol should be aggregated."""
        store = MagicMock(spec=PositionStore)
        store.positions = {
            "acc:strat_a:2330": Position("acc", "strat_a", "2330", net_qty=60),
            "acc:strat_b:2330": Position("acc", "strat_b", "2330", net_qty=30),
        }
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        # current_net = 90, buy 20 -> projected 110 > 100
        ok, reason = v.check(_make_intent(symbol="2330", side=Side.BUY, qty=20))
        assert ok is False

    def test_different_symbol_not_counted(self) -> None:
        store = _make_store_with_positions(**{"2330": 900, "2317": 5})
        v = SymbolPositionLimitValidator({"default_max_position_lots": 100}, position_store=store)
        # 2317 net=5, buy 50 -> projected 55, within limit
        ok, reason = v.check(_make_intent(symbol="2317", side=Side.BUY, qty=50))
        assert ok is True


@pytest.mark.parametrize(
    "current_net, side, qty, limit, expected_ok",
    [
        (0, Side.BUY, 100, 100, True),  # exactly at limit
        (0, Side.BUY, 101, 100, False),  # one over
        (0, Side.SELL, 100, 100, True),  # short exactly at limit
        (0, Side.SELL, 101, 100, False),  # short one over
        (-50, Side.BUY, 50, 100, True),  # closing short to flat
        (-50, Side.SELL, 50, 100, True),  # deepening short exactly at limit
        (-50, Side.SELL, 51, 100, False),  # deepening short one over
        (99, Side.BUY, 1, 100, True),  # boundary: 100 = limit
        (100, Side.BUY, 1, 100, False),  # boundary: 101 > limit
    ],
    ids=[
        "flat_buy_exact",
        "flat_buy_over",
        "flat_sell_exact",
        "flat_sell_over",
        "close_short",
        "deepen_short_exact",
        "deepen_short_over",
        "boundary_at_limit",
        "boundary_over_limit",
    ],
)
def test_parametrized_edges(
    current_net: int,
    side: Side,
    qty: int,
    limit: int,
    expected_ok: bool,
) -> None:
    store = _make_store_with_positions(**{"2330": current_net})
    v = SymbolPositionLimitValidator({"default_max_position_lots": limit}, position_store=store)
    ok, _ = v.check(_make_intent(symbol="2330", side=side, qty=qty))
    assert ok is expected_ok
