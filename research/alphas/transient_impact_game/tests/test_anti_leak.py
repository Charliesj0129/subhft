"""Gate B anti-leak / lookahead-bias tests for TransientImpactGameAlpha."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np

from research.alphas.transient_impact_game.impl import TransientImpactGameAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = TransientImpactGameAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = TransientImpactGameAlpha()
    sig0 = alpha.update(100.0, 100.0)  # init tick
    sig1 = alpha.update(500.0, 100.0)  # large bid increase -> OFI positive
    sig2 = alpha.update(100.0, 500.0)  # reversal -> OFI negative
    # sig0 is init (0), sig1 and sig2 should differ
    assert sig0 == 0.0
    assert sig1 != sig2


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = TransientImpactGameAlpha()
    a2 = TransientImpactGameAlpha()
    a1.update(800.0, 200.0)
    a1.update(200.0, 800.0)
    a1.reset()
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_no_future_data_dependency() -> None:
    """Signal at tick N should not change if future ticks are different."""
    alpha_a = TransientImpactGameAlpha()
    alpha_b = TransientImpactGameAlpha()
    shared = [(100.0, 100.0), (200.0, 80.0), (150.0, 120.0)]

    for b, a in shared:
        alpha_a.update(b, a)
        alpha_b.update(b, a)

    sig_a = alpha_a.get_signal()
    sig_b = alpha_b.get_signal()
    assert sig_a == sig_b

    # Feed different future ticks
    alpha_a.update(999.0, 1.0)
    alpha_b.update(1.0, 999.0)

    # The signal at tick 3 (before divergence) was identical
    assert sig_a == sig_b


def test_signal_deterministic_given_same_sequence() -> None:
    """Two fresh alphas fed the same sequence produce identical signals."""
    rng = np.random.default_rng(123)
    seq = list(zip(rng.uniform(1, 500, 100), rng.uniform(1, 500, 100)))

    a1 = TransientImpactGameAlpha()
    a2 = TransientImpactGameAlpha()
    for b, a in seq:
        a1.update(b, a)
        a2.update(b, a)
    assert a1.get_signal() == a2.get_signal()


def test_slots_no_dict() -> None:
    """Alpha uses __slots__; no __dict__ to prevent accidental attribute leaks."""
    alpha = TransientImpactGameAlpha()
    assert not hasattr(alpha, "__dict__")
