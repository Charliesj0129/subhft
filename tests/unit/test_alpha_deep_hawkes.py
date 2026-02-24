"""Unit tests for alpha_deep_hawkes (LSTM-based Hawkes) module (T2 coverage)."""
from __future__ import annotations

import pytest
import numpy as np

numba = pytest.importorskip("numba", reason="numba required for alpha_deep_hawkes")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_deep_hawkes  # noqa: F401

    assert hasattr(alpha_deep_hawkes, "strategy")
    assert callable(alpha_deep_hawkes.strategy)


def test_lstm_tracker_initial_state():
    """LSTMTracker should initialize with zero hidden/cell state and zero intensity."""
    from hft_platform.strategies.alpha.alpha_deep_hawkes import LSTMTracker, HIDDEN_DIM

    tracker = LSTMTracker()
    np.testing.assert_allclose(tracker.h, np.zeros(HIDDEN_DIM))
    np.testing.assert_allclose(tracker.c, np.zeros(HIDDEN_DIM))
    assert tracker.last_ts == 0
    assert tracker.intensity == pytest.approx(0.0)


def test_lstm_tracker_update_sets_ts():
    """update() should set last_ts on first call."""
    from hft_platform.strategies.alpha.alpha_deep_hawkes import LSTMTracker

    tracker = LSTMTracker()
    tracker.update(1_000_000_000)
    assert tracker.last_ts == 1_000_000_000


def test_lstm_tracker_step_produces_nonneg_intensity():
    """step() should produce a non-negative intensity (softplus output)."""
    from hft_platform.strategies.alpha.alpha_deep_hawkes import LSTMTracker

    tracker = LSTMTracker()
    tracker.update(0)
    # jitclass methods do not support keyword arguments
    tracker.step(1.0, 10.0, 1_000_000_000)
    assert tracker.intensity >= 0.0


def test_lstm_tracker_step_changes_state():
    """step() should change hidden state from zero."""
    from hft_platform.strategies.alpha.alpha_deep_hawkes import LSTMTracker
    import numpy as np

    tracker = LSTMTracker()
    tracker.update(0)
    h_before = tracker.h.copy()
    # jitclass methods require positional arguments
    tracker.step(1.0, 5.0, 500_000_000)
    # Hidden state must have been updated
    assert not np.allclose(tracker.h, h_before)


def test_strategy_function_exists():
    from hft_platform.strategies.alpha.alpha_deep_hawkes import strategy

    assert callable(strategy)


