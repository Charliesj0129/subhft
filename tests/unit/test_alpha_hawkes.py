"""Unit tests for alpha_hawkes strategy module (T2 coverage).

Tests are guarded with pytest.importorskip for numba so CI doesn't fail
when numba is unavailable.
"""

from __future__ import annotations

import os

import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_hawkes")


def test_module_imports():
    """Module must import without error when numba is available."""
    from hft_platform.strategies.alpha import alpha_hawkes

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
    ts = 5_000_000_000
    tracker.update(ts, False)
    assert tracker.last_ts == ts
    assert tracker.intensity == pytest.approx(1.0)


def test_hawkes_tracker_event_jump():
    """An event should increase intensity by alpha."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    tracker.update(1_000_000_000, False)
    tracker.update(1_001_000_000, True)
    assert tracker.intensity > 1.0


def test_hawkes_tracker_decay():
    """Intensity should decay back toward mu over time without events."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 2.0, 10.0)
    tracker.update(1_000_000_000, False)
    tracker.update(1_001_000_000, True)
    spiked = tracker.intensity
    tracker.update(2_001_000_000, False)
    assert tracker.intensity < spiked
    assert tracker.intensity >= tracker.mu - 1e-9


def test_strategy_function_exists():
    """The hawkes_strategy function should be callable (njit-compiled)."""
    from hft_platform.strategies.alpha.alpha_hawkes import hawkes_strategy

    assert callable(hawkes_strategy)


def test_hawkes_tracker_negative_dt_clamped():
    """Negative dt (clock jump) should be clamped to zero — intensity must not explode."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    tracker.update(2_000_000_000, False)
    tracker.update(1_000_000_000, False)
    assert tracker.intensity >= 0.0


def test_hawkes_tracker_no_event_no_intensity_increase():
    """No event should not raise intensity above current value (only decay applies)."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    tracker.update(1_000_000_000, False)
    tracker.update(1_001_000_000, True)
    spiked = tracker.intensity
    tracker.update(1_002_000_000, False)
    assert tracker.intensity <= spiked


def test_hawkes_tracker_multiple_events_accumulate():
    """Multiple successive events should accumulate intensity."""
    from hft_platform.strategies.alpha.alpha_hawkes import HawkesTracker

    tracker = HawkesTracker(1.0, 0.5, 10.0)
    tracker.update(1_000_000_000, False)
    tracker.update(1_001_000_000, True)
    after_first = tracker.intensity
    tracker.update(1_002_000_000, True)
    after_second = tracker.intensity
    assert after_second > after_first


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_hawkes_strategy_runs_with_mock_hbt():
    """hawkes_strategy should return True when hbt.elapse terminates the loop."""
    from hft_platform.strategies.alpha.alpha_hawkes import hawkes_strategy

    class MockHbt:
        """Minimal hbt mock: elapse returns non-zero after N calls."""

        def __init__(self, ticks: int):
            self._ticks = ticks
            self._count = 0
            self.current_timestamp = 1_000_000_000

        def elapse(self, _ns: int) -> int:
            if self._count >= self._ticks:
                return 1
            self._count += 1
            self.current_timestamp += 1_000_000
            return 0

        def last_trades(self, _asset_no: int):
            if self._count == 1:
                return [object()]
            return []

        def clear_last_trades(self, _asset_no: int) -> None:
            pass

    result = hawkes_strategy(MockHbt(ticks=3))
    assert result is True


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_hawkes_strategy_high_intensity_branch():
    """hawkes_strategy should exercise the intensity > 5.0 branch."""
    from hft_platform.strategies.alpha.alpha_hawkes import hawkes_strategy

    class HighIntensityHbt:
        """Fires many trade events to push intensity above 5.0."""

        def __init__(self):
            self._count = 0
            self.current_timestamp = 1_000_000_000

        def elapse(self, _ns: int) -> int:
            if self._count >= 20:
                return 1
            self._count += 1
            self.current_timestamp += 1_000_000
            return 0

        def last_trades(self, _asset_no: int):
            return [object(), object()]

        def clear_last_trades(self, _asset_no: int) -> None:
            pass

    result = hawkes_strategy(HighIntensityHbt())
    assert result is True
