"""Slice B Task 13 — MakerStrategyBridge.on_session_end FORCE_FLAT.

Returns one MARKET (price_type='MKT') OrderIntent with intent_type=FORCE_FLAT
to close any non-zero residual position when the session enters FORCE_FLAT
phase. The runner then drains the intent through the standard risk pipeline.

Position semantics:
  net_qty > 0 (long) → SELL to flatten
  net_qty < 0 (short) → BUY to flatten
  net_qty == 0 → no intent
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.backtest.maker_bridge import MakerStrategyBridge
from hft_platform.contracts.strategy import IntentType, Side


def _make_ctx(net_qty: int, symbol: str = "TMFD6", mid_scaled: int = 17_000_000) -> SimpleNamespace:
    """Build a minimal StrategyContext-like object exposing positions + L1 source.

    Mirrors the StrategyContext API in src/hft_platform/strategy/base.py:
      ctx.positions: dict[str, int]  (line 35-36, 69)
      ctx.get_l1_scaled(symbol) -> tuple  (line 130-139)
    L1 tuple shape: (timestamp_ns, best_bid, best_ask, mid_price_x2,
                     spread_scaled, bid_depth, ask_depth)
    """
    l1_tuple = (0, mid_scaled - 5_000, mid_scaled + 5_000, mid_scaled * 2, 10_000, 1, 1)
    return SimpleNamespace(
        positions={symbol: net_qty},
        get_l1_scaled=MagicMock(return_value=l1_tuple),
    )


def test_on_session_end_flat_returns_empty():
    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol="TMFD6")
    ctx = _make_ctx(net_qty=0)

    intents = bridge.on_session_end(ctx)

    assert intents == []


def test_on_session_end_long_returns_sell_force_flat():
    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol="TMFD6")
    ctx = _make_ctx(net_qty=1, mid_scaled=17_000_000)

    intents = bridge.on_session_end(ctx)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.intent_type == IntentType.FORCE_FLAT
    assert intent.side == Side.SELL
    assert intent.qty == 1
    assert intent.symbol == "TMFD6"
    assert intent.strategy_id == "maker_test"
    assert intent.price_type == "MKT"
    assert intent.reason == "session_end_force_flat"
    assert intent.price == 17_000_000


def test_on_session_end_short_returns_buy_force_flat_qty_2():
    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol="TMFD6")
    ctx = _make_ctx(net_qty=-2, mid_scaled=17_500_000)

    intents = bridge.on_session_end(ctx)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.intent_type == IntentType.FORCE_FLAT
    assert intent.side == Side.BUY
    assert intent.qty == 2
    assert intent.symbol == "TMFD6"
    assert intent.price_type == "MKT"
    assert intent.reason == "session_end_force_flat"


def test_on_session_end_unique_intent_ids():
    """Successive on_session_end calls must produce unique intent_ids
    so the risk pipeline can de-dup correctly."""
    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol="TMFD6")
    ctx = _make_ctx(net_qty=1)

    first = bridge.on_session_end(ctx)
    second = bridge.on_session_end(ctx)

    assert first[0].intent_id != second[0].intent_id


def test_on_session_end_no_l1_uses_zero_price():
    """If LOB has no L1 quote (e.g. market closed pre-flat), MARKET intent
    still emits with price=0 — risk + adapter handle MKT pricing downstream."""
    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="maker_test", symbol="TMFD6")
    ctx = SimpleNamespace(
        positions={"TMFD6": 1},
        get_l1_scaled=MagicMock(return_value=None),
    )

    intents = bridge.on_session_end(ctx)

    assert len(intents) == 1
    assert intents[0].price == 0
    assert intents[0].price_type == "MKT"
    assert intents[0].intent_type == IntentType.FORCE_FLAT
