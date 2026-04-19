"""Tests for SimpleMakerStrategy."""

from research.backtest.maker_engine import Hold, PostQuote, SimpleMakerStrategy, TickData


def test_posts_quotes_when_spread_above_threshold():
    s = SimpleMakerStrategy(spread_threshold_pts=5, max_pos=1)
    tick = TickData(
        exch_ts=1,
        bid_price=100_000_000,
        ask_price=105_000_000,
        bid_qty=10,
        ask_qty=10,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=1_000_000,
    )
    actions = s.on_tick(tick)
    assert any(isinstance(a, PostQuote) and a.side == "buy" for a in actions)
    assert any(isinstance(a, PostQuote) and a.side == "sell" for a in actions)


def test_holds_when_spread_below_threshold():
    s = SimpleMakerStrategy(spread_threshold_pts=5, max_pos=1)
    tick = TickData(
        exch_ts=1,
        bid_price=100_000_000,
        ask_price=103_000_000,
        bid_qty=10,
        ask_qty=10,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=1_000_000,
    )
    actions = s.on_tick(tick)
    assert all(isinstance(a, Hold) for a in actions)


def test_respects_max_position():
    s = SimpleMakerStrategy(spread_threshold_pts=1, max_pos=1)
    s._position = 1  # already at max long
    tick = TickData(
        exch_ts=1,
        bid_price=100_000_000,
        ask_price=105_000_000,
        bid_qty=10,
        ask_qty=10,
        trade_price=0,
        trade_volume=0,
        is_trade=False,
        scale=1_000_000,
    )
    actions = s.on_tick(tick)
    # Should only have sell quote (buy blocked by max_pos)
    assert not any(isinstance(a, PostQuote) and a.side == "buy" for a in actions)
    assert any(isinstance(a, PostQuote) and a.side == "sell" for a in actions)


def test_on_fill_updates_position():
    s = SimpleMakerStrategy(spread_threshold_pts=1, max_pos=3)
    assert s._position == 0
    s.on_fill("buy", 100_000_000, 100.5)
    assert s._position == 1
    s.on_fill("sell", 101_000_000, 100.5)
    assert s._position == 0
