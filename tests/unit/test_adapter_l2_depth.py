"""Tests for L2 depth support and pre-allocated buffers in HftBacktestAdapter."""

import numpy as np
import pytest

from hft_platform.backtest import adapter as hbt_adapter
from hft_platform.events import BidAskEvent
from hft_platform.strategy.base import BaseStrategy


# ---------------------------------------------------------------------------
# Stub / mock objects
# ---------------------------------------------------------------------------


class _BacktestAsset:
    def data(self, *a, **kw):
        return self

    def linear_asset(self, *a, **kw):
        return self

    def constant_order_latency(self, *a, **kw):
        return self

    def power_prob_queue_model(self, *a, **kw):
        return self

    def no_partial_fill_exchange(self, *a, **kw):
        return self

    def int_order_id_converter(self):
        return self

    def tick_size(self, *a, **kw):
        return self

    def lot_size(self, *a, **kw):
        return self


class _Hbt:
    def __init__(self, *a, **kw):
        self._ran = False
        self.current_timestamp = 1000
        self.submitted = []

    def wait_next_feed(self, *a, **kw):
        if self._ran:
            return 1
        self._ran = True
        return 0

    def depth(self, *a, **kw):
        return _DepthL1()

    def position(self, *a, **kw):
        return 0

    def submit_buy_order(self, *a, **kw):
        self.submitted.append(("buy", *a))

    def submit_sell_order(self, *a, **kw):
        self.submitted.append(("sell", *a))

    def cancel(self, *a, **kw):
        pass

    def close(self):
        return True


class _DepthL1:
    """Minimal depth stub with L1 only."""

    best_bid = 100.5
    best_ask = 101.0
    best_bid_qty = 10
    best_ask_qty = 20


class _DepthL2Mapping:
    """Depth stub that exposes bid_depth/ask_depth as dict mappings (5 levels)."""

    best_bid = 100.0
    best_ask = 101.0
    best_bid_qty = 10
    best_ask_qty = 15

    def __init__(self, tick_size: float = 1.0):
        ts = tick_size
        self.bid_depth = {
            100.0: 10,
            100.0 - ts: 20,
            100.0 - 2 * ts: 30,
            100.0 - 3 * ts: 40,
            100.0 - 4 * ts: 50,
        }
        self.ask_depth = {
            101.0: 15,
            101.0 + ts: 25,
            101.0 + 2 * ts: 35,
            101.0 + 3 * ts: 45,
            101.0 + 4 * ts: 55,
        }


class _DepthL2Partial:
    """Depth stub with only 3 valid bid levels, 2 valid ask levels."""

    best_bid = 50.0
    best_ask = 51.0
    best_bid_qty = 5
    best_ask_qty = 8

    def __init__(self, tick_size: float = 1.0):
        ts = tick_size
        self.bid_depth = {
            50.0: 5,
            50.0 - ts: 10,
            50.0 - 2 * ts: 15,
            # levels 4 and 5 missing
        }
        self.ask_depth = {
            51.0: 8,
            51.0 + ts: 12,
            # levels 3-5 missing
        }


