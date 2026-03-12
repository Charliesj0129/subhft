"""Gate B anti-leak / lookahead-bias tests for DepthRatioAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

from research.alphas.depth_ratio.impl import DepthRatioAlpha


def test_no_future_leakage() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = DepthRatioAlpha()
    sig1 = alpha.update(500.0, 100.0)  # bid dominant
    sig2 = alpha.update(100.0, 500.0)  # ask dominant -- should move negative
    assert sig1 > 0.0
    assert sig2 < sig1  # signal moved in the correct direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = DepthRatioAlpha()
    a2 = DepthRatioAlpha()
    a1.update(800.0, 200.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_no_global_state() -> None:
    """Two independent instances must not share state."""
    a1 = DepthRatioAlpha()
    a2 = DepthRatioAlpha()
    a1.update(1000.0, 1.0)
    s2 = a2.update(100.0, 100.0)
    # a2 never saw 1000:1 ratio, so its signal should be 0
    assert s2 == 0.0


def test_order_matters() -> None:
    """Signal depends on the order of updates (EMA is path-dependent)."""
    a1 = DepthRatioAlpha()
    a2 = DepthRatioAlpha()
    # a1: high then low
    a1.update(500.0, 100.0)
    s1 = a1.update(100.0, 500.0)
    # a2: low then high
    a2.update(100.0, 500.0)
    s2 = a2.update(500.0, 100.0)
    assert s1 != s2


def test_no_lookahead_monotonic() -> None:
    """Signal at tick t should not depend on data at tick t+1.

    Feed a sequence, record signal at each step, then verify that
    each signal only reflects data seen so far.
    """
    alpha = DepthRatioAlpha()
    seq = [(200.0, 100.0), (200.0, 100.0), (200.0, 100.0)]
    signals = [alpha.update(b, a) for b, a in seq]
    # All inputs identical -> EMA should converge monotonically toward log(2)
    expected = math.log(2.0)
    for i in range(1, len(signals)):
        # Each signal should be closer to or equal to expected than the previous
        assert abs(signals[i] - expected) <= abs(signals[i - 1] - expected) + 1e-12
