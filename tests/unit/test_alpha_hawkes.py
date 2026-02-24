"""Unit tests for alpha_hawkes strategy module (T2 coverage).

Tests are guarded with pytest.importorskip for numba so CI doesn't fail
when numba is unavailable.
"""
from __future__ import annotations

import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_hawkes")


def test_module_imports():
    """Module must import without error when numba is available."""
    from hft_platform.strategies.alpha import alpha_hawkes  # noqa: F401

    assert hasattr(alpha_hawkes, "strategy")
    assert callable(alpha_hawkes.strategy)


def test_hawkes_tracker_initial_state():
    """HawkesTracker should initialize with mu as the base intensity."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    mu, alpha, beta = 1.0, 0.5, 10.0
    tracker = HawkesTracker(mu, alpha, beta)
    assert tracker.mu == mu
    assert tracker.alpha == alpha
    assert tracker.beta == beta
    assert tracker.intensity == mu
    assert tracker.last_ts == 0


def test_hawkes_tracker_first_update_sets_ts():
    """First update should set last_ts without changing intensity."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    # jitclass methods do not support keyword arguments — use positional
    # last_ts==0 is sentinel for first call; non-zero ts initializes tracker
    ts = 5_000_000_000
    tracker.update(ts, False)
    assert tracker.last_ts == ts
    assert tracker.intensity == pytest.approx(1.0)  # mu unchanged on first call


def test_hawkes_tracker_event_jump():
    """An event should increase intensity by alpha."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    # last_ts==0 is the sentinel for "first call" — use non-zero ts to init
    tracker.update(1_000_000_000, False)    # initializes last_ts
    # Second call with event: very small dt → minimal decay, intensity should rise
    tracker.update(1_001_000_000, True)    # 1ms later with event
    assert tracker.intensity > 1.0


def test_hawkes_tracker_decay():
    """Intensity should decay back toward mu over time without events."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 2.0, 10.0)
    tracker.update(1_000_000_000, False)   # initialize ts
    # Spike with an event
    tracker.update(1_001_000_000, True)    # 1ms later + event
    spiked = tracker.intensity
    # Long gap without event — should decay back toward mu
    tracker.update(2_001_000_000, False)   # 1s later, no event
    assert tracker.intensity < spiked
    assert tracker.intensity >= tracker.mu - 1e-9


def test_strategy_function_exists():
    """The hawkes_strategy function should be callable (njit-compiled)."""
    from hft_platform.strategies.alpha.alpha_hawkes import hawkes_strategy

    assert callable(hawkes_strategy)


