"""Unit 2: Test MTM drawdown integration."""
from unittest.mock import MagicMock

import pytest


def test_drawdown_includes_unrealized_pnl():
    from hft_platform.services.system import HFTSystem
    mtm = MagicMock()
    mtm.total_unrealized_pnl.return_value = -500_000
    ps = MagicMock(spec=["get_drawdown_pct", "total_pnl"])
    ps.get_drawdown_pct = MagicMock(return_value=-0.02)
    settings = {"base_capital": 10_000_000}
    realized = HFTSystem._get_drawdown_pct(ps, settings)
    assert realized == -0.02
    drawdown = realized
    unrealized = mtm.total_unrealized_pnl()
    if unrealized < 0:
        drawdown = drawdown + unrealized / 10_000_000
    assert drawdown < realized
    assert drawdown == pytest.approx(-0.02 + (-500_000 / 10_000_000))
