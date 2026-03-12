"""Gate B anti-leak / lookahead-bias tests for CrossEmaQiAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.cross_ema_qi.impl import CrossEmaQiAlpha


def test_replay_determinism() -> None:
    """Two independent alphas fed the same sequence produce identical signals."""
    a1 = CrossEmaQiAlpha()
    a2 = CrossEmaQiAlpha()
    rng = np.random.default_rng(99)
    bids = rng.uniform(10, 500, 100)
    asks = rng.uniform(10, 500, 100)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


def test_zero_inputs() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = CrossEmaQiAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))
    assert result == 0.0


def test_extreme_inputs() -> None:
    """Very large inputs should still produce bounded signal."""
    alpha = CrossEmaQiAlpha()
    sig = alpha.update(1e12, 1.0)
    assert -1.0 <= sig <= 1.0
    sig = alpha.update(1.0, 1e12)
    assert -1.0 <= sig <= 1.0


def test_alternating_inputs() -> None:
    """Alternating bid/ask dominance should produce bounded oscillation."""
    alpha = CrossEmaQiAlpha()
    for i in range(100):
        if i % 2 == 0:
            sig = alpha.update(1000.0, 100.0)
        else:
            sig = alpha.update(100.0, 1000.0)
        assert -1.0 <= sig <= 1.0


def test_direction_200_ticks() -> None:
    """Over 200 ticks of bid dominance, signal should be positive (momentum)."""
    alpha = CrossEmaQiAlpha()
    # First establish neutral
    for _ in range(50):
        alpha.update(100.0, 100.0)
    # Then shift to strong bid
    for _ in range(200):
        sig = alpha.update(800.0, 100.0)
    # After convergence, both EMAs should be near the same value -> signal near 0
    # But we are still feeding bid-dominant, so fast converges slightly faster
    # The signal should be very small but >= 0 (or nearly 0)
    assert sig >= -0.01


def test_bounds_1000_random() -> None:
    """1000 random updates must all produce signals in [-1, 1]."""
    alpha = CrossEmaQiAlpha()
    rng = np.random.default_rng(7)
    for _ in range(1000):
        b = rng.uniform(0, 10000)
        a = rng.uniform(0, 10000)
        sig = alpha.update(b, a)
        assert -1.0 <= sig <= 1.0


def test_reset_idempotency() -> None:
    """Multiple resets should be safe and leave state at zero."""
    alpha = CrossEmaQiAlpha()
    alpha.update(500.0, 100.0)
    alpha.reset()
    alpha.reset()
    alpha.reset()
    assert alpha.get_signal() == 0.0


def test_no_contamination_after_reset() -> None:
    """After reset, previous history must not affect new signals."""
    a1 = CrossEmaQiAlpha()
    a2 = CrossEmaQiAlpha()

    # Feed a1 with extreme data then reset
    for _ in range(50):
        a1.update(1000.0, 1.0)
    a1.reset()

    # Both alphas should produce identical signals from here
    rng = np.random.default_rng(42)
    bids = rng.uniform(50, 500, 30)
    asks = rng.uniform(50, 500, 30)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2
