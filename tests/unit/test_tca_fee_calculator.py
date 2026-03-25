"""FeeCalculator unit tests — validates per-contract fee model for Taiwan futures."""
from __future__ import annotations

import pytest
import yaml

from hft_platform.contracts.strategy import Side
from hft_platform.tca.fee_calculator import FeeCalculator
from hft_platform.tca.types import FeeBreakdown


@pytest.fixture()
def fee_config() -> dict:
    return yaml.safe_load("""
futures:
  XMT:
    commission_per_contract: 13
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 10
  TX:
    commission_per_contract: 60
    tax_rate_bps: 2.0
    tax_side: sell
    tick_size: 1
    point_value: 200
  stock_futures_default:
    commission_per_contract: 20
    tax_rate_bps: 4.0
    tax_side: sell
    tick_size: 0.01
    point_value: 2000
  overrides:
    "2330F":
      commission_per_contract: 25
""")


@pytest.fixture()
def calc(fee_config: dict) -> FeeCalculator:
    return FeeCalculator(fee_config)


class TestBuySide:
    def test_buy_xmt_commission_only(self, calc: FeeCalculator) -> None:
        result = calc.calculate("XMT", Side.BUY, qty=1, fill_price=200_000_000)
        assert result.commission == 130_000
        assert result.tax == 0
        assert result.total == 130_000

    def test_buy_tx_commission(self, calc: FeeCalculator) -> None:
        result = calc.calculate("TX", Side.BUY, qty=2, fill_price=200_000_000)
        assert result.commission == 1_200_000
        assert result.tax == 0


class TestSellSide:
    def test_sell_xmt_reference_validation(self, calc: FeeCalculator) -> None:
        """Validate against known reference: XMT sell at ~7000, tax ≈ 14 NTD."""
        result = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        assert result.tax == 140_000
        assert result.commission == 130_000
        assert result.total == 270_000

    def test_sell_xmt_round_trip_40ntd(self, calc: FeeCalculator) -> None:
        """Full round-trip at index ~7000 should be ~40 NTD (400_000 scaled)."""
        buy = calc.calculate("XMT", Side.BUY, qty=1, fill_price=70_000_000)
        sell = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        round_trip = buy.total + sell.total
        assert round_trip == 400_000


class TestMultiContract:
    def test_qty_scales_linearly(self, calc: FeeCalculator) -> None:
        one = calc.calculate("XMT", Side.SELL, qty=1, fill_price=70_000_000)
        five = calc.calculate("XMT", Side.SELL, qty=5, fill_price=70_000_000)
        assert five.commission == one.commission * 5
        assert five.tax == one.tax * 5
        assert five.total == one.total * 5


class TestOverrides:
    def test_2330f_uses_custom_commission(self, calc: FeeCalculator) -> None:
        result = calc.calculate("2330F", Side.BUY, qty=1, fill_price=5_000_000_000)
        assert result.commission == 250_000

    def test_unknown_stock_future_uses_default(self, calc: FeeCalculator) -> None:
        result = calc.calculate("2317F", Side.BUY, qty=1, fill_price=1_000_000_000)
        assert result.commission == 200_000


class TestEdgeCases:
    def test_zero_price_fill(self, calc: FeeCalculator) -> None:
        result = calc.calculate("XMT", Side.SELL, qty=1, fill_price=0)
        assert result.commission == 130_000
        assert result.tax == 0

    def test_unknown_symbol_raises(self, calc: FeeCalculator) -> None:
        with pytest.raises(KeyError):
            calc.calculate("INVALID", Side.BUY, qty=1, fill_price=100_000_000)
