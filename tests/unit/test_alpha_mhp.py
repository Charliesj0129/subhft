"""Unit tests for alpha_mhp (Multivariate Hawkes Process) module (T2 coverage)."""
from __future__ import annotations

import pytest
import numpy as np

numba = pytest.importorskip("numba", reason="numba required for alpha_mhp")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_mhp  # noqa: F401

    assert hasattr(alpha_mhp, "strategy")
    assert callable(alpha_mhp.strategy)


def test_mhp_tracker_initial_intensities():
    """MHPTracker should initialize intensities equal to mu."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker, NUM_ASSETS

    mu = np.array([1.0, 2.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)

    tracker = MHPTracker(mu, alpha, beta)
    np.testing.assert_allclose(tracker.intensities, mu)
    assert tracker.last_ts[0] == 0
    assert tracker.last_ts[1] == 0


def test_mhp_tracker_decay_reduces_intensity():
    """update_decay should reduce elevated intensity back toward mu."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

    mu = np.array([1.0, 1.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)
    tracker = MHPTracker(mu, alpha, beta)

    # Initialize last_ts[0] to a non-zero value (0 is first-call sentinel)
    tracker.update_decay(0, 1_000_000_000)   # first call: sets last_ts[0]
    # Manually elevate intensity for the actual decay test
    tracker.intensities[0] = 5.0
    # Now decay 1 second later (from 1_000_000_000)
    tracker.update_decay(0, 2_000_000_000)
    assert tracker.intensities[0] < 5.0
    assert tracker.intensities[0] >= 1.0 - 1e-9


def test_mhp_tracker_excitation_raises_all():
    """Triggering excitation on one dimension should raise all intensities."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

    mu = np.array([1.0, 1.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)
    tracker = MHPTracker(mu, alpha, beta)

    before = tracker.intensities.copy()
    tracker.trigger_excitation(0)  # Event on asset 0
    assert tracker.intensities[0] > before[0]
    assert tracker.intensities[1] > before[1]


def test_mhp_strategy_callable():
    from hft_platform.strategies.alpha.alpha_mhp import mhp_strategy

    assert callable(mhp_strategy)


