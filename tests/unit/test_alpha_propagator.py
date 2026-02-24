"""Unit tests for alpha_propagator (Sum-of-Exponentials propagator) module (T2 coverage)."""
from __future__ import annotations

import pytest
import numpy as np

numba = pytest.importorskip("numba", reason="numba required for alpha_propagator")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_propagator  # noqa: F401

    assert hasattr(alpha_propagator, "strategy")
    assert callable(alpha_propagator.strategy)


def test_propagator_initial_state():
    """PropagatorTracker should start with zero components and zero impact."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker, K

    tracker = PropagatorTracker()
    np.testing.assert_allclose(tracker.components, np.zeros(K))
    assert tracker.total_impact == pytest.approx(0.0)
    assert tracker.last_ts == 0


def test_propagator_add_event_positive_sign():
    """Positive sign event should produce positive total_impact."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    # jitclass methods require positional arguments
    tracker.add_event(1.0, 10.0)
    assert tracker.total_impact > 0.0


def test_propagator_add_event_negative_sign():
    """Negative sign event should produce negative total_impact."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(-1.0, 10.0)
    assert tracker.total_impact < 0.0


def test_propagator_decay_reduces_impact():
    """update() with time passage should decay total_impact toward zero."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 100.0)
    impact_after_event = tracker.total_impact

    # Initialize last_ts with first update call (0 is sentinel for first call)
    tracker.update(1_000_000_000)    # initializes last_ts
    # Now decay with a later timestamp
    tracker.update(2_000_000_000)    # 1 second later — should decay
    assert abs(tracker.total_impact) < abs(impact_after_event)


def test_propagator_update_no_op_on_first_call():
    """update() on first call should just set last_ts without decaying."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.update(1_000_000_000)
    assert tracker.last_ts == 1_000_000_000
    assert tracker.total_impact == pytest.approx(0.0)


def test_strategy_function_exists():
    from hft_platform.strategies.alpha.alpha_propagator import strategy

    assert callable(strategy)


