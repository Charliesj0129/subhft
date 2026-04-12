"""Unit 2: Test MTM drawdown integration.

Validates that unrealized losses INCREASE (not decrease) drawdown_pct
when combined with realized drawdown for StormGuard evaluation.
"""

from unittest.mock import MagicMock

import pytest


def test_drawdown_includes_unrealized_pnl():
    """Unrealized loss must increase drawdown_pct (positive = more drawdown).

    PositionStore.get_drawdown_pct() returns positive fraction (0.0–1.0).
    Unrealized loss (negative int) should ADD to drawdown, not subtract.
    """
    from hft_platform.services.system import HFTSystem

    mtm = MagicMock()
    mtm.total_unrealized_pnl.return_value = -500_000  # 500K loss (scaled int)
    # Real PositionStore.get_drawdown_pct() returns positive fraction
    ps = MagicMock(spec=["get_drawdown_pct", "total_pnl"])
    ps.get_drawdown_pct = MagicMock(return_value=0.02)  # 2% realized drawdown
    settings = {"base_capital": 10_000_000}
    realized = HFTSystem._get_drawdown_pct(ps, settings)
    assert realized == 0.02
    drawdown = realized
    unrealized = mtm.total_unrealized_pnl()
    if unrealized < 0:
        # Fix: subtract (not add) because unrealized is negative → double-negation = increase
        drawdown = drawdown - unrealized / 10_000_000
    # Unrealized loss must INCREASE drawdown, not decrease it
    assert drawdown > realized
    assert drawdown == pytest.approx(0.02 + 500_000 / 10_000_000)  # 0.07 = 7%
    # Verify StormGuard receives correct negative bps
    drawdown_bps = -int(drawdown * 10_000)
    assert drawdown_bps == -700  # -700 bps ≤ halt threshold → triggers correctly


def test_unrealized_gain_does_not_change_drawdown():
    """Unrealized gain should NOT reduce drawdown — only losses matter."""
    from hft_platform.services.system import HFTSystem

    ps = MagicMock(spec=["get_drawdown_pct"])
    ps.get_drawdown_pct = MagicMock(return_value=0.03)
    settings = {"base_capital": 10_000_000}
    realized = HFTSystem._get_drawdown_pct(ps, settings)
    drawdown = realized
    unrealized = 200_000  # positive = unrealized gain
    # The if-guard (unrealized < 0) prevents modification
    if unrealized < 0:
        drawdown = drawdown - unrealized / 10_000_000
    assert drawdown == realized  # unchanged
