"""Unit tests for RustAlpha strategy stop-loss logic.

Tests cover:
- Long stop-loss triggers when bid falls below entry - stop_dist
- Short stop-loss triggers when ask rises above entry + stop_dist
- Stop-loss not triggered when price moves are within tolerance
- Entry price cleared after stop-loss fires
- No stop-loss when entry price not set
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class _FakeCore:
    """Minimal AlphaStrategy stub that returns a configurable signal."""

    def __init__(self, *args, **kwargs):
        self.signal = 0.0

    def on_depth(self, bids, asks):
        return self.signal

    def on_trade(self, *args, **kwargs):
        pass


def _make_strategy(signal_threshold=0.3, stop_loss_ticks=10, tick_size=1.0, max_pos=5, lot_size=1):
    """Build a Strategy instance with Rust core stubbed out."""
    import importlib
    import sys

    # Stub out hft_platform.rust_core so Strategy can be instantiated
    rust_mock = MagicMock()
    rust_mock.AlphaStrategy = _FakeCore
    sys.modules.setdefault("hft_platform.rust_core", rust_mock)

    from hft_platform.strategies.rust_alpha import Strategy

    strat = Strategy(
        strategy_id="test",
        symbols={"BTCUSD"},
        signal_threshold=signal_threshold,
        stop_loss_ticks=stop_loss_ticks,
        tick_size=tick_size,
        max_pos=max_pos,
        lot_size=lot_size,
    )
    # Replace real core with stub
    strat.core = _FakeCore()
    return strat


def _make_ctx(positions=None):
    """Build a minimal StrategyContext mock."""
    from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF

    ctx = MagicMock()
    ctx.positions = positions or {}

    def _place(**kwargs):
        return MagicMock(spec=OrderIntent)

    ctx.place_order.side_effect = _place
    return ctx


def _bid_ask_event(symbol="BTCUSD", bids=None, asks=None):
    from hft_platform.events import BidAskEvent, EventMeta

    return BidAskEvent(
        symbol=symbol,
        bids=bids or [],
        asks=asks or [],
        meta=EventMeta(topic="book", seq=1, source_ts=0, local_ts=0),
    )


# --------------------------------------------------------------------------- #
# Helper to inject bbo state directly without going through on_book_update


def _set_bbo(strat, bid: int, ask: int, entry: int | None = None, pos: int = 0):
    strat.best_bid = bid
    strat.best_ask = ask
    if entry is not None:
        strat._entry_price["BTCUSD"] = entry


# --------------------------------------------------------------------------- #


class TestLongStopLoss:
    def test_stop_loss_fires_when_bid_below_threshold(self):
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx

        entry = 1_000_000  # scaled int (price * 10_000 convention from default)
        stop_dist = 10 * int(1.0 * 10_000)  # = 100_000
        bid = entry - stop_dist - 1  # one tick below stop
        _set_bbo(strat, bid=bid, ask=bid + 10, entry=entry)

        strat._execute_on_signal("BTCUSD", signal=0.0)

        # sell should have been called (close long)
        assert ctx.place_order.called
        call_kwargs = ctx.place_order.call_args.kwargs
        from hft_platform.contracts.strategy import Side

        assert call_kwargs["side"] == Side.SELL
        # Entry price should be cleared
        assert "BTCUSD" not in strat._entry_price

    def test_stop_loss_does_not_fire_when_bid_at_threshold(self):
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx

        entry = 1_000_000
        stop_dist = 10 * int(1.0 * 10_000)
        bid = entry - stop_dist  # exactly at stop (< is strict)
        _set_bbo(strat, bid=bid, ask=bid + 10, entry=entry)

        strat._execute_on_signal("BTCUSD", signal=0.0)

        assert not ctx.place_order.called

    def test_stop_loss_does_not_fire_when_no_entry(self):
        strat = _make_strategy()
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx
        _set_bbo(strat, bid=100_000, ask=110_000)  # no entry set

        strat._execute_on_signal("BTCUSD", signal=0.0)

        assert not ctx.place_order.called


class TestShortStopLoss:
    def test_stop_loss_fires_when_ask_above_threshold(self):
        strat = _make_strategy(stop_loss_ticks=5, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": -1})
        strat.ctx = ctx

        entry = 1_000_000
        stop_dist = 5 * int(1.0 * 10_000)  # = 50_000
        ask = entry + stop_dist + 1  # one tick above stop
        _set_bbo(strat, bid=ask - 10, ask=ask, entry=entry)

        strat._execute_on_signal("BTCUSD", signal=0.0)

        assert ctx.place_order.called
        call_kwargs = ctx.place_order.call_args.kwargs
        from hft_platform.contracts.strategy import Side

        assert call_kwargs["side"] == Side.BUY
        assert "BTCUSD" not in strat._entry_price

    def test_stop_loss_not_fire_when_ask_at_threshold(self):
        strat = _make_strategy(stop_loss_ticks=5, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": -1})
        strat.ctx = ctx

        entry = 1_000_000
        stop_dist = 5 * int(1.0 * 10_000)
        ask = entry + stop_dist  # exactly at (> is strict)
        _set_bbo(strat, bid=ask - 10, ask=ask, entry=entry)

        strat._execute_on_signal("BTCUSD", signal=0.0)

        assert not ctx.place_order.called


class TestEntryPriceTracking:
    def test_entry_recorded_on_buy(self):
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        bid, ask = 1_000_000, 1_010_000
        _set_bbo(strat, bid=bid, ask=ask)
        strat.core.signal = 0.5  # above threshold

        strat._execute_on_signal("BTCUSD", signal=0.5)

        assert strat._entry_price.get("BTCUSD") == bid

    def test_entry_recorded_on_sell(self):
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        bid, ask = 1_000_000, 1_010_000
        _set_bbo(strat, bid=bid, ask=ask)

        strat._execute_on_signal("BTCUSD", signal=-0.5)

        assert strat._entry_price.get("BTCUSD") == ask
