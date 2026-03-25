"""Unit tests for DriftBurstDetector — drift-burst toxicity scoring for StormGuard.

Tests:
  1. Burst detection on synthetic data with known drift
  2. No false positives on random walk (martingale null)
  3. Cooldown behavior
  4. Pre-allocated buffer doesn't grow
  5. Edge cases (zero prices, single tick, reset)
  6. Toxicity classification
  7. ToxicityResult interface
"""

from __future__ import annotations

import math
import sys

import numpy as np
import pytest

from hft_platform.risk.drift_burst_detector import (
    BurstEvent,
    DriftBurstDetector,
    ToxicityResult,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_trending_prices(
    start_mid_x2: int,
    n_ticks: int,
    drift_per_tick: float,
    seed: int = 42,
) -> list[int]:
    """Generate prices with known drift + small noise."""
    rng = np.random.default_rng(seed)
    prices = [start_mid_x2]
    for _ in range(n_ticks - 1):
        noise = rng.normal(0, 0.00005)
        log_ret = drift_per_tick + noise
        new_price = int(prices[-1] * math.exp(log_ret))
        if new_price <= 0:
            new_price = 1
        prices.append(new_price)
    return prices


def _make_random_walk_prices(
    start_mid_x2: int,
    n_ticks: int,
    volatility: float = 0.0002,
    seed: int = 123,
) -> list[int]:
    """Generate pure random walk (zero drift) prices."""
    rng = np.random.default_rng(seed)
    prices = [start_mid_x2]
    for _ in range(n_ticks - 1):
        log_ret = rng.normal(0, volatility)
        new_price = int(prices[-1] * math.exp(log_ret))
        if new_price <= 0:
            new_price = 1
        prices.append(new_price)
    return prices


# ---------------------------------------------------------------------------
# Test: Burst detection on synthetic data with known drift
# ---------------------------------------------------------------------------


class TestBurstDetectionKnownDrift:
    """Verify detector fires on strong upward drift."""

    def test_detects_strong_upward_drift(self) -> None:
        detector = DriftBurstDetector(
            window_size=50, burst_threshold=2.5, cooldown_ticks=10, cooldown_ns=0, skip_zero_returns=False
        )
        # Start with random walk for warmup
        warmup = _make_random_walk_prices(2000_0000, 60, volatility=0.0001, seed=1)
        # Then inject strong drift
        drift_prices = _make_trending_prices(warmup[-1], 100, drift_per_tick=0.002, seed=2)
        all_prices = warmup + drift_prices[1:]

        burst_detected = False
        burst_event: BurstEvent | None = None
        for p in all_prices:
            result = detector.evaluate(mid_price_x2=p, spread_scaled=100, imbalance=0.0)
            if result.burst_detected:
                burst_detected = True
                burst_event = result.burst_event
                break

        assert burst_detected, "Expected burst detection on strong upward drift"
        assert burst_event is not None
        assert burst_event.direction == 1  # upward
        assert burst_event.magnitude > 2.5
        assert burst_event.t_statistic > 0

    def test_detects_strong_downward_drift(self) -> None:
        detector = DriftBurstDetector(
            window_size=50, burst_threshold=2.5, cooldown_ticks=10, cooldown_ns=0, skip_zero_returns=False
        )
        warmup = _make_random_walk_prices(2000_0000, 60, volatility=0.0001, seed=3)
        drift_prices = _make_trending_prices(warmup[-1], 100, drift_per_tick=-0.002, seed=4)
        all_prices = warmup + drift_prices[1:]

        burst_detected = False
        burst_event = None
        for p in all_prices:
            result = detector.evaluate(mid_price_x2=p, spread_scaled=100, imbalance=0.0)
            if result.burst_detected:
                burst_detected = True
                burst_event = result.burst_event
                break

        assert burst_detected, "Expected burst detection on strong downward drift"
        assert burst_event is not None
        assert burst_event.direction == -1


# ---------------------------------------------------------------------------
# Test: No false positives on random walk
# ---------------------------------------------------------------------------


class TestNoFalsePositivesRandomWalk:
    """With threshold=3.0, false positive rate on random walk should be very low."""

    def test_random_walk_low_false_positive_rate(self) -> None:
        detector = DriftBurstDetector(
            window_size=100,
            burst_threshold=3.5,  # high threshold for strictness
            cooldown_ticks=50,
            cooldown_ns=0,
            skip_zero_returns=False,
        )
        prices = _make_random_walk_prices(2000_0000, 5000, volatility=0.0001, seed=99)

        burst_count = 0
        for p in prices:
            result = detector.evaluate(mid_price_x2=p)
            if result.burst_detected:
                burst_count += 1

        # With 5000 ticks and threshold 3.5, expect very few false positives
        # (< 2% of effective windows after warmup)
        max_expected = 5  # conservative bound
        assert burst_count <= max_expected, f"Too many false positives on random walk: {burst_count} > {max_expected}"

    def test_toxicity_score_stays_low_on_random_walk(self) -> None:
        detector = DriftBurstDetector(
            window_size=100, burst_threshold=3.0, cooldown_ticks=20, cooldown_ns=0, skip_zero_returns=False
        )
        prices = _make_random_walk_prices(2000_0000, 1000, volatility=0.0001, seed=77)

        max_score = 0.0
        for p in prices:
            result = detector.evaluate(mid_price_x2=p)
            if result.toxicity_score > max_score:
                max_score = result.toxicity_score

        # On a random walk, toxicity score should rarely exceed 0.5
        # (it can spike occasionally but the mean should be low)
        assert max_score < 0.95, f"Toxicity score too high on random walk: {max_score:.3f}"


# ---------------------------------------------------------------------------
# Test: Cooldown behavior
# ---------------------------------------------------------------------------


class TestCooldownBehavior:
    """Verify cooldown suppresses repeated detections."""

    def test_cooldown_suppresses_repeated_bursts(self) -> None:
        cooldown = 30
        detector = DriftBurstDetector(
            window_size=50,
            burst_threshold=2.0,
            cooldown_ticks=cooldown,
            cooldown_ns=0,
            skip_zero_returns=False,
        )
        # Strong continuous drift should only fire once per cooldown period
        prices = _make_trending_prices(2000_0000, 300, drift_per_tick=0.003, seed=10)

        burst_ticks: list[int] = []
        for i, p in enumerate(prices):
            result = detector.evaluate(mid_price_x2=p)
            if result.burst_detected:
                burst_ticks.append(i)

        assert len(burst_ticks) >= 1, "Expected at least one burst"

        # Check spacing between bursts respects cooldown
        for i in range(1, len(burst_ticks)):
            gap = burst_ticks[i] - burst_ticks[i - 1]
            assert gap > cooldown, (
                f"Burst at tick {burst_ticks[i]} too close to previous at "
                f"{burst_ticks[i - 1]}: gap={gap} < cooldown={cooldown}"
            )

    def test_cooldown_zero_allows_immediate_refire(self) -> None:
        detector = DriftBurstDetector(
            window_size=50,
            burst_threshold=2.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            skip_zero_returns=False,
        )
        prices = _make_trending_prices(2000_0000, 200, drift_per_tick=0.003, seed=11)

        burst_count = 0
        for p in prices:
            result = detector.evaluate(mid_price_x2=p)
            if result.burst_detected:
                burst_count += 1

        # With zero cooldown and strong drift, should see many bursts
        assert burst_count >= 2, f"Expected multiple bursts with zero cooldown, got {burst_count}"

    def test_cooldown_ns_timestamp_based(self) -> None:
        """Timestamp-based cooldown uses nanosecond elapsed time, not tick count."""
        cooldown_ns = 1_000_000_000  # 1 second
        detector = DriftBurstDetector(
            window_size=50,
            burst_threshold=2.0,
            cooldown_ticks=0,  # tick-based would allow immediate refire
            cooldown_ns=cooldown_ns,
            skip_zero_returns=False,
        )
        prices = _make_trending_prices(2000_0000, 300, drift_per_tick=0.003, seed=12)

        burst_timestamps: list[int] = []
        base_ts = 1_000_000_000_000  # 1000s in ns
        tick_interval_ns = 100_000_000  # 100ms per tick

        for i, p in enumerate(prices):
            ts = base_ts + i * tick_interval_ns
            result = detector.evaluate(mid_price_x2=p, ts=ts)
            if result.burst_detected:
                burst_timestamps.append(ts)

        assert len(burst_timestamps) >= 1, "Expected at least one burst"

        # Check that bursts are spaced by at least cooldown_ns
        for i in range(1, len(burst_timestamps)):
            gap_ns = burst_timestamps[i] - burst_timestamps[i - 1]
            assert gap_ns >= cooldown_ns, (
                f"Burst at ts={burst_timestamps[i]} too close to previous: gap={gap_ns}ns < cooldown={cooldown_ns}ns"
            )

    def test_cooldown_ns_zero_falls_back_to_tick_based(self) -> None:
        """When cooldown_ns=0, tick-based cooldown is used."""
        cooldown_ticks = 30
        detector = DriftBurstDetector(
            window_size=50,
            burst_threshold=2.0,
            cooldown_ticks=cooldown_ticks,
            cooldown_ns=0,
            skip_zero_returns=False,
        )
        prices = _make_trending_prices(2000_0000, 300, drift_per_tick=0.003, seed=13)

        burst_ticks: list[int] = []
        for i, p in enumerate(prices):
            result = detector.evaluate(mid_price_x2=p, ts=i * 1_000_000)
            if result.burst_detected:
                burst_ticks.append(i)

        assert len(burst_ticks) >= 1
        for i in range(1, len(burst_ticks)):
            gap = burst_ticks[i] - burst_ticks[i - 1]
            assert gap > cooldown_ticks


# ---------------------------------------------------------------------------
# Test: Pre-allocated buffer doesn't grow
# ---------------------------------------------------------------------------


class TestPreAllocatedBuffer:
    """Verify that internal arrays don't grow during operation."""

    def test_buffer_size_constant(self) -> None:
        window = 100
        detector = DriftBurstDetector(window_size=window, cooldown_ns=0, skip_zero_returns=False)

        initial_returns_nbytes = detector._returns.nbytes
        initial_abs_returns_nbytes = detector._abs_returns.nbytes
        initial_returns_id = id(detector._returns)
        initial_abs_id = id(detector._abs_returns)

        # Feed many ticks
        prices = _make_random_walk_prices(2000_0000, 500, seed=55)
        for p in prices:
            detector.evaluate(mid_price_x2=p)

        # Verify no reallocation
        assert detector._returns.nbytes == initial_returns_nbytes
        assert detector._abs_returns.nbytes == initial_abs_returns_nbytes
        assert id(detector._returns) == initial_returns_id
        assert id(detector._abs_returns) == initial_abs_id
        assert len(detector._returns) == window

    def test_object_size_bounded(self) -> None:
        detector = DriftBurstDetector(window_size=100, cooldown_ns=0, skip_zero_returns=False)
        size_before = sys.getsizeof(detector)

        prices = _make_random_walk_prices(2000_0000, 1000, seed=66)
        for p in prices:
            detector.evaluate(mid_price_x2=p)

        size_after = sys.getsizeof(detector)
        # __slots__ class size should be identical
        assert size_after == size_before


# ---------------------------------------------------------------------------
# Test: Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Verify correct handling of edge conditions."""

    def test_zero_mid_price_safe(self) -> None:
        detector = DriftBurstDetector(window_size=20, cooldown_ns=0, skip_zero_returns=False)
        result = detector.evaluate(mid_price_x2=0)
        assert not result.burst_detected
        assert result.toxicity_score == 0.0

    def test_single_tick_no_burst(self) -> None:
        detector = DriftBurstDetector(window_size=20, cooldown_ns=0, skip_zero_returns=False)
        result = detector.evaluate(mid_price_x2=2000_0000)
        assert not result.burst_detected
        assert result.toxicity_score == 0.0

    def test_constant_price_no_burst(self) -> None:
        detector = DriftBurstDetector(window_size=20, burst_threshold=2.0, cooldown_ns=0, skip_zero_returns=False)
        for _ in range(50):
            result = detector.evaluate(mid_price_x2=2000_0000)
        assert not result.burst_detected
        # Constant price => zero drift, zero vol => score should be 0
        assert result.toxicity_score == 0.0

    def test_reset_clears_state(self) -> None:
        detector = DriftBurstDetector(window_size=50, cooldown_ns=0, skip_zero_returns=False)
        prices = _make_trending_prices(2000_0000, 100, drift_per_tick=0.002, seed=20)
        for p in prices:
            detector.evaluate(mid_price_x2=p)

        assert detector._count > 0
        assert detector.t_statistic != 0.0

        detector.reset()

        assert detector._count == 0
        assert detector.t_statistic == 0.0
        assert detector.toxicity_score == 0.0
        assert detector.last_burst is None
        assert not detector.is_warm

    def test_invalid_window_size(self) -> None:
        with pytest.raises(ValueError, match="window_size must be >= 10"):
            DriftBurstDetector(window_size=5)

    def test_invalid_threshold(self) -> None:
        with pytest.raises(ValueError, match="burst_threshold must be > 0"):
            DriftBurstDetector(burst_threshold=-1.0)

    def test_is_warm_transitions(self) -> None:
        window = 20
        detector = DriftBurstDetector(window_size=window, cooldown_ns=0, skip_zero_returns=False)
        prices = _make_random_walk_prices(2000_0000, window + 5, seed=30)

        # After 0 ticks: not warm
        assert not detector.is_warm

        for i, p in enumerate(prices):
            detector.evaluate(mid_price_x2=p)
            # First tick sets last_mid_x2 (no return computed), so we need
            # window+1 ticks total (1 seed + window returns) to fill the buffer.
            if i < window:
                assert not detector.is_warm, f"Expected not warm after {i + 1} ticks (need {window + 1})"

        # After window+1+ ticks: warm
        assert detector.is_warm

        # After reset: not warm again
        detector.reset()
        assert not detector.is_warm


# ---------------------------------------------------------------------------
# Test: Toxicity classification
# ---------------------------------------------------------------------------


class TestToxicityClassification:
    """Verify burst events carry correct toxicity type."""

    def test_informed_when_imbalance_opposes_and_spread_positive(self) -> None:
        detector = DriftBurstDetector(
            window_size=50, burst_threshold=2.0, cooldown_ticks=10, cooldown_ns=0, skip_zero_returns=False
        )
        warmup = _make_random_walk_prices(2000_0000, 60, volatility=0.0001, seed=40)
        drift_prices = _make_trending_prices(warmup[-1], 100, drift_per_tick=0.003, seed=41)
        all_prices = warmup + drift_prices[1:]

        burst_event = None
        for p in all_prices:
            # Upward drift + negative imbalance (asks depleted) = informed
            result = detector.evaluate(
                mid_price_x2=p,
                spread_scaled=200,
                imbalance=-0.5,
            )
            if result.burst_detected:
                burst_event = result.burst_event
                break

        assert burst_event is not None
        assert burst_event.toxicity_type == "informed"

    def test_liquidity_when_imbalance_aligned(self) -> None:
        detector = DriftBurstDetector(
            window_size=50, burst_threshold=2.0, cooldown_ticks=10, cooldown_ns=0, skip_zero_returns=False
        )
        warmup = _make_random_walk_prices(2000_0000, 60, volatility=0.0001, seed=50)
        drift_prices = _make_trending_prices(warmup[-1], 100, drift_per_tick=0.003, seed=51)
        all_prices = warmup + drift_prices[1:]

        burst_event = None
        for p in all_prices:
            # Upward drift + positive imbalance (bids present) = liquidity
            result = detector.evaluate(
                mid_price_x2=p,
                spread_scaled=100,
                imbalance=0.5,
            )
            if result.burst_detected:
                burst_event = result.burst_event
                break

        assert burst_event is not None
        assert burst_event.toxicity_type == "liquidity"


# ---------------------------------------------------------------------------
# Test: ToxicityResult interface (for StormGuard integration)
# ---------------------------------------------------------------------------


class TestToxicityResultInterface:
    """Verify ToxicityResult provides the interface StormGuard expects."""

    def test_result_is_named_tuple(self) -> None:
        result = ToxicityResult(burst_detected=False, toxicity_score=0.5, burst_event=None)
        assert result.burst_detected is False
        assert result.toxicity_score == 0.5
        assert result.burst_event is None

    def test_toxicity_sigmoid_mapping_values(self) -> None:
        """Verify sigmoid mapping: 2/(1+exp(-|T|/scale))-1."""
        threshold = 3.0
        # T=0 → 0
        score_0 = 2.0 / (1.0 + math.exp(0.0)) - 1.0
        assert abs(score_0) < 1e-10

        # T=threshold → ~0.462
        score_t = 2.0 / (1.0 + math.exp(-1.0)) - 1.0
        assert 0.45 < score_t < 0.48, f"T=threshold score: {score_t}"

        # T=2*threshold → ~0.762
        score_2t = 2.0 / (1.0 + math.exp(-2.0)) - 1.0
        assert 0.75 < score_2t < 0.78, f"T=2*threshold score: {score_2t}"

        # T→∞ → 1.0
        score_inf = 2.0 / (1.0 + math.exp(-100.0)) - 1.0
        assert score_inf > 0.999

    def test_toxicity_score_bounded(self) -> None:
        detector = DriftBurstDetector(window_size=50, burst_threshold=2.0, cooldown_ns=0, skip_zero_returns=False)
        prices = _make_trending_prices(2000_0000, 300, drift_per_tick=0.005, seed=60)

        for p in prices:
            result = detector.evaluate(mid_price_x2=p)
            assert 0.0 <= result.toxicity_score <= 1.0, f"toxicity_score out of bounds: {result.toxicity_score}"

    def test_burst_event_fields(self) -> None:
        event = BurstEvent(
            ts=1_000_000_000,
            direction=1,
            magnitude=3.5,
            toxicity_type="informed",
            t_statistic=3.5,
        )
        assert event.ts == 1_000_000_000
        assert event.direction == 1
        assert event.magnitude == 3.5
        assert event.toxicity_type == "informed"
        assert event.t_statistic == 3.5


# ---------------------------------------------------------------------------
# Test: Skip zero returns filter (Fix 2)
# ---------------------------------------------------------------------------


class TestSkipZeroReturns:
    """Verify skip_zero_returns filters out flat ticks."""

    def test_skip_zero_returns_filters_flat_ticks(self) -> None:
        """Feed 100 identical prices then 1 price change. Should NOT trigger burst."""
        detector = DriftBurstDetector(
            window_size=20,
            burst_threshold=2.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            skip_zero_returns=True,
        )
        base_price = 2000_0000

        # 100 ticks at the same price — all zero returns, should be skipped
        for _ in range(100):
            result = detector.evaluate(mid_price_x2=base_price)
            assert not result.burst_detected

        # The ring buffer should have count=0 since all returns were zero
        assert detector._count == 0
        assert not detector.is_warm

        # One price change: insufficient data for burst (window not full)
        result = detector.evaluate(mid_price_x2=base_price + 100)
        assert not result.burst_detected

    def test_skip_zero_returns_only_counts_moves(self) -> None:
        """With skip_zero_returns, window fills only on actual price moves."""
        window = 10
        detector = DriftBurstDetector(
            window_size=window,
            burst_threshold=5.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            skip_zero_returns=True,
        )

        base_price = 2000_0000
        # Alternate: 5 flat ticks, 1 move, repeat
        move_count = 0
        for i in range(200):
            price = base_price + (i // 5)  # changes every 5 ticks
            result = detector.evaluate(mid_price_x2=price)
            if price != base_price + ((i - 1) // 5 if i > 0 else -1):
                move_count += 1

        # After enough actual moves, detector should be warm
        assert detector.is_warm

    def test_skip_zero_returns_disabled_behaves_as_before(self) -> None:
        """With skip_zero_returns=False, constant price produces zero T-stat."""
        detector = DriftBurstDetector(
            window_size=20,
            burst_threshold=2.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            skip_zero_returns=False,
        )
        for _ in range(50):
            result = detector.evaluate(mid_price_x2=2000_0000)

        assert detector.is_warm
        assert not result.burst_detected
        assert result.toxicity_score == 0.0


# ---------------------------------------------------------------------------
# Test: Minimum BPV floor (Fix 1)
# ---------------------------------------------------------------------------


class TestMinBpvFloor:
    """Verify min_bpv prevents T-statistic explosion."""

    def test_min_bpv_floor_prevents_explosion(self) -> None:
        """Data with very low BPV should not produce extreme T-statistics."""
        detector = DriftBurstDetector(
            window_size=20,
            burst_threshold=3.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            min_bpv=1e-10,
            skip_zero_returns=False,  # allow zero returns to create low BPV
        )
        base_price = 2000_0000

        # Feed many ticks with tiny returns (BPV will be very small)
        # Simulate: mostly flat with tiny 1-unit changes
        max_abs_t = 0.0
        for i in range(50):
            # Alternate between base_price and base_price+1
            price = base_price + (i % 2)
            result = detector.evaluate(mid_price_x2=price)
            abs_t = abs(detector.t_statistic)
            if abs_t > max_abs_t:
                max_abs_t = abs_t

        # With min_bpv floor, T-stat should never explode to hundreds
        # (without floor, BPV near zero causes T to spike to thousands)
        assert max_abs_t < 50.0, f"T-stat too large: {max_abs_t}, min_bpv floor not working"

    def test_min_bpv_zero_disables_floor(self) -> None:
        """With min_bpv=0, the floor check is disabled (old behavior)."""
        detector = DriftBurstDetector(
            window_size=20,
            burst_threshold=3.0,
            cooldown_ticks=0,
            cooldown_ns=0,
            min_bpv=0.0,
            skip_zero_returns=False,
        )
        # Should still work — just uses _EPS as before
        for _ in range(30):
            detector.evaluate(mid_price_x2=2000_0000)
        assert detector.is_warm


# ---------------------------------------------------------------------------
# Test: Production cooldown_ns default (Fix 3)
# ---------------------------------------------------------------------------


class TestCooldownNsProductionDefault:
    """Verify 5s cooldown_ns is the default for production use."""

    def test_cooldown_ns_default_production(self) -> None:
        """Default cooldown_ns is 5 seconds, not 0."""
        detector = DriftBurstDetector(window_size=20, burst_threshold=2.0)
        assert detector._cooldown_ns == 5_000_000_000

    def test_cooldown_ns_5s_suppresses_rapid_bursts(self) -> None:
        """With 5s cooldown, bursts within 5s window are suppressed."""
        cooldown_ns = 5_000_000_000
        detector = DriftBurstDetector(
            window_size=20,
            burst_threshold=2.0,
            cooldown_ticks=0,
            cooldown_ns=cooldown_ns,
            skip_zero_returns=False,
        )
        prices = _make_trending_prices(2000_0000, 300, drift_per_tick=0.003, seed=77)

        burst_timestamps: list[int] = []
        base_ts = 1_000_000_000_000
        # 1ms per tick — 300 ticks = 0.3s total, so at most 1 burst
        tick_interval_ns = 1_000_000  # 1ms

        for i, p in enumerate(prices):
            ts = base_ts + i * tick_interval_ns
            result = detector.evaluate(mid_price_x2=p, ts=ts)
            if result.burst_detected:
                burst_timestamps.append(ts)

        # Total time span is ~300ms, so with 5s cooldown only 1 burst max
        assert len(burst_timestamps) <= 1, (
            f"Expected at most 1 burst in 300ms with 5s cooldown, got {len(burst_timestamps)}"
        )
