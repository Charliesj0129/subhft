"""Gate B anti-leak / lookahead-bias tests for VolOfImbalanceAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.vol_of_imbalance.impl import VolOfImbalanceAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = VolOfImbalanceAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_deterministic() -> None:
    """Two fresh alphas fed the same sequence produce identical signals."""
    a1 = VolOfImbalanceAlpha()
    a2 = VolOfImbalanceAlpha()
    rng = np.random.default_rng(99)
    bids = rng.uniform(50, 500, 100)
    asks = rng.uniform(50, 500, 100)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = VolOfImbalanceAlpha()
    a2 = VolOfImbalanceAlpha()
    a1.update(800.0, 200.0)
    a1.update(200.0, 800.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_extreme_bid_signal_bounded() -> None:
    """Extreme bid-only input: signal must stay within [-2, 2]."""
    alpha = VolOfImbalanceAlpha()
    for _ in range(200):
        sig = alpha.update(1e9, 0.0)
        assert -2.0 <= sig <= 2.0


def test_alternating_input_no_divergence() -> None:
    """Rapidly alternating bid/ask must not cause signal divergence."""
    alpha = VolOfImbalanceAlpha()
    for i in range(200):
        if i % 2 == 0:
            sig = alpha.update(1000.0, 1.0)
        else:
            sig = alpha.update(1.0, 1000.0)
        assert -2.0 <= sig <= 2.0


def test_bounds_1000_random_ticks() -> None:
    """1000 random ticks: signal always in [-2, 2]."""
    alpha = VolOfImbalanceAlpha()
    rng = np.random.default_rng(777)
    for _ in range(1000):
        b = rng.uniform(0, 10000)
        a = rng.uniform(0, 10000)
        sig = alpha.update(b, a)
        assert -2.0 <= sig <= 2.0


def test_reset_idempotency() -> None:
    """Multiple resets produce identical state."""
    alpha = VolOfImbalanceAlpha()
    alpha.update(500.0, 100.0)
    alpha.reset()
    alpha.reset()
    assert alpha._qi_ema == 0.0
    assert alpha._dev_ema == 0.0
    assert alpha._vol_baseline == 0.0
    assert alpha._signal == 0.0
    assert alpha._initialized is False


def test_no_cross_instance_contamination() -> None:
    """Two instances do not share state."""
    a1 = VolOfImbalanceAlpha()
    a2 = VolOfImbalanceAlpha()
    # Feed varying input to build volatility state
    a1.update(800.0, 100.0)
    a1.update(100.0, 800.0)
    a1.update(800.0, 100.0)
    assert a2.get_signal() == 0.0
    s2 = a2.update(100.0, 100.0)
    assert s2 == 0.0
    # a1 has accumulated deviation from oscillating input
    assert a1._dev_ema > 0.0
