"""Gate B anti-leak / lookahead-bias tests for AmihudIlliquidityAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.amihud_illiquidity.impl import AmihudIlliquidityAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (defaults to 0) must return a numeric value."""
    alpha = AmihudIlliquidityAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same inputs produce identical signals — no hidden randomness."""
    a1 = AmihudIlliquidityAlpha()
    a2 = AmihudIlliquidityAlpha()
    inputs = [(100.0, 500.0), (101.0, 600.0), (99.5, 400.0)]
    for mid, vol in inputs:
        s1 = a1.update(mid_price=mid, volume=vol)
        s2 = a2.update(mid_price=mid, volume=vol)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = AmihudIlliquidityAlpha()
    a2 = AmihudIlliquidityAlpha()
    a1.update(mid_price=200.0, volume=100.0)
    a1.update(mid_price=210.0, volume=200.0)
    a1.reset()
    s1 = a1.update(mid_price=100.0, volume=500.0)
    s2 = a2.update(mid_price=100.0, volume=500.0)
    assert s1 == s2
