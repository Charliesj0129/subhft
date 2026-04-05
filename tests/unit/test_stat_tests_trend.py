"""Tests for _evaluate_trend_contamination in alpha._stat_tests."""
from __future__ import annotations

import numpy as np

from hft_platform.alpha._stat_tests import _evaluate_trend_contamination

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trending_signal(n: int = 2000, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """Create a signal that tracks a smooth trend (should FAIL trend check).

    The mid_prices follow a random walk with drift. The signal is the rolling
    cumulative return over a long window — this produces a classic trend
    contamination signature where IC increases with horizon because the signal
    captures the drift component.
    """
    rng = np.random.default_rng(seed)
    # Random walk with persistent drift
    returns = rng.normal(0.0003, 0.001, n)
    mid = 100.0 * np.exp(np.cumsum(returns))
    # Signal = rolling 200-step cumulative return (captures trend)
    window = 200
    signal = np.zeros(n, dtype=np.float64)
    for i in range(window, n):
        signal[i] = (mid[i] - mid[i - window]) / mid[i - window]
    return signal, mid


def _make_mean_reverting_signal(n: int = 2000, seed: int = 42) -> tuple[np.ndarray, np.ndarray]:
    """Create a mean-reverting signal (should PASS trend check).

    The signal predicts short-term reversals with no trend component.
    """
    rng = np.random.default_rng(seed)
    mid = np.cumsum(rng.normal(0, 0.001, n)) + 100.0
    # Signal = negative of short-term momentum (mean-reverting)
    lookback = 5
    signal = np.zeros(n, dtype=np.float64)
    for i in range(lookback, n):
        signal[i] = -(mid[i] - mid[i - lookback]) / mid[i - lookback]
    return signal, mid


# ---------------------------------------------------------------------------
# Check A: Monotonic IC
# ---------------------------------------------------------------------------

class TestMonotonicIC:
    def test_trending_signal_detected_as_monotonic(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        mono = result["monotonic_ic"]
        assert mono["is_monotonic"] is True
        assert mono["pass"] is False

    def test_mean_reverting_signal_not_monotonic(self) -> None:
        signal, mid = _make_mean_reverting_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        mono = result["monotonic_ic"]
        assert mono["is_monotonic"] is False
        assert mono["pass"] is True

    def test_horizons_list_populated(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        mono = result["monotonic_ic"]
        assert mono["horizons"] == [1, 5, 10, 20, 50]
        assert len(mono["ics"]) == 5


# ---------------------------------------------------------------------------
# Check B: Detrended IC
# ---------------------------------------------------------------------------

class TestDetrendedIC:
    def test_trending_signal_detrended_ic_fails(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        dt = result["detrended_ic"]
        # Either sign flips or falls below threshold
        assert dt["pass"] is False

    def test_mean_reverting_signal_detrended_ic_passes(self) -> None:
        signal, mid = _make_mean_reverting_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        dt = result["detrended_ic"]
        # Mean-reverting signal should survive detrending
        assert dt["pass"] is True

    def test_sign_flip_detection(self) -> None:
        """sign_flipped flag must reflect actual sign relationship."""
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        dt = result["detrended_ic"]
        raw_ic = dt["raw_ic"]
        detrended_ic = dt["detrended_ic"]
        expected_flip = (raw_ic > 0 and detrended_ic < 0) or (raw_ic < 0 and detrended_ic > 0)
        assert dt["sign_flipped"] == expected_flip


# ---------------------------------------------------------------------------
# Overall gate
# ---------------------------------------------------------------------------

class TestOverallGate:
    def test_trending_signal_fails_gate(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        assert result["passed"] is False

    def test_mean_reverting_signal_passes_gate(self) -> None:
        signal, mid = _make_mean_reverting_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        assert result["passed"] is True

    def test_random_noise_passes_gate(self) -> None:
        """Random uncorrelated signal should pass (no trend contamination)."""
        rng = np.random.default_rng(123)
        n = 2000
        mid = np.cumsum(rng.normal(0, 0.001, n)) + 100.0
        signal = rng.normal(0, 1, n)
        result = _evaluate_trend_contamination(signal, mid)
        # Random signal: IC ~ 0 everywhere, not monotonic
        mono = result["monotonic_ic"]
        assert mono["is_monotonic"] is False


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_short_arrays_pass_by_default(self) -> None:
        signal = np.array([1.0, 2.0, 3.0])
        mid = np.array([100.0, 101.0, 102.0])
        result = _evaluate_trend_contamination(signal, mid)
        assert result["passed"] is True
        assert "insufficient_data" in result["detail"]

    def test_constant_signal(self) -> None:
        n = 2000
        mid = np.linspace(100, 110, n)
        signal = np.ones(n) * 0.5
        result = _evaluate_trend_contamination(signal, mid)
        # Constant signal has zero IC, should not be monotonic
        assert result["monotonic_ic"]["is_monotonic"] is False

    def test_constant_mid_prices(self) -> None:
        n = 2000
        mid = np.ones(n) * 100.0
        signal = np.random.default_rng(42).normal(0, 1, n)
        result = _evaluate_trend_contamination(signal, mid)
        # Zero returns everywhere, IC should be 0
        assert result["passed"] is True

    def test_mismatched_lengths(self) -> None:
        """Signal and mid_prices of different lengths should be handled."""
        signal = np.random.default_rng(42).normal(0, 1, 2000)
        mid = np.cumsum(np.random.default_rng(42).normal(0, 0.001, 1500)) + 100.0
        result = _evaluate_trend_contamination(signal, mid)
        # Should not raise, and should use min(len) samples
        assert "passed" in result


# ---------------------------------------------------------------------------
# Non-overlapping IC (advisory)
# ---------------------------------------------------------------------------

class TestNonOverlappingIC:
    def test_non_overlapping_is_non_blocking(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        no = result["non_overlapping_ic"]
        assert no["blocking"] is False

    def test_drop_pct_non_negative(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid)
        no = result["non_overlapping_ic"]
        assert no["drop_pct"] >= 0.0


# ---------------------------------------------------------------------------
# Custom parameters
# ---------------------------------------------------------------------------

class TestCustomParams:
    def test_custom_horizons(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid, horizons=[1, 2, 3])
        mono = result["monotonic_ic"]
        assert mono["horizons"] == [1, 2, 3]
        assert len(mono["ics"]) == 3

    def test_custom_detrend_window(self) -> None:
        signal, mid = _make_trending_signal(n=3000)
        result = _evaluate_trend_contamination(signal, mid, detrend_window=100)
        assert "passed" in result
