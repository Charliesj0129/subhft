"""Tests for SlippageTracker per-fill slippage computation."""
import pytest
from hft_platform.execution.slippage_tracker import SlippageTracker, SlippageRecord
from hft_platform.contracts.strategy import Side


class TestSlippageComputation:
    def test_buy_adverse_slippage(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_1", symbol="TMFD6", side=Side.BUY,
            decision_mid=204980000, fill_price=205000000,
            order_ts_ns=1000, fill_ts_ns=2000,
            tick_size_scaled=10000, point_value=10,
        )
        assert record.slippage_ticks == 2
        assert record.slippage_ntd == 20
        assert record.latency_ns == 1000

    def test_sell_adverse_slippage(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_2", symbol="TMFD6", side=Side.SELL,
            decision_mid=204980000, fill_price=204960000,
            order_ts_ns=1000, fill_ts_ns=2000,
            tick_size_scaled=10000, point_value=10,
        )
        assert record.slippage_ticks == 2
        assert record.slippage_ntd == 20

    def test_favorable_slippage_negative(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_3", symbol="TMFD6", side=Side.BUY,
            decision_mid=204980000, fill_price=204970000,
            order_ts_ns=1000, fill_ts_ns=2000,
            tick_size_scaled=10000, point_value=10,
        )
        assert record.slippage_ticks == -1
        assert record.slippage_ntd == -10

    def test_zero_slippage(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_4", symbol="TMFD6", side=Side.BUY,
            decision_mid=205000000, fill_price=205000000,
            order_ts_ns=1000, fill_ts_ns=2000,
            tick_size_scaled=10000, point_value=10,
        )
        assert record.slippage_ticks == 0
        assert record.slippage_ntd == 0

    def test_skips_when_decision_mid_is_zero(self):
        record = SlippageTracker.compute_slippage(
            order_id="test_5", symbol="TMFD6", side=Side.BUY,
            decision_mid=0, fill_price=205000000,
            order_ts_ns=1000, fill_ts_ns=2000,
            tick_size_scaled=10000, point_value=10,
        )
        assert record is None
