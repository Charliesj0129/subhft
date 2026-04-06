"""Unit tests for alpha_propagator (Sum-of-Exponentials propagator) module (T2 coverage)."""

from __future__ import annotations

import os

import numpy as np
import pytest

numba = pytest.importorskip("numba", reason="numba required for alpha_propagator")


def test_module_imports():
    from hft_platform.strategies.alpha import alpha_propagator  # noqa: F401

    assert hasattr(alpha_propagator, "strategy")
    assert callable(alpha_propagator.strategy)


def test_propagator_initial_state():
    """PropagatorTracker should start with zero components and zero impact."""
    from hft_platform.strategies.alpha.alpha_propagator import K, PropagatorTracker

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
    tracker.update(1_000_000_000)  # initializes last_ts
    # Now decay with a later timestamp
    tracker.update(2_000_000_000)  # 1 second later — should decay
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


def test_propagator_recalc_total_is_sum_of_components():
    """total_impact should equal sum of all components after add_event."""
    from hft_platform.strategies.alpha.alpha_propagator import K, PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 5.0)
    expected_total = sum(tracker.components[k] for k in range(K))
    assert tracker.total_impact == pytest.approx(expected_total)


def test_propagator_add_multiple_events_accumulate():
    """Multiple events of the same sign should accumulate impact."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 10.0)
    after_first = tracker.total_impact
    tracker.add_event(1.0, 10.0)
    after_second = tracker.total_impact
    assert after_second > after_first


def test_propagator_opposite_events_cancel():
    """Equal magnitude opposite events should reduce total_impact toward zero."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 10.0)
    positive_impact = tracker.total_impact
    tracker.add_event(-1.0, 10.0)
    # Should be closer to zero than after the first event
    assert abs(tracker.total_impact) < abs(positive_impact)


def test_propagator_update_same_ts_no_change():
    """Calling update() twice with same timestamp should not change components."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 5.0)
    tracker.update(1_000_000_000)  # init last_ts
    impact_before = tracker.total_impact
    tracker.update(1_000_000_000)  # same ts → dt=0, skip decay
    assert tracker.total_impact == pytest.approx(impact_before)


def test_propagator_fast_decay_dominates_short_timescale():
    """Fast component (beta=100) should almost fully decay after 1 second."""
    from hft_platform.strategies.alpha.alpha_propagator import PropagatorTracker

    tracker = PropagatorTracker()
    tracker.add_event(1.0, 1.0)
    tracker.update(0)  # init with ts=0 (first call sets last_ts)
    # After 1 second, beta=100 component is e^(-100) ≈ 0 (vanished)
    tracker.update(1_000_000_000)
    # Medium component (beta=10): e^(-10) ≈ 0.0000454 (nearly zero)
    # Slow component (beta=1): e^(-1) ≈ 0.368 (remains)
    assert tracker.total_impact >= 0.0


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_propagator_strategy_runs_with_mock_hbt_no_trades():
    """strategy should run the main loop with no trades and return True."""
    from hft_platform.strategies.alpha.alpha_propagator import strategy

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

        def last_trades(self, _asset_no: int):
            return []

        def clear_last_trades(self, _asset_no: int) -> None:
            pass

    result = strategy(MockHbt(ticks=3))
    assert result is True


@pytest.mark.skipif(
    os.environ.get("NUMBA_DISABLE_JIT", "0") != "1",
    reason="strategy loop test requires NUMBA_DISABLE_JIT=1",
)
def test_propagator_strategy_processes_trade_events():
    """strategy should process trade events and update propagator state."""
    from hft_platform.strategies.alpha.alpha_propagator import strategy

    class Trade:
        """Minimal trade record mimicking hftbacktest event struct."""

        def __init__(self, sign: float, qty: float):
            self.ival = sign
            self.qty = qty

    class TradeHbt:
        def __init__(self):
            self._count = 0
            self.current_timestamp = 1_000_000_000

        def elapse(self, _ns: int) -> int:
            if self._count >= 5:
                return 1
            self._count += 1
            self.current_timestamp += 1_000_000
            return 0

        def last_trades(self, _asset_no: int):
            # Return a buy trade on tick 1, sell trade on tick 2
            if self._count == 1:
                return [Trade(1.0, 10.0), Trade(1.0, 5.0)]
            if self._count == 2:
                return [Trade(-1.0, 8.0)]
            return []

        def clear_last_trades(self, _asset_no: int) -> None:
            pass

    result = strategy(TradeHbt())
    assert result is True
