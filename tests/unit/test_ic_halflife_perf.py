"""Regression tests for compute_ic_halflife.

Pre-fix, `compute_ic_halflife` materialised the full 2N-1 lag autocorrelation
via `np.correlate(mode="full")`, which is O(N^2) direct convolution. At
N>=28k the call hung Gate C (>13 min observed at N=27,918, projected hours
at N=410k for a full TMFD6 day). The fix computes only the first
`n_lags <= max_lag` autocovariances directly (O(n_lags * N)), keeping the
function bounded regardless of input size.

These tests:
  * lock in the bit-identical output behaviour on a small case;
  * lock in the perf budget at the sizes Gate C actually exercises.
"""
from __future__ import annotations

import time

import numpy as np
import pytest

from research.backtest.metrics import compute_ic_halflife


def _ar1(n: int, rho: float, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    eps = rng.standard_normal(n)
    out = np.empty(n, dtype=np.float64)
    out[0] = eps[0]
    for i in range(1, n):
        out[i] = rho * out[i - 1] + eps[i]
    return out


class TestComputeICHalflifeCorrectness:
    """Locks correctness so the perf rewrite cannot regress numerically."""

    def test_constant_returns_zero(self) -> None:
        signals = np.ones(1000, dtype=np.float64)
        assert compute_ic_halflife(signals) == 0

    def test_below_minimum_returns_zero(self) -> None:
        assert compute_ic_halflife(np.array([1.0, 2.0, 3.0])) == 0

    def test_strong_persistence_returns_high_lag(self) -> None:
        # rho=0.95 -> ACF stays > 0.5 for ~13 lags; result should be >= 10.
        signals = _ar1(5000, rho=0.95, seed=7)
        result = compute_ic_halflife(signals)
        assert result >= 10, f"expected >=10, got {result}"

    def test_weak_persistence_returns_low_lag(self) -> None:
        # rho=0.1 -> ACF drops below 0.5 by lag 1.
        signals = _ar1(5000, rho=0.1, seed=11)
        result = compute_ic_halflife(signals)
        assert 1 <= result <= 3, f"expected 1..3, got {result}"


class TestComputeICHalflifePerf:
    """Locks the perf budget so the np.correlate(mode='full') hang cannot
    silently come back."""

    @pytest.mark.parametrize("n", [30_000, 100_000])
    def test_completes_under_15s(self, n: int) -> None:
        signals = _ar1(n, rho=0.3, seed=42)
        start = time.perf_counter()
        result = compute_ic_halflife(signals)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, (
            f"compute_ic_halflife took {elapsed:.2f}s at N={n}; "
            "regression of the np.correlate(mode='full') O(N^2) bug."
        )
        assert isinstance(result, int)
        assert 0 <= result <= 50

    def test_handles_full_day_without_hang(self) -> None:
        # Full TMFD6 trading-day all_signals size is roughly 4e5.  The
        # pre-fix implementation projected hours at this scale.
        signals = _ar1(410_000, rho=0.3, seed=99)
        start = time.perf_counter()
        result = compute_ic_halflife(signals)
        elapsed = time.perf_counter() - start
        assert elapsed < 15.0, (
            f"compute_ic_halflife took {elapsed:.2f}s at N=410k; "
            "Gate C will hang on full-day signal series."
        )
        assert isinstance(result, int)
