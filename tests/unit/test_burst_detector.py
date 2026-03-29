"""Tests for BurstDetector — intensity burst detection."""

from __future__ import annotations

from hft_platform.feature.burst_detector import BurstDetector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEC_NS: int = 1_000_000_000
_MS_NS: int = 1_000_000


def _feed_ticks(det: BurstDetector, start_ns: int, count: int, interval_ns: int) -> list[bool]:
    """Feed `count` ticks at regular `interval_ns` spacing, return burst flags."""
    results: list[bool] = []
    for i in range(count):
        ts = start_ns + i * interval_ns
        results.append(det.on_tick(ts))
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBurstDetectorConstruction:
    """Test default construction and initial state."""

    def test_default_not_in_burst(self) -> None:
        det = BurstDetector()
        assert det.is_burst is False
        assert det.total_ticks == 0

    def test_default_rates_zero(self) -> None:
        det = BurstDetector()
        assert det.tick_rate == 0
        assert det.baseline_rate == 0

    def test_custom_params(self) -> None:
        det = BurstDetector(
            window_ns=10 * _SEC_NS,
            multiplier=5.0,
            cooldown_ns=2 * _SEC_NS,
            capacity=64,
            enabled=False,
        )
        assert det.is_burst is False
        assert det.total_ticks == 0


class TestDisabledDetector:
    """When disabled, on_tick never triggers burst."""

    def test_disabled_never_triggers(self) -> None:
        det = BurstDetector(enabled=False, window_ns=1 * _SEC_NS, multiplier=2.0)
        # Feed a massive burst: 100 ticks in 100ms
        results = _feed_ticks(det, start_ns=_SEC_NS, count=100, interval_ns=_MS_NS)
        assert all(r is False for r in results)
        assert det.is_burst is False
        # Ticks are still NOT counted when disabled
        assert det.total_ticks == 0


class TestNormalRate:
    """Normal tick rate should not trigger burst."""

    def test_steady_rate_no_burst(self) -> None:
        det = BurstDetector(
            window_ns=10 * _SEC_NS,
            multiplier=3.0,
            cooldown_ns=5 * _SEC_NS,
            capacity=256,
        )
        # Feed 8 ticks/s for 30s (TXFD6-like) — 240 ticks at 125ms interval
        results = _feed_ticks(det, start_ns=_SEC_NS, count=240, interval_ns=125 * _MS_NS)
        assert not any(results), "Steady rate should never trigger burst"
        assert det.is_burst is False


class TestBurstDetection:
    """3x normal rate should trigger burst."""

    def test_burst_triggers_on_3x_rate(self) -> None:
        det = BurstDetector(
            window_ns=2 * _SEC_NS,
            multiplier=3.0,
            cooldown_ns=1 * _SEC_NS,
            capacity=512,
        )
        # Phase 1: establish baseline — 8 ticks/s for 10s (80 ticks at 125ms)
        # Window=2s → baseline ~16 ticks/window. Threshold = 3*16 = 48.
        base_start = _SEC_NS
        _feed_ticks(det, start_ns=base_start, count=80, interval_ns=125 * _MS_NS)
        assert det.is_burst is False

        # Phase 2: burst — 30 ticks/s for 3s (90 ticks at ~33ms)
        # After 2s of burst: ~60 ticks in 2s window > 48 threshold → triggers
        burst_start = base_start + 80 * 125 * _MS_NS
        burst_results = _feed_ticks(det, start_ns=burst_start, count=90, interval_ns=33 * _MS_NS)
        assert any(burst_results), "3x+ rate should trigger burst"

    def test_burst_rising_edge_only(self) -> None:
        """Only the first tick of a burst returns True (rising edge)."""
        det = BurstDetector(
            window_ns=2 * _SEC_NS,
            multiplier=3.0,
            cooldown_ns=10 * _SEC_NS,  # long cooldown → only 1 rising edge
            capacity=512,
        )
        # Establish baseline: 8 ticks/s for 10s
        _feed_ticks(det, start_ns=_SEC_NS, count=80, interval_ns=125 * _MS_NS)

        # Burst: 30 ticks/s for 3s → exceeds 3x after ~2s in window
        burst_start = _SEC_NS + 80 * 125 * _MS_NS
        burst_results = _feed_ticks(det, start_ns=burst_start, count=90, interval_ns=33 * _MS_NS)
        true_count = sum(1 for r in burst_results if r)
        assert true_count == 1, f"Expected exactly 1 rising edge, got {true_count}"


class TestCooldown:
    """Burst signals respect cooldown period."""

    def test_burst_cooldown_prevents_immediate_retrigger(self) -> None:
        cooldown_ns = 5 * _SEC_NS
        det = BurstDetector(
            window_ns=1 * _SEC_NS,
            multiplier=2.0,
            cooldown_ns=cooldown_ns,
            capacity=512,
        )
        # Establish baseline: 5 ticks/s for 10s (window=1s → ~5 ticks/window)
        _feed_ticks(det, start_ns=_SEC_NS, count=50, interval_ns=200 * _MS_NS)

        # First burst: 50 ticks/s for 2s → 50 ticks in 1s window > 2*5=10 → triggers
        burst1_start = _SEC_NS + 50 * 200 * _MS_NS
        burst1 = _feed_ticks(det, start_ns=burst1_start, count=100, interval_ns=20 * _MS_NS)
        first_triggers = sum(1 for r in burst1 if r)
        assert first_triggers >= 1, "First burst should trigger"

        # Brief normal gap (1s — less than cooldown)
        gap_start = burst1_start + 100 * 20 * _MS_NS
        _feed_ticks(det, start_ns=gap_start, count=5, interval_ns=200 * _MS_NS)

        # Second burst within cooldown — should NOT get rising edge
        burst2_start = gap_start + 5 * 200 * _MS_NS
        burst2 = _feed_ticks(det, start_ns=burst2_start, count=100, interval_ns=20 * _MS_NS)
        second_triggers = sum(1 for r in burst2 if r)
        # With ~2s gap total < 5s cooldown, should be 0
        assert second_triggers == 0, "Second burst within cooldown should not re-trigger"


