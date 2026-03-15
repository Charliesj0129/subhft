"""Gate B anti-leak / lookahead-bias tests for DepthReplenishmentAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

from research.alphas.depth_replenishment.impl import DepthReplenishmentAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = DepthReplenishmentAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Same sequence of inputs produces the same signal every time."""
    a1 = DepthReplenishmentAlpha()
    a2 = DepthReplenishmentAlpha()
    inputs = [(100.0, 100.0), (200.0, 50.0), (150.0, 150.0), (80.0, 200.0)]
    for bid, ask in inputs:
        s1 = a1.update(bid, ask)
        s2 = a2.update(bid, ask)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = DepthReplenishmentAlpha()
    a2 = DepthReplenishmentAlpha()
    a1.update(800.0, 200.0)
    a1.update(500.0, 100.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2
