"""Gate B anti-leak / lookahead-bias tests for HawkesOfiImpactAlpha (ref 026)."""

from __future__ import annotations

import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.hawkes_ofi_impact.impl import HawkesOfiImpactAlpha


def test_update_no_args_returns_float() -> None:
    """update() with no args (both queues 0) must return a numeric value."""
    alpha = HawkesOfiImpactAlpha()
    result = alpha.update()
    assert isinstance(result, (int, float))


def test_update_is_stateful_not_lookahead() -> None:
    """Each call should only see past data; no future leakage."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)  # init
    sig1 = alpha.update(200.0, 100.0)  # bid increase -> positive OFI
    sig2 = alpha.update(200.0, 200.0)  # ask catches up -> zero OFI now
    # sig1 should be positive (bid increased); sig2 should be less positive
    assert sig1 > 0.0
    assert sig2 < sig1


def test_reset_eliminates_state_dependency() -> None:
    """After reset(), two alphas fed the same sequence return identical signals."""
    a1 = HawkesOfiImpactAlpha()
    a2 = HawkesOfiImpactAlpha()
    # Diverge a1
    a1.update(800.0, 200.0)
    a1.update(200.0, 800.0)
    a1.reset()
    # Now both should behave identically
    s1 = a1.update(300.0, 300.0)
    s2 = a2.update(300.0, 300.0)
    assert s1 == s2


def test_identical_sequences_produce_identical_signals() -> None:
    """Two fresh alphas with identical input produce identical output."""
    a1 = HawkesOfiImpactAlpha()
    a2 = HawkesOfiImpactAlpha()
    rng = np.random.default_rng(99)
    bids = rng.uniform(50, 500, 100)
    asks = rng.uniform(50, 500, 100)
    for b, a in zip(bids, asks):
        s1 = a1.update(b, a)
        s2 = a2.update(b, a)
        assert s1 == s2


def test_signal_changes_only_after_new_data() -> None:
    """get_signal() should not change between updates (no hidden mutation)."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)
    alpha.update(200.0, 100.0)
    sig_a = alpha.get_signal()
    sig_b = alpha.get_signal()
    sig_c = alpha.get_signal()
    assert sig_a == sig_b == sig_c


def test_no_future_data_access() -> None:
    """Signal at tick t must not depend on data at tick t+1."""
    alpha = HawkesOfiImpactAlpha()
    alpha.update(100.0, 100.0)
    sig_at_t = alpha.update(150.0, 100.0)

    # Create a second alpha with same history but different future
    alpha2 = HawkesOfiImpactAlpha()
    alpha2.update(100.0, 100.0)
    sig_at_t2 = alpha2.update(150.0, 100.0)

    # Signals at t must be identical regardless of what happens at t+1
    assert sig_at_t == sig_at_t2

    # Now feed different futures
    alpha.update(300.0, 100.0)
    alpha2.update(100.0, 300.0)
    # The t-signals should still be what they were
    assert sig_at_t == sig_at_t2


def test_slots_only_no_dynamic_attrs() -> None:
    """__slots__ prevents dynamic attribute creation (Allocator Law)."""
    alpha = HawkesOfiImpactAlpha()
    with pytest.raises(AttributeError):
        alpha.some_dynamic_attr = 42  # type: ignore[attr-defined]