class TestRateCalculations:
    """Rate properties return correct values."""

    def test_tick_rate_after_feeding(self) -> None:
        det = BurstDetector(window_ns=10 * _SEC_NS, capacity=256)
        # Feed 80 ticks over 10s = 8 ticks/s = 8000 milliticks/s
        _feed_ticks(det, start_ns=_SEC_NS, count=80, interval_ns=125 * _MS_NS)
        rate = det.tick_rate
        # Should be close to 8000 milliticks/s
        assert 7000 <= rate <= 9000, f"Expected ~8000, got {rate}"

    def test_baseline_rate_adapts(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, capacity=256)
        # Feed steady ticks for a while to let EMA converge
        _feed_ticks(det, start_ns=_SEC_NS, count=200, interval_ns=125 * _MS_NS)
        baseline = det.baseline_rate
        assert baseline > 0, "Baseline should be positive after feeding ticks"

    def test_rates_are_int(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, capacity=256)
        _feed_ticks(det, start_ns=_SEC_NS, count=50, interval_ns=125 * _MS_NS)
        assert isinstance(det.tick_rate, int)
        assert isinstance(det.baseline_rate, int)


class TestRingBufferWrap:
    """Ring buffer correctly wraps at capacity."""

    def test_wrap_at_capacity(self) -> None:
        capacity = 32
        det = BurstDetector(
            window_ns=10 * _SEC_NS,
            multiplier=3.0,
            capacity=capacity,
        )
        # Feed more ticks than capacity
        total = capacity * 3
        _feed_ticks(det, start_ns=_SEC_NS, count=total, interval_ns=125 * _MS_NS)
        assert det.total_ticks == total
        # Internal count should be capped at capacity
        assert det._count == capacity  # noqa: SLF001

    def test_wrap_preserves_recent_timestamps(self) -> None:
        capacity = 16
        det = BurstDetector(window_ns=5 * _SEC_NS, capacity=capacity)
        # Feed 48 ticks (3x capacity)
        _feed_ticks(det, start_ns=_SEC_NS, count=48, interval_ns=100 * _MS_NS)
        # The oldest tick in buffer should be tick #33 (index 32 from 0-based)
        # at timestamp = 1e9 + 32 * 100e6 = 1e9 + 3.2e9 = 4.2e9
        oldest_idx = det._head % capacity  # noqa: SLF001
        oldest_ts = det._timestamps[oldest_idx]  # noqa: SLF001
        expected_oldest = _SEC_NS + 32 * 100 * _MS_NS
        assert oldest_ts == expected_oldest


class TestReset:
    """Reset clears all state."""

    def test_reset_clears_state(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, capacity=64)
        _feed_ticks(det, start_ns=_SEC_NS, count=50, interval_ns=125 * _MS_NS)
        assert det.total_ticks == 50

        det.reset()

        assert det.total_ticks == 0
        assert det.is_burst is False
        assert det.tick_rate == 0
        assert det.baseline_rate == 0
        assert det._count == 0  # noqa: SLF001
        assert det._head == 0  # noqa: SLF001

    def test_reset_allows_reuse(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, capacity=64)
        _feed_ticks(det, start_ns=_SEC_NS, count=50, interval_ns=125 * _MS_NS)
        det.reset()
        # Can feed again from scratch
        _feed_ticks(det, start_ns=10 * _SEC_NS, count=30, interval_ns=200 * _MS_NS)
        assert det.total_ticks == 30


class TestEdgeCases:
    """Edge cases and boundary conditions."""

    def test_single_tick_no_burst(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, multiplier=3.0)
        result = det.on_tick(_SEC_NS)
        assert result is False
        assert det.is_burst is False
        assert det.total_ticks == 1

    def test_two_ticks_no_burst(self) -> None:
        det = BurstDetector(window_ns=5 * _SEC_NS, multiplier=3.0)
        det.on_tick(_SEC_NS)
        result = det.on_tick(_SEC_NS + 100 * _MS_NS)
        assert result is False
        assert det.total_ticks == 2

    def test_variable_intervals_burst(self) -> None:
        """Variable tick intervals: slow then sudden cluster triggers burst."""
        det = BurstDetector(
            window_ns=1 * _SEC_NS,
            multiplier=3.0,
            cooldown_ns=1 * _SEC_NS,
            capacity=512,
        )
        # Phase 1: slow ticks — 2 ticks/s for 10s (window=1s → ~2 ticks/window)
        _feed_ticks(det, start_ns=_SEC_NS, count=20, interval_ns=500 * _MS_NS)

        # Phase 2: sudden cluster — 100 ticks/s for 2s (200 ticks at 10ms)
        # In 1s window: 100 ticks > 3*2=6 → triggers
        cluster_start = _SEC_NS + 20 * 500 * _MS_NS
        cluster_results = _feed_ticks(det, start_ns=cluster_start, count=200, interval_ns=10 * _MS_NS)
        assert any(cluster_results), "Sudden cluster after slow ticks should trigger burst"

    def test_zero_window_no_crash(self) -> None:
        """Zero window_ns should not crash, just never burst."""
        det = BurstDetector(window_ns=0, multiplier=3.0)
        result = det.on_tick(_SEC_NS)
        assert result is False
        assert det.tick_rate == 0
        assert det.baseline_rate == 0
