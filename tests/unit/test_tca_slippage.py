# tests/unit/test_tca_slippage.py
"""Tests for TCA SlippageDecomposer."""

from __future__ import annotations

import pytest

from hft_platform.tca.slippage import SlippageDecomposer
from hft_platform.tca.types import SlippageBreakdown


class TestSlippageDecomposer:
    def setup_method(self) -> None:
        self.decomposer = SlippageDecomposer(point_value=10, tick_size=1.0)

    def test_zero_slippage_when_prices_equal(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_000_000,
            fill_price=200_000_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert isinstance(result, SlippageBreakdown)
        assert result.delay_cost_bps == pytest.approx(0.0, abs=0.01)
        assert result.execution_cost_bps == pytest.approx(0.0, abs=0.01)

    def test_delay_cost_captured(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_010_000,
            fill_price=200_010_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert result.delay_cost_bps > 0

    def test_execution_cost_captured(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_000_000,
            fill_price=200_020_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=0,
        )
        assert result.execution_cost_bps > 0

    def test_total_is_sum_of_components(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_010_000,
            fill_price=200_030_000,
            notional_ntd=200_000,
            fee_ntd=13,
            tax_ntd=6,
        )
        expected_total = (
            result.commission_bps
            + result.tax_bps
            + result.delay_cost_bps
            + result.execution_cost_bps
            + result.market_impact_bps
        )
        assert result.total_bps == pytest.approx(expected_total, abs=0.01)

    def test_zero_notional_returns_zero_breakdown(self) -> None:
        result = self.decomposer.decompose(
            decision_price=200_000_000,
            arrival_price=200_000_000,
            fill_price=200_000_000,
            notional_ntd=0,
            fee_ntd=0,
            tax_ntd=0,
        )
        assert result.total_bps == 0.0
