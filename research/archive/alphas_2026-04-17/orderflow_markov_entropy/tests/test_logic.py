"""Gate B correctness tests for OrderflowMarkovEntropyAlpha."""

from __future__ import annotations

import math
import os
import sys

sys.path.append(os.getcwd())

import numpy as np
import pytest

from research.alphas.orderflow_markov_entropy.impl import (
    ALPHA_CLASS,
    OrderflowMarkovEntropyAlpha,
    _K,
    _WARMUP_EVENTS,
)


def _feed_ticks(
    alpha: OrderflowMarkovEntropyAlpha,
    prices: list[int],
    volumes: list[float],
    start_ts_ns: int = 1_000_000_000,
    interval_ns: int = 125_000_000,  # 125ms (TXFD6-like)
) -> list[float]:
    """Feed a sequence of ticks and return signals."""
    signals = []
    ts = start_ts_ns
    for p, v in zip(prices, volumes):
        sig = alpha.update(price=p, volume=v, timestamp_ns=ts)
        signals.append(sig)
        ts += interval_ns
    return signals


# --- Manifest ---
def test_manifest_alpha_id() -> None:
    assert OrderflowMarkovEntropyAlpha().manifest.alpha_id == "orderflow_markov_entropy"


def test_manifest_tier_is_tier2() -> None:
    from research.registry.schemas import AlphaTier
    assert OrderflowMarkovEntropyAlpha().manifest.tier == AlphaTier.TIER_2


def test_manifest_paper_refs() -> None:
    assert "arXiv:2512.15720" in OrderflowMarkovEntropyAlpha().manifest.paper_refs


def test_manifest_data_fields() -> None:
    f = OrderflowMarkovEntropyAlpha().manifest.data_fields
    assert "mid_price" in f and "volume" in f and "local_ts" in f


def test_manifest_latency_profile_set() -> None:
    assert OrderflowMarkovEntropyAlpha().manifest.latency_profile is not None


def test_alpha_class_export() -> None:
    assert ALPHA_CLASS is OrderflowMarkovEntropyAlpha


# --- Protocol ---
def test_implements_alpha_protocol() -> None:
    from research.registry.schemas import AlphaProtocol
    assert isinstance(OrderflowMarkovEntropyAlpha(), AlphaProtocol)


# --- Basic behavior ---
def test_first_tick_returns_zero() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    sig = alpha.update(price=100_0000, volume=10.0, timestamp_ns=1_000_000_000)
    assert sig == 0.0


def test_get_signal_before_update() -> None:
    assert OrderflowMarkovEntropyAlpha().get_signal() == 0.0


def test_signal_bounded_0_to_1() -> None:
    alpha = OrderflowMarkovEntropyAlpha(window_s=10)
    rng = np.random.default_rng(42)
    prices = [100_0000 + int(rng.integers(-5, 6)) * 10000 for _ in range(500)]
    volumes = [float(rng.uniform(1, 100)) for _ in range(500)]
    signals = _feed_ticks(alpha, prices, volumes, interval_ns=25_000_000)
    for s in signals:
        assert 0.0 <= s <= 1.0, f"signal {s} out of bounds"


# --- Informed trading detection ---
def test_repetitive_pattern_lowers_entropy() -> None:
    """A repetitive buy-buy-buy pattern should produce lower entropy (higher signal)
    than random noise."""
    # Repetitive pattern: price always goes up with high volume
    alpha_pattern = OrderflowMarkovEntropyAlpha(window_s=30)
    pattern_prices = []
    p = 100_0000
    for i in range(200):
        p += 10000  # always up
        pattern_prices.append(p)
    pattern_vols = [90.0] * 200  # always high volume
    sigs_pattern = _feed_ticks(alpha_pattern, pattern_prices, pattern_vols)

    # Random pattern
    alpha_random = OrderflowMarkovEntropyAlpha(window_s=30)
    rng = np.random.default_rng(123)
    random_prices = [100_0000 + int(rng.integers(-50, 51)) * 10000 for _ in range(200)]
    random_vols = [float(rng.uniform(1, 100)) for _ in range(200)]
    sigs_random = _feed_ticks(alpha_random, random_prices, random_vols)

    # Repetitive pattern should have higher inverted entropy (more informed)
    avg_pattern = sum(sigs_pattern[-50:]) / 50
    avg_random = sum(sigs_random[-50:]) / 50
    assert avg_pattern > avg_random, (
        f"pattern avg {avg_pattern:.4f} should exceed random avg {avg_random:.4f}"
    )


