"""Coverage tests for strategies/rust_alpha.py — targeting uncovered lines.

Covers: Strategy init, param validation, _trim_book, on_book_update,
on_tick (L1 fallback, direction inference), _execute_on_signal (signal
thresholds, max_pos enforcement, entry price tracking), exception paths,
and metrics recording.

All prices use scaled int x10000 (Precision Law).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

_PRICE_SCALE = 10_000


# ---------------------------------------------------------------------------
# Stubbing
# ---------------------------------------------------------------------------


class _FakeCore:
    """Minimal AlphaStrategy stub returning configurable signal."""

    def __init__(self, *args, **kwargs):
        self.signal = 0.0

    def on_depth(self, bids, asks):
        return self.signal

    def on_trade(self, *args, **kwargs):
        pass


_saved_rust_core = None


def _stub_rust_core():
    """Ensure hft_platform.rust_core is importable with a stub AlphaStrategy."""
    global _saved_rust_core
    _saved_rust_core = sys.modules.get("hft_platform.rust_core")
    mod = MagicMock()
    mod.AlphaStrategy = _FakeCore
    sys.modules["hft_platform.rust_core"] = mod
    return mod


def _restore_rust_core():
    """Restore the original rust_core module."""
    global _saved_rust_core
    if _saved_rust_core is not None:
        sys.modules["hft_platform.rust_core"] = _saved_rust_core
    else:
        sys.modules.pop("hft_platform.rust_core", None)
    _saved_rust_core = None


@pytest.fixture(autouse=True)
def _cleanup_rust_core():
    """Restore sys.modules after each test."""
    yield
    _restore_rust_core()


def _make_strategy(**overrides):
    """Build a Strategy instance with Rust core stubbed out."""
    _stub_rust_core()
    from hft_platform.strategies.rust_alpha import Strategy

    defaults = {
        "strategy_id": "test_rust_alpha",
        "symbols": {"BTCUSD"},
        "signal_threshold": 0.3,
        "max_pos": 5,
        "lot_size": 1,
        "tick_size": 1.0,
        "stop_loss_ticks": 10,
    }
    defaults.update(overrides)
    strat = Strategy(**defaults)
    strat.core = _FakeCore()
    return strat


def _make_ctx(positions=None, l1_data=None):
    """Build a minimal StrategyContext mock."""
    from hft_platform.contracts.strategy import OrderIntent

    ctx = MagicMock()
    ctx.positions = positions or {}

    def _place(**kwargs):
        return MagicMock(spec=OrderIntent)

    ctx.place_order.side_effect = _place

    def _get_l1(symbol):
        if l1_data is not None:
            return l1_data
        return None

    ctx.get_l1_scaled = _get_l1
    return ctx


def _make_tick(symbol="BTCUSD", price=1000_0000, volume=10, source_ts=100):
    from hft_platform.events import MetaData, TickEvent

    return TickEvent(
        meta=MetaData(seq=1, source_ts=source_ts, local_ts=source_ts),
        symbol=symbol,
        price=price,
        volume=volume,
    )


def _make_bidask(symbol="BTCUSD", bids=None, asks=None):
    from hft_platform.events import BidAskEvent, MetaData

    return BidAskEvent(
        symbol=symbol,
        bids=bids or [],
        asks=asks or [],
        meta=MetaData(seq=1, source_ts=0, local_ts=0, topic="book"),
    )


# ---------------------------------------------------------------------------
# Initialization & Parameter Validation
# ---------------------------------------------------------------------------


class TestStrategyInit:
    def test_default_params_stored(self):
        """Lines 56-90: init stores merged params."""
        strat = _make_strategy()
        assert strat.params["signal_threshold"] == 0.3
        assert strat.params["max_pos"] == 5
        assert strat.params["lot_size"] == 1

    def test_custom_params_override_defaults(self):
        strat = _make_strategy(signal_threshold=0.5, max_pos=10)
        assert strat.params["signal_threshold"] == 0.5
        assert strat.params["max_pos"] == 10

    def test_initial_bbo_zero(self):
        """Lines 73-74: initial BBO is zero."""
        strat = _make_strategy()
        assert strat.best_bid == 0
        assert strat.best_ask == 0

    def test_shadow_book_empty(self):
        """Lines 95-96: shadow books start empty."""
        strat = _make_strategy()
        assert len(strat.bids) == 0
        assert len(strat.asks) == 0

    def test_entry_price_empty(self):
        """Line 78: entry price dict starts empty."""
        strat = _make_strategy()
        assert len(strat._entry_price) == 0

    def test_rust_core_not_found_raises(self):
        """Lines 61-62: ImportError when AlphaStrategy is None."""
        _stub_rust_core()
        import hft_platform.strategies.rust_alpha as mod

        old_alpha = mod.AlphaStrategy
        try:
            mod.AlphaStrategy = None
            with pytest.raises(ImportError, match="rust_core"):
                mod.Strategy(strategy_id="fail", symbols={"X"})
        finally:
            mod.AlphaStrategy = old_alpha


class TestParamValidation:
    def test_out_of_bounds_raises_value_error(self):
        """Lines 98-111: param out of range raises ValueError."""
        with pytest.raises(ValueError, match="out of bounds"):
            _make_strategy(hawkes_mu=-1.0)

    def test_non_numeric_param_raises_value_error(self):
        """Line 106: non-numeric type reported."""
        with pytest.raises(ValueError, match="expected numeric"):
            _make_strategy(hawkes_mu="bad")

    def test_valid_params_no_error(self):
        """All default params are within bounds."""
        strat = _make_strategy()
        assert strat.params["hawkes_mu"] == 0.5

    def test_boundary_min_value_accepted(self):
        strat = _make_strategy(hawkes_mu=0.0)
        assert strat.params["hawkes_mu"] == 0.0

    def test_boundary_max_value_accepted(self):
        strat = _make_strategy(hawkes_mu=100.0)
        assert strat.params["hawkes_mu"] == 100.0


# ---------------------------------------------------------------------------
# _trim_book
# ---------------------------------------------------------------------------


class TestTrimBook:
    def test_no_trim_when_within_limits(self):
        """Lines 119-120: early return when within max_book_levels."""
        strat = _make_strategy()
        strat.bids = {i * _PRICE_SCALE: 10 for i in range(10)}
        strat.asks = {i * _PRICE_SCALE: 10 for i in range(100, 110)}
        strat._trim_book()
        assert len(strat.bids) == 10
        assert len(strat.asks) == 10

    def test_trim_bids_keeps_best(self):
        """Lines 123-126: trims to max_book_levels, keeping highest bids."""
        strat = _make_strategy()
        strat._max_book_levels = 5
        strat.bids = {i * _PRICE_SCALE: 10 for i in range(20)}
        strat._trim_book()
        assert len(strat.bids) == 5
        # Should keep highest 5 prices
        kept = sorted(strat.bids.keys(), reverse=True)
        assert kept[0] == 19 * _PRICE_SCALE

    def test_trim_asks_keeps_best(self):
        """Lines 129-133: trims to max_book_levels, keeping lowest asks."""
        strat = _make_strategy()
        strat._max_book_levels = 5
        strat.asks = {(100 + i) * _PRICE_SCALE: 10 for i in range(20)}
        strat._trim_book()
        assert len(strat.asks) == 5
        kept = sorted(strat.asks.keys())
        assert kept[0] == 100 * _PRICE_SCALE

    def test_trim_both_sides(self):
        """Trim both bids and asks when both exceed limit."""
        strat = _make_strategy()
        strat._max_book_levels = 3
        strat.bids = {i * _PRICE_SCALE: 10 for i in range(10)}
        strat.asks = {(100 + i) * _PRICE_SCALE: 10 for i in range(10)}
        strat._trim_book()
        assert len(strat.bids) == 3
        assert len(strat.asks) == 3


# ---------------------------------------------------------------------------
# on_book_update
# ---------------------------------------------------------------------------


class TestOnBookUpdate:
    def test_unknown_symbol_ignored(self):
        """Line 136: symbol not in self.symbols returns early."""
        strat = _make_strategy()
        ctx = _make_ctx()
        event = _make_bidask(symbol="UNKNOWN", bids=[[100 * _PRICE_SCALE, 10]])
        strat.handle_event(ctx, event)
        assert len(strat.bids) == 0

    def test_bids_added_to_shadow_book(self):
        """Lines 143-149: bids populate shadow book."""
        strat = _make_strategy()
        ctx = _make_ctx()
        bids = [[100 * _PRICE_SCALE, 10], [99 * _PRICE_SCALE, 20]]
        event = _make_bidask(bids=bids, asks=[[101 * _PRICE_SCALE, 5]])
        strat.handle_event(ctx, event)
        assert strat.bids[100 * _PRICE_SCALE] == 10
        assert strat.bids[99 * _PRICE_SCALE] == 20

    def test_asks_added_to_shadow_book(self):
        """Lines 153-159: asks populate shadow book."""
        strat = _make_strategy()
        ctx = _make_ctx()
        asks = [[101 * _PRICE_SCALE, 15], [102 * _PRICE_SCALE, 25]]
        event = _make_bidask(bids=[[100 * _PRICE_SCALE, 5]], asks=asks)
        strat.handle_event(ctx, event)
        assert strat.asks[101 * _PRICE_SCALE] == 15
        assert strat.asks[102 * _PRICE_SCALE] == 25

    def test_zero_qty_removes_from_book(self):
        """Lines 146-147: qty <= 0 removes price level."""
        strat = _make_strategy()
        ctx = _make_ctx()
        # First add a level
        event1 = _make_bidask(bids=[[100 * _PRICE_SCALE, 10]], asks=[[101 * _PRICE_SCALE, 5]])
        strat.handle_event(ctx, event1)
        assert 100 * _PRICE_SCALE in strat.bids
        # Then remove it
        event2 = _make_bidask(bids=[[100 * _PRICE_SCALE, 0]], asks=[[101 * _PRICE_SCALE, 5]])
        strat.handle_event(ctx, event2)
        assert 100 * _PRICE_SCALE not in strat.bids

    def test_bbo_updated_when_both_sides_present(self):
        """Lines 165-167: best_bid = max(bids), best_ask = min(asks)."""
        strat = _make_strategy()
        ctx = _make_ctx()
        bids = [[100 * _PRICE_SCALE, 10], [99 * _PRICE_SCALE, 20]]
        asks = [[101 * _PRICE_SCALE, 15], [102 * _PRICE_SCALE, 25]]
        event = _make_bidask(bids=bids, asks=asks)
        strat.handle_event(ctx, event)
        assert strat.best_bid == 100 * _PRICE_SCALE
        assert strat.best_ask == 101 * _PRICE_SCALE

    def test_rust_core_on_depth_called(self):
        """Lines 170-174: sorted bids/asks passed to core.on_depth."""
        strat = _make_strategy()
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_depth.return_value = 0.0
        bids = [[100 * _PRICE_SCALE, 10], [99 * _PRICE_SCALE, 20]]
        asks = [[101 * _PRICE_SCALE, 15], [102 * _PRICE_SCALE, 25]]
        event = _make_bidask(bids=bids, asks=asks)
        strat.handle_event(ctx, event)
        assert strat.core.on_depth.called

    def test_on_book_update_exception_logged(self):
        """Lines 178-189: exception in on_depth caught and logged."""
        strat = _make_strategy()
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_depth.side_effect = RuntimeError("boom")
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        # Should not raise
        strat.handle_event(ctx, event)

    def test_on_book_update_exception_increments_metrics(self):
        """Lines 180-189: exception increments metrics counter."""
        strat = _make_strategy()
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_depth.side_effect = RuntimeError("boom")
        counter = MagicMock()
        strat._exc_metrics = counter
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        strat.handle_event(ctx, event)
        assert counter.labels.called

    def test_empty_bids_no_update(self):
        """Line 142: empty bids array skips bid processing."""
        strat = _make_strategy()
        ctx = _make_ctx()
        event = _make_bidask(bids=[], asks=[[101 * _PRICE_SCALE, 5]])
        strat.handle_event(ctx, event)
        assert len(strat.bids) == 0

    def test_empty_asks_no_update(self):
        """Line 152: empty asks array skips ask processing."""
        strat = _make_strategy()
        ctx = _make_ctx()
        event = _make_bidask(bids=[[100 * _PRICE_SCALE, 10]], asks=[])
        strat.handle_event(ctx, event)
        assert len(strat.asks) == 0


# ---------------------------------------------------------------------------
# on_tick
# ---------------------------------------------------------------------------


class TestOnTick:
    def test_unknown_symbol_ignored(self):
        """Line 192: symbol not in symbols."""
        strat = _make_strategy()
        ctx = _make_ctx()
        tick = _make_tick(symbol="UNKNOWN")
        strat.handle_event(ctx, tick)
        # No change to state
        assert strat.best_bid == 0

    def test_l1_fallback_when_bbo_not_set(self):
        """Lines 197-204: L1 fast path used when local BBO = 0."""
        strat = _make_strategy()
        l1 = (100, 50 * _PRICE_SCALE, 51 * _PRICE_SCALE, 0, 0, 0, 0)
        ctx = _make_ctx(l1_data=l1)
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=50 * _PRICE_SCALE)
        strat.handle_event(ctx, tick)
        assert strat.best_bid == 50 * _PRICE_SCALE
        assert strat.best_ask == 51 * _PRICE_SCALE

    def test_l1_fallback_skips_zero_values(self):
        """Lines 201-204: zero bid/ask from L1 not used."""
        strat = _make_strategy()
        l1 = (100, 0, 0, 0, 0, 0, 0)
        ctx = _make_ctx(l1_data=l1)
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=50 * _PRICE_SCALE)
        strat.handle_event(ctx, tick)
        assert strat.best_bid == 0  # unchanged

    def test_l1_not_used_when_bbo_set(self):
        """Lines 197: skip L1 fallback when BBO already set."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick()
        strat.handle_event(ctx, tick)
        # L1 not fetched
        assert strat.best_bid == 100 * _PRICE_SCALE

    def test_aggressor_buy_inferred_from_ask(self):
        """Lines 214-215: price >= best_ask → aggressor buy."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=101 * _PRICE_SCALE)
        strat.handle_event(ctx, tick)
        # on_trade called with is_buyer_maker=False (aggressor is buyer)
        call_args = strat.core.on_trade.call_args
        assert call_args[0][3] is False  # is_buyer_maker

    def test_aggressor_sell_inferred_from_bid(self):
        """Lines 216-217: price <= best_bid → aggressor sell."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=100 * _PRICE_SCALE)
        strat.handle_event(ctx, tick)
        call_args = strat.core.on_trade.call_args
        assert call_args[0][3] is True  # is_buyer_maker

    def test_midprice_defaults_to_aggressor_buy(self):
        """Lines 219-221: unclear price defaults to aggressor buy."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 102 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=101 * _PRICE_SCALE)  # between bid and ask
        strat.handle_event(ctx, tick)
        call_args = strat.core.on_trade.call_args
        assert call_args[0][3] is False  # is_buyer_maker (default aggressor buy)

    def test_on_tick_exception_logged(self):
        """Lines 227-238: exception in on_trade caught."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.side_effect = RuntimeError("tick boom")
        tick = _make_tick()
        # Should not raise
        strat.handle_event(ctx, tick)

    def test_on_tick_exception_increments_metrics(self):
        """Lines 229-238: exception increments metrics counter."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.side_effect = RuntimeError("tick boom")
        counter = MagicMock()
        strat._exc_metrics = counter
        tick = _make_tick()
        strat.handle_event(ctx, tick)
        assert counter.labels.called


# ---------------------------------------------------------------------------
# _execute_on_signal
# ---------------------------------------------------------------------------


class TestExecuteOnSignal:
    def test_buy_signal_above_threshold(self):
        """Lines 269-276: signal > threshold, pos < max_pos → buy."""
        strat = _make_strategy(signal_threshold=0.3, max_pos=5)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.5)
        assert ctx.place_order.called
        from hft_platform.contracts.strategy import Side
        call_kw = ctx.place_order.call_args.kwargs
        assert call_kw["side"] == Side.BUY
        assert call_kw["price"] == 100 * _PRICE_SCALE

    def test_sell_signal_below_neg_threshold(self):
        """Lines 279-286: signal < -threshold, pos > -max_pos → sell."""
        strat = _make_strategy(signal_threshold=0.3, max_pos=5)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=-0.5)
        assert ctx.place_order.called
        from hft_platform.contracts.strategy import Side
        call_kw = ctx.place_order.call_args.kwargs
        assert call_kw["side"] == Side.SELL
        assert call_kw["price"] == 101 * _PRICE_SCALE

    def test_signal_within_threshold_no_action(self):
        """Signal between -threshold and +threshold does nothing."""
        strat = _make_strategy(signal_threshold=0.3, max_pos=5)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.1)
        assert not ctx.place_order.called

    def test_max_pos_blocks_buy(self):
        """Lines 270: pos >= max_pos blocks buy signal."""
        strat = _make_strategy(signal_threshold=0.3, max_pos=2)
        ctx = _make_ctx(positions={"BTCUSD": 2})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.5)
        assert not ctx.place_order.called

    def test_max_pos_blocks_sell(self):
        """Lines 280: pos <= -max_pos blocks sell signal."""
        strat = _make_strategy(signal_threshold=0.3, max_pos=2)
        ctx = _make_ctx(positions={"BTCUSD": -2})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=-0.5)
        assert not ctx.place_order.called

    def test_zero_bid_price_no_buy(self):
        """Lines 273: price == 0 skips buy order."""
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 0
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.5)
        assert not ctx.place_order.called

    def test_zero_ask_price_no_sell(self):
        """Lines 283: price == 0 skips sell order."""
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 0
        strat._execute_on_signal("BTCUSD", signal=-0.5)
        assert not ctx.place_order.called

    def test_entry_price_recorded_on_buy(self):
        """Line 276: entry price tracked for stop-loss."""
        strat = _make_strategy()
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.5)
        assert strat._entry_price["BTCUSD"] == 100 * _PRICE_SCALE

    def test_entry_price_recorded_on_sell(self):
        """Line 286: entry price tracked for stop-loss on short."""
        strat = _make_strategy()
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=-0.5)
        assert strat._entry_price["BTCUSD"] == 101 * _PRICE_SCALE

    def test_stop_loss_long_fires(self):
        """Lines 254-258: long stop-loss triggers sell at best_bid."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        entry = 100 * _PRICE_SCALE
        stop_dist = 10 * int(1.0 * _PRICE_SCALE)
        bid = entry - stop_dist - 1
        ctx = _make_ctx(positions={"BTCUSD": 3})
        strat.ctx = ctx
        strat.best_bid = bid
        strat.best_ask = bid + _PRICE_SCALE
        strat._entry_price["BTCUSD"] = entry
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert ctx.place_order.called
        from hft_platform.contracts.strategy import Side
        call_kw = ctx.place_order.call_args.kwargs
        assert call_kw["side"] == Side.SELL
        assert call_kw["qty"] == 3  # abs(pos)
        assert "BTCUSD" not in strat._entry_price

    def test_stop_loss_short_fires(self):
        """Lines 261-265: short stop-loss triggers buy at best_ask."""
        strat = _make_strategy(stop_loss_ticks=5, tick_size=1.0)
        entry = 100 * _PRICE_SCALE
        stop_dist = 5 * int(1.0 * _PRICE_SCALE)
        ask = entry + stop_dist + 1
        ctx = _make_ctx(positions={"BTCUSD": -2})
        strat.ctx = ctx
        strat.best_bid = ask - _PRICE_SCALE
        strat.best_ask = ask
        strat._entry_price["BTCUSD"] = entry
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert ctx.place_order.called
        from hft_platform.contracts.strategy import Side
        call_kw = ctx.place_order.call_args.kwargs
        assert call_kw["side"] == Side.BUY
        assert call_kw["qty"] == 2
        assert "BTCUSD" not in strat._entry_price

    def test_stop_loss_not_triggered_within_threshold(self):
        """Lines 254: bid at exactly entry - stop_dist does not trigger."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        entry = 100 * _PRICE_SCALE
        stop_dist = 10 * int(1.0 * _PRICE_SCALE)
        bid = entry - stop_dist  # exactly at threshold (< is strict)
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx
        strat.best_bid = bid
        strat.best_ask = bid + _PRICE_SCALE
        strat._entry_price["BTCUSD"] = entry
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert not ctx.place_order.called

    def test_stop_loss_no_entry_price_skips(self):
        """Lines 253: entry == 0 skips stop-loss check."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx
        strat.best_bid = 50 * _PRICE_SCALE
        strat.best_ask = 51 * _PRICE_SCALE
        # No entry price set
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert not ctx.place_order.called

    def test_stop_loss_zero_bid_skips_long(self):
        """Lines 254: best_bid <= 0 skips long stop-loss."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx
        strat.best_bid = 0
        strat.best_ask = 101 * _PRICE_SCALE
        strat._entry_price["BTCUSD"] = 100 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert not ctx.place_order.called

    def test_stop_loss_zero_ask_skips_short(self):
        """Lines 261: best_ask <= 0 skips short stop-loss."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0)
        ctx = _make_ctx(positions={"BTCUSD": -1})
        strat.ctx = ctx
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 0
        strat._entry_price["BTCUSD"] = 100 * _PRICE_SCALE
        strat._execute_on_signal("BTCUSD", signal=0.0)
        assert not ctx.place_order.called

    def test_stop_loss_takes_priority_over_signal(self):
        """Stop-loss fires and returns before signal processing."""
        strat = _make_strategy(stop_loss_ticks=10, tick_size=1.0, signal_threshold=0.3)
        entry = 100 * _PRICE_SCALE
        stop_dist = 10 * int(1.0 * _PRICE_SCALE)
        bid = entry - stop_dist - 1
        ctx = _make_ctx(positions={"BTCUSD": 1})
        strat.ctx = ctx
        strat.best_bid = bid
        strat.best_ask = bid + _PRICE_SCALE
        strat._entry_price["BTCUSD"] = entry
        strat._execute_on_signal("BTCUSD", signal=0.8)  # strong buy signal
        # Stop-loss sell happens, not a buy
        from hft_platform.contracts.strategy import Side
        call_kw = ctx.place_order.call_args.kwargs
        assert call_kw["side"] == Side.SELL


