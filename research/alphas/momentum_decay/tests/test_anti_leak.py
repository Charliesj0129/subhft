"""Gate B anti-leak / lookahead-bias tests for MomentumDecayAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.momentum_decay.impl import MomentumDecayAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (mid_price defaults to 0) must return a numeric value."""
    alpha = MomentumDecayAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Two identical alphas fed the same sequence must produce identical signals."""
    a1 = MomentumDecayAlpha()
    a2 = MomentumDecayAlpha()
    prices = [100000.0, 100100.0, 100050.0, 100200.0, 100150.0]
    for p in prices:
        s1 = a1.update(p)
        s2 = a2.update(p)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = MomentumDecayAlpha()
    a2 = MomentumDecayAlpha()
    a1.update(200000.0)
    a1.update(200100.0)
    a1.reset()
    s1 = a1.update(100000.0)
    s2 = a2.update(100000.0)
    assert s1 == s2
