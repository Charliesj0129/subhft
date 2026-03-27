# tests/unit/test_liquidity_gate.py
"""Tests for spread-based liquidity gate."""

from __future__ import annotations

from hft_platform.risk.liquidity_gate import LiquidityGate


class TestLiquidityGate:
    def test_allows_order_when_spread_below_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=3.0) is True

    def test_rejects_order_when_spread_above_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=7.0) is False

    def test_allows_at_exact_threshold(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        assert gate.check(spread_pts=5.0) is True

    def test_rejection_counter_increments(self) -> None:
        gate = LiquidityGate(max_spread_pts=5.0)
        gate.check(spread_pts=3.0)
        gate.check(spread_pts=7.0)
        gate.check(spread_pts=8.0)
        assert gate.total_rejected == 2
        assert gate.total_checked == 3

    def test_zero_threshold_rejects_all_nonzero(self) -> None:
        gate = LiquidityGate(max_spread_pts=0.0)
        assert gate.check(spread_pts=0.0) is True
        assert gate.check(spread_pts=0.1) is False
