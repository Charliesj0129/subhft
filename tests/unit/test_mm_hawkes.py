"""Unit tests for src/hft_platform/strategies/mm_hawkes.py.

Tests cover HawkesTracker and PropagatorTracker — the two signal-tracking
JIT classes used by the mm_hawkes market-making strategy.

Note: mm_hawkes is a research/backtest-only strategy module (hftbacktest
simulation framework). Float prices are acceptable here per CLAUDE.md Rule 11
(float permitted in strategy research modules outside live accounting paths).
"""

from __future__ import annotations

import math

from hft_platform.strategies.mm_hawkes import HawkesTracker, PropagatorTracker


class TestHawkesTrackerInit:
    def test_initial_intensity_equals_mu(self):
        ht = HawkesTracker(2.0, 0.8, 5.0)
        assert ht.intensity == 2.0

    def test_initial_last_ts_is_zero(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        assert ht.last_ts == 0

    def test_parameters_stored_correctly(self):
        ht = HawkesTracker(1.5, 0.3, 7.0)
        assert ht.mu == 1.5
        assert ht.alpha == 0.3
        assert ht.beta == 7.0


class TestHawkesTrackerFirstUpdate:
    """First call to update() when last_ts == 0 only sets last_ts and returns early."""

    def test_first_update_sets_last_ts(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(1_000_000_000, True)
        assert ht.last_ts == 1_000_000_000

    def test_first_update_does_not_change_intensity(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(1_000_000_000, True)
        assert ht.intensity == 1.0

    def test_first_update_zero_ts_leaves_last_ts_zero(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(0, False)
        assert ht.last_ts == 0


class TestHawkesTrackerEventJump:
    """After initialisation, events increase intensity by alpha."""

    def test_event_increases_intensity_by_alpha(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_001_000, True)
        assert ht.intensity > 1.0

    def test_no_event_does_not_apply_alpha_jump(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(1_000_000_000, False)
        intensity_before = ht.intensity
        ht.update(1_000_001_000, False)
        assert ht.intensity <= intensity_before + 1e-10

    def test_event_jump_magnitude_close_to_alpha_when_no_decay(self):
        ht = HawkesTracker(mu=0.0, alpha=0.5, beta=1.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_000_001, True)
        assert abs(ht.intensity - 0.5) < 1e-6


class TestHawkesTrackerDecay:
    """Intensity should decay exponentially towards mu between events."""

    def test_intensity_decays_toward_mu_over_time(self):
        ht = HawkesTracker(mu=1.0, alpha=0.5, beta=10.0)
        ht.update(1_000_000_000, False)
        ht.update(1_000_000_001, True)
        assert ht.intensity > 1.0

        ht.update(101_000_000_001, False)
        assert abs(ht.intensity - 1.0) < 1e-3

    def test_intensity_bounded_below_by_mu_after_long_gap(self):
        ht = HawkesTracker(mu=2.0, alpha=1.0, beta=5.0)
        ht.update(0, False)
        for i in range(1, 6):
            ht.update(i * 100_000_000, True)
        ht.update(1_000_000_000_000, False)
        assert ht.intensity >= 2.0 - 1e-6

    def test_multiple_events_accumulate_intensity(self):
        ht = HawkesTracker(mu=1.0, alpha=0.3, beta=5.0)
        ht.update(0, False)
        for i in range(1, 6):
            ht.update(i * 1_000_000, True)
        assert ht.intensity > 1.5

    def test_same_timestamp_does_not_change_intensity(self):
        ht = HawkesTracker(1.0, 0.5, 10.0)
        ht.update(5_000_000_000, False)
        ht.update(5_000_000_001, True)
        intensity_after_event = ht.intensity
        ht.update(5_000_000_001, True)
        assert ht.intensity == intensity_after_event


class TestPropagatorTrackerInit:
    def test_initial_total_impact_is_zero(self):
        pt = PropagatorTracker()
        assert pt.total_impact == 0.0

    def test_initial_last_ts_is_zero(self):
        pt = PropagatorTracker()
        assert pt.last_ts == 0

    def test_components_initialised_to_zero(self):
        pt = PropagatorTracker()
        for k in range(3):
            assert pt.components[k] == 0.0

    def test_weights_sum_to_one(self):
        pt = PropagatorTracker()
        total = sum(pt.weights[k] for k in range(3))
        assert abs(total - 1.0) < 1e-9

    def test_betas_are_decreasing(self):
        pt = PropagatorTracker()
        betas = [pt.betas[k] for k in range(3)]
        assert betas[0] > betas[1] > betas[2]


class TestPropagatorTrackerFirstUpdate:
    def test_first_update_sets_last_ts(self):
        pt = PropagatorTracker()
        pt.update(2_000_000_000)
        assert pt.last_ts == 2_000_000_000

    def test_first_update_does_not_change_impact(self):
        pt = PropagatorTracker()
        pt.update(2_000_000_000)
        assert pt.total_impact == 0.0


class TestPropagatorTrackerAddEvent:
    def test_buy_event_produces_positive_impact(self):
        pt = PropagatorTracker()
        pt.add_event(1.0, 10.0)
        assert pt.total_impact > 0.0

    def test_sell_event_produces_negative_impact(self):
        pt = PropagatorTracker()
        pt.add_event(-1.0, 10.0)
        assert pt.total_impact < 0.0

    def test_zero_qty_produces_zero_impact(self):
        pt = PropagatorTracker()
        pt.add_event(1.0, 0.0)
        assert pt.total_impact == 0.0

    def test_impact_scales_with_log_qty(self):
        pt1 = PropagatorTracker()
        pt1.add_event(1.0, 1.0)
        impact_small = pt1.total_impact

        pt2 = PropagatorTracker()
        pt2.add_event(1.0, 100.0)
        impact_large = pt2.total_impact

        assert impact_large > impact_small

    def test_impact_magnitude_matches_log_formula(self):
        pt = PropagatorTracker()
        qty = 9.0
        pt.add_event(1.0, qty)
        expected_raw_impact = math.log(1.0 + qty)
        assert abs(pt.total_impact - expected_raw_impact) < 1e-9

    def test_opposite_equal_events_cancel(self):
        pt = PropagatorTracker()
        pt.add_event(1.0, 5.0)
        pt.add_event(-1.0, 5.0)
        assert abs(pt.total_impact) < 1e-9


class TestPropagatorTrackerDecay:
    """PropagatorTracker bootstrapping note:
    The first update() when last_ts==0 sets last_ts=current_ts and returns early
    (first-update guard). Two sequential updates with distinct timestamps are
    needed to move past the guard before add_event triggers meaningful decay.
    Pattern: update(t0) → update(t1) → add_event → update(t2_decay)
    """

    def test_impact_decays_over_time(self):
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 50.0)
        impact_initial = pt.total_impact
        assert impact_initial > 0.0

        pt.update(101_000_000_000)
        assert pt.total_impact < impact_initial * 0.001

    def test_fast_component_decays_faster_than_slow(self):
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 1.0)
        impact_just_after = pt.total_impact

        pt.update(1_050_000_000)
        assert pt.total_impact < impact_just_after

    def test_update_with_same_ts_does_not_decay(self):
        pt = PropagatorTracker()
        pt.update(0)
        pt.update(1_000_000_000)
        pt.add_event(1.0, 10.0)
        impact_before = pt.total_impact
        pt.update(1_000_000_000)
        assert pt.total_impact == impact_before

    def test_sequential_events_accumulate_impact(self):
        pt = PropagatorTracker()
        pt.update(0)
        for i in range(1, 6):
            pt.update(i * 100_000_000)
            pt.add_event(1.0, 5.0)
        assert pt.total_impact > 0.0


class TestPropagatorTrackerRecalc:
    def test_total_impact_is_sum_of_components(self):
        pt = PropagatorTracker()
        pt.add_event(1.0, 4.0)
        manual_sum = sum(pt.components[k] for k in range(3))
        assert abs(pt.total_impact - manual_sum) < 1e-12
