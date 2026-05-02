"""Tests for MakerStrategyBridge."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from hft_platform.backtest.maker_bridge import MakerStrategyBridge, _side_from_str
from hft_platform.contracts.strategy import TIF, IntentType, Side


def test_side_from_str_buy():
    assert _side_from_str("buy") == Side.BUY
    assert _side_from_str("BUY") == Side.BUY


def test_side_from_str_sell():
    assert _side_from_str("sell") == Side.SELL


def test_side_from_str_invalid():
    with pytest.raises(ValueError, match="Unknown side"):
        _side_from_str("sideways")


def test_bridge_translates_post_quote_buy():
    from research.backtest.maker_engine import PostQuote

    inner = MagicMock()
    inner.on_tick.return_value = PostQuote(side="buy", price=17000, qty=2)

    bridge = MakerStrategyBridge(inner=inner, strategy_id="test", symbol="TMFD6")
    event = SimpleNamespace(symbol="TMFD6", best_bid=17000, best_ask=17001)
    intents = bridge.handle_event(ctx=None, event=event)

    assert len(intents) == 1
    intent = intents[0]
    assert intent.intent_type == IntentType.NEW
    assert intent.side == Side.BUY
    assert intent.price == 17000
    assert intent.qty == 2
    assert intent.tif == TIF.LIMIT
    assert intent.strategy_id == "test"
    assert intent.symbol == "TMFD6"
    assert intent.intent_id == 1  # counter starts at 1


def test_bridge_translates_post_quote_sell():
    from research.backtest.maker_engine import PostQuote

    inner = MagicMock()
    inner.on_tick.return_value = PostQuote(side="sell", price=17001, qty=1)

    bridge = MakerStrategyBridge(inner=inner, symbol="TMFD6")
    intents = bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))
    assert intents[0].side == Side.SELL


def test_bridge_translates_cancel_quote_uses_tracked_target():
    from research.backtest.maker_engine import CancelQuote, PostQuote

    inner = MagicMock()
    bridge = MakerStrategyBridge(inner=inner, strategy_id="test", symbol="TMFD6")

    # First place a buy quote
    inner.on_tick.return_value = PostQuote(side="buy", price=17000, qty=1)
    post_intents = bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))
    assert post_intents[0].intent_id == 1

    # Then cancel that side
    inner.on_tick.return_value = CancelQuote(side="buy")
    cancel_intents = bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))
    assert len(cancel_intents) == 1
    cancel = cancel_intents[0]
    assert cancel.intent_type == IntentType.CANCEL
    assert cancel.side == Side.BUY
    assert cancel.target_order_id == "test-1"


def test_bridge_translates_cancel_quote_with_no_active_order():
    """Cancel when no active order on that side — target_order_id is None."""
    from research.backtest.maker_engine import CancelQuote

    inner = MagicMock()
    inner.on_tick.return_value = CancelQuote(side="sell")

    bridge = MakerStrategyBridge(inner=inner, symbol="TMFD6")
    intents = bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))

    assert len(intents) == 1
    assert intents[0].intent_type == IntentType.CANCEL
    assert intents[0].target_order_id is None


def test_bridge_translates_hold():
    from research.backtest.maker_engine import Hold

    inner = MagicMock()
    inner.on_tick.return_value = Hold()

    bridge = MakerStrategyBridge(inner=inner, symbol="TMFD6")
    intents = bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))
    assert intents == []


def test_bridge_unknown_action_raises():
    inner = MagicMock()
    inner.on_tick.return_value = "some weird object"

    bridge = MakerStrategyBridge(inner=inner, symbol="TMFD6")
    with pytest.raises(TypeError, match="unknown action"):
        bridge.handle_event(ctx=None, event=SimpleNamespace(symbol="TMFD6"))


def test_bridge_uses_event_symbol_over_default():
    from research.backtest.maker_engine import PostQuote

    inner = MagicMock()
    inner.on_tick.return_value = PostQuote(side="buy", price=17000, qty=1)

    bridge = MakerStrategyBridge(inner=inner, symbol="DEFAULT")
    event = SimpleNamespace(symbol="TXFD6")
    intents = bridge.handle_event(ctx=None, event=event)
    assert intents[0].symbol == "TXFD6"
