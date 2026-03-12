"""Gate B anti-leak / lookahead-bias tests for DepthVelocityDiffAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.depth_velocity_diff.impl import DepthVelocityDiffAlpha


def test_replay_determinism() -> None:
    """Two alphas fed the same sequence produce identical signals."""
    a1 = DepthVelocityDiffAlpha()
    a2 = DepthVelocityDiffAlpha()
    rng = np.random.default_rng(123)
    bids = rng.uniform(50, 500, 50)
    asks = rng.uniform(50, 500, 50)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


def test_zero_depth_no_crash() -> None:
    """Zero bid and ask depth must not raise or produce NaN."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(0.0, 0.0)
    sig = alpha.update(0.0, 0.0)
    assert not np.isnan(sig)
    assert isinstance(sig, float)


def test_extreme_values_no_nan() -> None:
    """Very large inputs must not produce NaN or Inf."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(1e12, 1e12)
    sig = alpha.update(1e12, 0.0)
    assert not np.isnan(sig)
    assert not np.isinf(sig)
    assert -2.0 <= sig <= 2.0


def test_alternating_direction() -> None:
    """Alternating bid/ask dominance should keep signal bounded."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)
    for i in range(100):
        if i % 2 == 0:
            alpha.update(200.0, 100.0)
        else:
            alpha.update(100.0, 200.0)
    assert -2.0 <= alpha.get_signal() <= 2.0


def test_direction_tracks_bid_growth() -> None:
    """Persistent bid growth must yield positive signal."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(100.0, 100.0)
    for i in range(50):
        alpha.update(100.0 + (i + 1) * 10.0, 100.0)
    assert alpha.get_signal() > 0.0


def test_bounds_never_exceeded_random_walk() -> None:
    """Random-walk input must never exceed [-2, 2]."""
    alpha = DepthVelocityDiffAlpha()
    rng = np.random.default_rng(99)
    bid = 500.0
    ask = 500.0
    for _ in range(500):
        bid = max(0.0, bid + rng.normal(0, 50))
        ask = max(0.0, ask + rng.normal(0, 50))
        sig = alpha.update(bid, ask)
        assert -2.0 <= sig <= 2.0


def test_reset_idempotency() -> None:
    """Multiple resets should be safe and produce same clean state."""
    alpha = DepthVelocityDiffAlpha()
    alpha.update(500.0, 100.0)
    alpha.update(600.0, 100.0)
    alpha.reset()
    alpha.reset()  # double reset
    assert alpha.get_signal() == 0.0
    sig = alpha.update(300.0, 300.0)
    assert sig == pytest.approx(0.0, abs=1e-9)


def test_no_contamination_between_instances() -> None:
    """Two independent instances must not share state."""
    a1 = DepthVelocityDiffAlpha()
    a2 = DepthVelocityDiffAlpha()
    a1.update(100.0, 100.0)
    a1.update(500.0, 100.0)
    # a2 is untouched
    assert a2.get_signal() == 0.0
    a2.update(100.0, 100.0)
    a2.update(100.0, 500.0)
    # a1 should still be positive, a2 negative
    assert a1.get_signal() > 0.0
    assert a2.get_signal() < 0.0
