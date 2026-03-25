"""Tests for SlippageDecomposer."""
from __future__ import annotations

from types import SimpleNamespace

from hft_platform.contracts.strategy import Side
from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.types import SlippageBreakdown


def _make_fill(
    price: int,
    fee: int = 0,
    tax: int = 0,
    decision_price: int = 0,
    arrival_price: int = 0,
    side: Side = Side.BUY,
) -> SimpleNamespace:
    return SimpleNamespace(
        price=price,
        fee=fee,
        tax=tax,
        decision_price=decision_price,
        arrival_price=arrival_price,
        side=side,
    )


class TestSlippageDecomposer:
    def setup_method(self):
        self.decomposer = SlippageDecomposer()

    def test_returns_slippage_breakdown_type(self):
        fill = _make_fill(price=1_500_000_000)  # 150000 NTD
        result = self.decomposer.decompose(fill, notional_ntd=150_000.0)
        assert isinstance(result, SlippageBreakdown)

    def test_commission_bps_proportional_to_fee(self):
        # fee = 20 NTD scaled x10000 = 200_000; notional = 200_000 NTD
        # comm_bps = (200_000 / 10_000) / 200_000 * 10_000 = 1.0 bps
        fill = _make_fill(price=2_000_000_000, fee=200_000)
        result = self.decomposer.decompose(fill, notional_ntd=200_000.0)
        assert abs(result.commission_bps - 1.0) < 1e-9

    def test_tax_bps_proportional_to_tax(self):
        # tax = 40 NTD scaled x10000 = 400_000; notional = 200_000 NTD
        # tax_bps = (400_000 / 10_000) / 200_000 * 10_000 = 2.0 bps
        fill = _make_fill(price=2_000_000_000, tax=400_000)
        result = self.decomposer.decompose(fill, notional_ntd=200_000.0)
        assert abs(result.tax_bps - 2.0) < 1e-9

    def test_buy_adverse_delay_positive_when_arrival_above_decision(self):
        # BUY: arrival > decision → positive delay cost (adverse)
        # prices are scaled x10000: 1000.0 NTD = 10_000_000 ticks
        # decision = 10_000_000, arrival = 10_001_000 (0.01% above)
        # delay = (10_001_000 - 10_000_000) / 10_000_000 * 10_000 = 1.0 bps
        fill = _make_fill(
            price=10_001_000,
            decision_price=10_000_000,
            arrival_price=10_001_000,
            side=Side.BUY,
        )
        result = self.decomposer.decompose(fill, notional_ntd=1_000.1)
        assert result.delay_cost_bps > 0

    def test_sell_adverse_delay_positive_when_arrival_below_decision(self):
        # SELL: decision > arrival → positive delay cost (adverse)
        # decision = 10_000_000, arrival = 9_999_000 (below)
        # delay = (10_000_000 - 9_999_000) / 10_000_000 * 10_000 = 1.0 bps
        fill = _make_fill(
            price=9_999_000,
            decision_price=10_000_000,
            arrival_price=9_999_000,
            side=Side.SELL,
        )
        result = self.decomposer.decompose(fill, notional_ntd=999.9)
        assert result.delay_cost_bps > 0

    def test_buy_favorable_execution_when_price_below_arrival(self):
        # BUY: fill below arrival → negative exec cost (favorable)
        fill = _make_fill(
            price=9_990_000,
            arrival_price=10_000_000,
            decision_price=10_000_000,
            side=Side.BUY,
        )
        result = self.decomposer.decompose(fill, notional_ntd=999.0)
        # exec_cost = (9_990_000 - 10_000_000) / 10_000_000 * 10_000 = -10.0 bps
        assert result.execution_cost_bps < 0

    def test_sell_favorable_execution_when_price_above_arrival(self):
        # SELL: fill above arrival → negative exec cost (favorable)
        fill = _make_fill(
            price=10_010_000,
            arrival_price=10_000_000,
            decision_price=10_000_000,
            side=Side.SELL,
        )
        result = self.decomposer.decompose(fill, notional_ntd=1_001.0)
        # exec_cost = (10_000_000 - 10_010_000) / 10_000_000 * 10_000 = -10.0 bps
        assert result.execution_cost_bps < 0

    def test_zero_decision_price_skips_delay(self):
        fill = _make_fill(price=10_000_000, decision_price=0, arrival_price=10_000_000)
        result = self.decomposer.decompose(fill, notional_ntd=1_000.0)
        assert result.delay_cost_bps == 0.0

    def test_zero_arrival_price_skips_exec_cost(self):
        fill = _make_fill(price=10_000_000, decision_price=10_000_000, arrival_price=0)
        result = self.decomposer.decompose(fill, notional_ntd=1_000.0)
        assert result.execution_cost_bps == 0.0

    def test_zero_notional_yields_zero_fee_and_tax(self):
        fill = _make_fill(price=10_000_000, fee=1_000_000, tax=500_000)
        result = self.decomposer.decompose(fill, notional_ntd=0.0)
        assert result.commission_bps == 0.0
        assert result.tax_bps == 0.0

    def test_total_is_sum_of_components(self):
        fill = _make_fill(
            price=10_050_000,
            fee=1_000_000,
            tax=500_000,
            decision_price=10_000_000,
            arrival_price=10_020_000,
            side=Side.BUY,
        )
        result = self.decomposer.decompose(fill, notional_ntd=1_005.0, market_impact_bps=3.0)
        expected_total = (
            result.commission_bps
            + result.tax_bps
            + result.delay_cost_bps
            + result.execution_cost_bps
            + result.market_impact_bps
        )
        assert abs(result.total_bps - expected_total) < 1e-9

    def test_market_impact_bps_passed_through(self):
        fill = _make_fill(price=10_000_000)
        result = self.decomposer.decompose(fill, notional_ntd=1_000.0, market_impact_bps=5.5)
        assert result.market_impact_bps == 5.5

    def test_zero_market_impact_default(self):
        fill = _make_fill(price=10_000_000)
        result = self.decomposer.decompose(fill, notional_ntd=1_000.0)
        assert result.market_impact_bps == 0.0