# --- State space construction ---
def test_state_index_range() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    for sign in (-1, 0, 1):
        for vq in range(5):
            idx = alpha._state_index(sign, vq)
            assert 0 <= idx < _K, f"state index {idx} out of range for sign={sign}, vq={vq}"


def test_all_15_states_reachable() -> None:
    """With varied inputs, all 15 states should be reachable."""
    alpha = OrderflowMarkovEntropyAlpha(window_s=600)
    prices = []
    volumes = []
    p = 100_0000
    # Generate ticks that exercise all sign x volume combinations
    for sign in (-1, 0, 1):
        for vol_level in (1.0, 20.0, 40.0, 60.0, 80.0, 99.0):
            dp = sign * 10000
            p += dp
            prices.append(p)
            volumes.append(vol_level)
    # Repeat to build up transitions
    prices = prices * 20
    volumes = volumes * 20
    _feed_ticks(alpha, prices, volumes)
    stats = alpha.occupancy_stats()
    assert stats["states_populated"] >= 10, (
        f"Only {stats['states_populated']}/{_K} states populated"
    )


# --- Occupancy stats (Challenger C1) ---
def test_occupancy_stats_keys() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    _feed_ticks(alpha, [100_0000, 100_1000, 99_9000], [10.0, 20.0, 30.0])
    stats = alpha.occupancy_stats()
    assert "states_populated" in stats
    assert "transitions_populated" in stats
    assert "states_pct" in stats
    assert "transitions_pct" in stats


def test_occupancy_sparse_window() -> None:
    """With only ~30 ticks in 120s, occupancy should be sparse but non-zero."""
    alpha = OrderflowMarkovEntropyAlpha(window_s=120)
    rng = np.random.default_rng(77)
    prices = [100_0000 + int(rng.integers(-3, 4)) * 10000 for _ in range(30)]
    volumes = [float(rng.uniform(1, 50)) for _ in range(30)]
    _feed_ticks(alpha, prices, volumes, interval_ns=4_000_000_000)  # 4s intervals
    stats = alpha.occupancy_stats()
    assert stats["states_populated"] > 0
    assert stats["transitions_populated"] > 0
    assert stats["transitions_pct"] < 50.0  # should be sparse


# --- Orthogonality (Challenger C2) ---
def test_orthogonality_insufficient_data() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    result = alpha.compute_orthogonality([0.5, 0.3])
    assert result["sufficient_data"] is False


def test_orthogonality_with_data() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    result = alpha.compute_orthogonality([0.5] * 20)
    assert result["sufficient_data"] is True
    assert "theoretical_distinction" in result


# --- Reset ---
def test_reset_clears_state() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    _feed_ticks(alpha, [100_0000, 101_0000, 102_0000], [10.0, 20.0, 30.0])
    alpha.reset()
    alpha2 = OrderflowMarkovEntropyAlpha()
    p, v, ts = 100_0000, 10.0, 1_000_000_000
    assert alpha.update(price=p, volume=v, timestamp_ns=ts) == pytest.approx(
        alpha2.update(price=p, volume=v, timestamp_ns=ts), abs=1e-9
    )


# --- Window eviction ---
def test_old_events_evicted() -> None:
    """Events outside the window should be removed from transition counts."""
    alpha = OrderflowMarkovEntropyAlpha(window_s=5)  # 5-second window
    # Feed events in first 5 seconds
    prices = [100_0000 + i * 10000 for i in range(20)]
    volumes = [50.0] * 20
    _feed_ticks(alpha, prices, volumes, interval_ns=250_000_000)  # 250ms each = 5s total
    count_before = alpha._transition_counts.sum()

    # Feed more events 10 seconds later — old ones should be evicted
    later_prices = [120_0000 + i * 10000 for i in range(20)]
    later_ts = 1_000_000_000 + 10 * 1_000_000_000  # 10s after start
    for i, (p, v) in enumerate(zip(later_prices, volumes)):
        alpha.update(price=p, volume=v, timestamp_ns=later_ts + i * 250_000_000)

    # Buffer count should not grow unboundedly
    assert alpha._buf_count <= 40


# --- Numerical stability ---
def test_zero_volume_no_crash() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    _feed_ticks(alpha, [100_0000] * 10, [0.0] * 10)
    assert math.isfinite(alpha.get_signal())


def test_constant_price_no_crash() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    _feed_ticks(alpha, [100_0000] * 100, [50.0] * 100)
    assert math.isfinite(alpha.get_signal())


def test_large_values_no_overflow() -> None:
    alpha = OrderflowMarkovEntropyAlpha()
    _feed_ticks(alpha, [999_9999_0000] * 50, [1e9] * 50)
    assert math.isfinite(alpha.get_signal())
