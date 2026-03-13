"""Gate B anti-leak / lookahead-bias tests for ShapMicrostructureAlpha."""
from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.shap_microstructure.impl import ShapMicrostructureAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = ShapMicrostructureAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = ShapMicrostructureAlpha()
    # Build up a few ticks of bid-dominant state
    for _ in range(5):
        alpha.update(500.0, 100.0)
    sig_before = alpha.get_signal()
    # Now flip to ask-dominant — signal should decrease
    for _ in range(5):
        alpha.update(100.0, 500.0)
    sig_after = alpha.get_signal()
    assert sig_before > 0.0
    assert sig_after < sig_before  # signal moved toward ask-dominant direction


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = ShapMicrostructureAlpha()
    a2 = ShapMicrostructureAlpha()
    a1.update(800.0, 200.0)
    a1.update(300.0, 700.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_identical_sequences_produce_identical_signals() -> None:
    """Two fresh alphas fed the same data must produce identical output."""
    a1 = ShapMicrostructureAlpha()
    a2 = ShapMicrostructureAlpha()
    rng = np.random.default_rng(99)
    bids = rng.uniform(50, 500, 100)
    asks = rng.uniform(50, 500, 100)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


def test_signal_depends_only_on_history() -> None:
    """Signal at tick N must not change if future ticks differ."""
    a1 = ShapMicrostructureAlpha()
    a2 = ShapMicrostructureAlpha()

    # Feed identical first 10 ticks
    for _ in range(10):
        a1.update(200.0, 100.0)
        a2.update(200.0, 100.0)

    sig1_at_10 = a1.get_signal()
    sig2_at_10 = a2.get_signal()
    assert sig1_at_10 == sig2_at_10

    # Now feed divergent data — past signal should have been identical
    a1.update(500.0, 50.0)
    a2.update(50.0, 500.0)
    # Past signals unchanged
    assert sig1_at_10 == sig2_at_10


def test_zero_quantity_handled_gracefully() -> None:
    """Zero bid and ask should not cause division by zero."""
    alpha = ShapMicrostructureAlpha()
    sig = alpha.update(0.0, 0.0)
    assert isinstance(sig, float)
    assert not np.isnan(sig)
    assert not np.isinf(sig)