# ---------------------------------------------------------------------------
# Book update + signal execution integration
# ---------------------------------------------------------------------------


class TestBookUpdateSignalFlow:
    def test_book_update_triggers_signal_execution(self):
        """Full flow: book update → on_depth signal → execute."""
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.core = _FakeCore()
        strat.core.signal = 0.5  # strong buy
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        intents = strat.handle_event(ctx, event)
        assert len(intents) == 1

    def test_book_update_below_threshold_no_order(self):
        """Signal below threshold generates no intents."""
        strat = _make_strategy(signal_threshold=0.3)
        ctx = _make_ctx(positions={"BTCUSD": 0})
        strat.core = _FakeCore()
        strat.core.signal = 0.1  # weak signal
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        intents = strat.handle_event(ctx, event)
        assert len(intents) == 0


# ---------------------------------------------------------------------------
# Metrics exception handling edge cases
# ---------------------------------------------------------------------------


class TestMetricsExceptionPaths:
    def test_on_book_update_metrics_exception_swallowed(self):
        """Lines 187-189: exception in metrics.labels().inc() is swallowed."""
        strat = _make_strategy()
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_depth.side_effect = RuntimeError("depth boom")
        counter = MagicMock()
        counter.labels.side_effect = RuntimeError("metrics boom")
        strat._exc_metrics = counter
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        # Should not raise even when metrics itself errors
        strat.handle_event(ctx, event)
        assert counter.labels.called

    def test_on_tick_metrics_exception_swallowed(self):
        """Lines 236-238: exception in metrics.labels().inc() is swallowed."""
        strat = _make_strategy()
        strat.best_bid = 100 * _PRICE_SCALE
        strat.best_ask = 101 * _PRICE_SCALE
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_trade.side_effect = RuntimeError("trade boom")
        counter = MagicMock()
        counter.labels.side_effect = RuntimeError("metrics boom")
        strat._exc_metrics = counter
        tick = _make_tick()
        strat.handle_event(ctx, tick)
        assert counter.labels.called

    def test_no_exc_metrics_on_exception(self):
        """Lines 180/229: _exc_metrics is None, exception still handled."""
        strat = _make_strategy()
        ctx = _make_ctx()
        strat.core = MagicMock()
        strat.core.on_depth.side_effect = RuntimeError("boom")
        strat._exc_metrics = None
        bids = [[100 * _PRICE_SCALE, 10]]
        asks = [[101 * _PRICE_SCALE, 15]]
        event = _make_bidask(bids=bids, asks=asks)
        strat.handle_event(ctx, event)  # no crash

    def test_on_tick_no_ctx_l1_fallback(self):
        """Lines 197-204: ctx.get_l1_scaled returns None."""
        strat = _make_strategy()
        ctx = _make_ctx(l1_data=None)
        # Override L1 source to return None always
        ctx._lob_l1_source = lambda _: None
        strat.core = MagicMock()
        strat.core.on_trade.return_value = None
        tick = _make_tick(price=50 * _PRICE_SCALE)
        strat.handle_event(ctx, tick)
        # BBO unchanged since L1 returned None
        assert strat.best_bid == 0
        assert strat.best_ask == 0