class _NoopStrategy(BaseStrategy):
    def on_stats(self, event):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def _patch_hbt(monkeypatch):
    """Patch hftbacktest imports so we can construct HftBacktestAdapter without the real lib."""
    monkeypatch.setattr(hbt_adapter, "HFTBACKTEST_AVAILABLE", True, raising=False)
    monkeypatch.setattr(hbt_adapter, "HashMapMarketDepthBacktest", _Hbt, raising=False)
    monkeypatch.setattr(hbt_adapter, "BacktestAsset", _BacktestAsset, raising=False)
    monkeypatch.setattr(hbt_adapter, "IOC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "GTC", object(), raising=False)
    monkeypatch.setattr(hbt_adapter, "LIMIT", object(), raising=False)


def _make_adapter(depth_levels: int = 1, price_scale: int = 10_000, tick_size: float | None = None):
    strategy = _NoopStrategy("test")
    return hbt_adapter.HftBacktestAdapter(
        strategy=strategy,
        asset_symbol="TEST",
        data_path="dummy",
        price_scale=price_scale,
        depth_levels=depth_levels,
        tick_size=tick_size,
    )


# ---------------------------------------------------------------------------
# Tests: pre-allocated buffer setup
# ---------------------------------------------------------------------------


class TestPreAllocatedBuffers:
    def test_buffers_allocated_with_correct_shape_l1(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=1)
        assert adapter._bid_buf.shape == (1, 2)
        assert adapter._ask_buf.shape == (1, 2)
        assert adapter._bid_buf.dtype == np.int64
        assert adapter._ask_buf.dtype == np.int64

    def test_buffers_allocated_with_correct_shape_l5(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=5)
        assert adapter._bid_buf.shape == (5, 2)
        assert adapter._ask_buf.shape == (5, 2)

    def test_depth_levels_clamped_to_min_1(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=0)
        assert adapter._depth_levels == 1
        assert adapter._bid_buf.shape == (1, 2)

    def test_buffer_reuse_across_l1_calls(self, _patch_hbt):
        """Verify the pre-allocated buffer object identity is stable across calls."""
        adapter = _make_adapter(depth_levels=1)
        buf_bid_id = id(adapter._bid_buf)
        buf_ask_id = id(adapter._ask_buf)

        depth = _DepthL1()
        adapter._build_l1_bidask_event(depth, 1000)
        assert id(adapter._bid_buf) == buf_bid_id
        assert id(adapter._ask_buf) == buf_ask_id

        adapter._build_l1_bidask_event(depth, 2000)
        assert id(adapter._bid_buf) == buf_bid_id
        assert id(adapter._ask_buf) == buf_ask_id

    def test_buffer_reuse_across_l2_calls(self, _patch_hbt):
        """Verify the pre-allocated buffer object identity is stable across L2 calls."""
        adapter = _make_adapter(depth_levels=5, tick_size=1.0)
        buf_bid_id = id(adapter._bid_buf)
        buf_ask_id = id(adapter._ask_buf)

        depth = _DepthL2Mapping(tick_size=1.0)
        adapter._build_l2_bidask_event(depth, 1000)
        assert id(adapter._bid_buf) == buf_bid_id
        assert id(adapter._ask_buf) == buf_ask_id

        adapter._build_l2_bidask_event(depth, 2000)
        assert id(adapter._bid_buf) == buf_bid_id
        assert id(adapter._ask_buf) == buf_ask_id


# ---------------------------------------------------------------------------
# Tests: L1 backward compatibility
# ---------------------------------------------------------------------------


class TestL1BackwardCompat:
    def test_l1_build_produces_correct_shape(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=1)
        depth = _DepthL1()
        event = adapter._build_l1_bidask_event(depth, 5000)
        assert isinstance(event, BidAskEvent)
        assert event.bids.shape == (1, 2)
        assert event.asks.shape == (1, 2)

    def test_l1_build_prices_and_volumes(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=1)
        depth = _DepthL1()
        # best_bid=100.5, best_ask=101.0, but _build_l1 uses int(getattr(...))
        # so best_bid becomes int(100.5)=100, best_ask=int(101.0)=101
        event = adapter._build_l1_bidask_event(depth, 5000)
        assert event.bids[0, 0] == 100  # int(100.5) = 100
        assert event.bids[0, 1] == 10
        assert event.asks[0, 0] == 101  # int(101.0)
        assert event.asks[0, 1] == 20

    def test_l1_event_returned_is_independent_copy(self, _patch_hbt):
        """BidAskEvent arrays must not share memory with internal buffer."""
        adapter = _make_adapter(depth_levels=1)
        depth = _DepthL1()
        event1 = adapter._build_l1_bidask_event(depth, 1000)
        bid_val = event1.bids[0, 0]
        # Mutate the internal buffer
        adapter._bid_buf[0, 0] = 999999
        # event1 should be unaffected
        assert event1.bids[0, 0] == bid_val

    def test_default_depth_levels_is_1(self, _patch_hbt):
        strategy = _NoopStrategy("test")
        adapter = hbt_adapter.HftBacktestAdapter(
            strategy=strategy,
            asset_symbol="TEST",
            data_path="dummy",
        )
        assert adapter._depth_levels == 1


# ---------------------------------------------------------------------------
# Tests: L2 depth building
# ---------------------------------------------------------------------------


class TestL2DepthBuild:
    def test_l2_full_5_levels_from_mapping(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=5, price_scale=10_000, tick_size=1.0)
        depth = _DepthL2Mapping(tick_size=1.0)
        event = adapter._build_l2_bidask_event(depth, 9000)

        assert isinstance(event, BidAskEvent)
        assert event.bids.shape == (5, 2)
        assert event.asks.shape == (5, 2)

        # Verify bid prices (descending): 100, 99, 98, 97, 96 scaled x10000
        expected_bid_prices = [100 * 10_000, 99 * 10_000, 98 * 10_000, 97 * 10_000, 96 * 10_000]
        expected_bid_qtys = [10, 20, 30, 40, 50]
        for i in range(5):
            assert event.bids[i, 0] == expected_bid_prices[i], f"bid level {i} price mismatch"
            assert event.bids[i, 1] == expected_bid_qtys[i], f"bid level {i} qty mismatch"

        # Verify ask prices (ascending): 101, 102, 103, 104, 105 scaled x10000
        expected_ask_prices = [101 * 10_000, 102 * 10_000, 103 * 10_000, 104 * 10_000, 105 * 10_000]
        expected_ask_qtys = [15, 25, 35, 45, 55]
        for i in range(5):
            assert event.asks[i, 0] == expected_ask_prices[i], f"ask level {i} price mismatch"
            assert event.asks[i, 1] == expected_ask_qtys[i], f"ask level {i} qty mismatch"

    def test_l2_partial_fill_3_bids_2_asks(self, _patch_hbt):
        """depth_levels=5 but only 3 bid levels and 2 ask levels have data."""
        adapter = _make_adapter(depth_levels=5, price_scale=10_000, tick_size=1.0)
        depth = _DepthL2Partial(tick_size=1.0)
        event = adapter._build_l2_bidask_event(depth, 9000)

        # Should return only the filled levels
        assert event.bids.shape == (3, 2)
        assert event.asks.shape == (2, 2)

        assert event.bids[0, 0] == 50 * 10_000
        assert event.bids[0, 1] == 5
        assert event.bids[2, 0] == 48 * 10_000
        assert event.bids[2, 1] == 15

        assert event.asks[0, 0] == 51 * 10_000
        assert event.asks[0, 1] == 8
        assert event.asks[1, 0] == 52 * 10_000
        assert event.asks[1, 1] == 12

    def test_l2_fallback_to_l1_when_no_mapping(self, _patch_hbt):
        """When depth_obj has no bid_depth/ask_depth, L2 builder falls back to L1 attrs."""
        adapter = _make_adapter(depth_levels=5, price_scale=10_000, tick_size=1.0)
        depth = _DepthL1()  # No bid_depth/ask_depth attrs
        event = adapter._build_l2_bidask_event(depth, 9000)

        # Fallback: shape (1,2) from best_bid/best_ask
        assert event.bids.shape == (1, 2)
        assert event.asks.shape == (1, 2)
        # L2 builder scales float prices: int(100.5 * 10000) = 1005000
        assert event.bids[0, 0] == int(100.5 * 10_000)
        assert event.bids[0, 1] == 10
        assert event.asks[0, 0] == int(101.0 * 10_000)
        assert event.asks[0, 1] == 20

    def test_l2_event_is_independent_copy(self, _patch_hbt):
        """L2 BidAskEvent arrays must not share memory with internal buffer."""
        adapter = _make_adapter(depth_levels=5, price_scale=10_000, tick_size=1.0)
        depth = _DepthL2Mapping(tick_size=1.0)
        event1 = adapter._build_l2_bidask_event(depth, 1000)
        saved_bid = event1.bids[0, 0]
        # Mutate internal buffer
        adapter._bid_buf[0, 0] = 999999
        assert event1.bids[0, 0] == saved_bid

    def test_l2_price_scaling(self, _patch_hbt):
        """Verify prices are multiplied by price_scale."""
        adapter = _make_adapter(depth_levels=5, price_scale=100, tick_size=1.0)
        depth = _DepthL2Mapping(tick_size=1.0)
        event = adapter._build_l2_bidask_event(depth, 9000)
        # best_bid=100.0, price_scale=100 -> 10000
        assert event.bids[0, 0] == 100 * 100

    def test_l2_seq_increments(self, _patch_hbt):
        adapter = _make_adapter(depth_levels=5, tick_size=1.0)
        depth = _DepthL2Mapping(tick_size=1.0)
        e1 = adapter._build_l2_bidask_event(depth, 1000)
        e2 = adapter._build_l2_bidask_event(depth, 2000)
        assert e2.meta.seq == e1.meta.seq + 1


# ---------------------------------------------------------------------------
# Tests: run() loop dispatches correct builder
# ---------------------------------------------------------------------------


class TestRunLoopDispatch:
    def test_run_with_depth_levels_1_uses_l1(self, _patch_hbt):
        """Default depth_levels=1 should still work end-to-end."""
        adapter = _make_adapter(depth_levels=1)
        result = adapter.run()
        assert result is True

    def test_run_with_depth_levels_5_lob_feature(self, _patch_hbt, monkeypatch):
        """depth_levels=5 with lob_feature mode uses L2 builder."""
        strategy = _NoopStrategy("test")
        adapter = hbt_adapter.HftBacktestAdapter(
            strategy=strategy,
            asset_symbol="TEST",
            data_path="dummy",
            depth_levels=5,
            tick_size=1.0,
            feature_mode="lob_feature",
        )
        # The run loop should not crash even with L2 + lob_feature
        result = adapter.run()
        assert result is True
