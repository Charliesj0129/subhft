"""Tests for Rust backtest kernels: signals_to_positions and apply_latency_to_positions."""

from __future__ import annotations

import pytest

_signals_to_positions = None
_apply_latency_to_positions = None


def _get_funcs():
    global _signals_to_positions, _apply_latency_to_positions
    if _signals_to_positions is None:
        try:
            from hft_platform.rust_core import (  # type: ignore[attr-defined]
                apply_latency_to_positions,
                signals_to_positions,
            )
        except ImportError:
            try:
                from rust_core import (  # type: ignore[assignment]
                    apply_latency_to_positions,
                    signals_to_positions,
                )
            except ImportError:
                pytest.skip("rust_core not available")
        _signals_to_positions = signals_to_positions
        _apply_latency_to_positions = apply_latency_to_positions
    return _signals_to_positions, _apply_latency_to_positions


class TestSignalsToPositions:
    def test_empty_signals(self):
        fn, _ = _get_funcs()
        result = fn([], 0.5, 3)
        assert result == []

    def test_single_element_always_flat(self):
        """First position is always 0.0 (flat start)."""
        fn, _ = _get_funcs()
        result = fn([1.0], 0.5, 5)
        assert result == [0.0]

    def test_basic_ladder_up(self):
        """Consecutive positive signals accumulate positions step by step."""
        fn, _ = _get_funcs()
        # signals: [0, 1, 1, -1, -1, 0], threshold=0.5, max_pos=2
        # directions: [_, +1, +1, -1, -1, 0]
        # positions: [0,  1,  2,   1,  0, 0]
        result = fn([0.0, 1.0, 1.0, -1.0, -1.0, 0.0], 0.5, 2)
        assert result == [0.0, 1.0, 2.0, 1.0, 0.0, 0.0]

    def test_clamp_positive_max(self):
        """Position is clamped at max_pos when driven repeatedly long."""
        fn, _ = _get_funcs()
        result = fn([0.0, 1.0, 1.0, 1.0], 0.5, 1)
        assert result == [0.0, 1.0, 1.0, 1.0]

    def test_clamp_negative_max(self):
        """Position is clamped at -max_pos when driven repeatedly short."""
        fn, _ = _get_funcs()
        result = fn([0.0, -1.0, -1.0, -1.0], 0.5, 1)
        assert result == [0.0, -1.0, -1.0, -1.0]

    def test_below_threshold_holds_position(self):
        """Signals within [-threshold, +threshold] do not change position."""
        fn, _ = _get_funcs()
        # threshold=0.5: signal 0.3 is within band -> no change
        result = fn([0.0, 1.0, 0.3, 0.3], 0.5, 5)
        assert result == [0.0, 1.0, 1.0, 1.0]

    def test_large_max_pos(self):
        """Position grows freely up to large max_pos."""
        fn, _ = _get_funcs()
        signals = [0.0] + [1.0] * 10
        result = fn(signals, 0.5, 100)
        assert result == [float(i) for i in range(11)]

    def test_symmetric_oscillation(self):
        """Alternating signals around zero produce an oscillating position."""
        fn, _ = _get_funcs()
        result = fn([0.0, 1.0, -1.0, 1.0, -1.0], 0.5, 2)
        assert result == [0.0, 1.0, 0.0, 1.0, 0.0]

    def test_zero_threshold(self):
        """With threshold=0, any non-zero signal activates a direction change."""
        fn, _ = _get_funcs()
        # signal=0.0 -> direction 0 (not > 0 and not < 0)
        result = fn([0.0, 0.001, 0.0, -0.001], 0.0, 5)
        assert result == [0.0, 1.0, 1.0, 0.0]


class TestApplyLatencyToPositions:
    def test_empty_desired(self):
        _, fn = _get_funcs()
        result = fn([], 3)
        assert result == []

    def test_single_element_always_flat(self):
        """First executed position is always 0.0."""
        _, fn = _get_funcs()
        result = fn([0.0], 3)
        assert result == [0.0]

    def test_no_delay(self):
        """submit_steps=0 means immediate execution -- executed mirrors desired."""
        _, fn = _get_funcs()
        desired = [0.0, 1.0, 1.0, 2.0]
        result = fn(desired, 0)
        assert result == [0.0, 1.0, 1.0, 2.0]

    def test_basic_delay(self):
        """submit_steps=2: order submitted at i=1 arrives at i=3."""
        _, fn = _get_funcs()
        # desired:  [0, 1, 1, 1, 1, 1]
        # executed: [0, 0, 0, 1, 1, 1]
        desired = [0.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        result = fn(desired, 2)
        assert result == [0.0, 0.0, 0.0, 1.0, 1.0, 1.0]

    def test_overwrite_pending_order(self):
        """A second order submission overwrites the first (last-write-wins)."""
        _, fn = _get_funcs()
        # desired:  [0, 1, 2, 2, 2, 2]
        # submit_steps=3
        # i=1: change 0->1, pending_due=4, pending_target=1
        # i=2: change 1->2, pending_due=5, pending_target=2 (overwrites)
        # i=5: arrival, executed=2
        desired = [0.0, 1.0, 2.0, 2.0, 2.0, 2.0]
        result = fn(desired, 3)
        assert result == [0.0, 0.0, 0.0, 0.0, 0.0, 2.0]

    def test_negative_submit_steps_treated_as_zero(self):
        """Negative submit_steps is clamped to 0 (immediate execution)."""
        _, fn = _get_funcs()
        desired = [0.0, 1.0, 2.0]
        result = fn(desired, -5)
        assert result == [0.0, 1.0, 2.0]

    def test_constant_desired_no_orders(self):
        """When desired never changes, no orders are submitted."""
        _, fn = _get_funcs()
        desired = [0.0, 0.0, 0.0, 0.0]
        result = fn(desired, 2)
        assert result == [0.0, 0.0, 0.0, 0.0]

    def test_late_arrival_clamped_to_last_index(self):
        """If delay would exceed the array, arrival is clamped to the last index."""
        _, fn = _get_funcs()
        # desired:  [0, 1, 1]
        # submit_steps=10: i=1, due=11 -> clamped to n-1=2
        # executed: [0, 0, 1]
        desired = [0.0, 1.0, 1.0]
        result = fn(desired, 10)
        assert result == [0.0, 0.0, 1.0]

    def test_delay_does_not_exceed_desired_length(self):
        """Executed array always has the same length as desired."""
        _, fn = _get_funcs()
        desired = [0.0, 1.0, -1.0, 0.0]
        result = fn(desired, 5)
        assert len(result) == len(desired)


class TestRoundtrip:
    def test_signals_then_latency_pipeline(self):
        """End-to-end: signals -> positions -> latency-adjusted executed positions."""
        s2p, alt = _get_funcs()
        signals = [0.0, 1.0, 1.0, 1.0, -1.0, -1.0, 0.0]
        positions = s2p(signals, 0.5, 3)
        executed = alt(positions, 1)

        # Executed lags positions by 1 step
        assert executed[0] == 0.0  # always flat start
        assert len(executed) == len(signals)
        # After 1-step delay, first non-zero executed position appears at i=2
        assert executed[1] == 0.0
        assert executed[2] == positions[1]
