"""Regression test for StormGuard MtM drawdown scale bug (Bug 11, 2026-04-17).

Root cause: services/system.py inline drawdown computation mixed scaled-int
unrealized PnL (x10000) with raw-NTD base_capital, inflating phantom drawdown
by 10,000x. A 2 pt price move on 1 Mini TAIEX contract produced a false -200bps
drawdown and HALT.

This test verifies that a small unrealized loss (typical intraday MtM noise)
must NOT produce a drawdown_bps value that would trigger HALT.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _combine(realized: float, unrealized_scaled: int, base_capital: int) -> float:
    """Shim that calls the production combiner."""
    from hft_platform.services.system import HFTSystem

    return HFTSystem._combine_drawdown_with_mtm(
        realized_drawdown_pct=realized,
        unrealized_scaled=unrealized_scaled,
        base_capital=base_capital,
    )


class TestMtmDrawdownScale:
    """Verify unrealized PnL is descaled before dividing by raw-NTD base_capital."""

    def test_1pt_loss_on_mini_taiex_is_negligible(self):
        """1 pt loss on 1 TMFE6 contract = 10 NTD real loss.

        On 10M NTD capital, real drawdown is 0.0001% = 0.01 bps.
        Must NOT return 100 bps (the pre-fix buggy value).
        """
        # mid=37113, avg=37114, qty=1, mult=10 → (−1 * 10000) * 1 * 10 = −100,000 scaled
        unrealized_scaled = -100_000
        base_capital = 10_000_000  # raw NTD
        drawdown = _combine(realized=0.0, unrealized_scaled=unrealized_scaled, base_capital=base_capital)
        # Real drawdown: 10 NTD loss / 10M NTD = 1e-6
        assert drawdown < 0.0001, f"1 pt MtM loss produced phantom drawdown {drawdown*10000:.1f}bps"
        drawdown_bps = -int(drawdown * 10_000)
        # Must be ≥ -200bps (HALT threshold). A fractional bps is fine.
        assert drawdown_bps > -200, f"1 pt loss triggered HALT: {drawdown_bps}bps"

    def test_2pt_loss_does_not_trigger_halt(self):
        """Even a 2 pt adverse move must not trigger HALT on micro capital."""
        unrealized_scaled = -200_000  # 2 pts × 1 contract × 10 mult, scaled
        drawdown = _combine(realized=0.0, unrealized_scaled=unrealized_scaled, base_capital=10_000_000)
        drawdown_bps = -int(drawdown * 10_000)
        assert drawdown_bps > -200, f"2 pt loss still triggers HALT: {drawdown_bps}bps"

    def test_catastrophic_loss_still_triggers_halt(self):
        """Real 2% capital loss (200K NTD) must still trigger HALT.

        Validates the MtM drawdown mechanism is functional, not broken.
        """
        # 200K NTD loss = 2_000_000_000 scaled int
        unrealized_scaled = -2_000_000_000
        drawdown = _combine(realized=0.0, unrealized_scaled=unrealized_scaled, base_capital=10_000_000)
        drawdown_bps = -int(drawdown * 10_000)
        assert drawdown_bps <= -200, f"2% real capital loss did not trigger HALT: {drawdown_bps}bps"

    def test_realized_plus_mtm_combines_correctly(self):
        """Realized 1% drawdown + unrealized 50K NTD loss → 1.5% total."""
        realized = 0.01  # 1% realized drawdown
        unrealized_scaled = -500_000_000  # 50K NTD loss (scaled)
        drawdown = _combine(realized=realized, unrealized_scaled=unrealized_scaled, base_capital=10_000_000)
        # 1% + 50K/10M = 1% + 0.5% = 1.5%
        assert abs(drawdown - 0.015) < 1e-6, f"combined drawdown wrong: {drawdown}"

    def test_unrealized_gain_does_not_change_drawdown(self):
        """Unrealized gain (positive) must not reduce drawdown."""
        drawdown = _combine(realized=0.02, unrealized_scaled=1_000_000_000, base_capital=10_000_000)
        assert drawdown == 0.02  # unchanged

    def test_zero_base_capital_no_divide_by_zero(self):
        drawdown = _combine(realized=0.01, unrealized_scaled=-1_000_000, base_capital=0)
        assert drawdown == 0.01  # unchanged, no crash
