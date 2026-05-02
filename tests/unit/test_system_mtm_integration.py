"""Unit 2: Test MTM drawdown integration.

Validates that unrealized losses INCREASE (not decrease) drawdown_pct
when combined with realized drawdown for StormGuard evaluation.

Calls the production helper HFTSystem._combine_drawdown_with_mtm so the
test stays in lock-step with the runtime calculation (Bug 11 fix,
2026-04-17: unrealized is scaled-int x10000, base_capital is raw NTD;
descale before dividing).
"""

from unittest.mock import MagicMock

import pytest

from hft_platform.services.system import HFTSystem


def test_drawdown_includes_unrealized_pnl():
    """Unrealized loss must increase drawdown_pct (positive fraction grows)."""
    mtm = MagicMock()
    # 500K NTD unrealized loss, expressed as scaled int (x10000)
    mtm.total_unrealized_pnl.return_value = -500_000 * 10_000
    ps = MagicMock(spec=["get_drawdown_pct", "total_pnl"])
    ps.get_drawdown_pct = MagicMock(return_value=0.02)  # 2% realized drawdown
    settings = {"base_capital": 10_000_000}
    realized = HFTSystem._get_drawdown_pct(ps, settings)
    assert realized == 0.02
    drawdown = HFTSystem._combine_drawdown_with_mtm(
        realized_drawdown_pct=realized,
        unrealized_scaled=mtm.total_unrealized_pnl(),
        base_capital=settings["base_capital"],
    )
    # 2% realized + 500K/10M = 2% + 5% = 7% total drawdown
    assert drawdown == pytest.approx(0.07)
    drawdown_bps = -int(drawdown * 10_000)
    assert drawdown_bps == -700


def test_unrealized_gain_does_not_change_drawdown():
    """Unrealized gain must NOT reduce drawdown — only losses matter."""
    drawdown = HFTSystem._combine_drawdown_with_mtm(
        realized_drawdown_pct=0.03,
        unrealized_scaled=200_000 * 10_000,  # +200K NTD gain, scaled
        base_capital=10_000_000,
    )
    assert drawdown == 0.03
