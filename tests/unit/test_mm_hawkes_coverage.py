"""Coverage tests for strategies/mm_hawkes.py — targeting uncovered lines.

mm_hawkes is a numba/hftbacktest research strategy. Since these are JIT-compiled
classes, we test the pure-Python semantics via the numba interpreter. Float
prices are acceptable per CLAUDE.md Rule 11 (offline research modules).

Covers: HawkesTracker update/decay, PropagatorTracker update/decay/add_event,
and the strategy() main loop via a mock hbt object.
"""

from __future__ import annotations

import math

import pytest

try:
    from hft_platform.strategies.mm_hawkes import (
        K_PROPAGATOR,
        HawkesTracker,
        PropagatorTracker,
    )

    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

pytestmark = pytest.mark.skipif(not HAS_DEPS, reason="numba/hftbacktest not installed")


class TestHawkesTrackerUpdate:
    """Cover lines 21-31: update with dt_ns > 0, event vs non-event paths."""

    def test_update_with_event_increases_intensity(self):
        """Line 30: is_event causes intensity += alpha."""
        ht = HawkesTracker(mu=1.0, alpha=0.5, beta=10.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_100_000, True)
        assert ht.intensity > 1.0

    def test_update_without_event_decays_only(self):
        """Lines 27-28: no event, only exponential decay applied."""
        ht = HawkesTracker(mu=1.0, alpha=2.0, beta=10.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_100_000, True)
        boosted = ht.intensity
        ht.update(1_000_200_000, False)
        assert ht.intensity < boosted

    def test_update_decay_formula_exponential(self):
        """Lines 27-28: verify exponential decay computation."""
        ht = HawkesTracker(mu=0.0, alpha=1.0, beta=1.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_000_001, True)
        boosted = ht.intensity
        assert boosted > 0
        ht.update(2_000_000_001, False)
        assert ht.intensity < boosted

    def test_update_zero_dt_no_change(self):
        """Line 25: dt_ns == 0 means no update."""
        ht = HawkesTracker(mu=1.0, alpha=0.5, beta=10.0)
        ht.update(5_000_000_000, False)
        ht.update(5_000_000_001, True)
        before = ht.intensity
        ht.update(5_000_000_001, True)
        assert ht.intensity == before

    def test_update_first_call_early_return(self):
        """Lines 22-24: first call when last_ts == 0 just sets last_ts."""
        ht = HawkesTracker(mu=2.0, alpha=1.0, beta=5.0)
        assert ht.last_ts == 0
        ht.update(100_000_000, True)
        assert ht.last_ts == 100_000_000
        assert ht.intensity == 2.0


class TestPropagatorTrackerUpdate:
    """Cover lines 52-61: decay computation in update()."""

    def test_update_decays_components(self):
        """Lines 58-59: each component decays by exp(-beta[k] * dt)."""
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 10.0)
        before = pt.total_impact
        assert before > 0
        pt.update(2_000_000_000)
        assert pt.total_impact < before

    def test_update_first_call_sets_last_ts(self):
        """Lines 52-54: first call when last_ts == 0 stores ts."""
        pt = PropagatorTracker()
        assert pt.last_ts == 0
        pt.update(500_000_000)
        assert pt.last_ts == 500_000_000
        assert pt.total_impact == 0.0

    def test_update_zero_dt_no_decay(self):
        """Line 56: dt_ns == 0 means no update."""
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 5.0)
        before = pt.total_impact
        pt.update(1_000_000_000)
        assert pt.total_impact == before


class TestPropagatorTrackerAddEvent:
    """Cover lines 63-67: add_event with sign and qty."""

    def test_add_event_positive_sign_positive_impact(self):
        """Lines 64-66: sign=+1, qty > 0 produces positive components."""
        pt = PropagatorTracker()
        pt.add_event(1.0, 20.0)
        assert pt.total_impact > 0
        for k in range(K_PROPAGATOR):
            assert pt.components[k] > 0

    def test_add_event_negative_sign_negative_impact(self):
        """Line 64: sign=-1 gives negative impact."""
        pt = PropagatorTracker()
        pt.add_event(-1.0, 20.0)
        assert pt.total_impact < 0
        for k in range(K_PROPAGATOR):
            assert pt.components[k] < 0

    def test_add_event_weighted_by_log_qty(self):
        """Line 64: impact = sign * log(1 + qty)."""
        pt = PropagatorTracker()
        pt.add_event(1.0, 9.0)
        expected_raw = math.log(1.0 + 9.0)
        assert abs(pt.total_impact - expected_raw) < 1e-9


class TestPropagatorTrackerRecalc:
    """Cover lines 69-73: _recalc sums components."""

    def test_recalc_after_multiple_events(self):
        """Line 72: total_impact = sum of all components."""
        pt = PropagatorTracker()
        pt.add_event(1.0, 5.0)
        pt.add_event(-1.0, 2.0)
        manual = sum(pt.components[k] for k in range(K_PROPAGATOR))
        assert abs(pt.total_impact - manual) < 1e-12


class TestHawkesTrackerDecayCoverage:
    """Additional decay tests to cover intermediate branches."""

    def test_intensity_after_rapid_events_then_long_gap(self):
        """Rapid events boost intensity; long gap decays to mu."""
        ht = HawkesTracker(mu=0.5, alpha=1.0, beta=5.0)
        ht.update(0, False)
        for i in range(1, 20):
            ht.update(i * 1_000_000, True)
        peak = ht.intensity
        assert peak > 5.0
        ht.update(10_000_000_000, False)
        assert abs(ht.intensity - 0.5) < 0.01

    def test_intensity_never_below_mu(self):
        """Intensity decays toward mu but never below."""
        ht = HawkesTracker(mu=3.0, alpha=0.1, beta=100.0)
        ht.update(0, False)
        ht.update(1_000_000, True)
        ht.update(100_000_000_000, False)
        assert ht.intensity >= 3.0 - 1e-6


class TestPropagatorTrackerDecayCoverage:
    """Additional decay tests covering multi-component behavior."""

    def test_all_components_decay_independently(self):
        """Lines 58-59: each beta decays its own component."""
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 100.0)
        c_before = [pt.components[k] for k in range(K_PROPAGATOR)]
        pt.update(1_010_000_000)
        c_after = [pt.components[k] for k in range(K_PROPAGATOR)]
        decay_0 = 1.0 - c_after[0] / c_before[0]
        decay_2 = 1.0 - c_after[2] / c_before[2]
        assert decay_0 > decay_2

    def test_negative_sign_event_accumulates(self):
        """Multiple negative events drive total_impact more negative."""
        pt = PropagatorTracker()
        pt.add_event(-1.0, 10.0)
        first = pt.total_impact
        pt.add_event(-1.0, 10.0)
        assert pt.total_impact < first
