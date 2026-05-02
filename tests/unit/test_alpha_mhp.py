"""Unit tests for alpha_mhp (Multivariate Hawkes Process) module (T2 coverage)."""

from __future__ import annotations

import os

import numpy as np
import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_mhp")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_mhp  # noqa: F401

    assert hasattr(alpha_mhp, "strategy")
    assert callable(alpha_mhp.strategy)


def test_mhp_tracker_initial_intensities():
    """MHPTracker should initialize intensities equal to mu."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

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
    tracker.update_decay(0, 1_000_000_000)  # first call: sets last_ts[0]
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


def test_mhp_tracker_excitation_source_1():
    """Triggering excitation from source index 1 should raise all intensities via column 1."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

    mu = np.array([1.0, 1.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)
    tracker = MHPTracker(mu, alpha, beta)

    before = tracker.intensities.copy()
    tracker.trigger_excitation(1)  # Event on asset 1
    # alpha[0,1]=0.1, alpha[1,1]=0.5 → both intensities increase
    assert tracker.intensities[0] > before[0]
    assert tracker.intensities[1] > before[1]


def test_mhp_tracker_update_decay_same_ts_no_change():
    """update_decay called twice with same timestamp should not change intensity."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

    mu = np.array([1.0, 1.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)
    tracker = MHPTracker(mu, alpha, beta)

    tracker.update_decay(0, 1_000_000_000)  # init
    tracker.intensities[0] = 3.0
    tracker.update_decay(0, 1_000_000_000)  # same ts → dt=0, no change
    assert tracker.intensities[0] == pytest.approx(3.0)


def test_mhp_tracker_decay_both_dimensions():
    """Both dimensions should decay independently over time."""
    from hft_platform.strategies.alpha.alpha_mhp import MHPTracker

    mu = np.array([1.0, 1.0], dtype=np.float64)
    alpha = np.array([[0.5, 0.1], [0.8, 0.5]], dtype=np.float64)
    beta = np.array([10.0, 10.0], dtype=np.float64)
    tracker = MHPTracker(mu, alpha, beta)

    # Initialize both dimensions
    tracker.update_decay(0, 1_000_000_000)
    tracker.update_decay(1, 1_000_000_000)
    # Elevate both
    tracker.intensities[0] = 4.0
    tracker.intensities[1] = 5.0
    # Decay both 1 second later
    tracker.update_decay(0, 2_000_000_000)
    tracker.update_decay(1, 2_000_000_000)
    assert tracker.intensities[0] < 4.0
    assert tracker.intensities[1] < 5.0


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_mhp_strategy_runs_with_mock_hbt_no_trades():
    """mhp_strategy should run loop without trades and return True."""
    from hft_platform.strategies.alpha.alpha_mhp import mhp_strategy

    class MockHbt:
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

        def last_trades(self, _asset_id: int):
            return []

        def clear_last_trades(self, _asset_id: int) -> None:
            pass

    result = mhp_strategy(MockHbt(ticks=3))
    assert result is True


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_mhp_strategy_runs_with_trades():
    """mhp_strategy should process trade events and trigger excitation."""
    from hft_platform.strategies.alpha.alpha_mhp import mhp_strategy

    class TradeHbt:
        def __init__(self):
            self._count = 0
            self.current_timestamp = 1_000_000_000

        def elapse(self, _ns: int) -> int:
            if self._count >= 4:
                return 1
            self._count += 1
            self.current_timestamp += 1_000_000
            return 0

        def last_trades(self, asset_id: int):
            # Return a trade on asset 0 at tick 1, asset 1 at tick 2
            if asset_id == 0 and self._count == 1:
                return [object()]
            if asset_id == 1 and self._count == 2:
                return [object()]
            return []

        def clear_last_trades(self, _asset_id: int) -> None:
            pass

    result = mhp_strategy(TradeHbt())
    assert result is True
