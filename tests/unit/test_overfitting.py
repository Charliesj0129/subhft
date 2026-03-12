"""Tests for hft_platform.alpha.overfitting module.

Covers deflated Sharpe ratio, probability of backtest overfitting,
and pool correlation guard.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from hft_platform.alpha.overfitting import (
    DeflatedSharpeResult,
    PBOResult,
    PoolCorrelationResult,
    deflated_sharpe_ratio,
    pool_correlation_guard,
    probability_of_backtest_overfitting,
)

# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------


class TestDeflatedSharpeRatio:
    """Tests for deflated_sharpe_ratio()."""

    def test_dsr_known_answer(self) -> None:
        """With 100 trials and SR=2.0 on normal returns, DSR should be low.

        E[max(SR)] from 100 i.i.d. normal draws is ~2.5, so SR=2.0 should
        not survive the deflation penalty.
        """
        rng = np.random.default_rng(42)
        # Generate returns that produce roughly SR=2.0 annualized
        returns = rng.normal(loc=0.002, scale=0.016, size=252)
        result = deflated_sharpe_ratio(
            sharpe_oos=2.0,
            n_trials=100,
            oos_returns=returns,
            annualization_factor=252.0,
        )
        assert isinstance(result, DeflatedSharpeResult)
        # With 100 trials, SR=2.0 should be deflated significantly
        assert result.dsr < 0.5
        assert result.expected_max_sharpe > 2.0  # E[max] > observed

    def test_dsr_high_sharpe_passes(self) -> None:
        """SR=5.0 with only 2 trials using monthly data should survive.

        With monthly data (annualization_factor=12), E[max] is much lower
        because the sqrt(12) multiplier is small.
        """
        rng = np.random.default_rng(123)
        returns = rng.normal(loc=0.02, scale=0.04, size=60)  # 5 years monthly
        result = deflated_sharpe_ratio(
            sharpe_oos=5.0,
            n_trials=2,
            oos_returns=returns,
            annualization_factor=12.0,
        )
        assert result.dsr > 0.95

    def test_dsr_single_trial(self) -> None:
        """n_trials=1 means no multiple-testing penalty; DSR should be high."""
        rng = np.random.default_rng(7)
        returns = rng.normal(loc=0.001, scale=0.01, size=252)
        result = deflated_sharpe_ratio(
            sharpe_oos=1.5,
            n_trials=1,
            oos_returns=returns,
        )
        # With no penalty (expected_max=0), any positive SR should pass
        assert result.expected_max_sharpe == 0.0
        assert result.dsr > 0.90

    def test_dsr_skewness_adjustment(self) -> None:
        """Returns with negative skew should produce lower adjusted SR."""
        rng = np.random.default_rng(99)
        # Normal returns
        normal_returns = rng.normal(loc=0.001, scale=0.01, size=500)
        # Negatively skewed: occasional large losses
        skewed_returns = normal_returns.copy()
        skewed_returns[::20] = -0.08  # inject large losses

        result_normal = deflated_sharpe_ratio(
            sharpe_oos=2.0, n_trials=2, oos_returns=normal_returns,
        )
        result_skewed = deflated_sharpe_ratio(
            sharpe_oos=2.0, n_trials=2, oos_returns=skewed_returns,
        )
        assert result_skewed.skewness < result_normal.skewness
        # Negative skew should reduce the adjusted Sharpe ratio
        assert result_skewed.sr_adjusted < result_normal.sr_adjusted

    def test_dsr_small_sample(self) -> None:
        """With only 20 observations, standard error should be large."""
        rng = np.random.default_rng(55)
        returns = rng.normal(loc=0.001, scale=0.01, size=20)
        result = deflated_sharpe_ratio(
            sharpe_oos=1.0, n_trials=5, oos_returns=returns,
        )
        assert result.n_obs == 20
        assert result.se_sharpe > 0.5  # Large SE with small sample

    def test_dsr_returns_valid_range(self) -> None:
        """DSR must always be in [0, 1]."""
        rng = np.random.default_rng(11)
        for sr in [-1.0, 0.0, 1.0, 3.0, 10.0]:
            returns = rng.normal(size=100)
            result = deflated_sharpe_ratio(
                sharpe_oos=sr, n_trials=50, oos_returns=returns,
            )
            assert 0.0 <= result.dsr <= 1.0, f"DSR={result.dsr} out of range for SR={sr}"


# ---------------------------------------------------------------------------
# Probability of Backtest Overfitting
# ---------------------------------------------------------------------------


class TestProbabilityOfBacktestOverfitting:
    """Tests for probability_of_backtest_overfitting()."""

    def test_pbo_all_positive_oos(self) -> None:
        """All OOS Sharpe > 0 means PBO = 0."""
        is_sharpes = np.array([1.0, 2.0, 1.5, 3.0])
        oos_sharpes = np.array([0.5, 1.0, 0.3, 0.8])
        result = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)
        assert isinstance(result, PBOResult)
        assert result.pbo == 0.0
        assert result.n_underperforming == 0
        assert result.logit_pbo == -math.inf

    def test_pbo_all_negative_oos(self) -> None:
        """All OOS Sharpe <= 0 means PBO = 1."""
        is_sharpes = np.array([2.0, 3.0, 1.5])
        oos_sharpes = np.array([-0.5, -1.0, 0.0])  # 0.0 counts as underperforming
        result = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)
        assert result.pbo == 1.0
        assert result.n_underperforming == 3
        assert result.logit_pbo == math.inf

    def test_pbo_mixed(self) -> None:
        """10 out of 20 paths negative yields PBO = 0.5."""
        is_sharpes = np.ones(20)
        oos_sharpes = np.array([1.0] * 10 + [-1.0] * 10)
        result = probability_of_backtest_overfitting(is_sharpes, oos_sharpes)
        assert result.pbo == pytest.approx(0.5)
        assert result.n_paths == 20
        assert result.n_underperforming == 10

    def test_pbo_logit_sign(self) -> None:
        """PBO < 0.5 gives logit < 0; PBO > 0.5 gives logit > 0."""
        # PBO < 0.5: 3 out of 10 negative
        is_s = np.ones(10)
        oos_low = np.array([1.0] * 7 + [-1.0] * 3)
        result_low = probability_of_backtest_overfitting(is_s, oos_low)
        assert result_low.pbo < 0.5
        assert result_low.logit_pbo < 0.0

        # PBO > 0.5: 7 out of 10 negative
        oos_high = np.array([1.0] * 3 + [-1.0] * 7)
        result_high = probability_of_backtest_overfitting(is_s, oos_high)
        assert result_high.pbo > 0.5
        assert result_high.logit_pbo > 0.0

    def test_pbo_mismatched_lengths_raises(self) -> None:
        """Mismatched array lengths should raise ValueError."""
        with pytest.raises(ValueError, match="same length"):
            probability_of_backtest_overfitting(np.array([1.0, 2.0]), np.array([1.0]))

    def test_pbo_empty_raises(self) -> None:
        """Empty arrays should raise ValueError."""
        with pytest.raises(ValueError, match="must not be empty"):
            probability_of_backtest_overfitting(np.array([]), np.array([]))


# ---------------------------------------------------------------------------
# Pool Correlation Guard
# ---------------------------------------------------------------------------


class TestPoolCorrelationGuard:
    """Tests for pool_correlation_guard()."""

    def test_pool_guard_orthogonal_passes(self) -> None:
        """Uncorrelated signals should pass."""
        rng = np.random.default_rng(42)
        candidate = rng.normal(size=200)
        pool = {
            "alpha_a": rng.normal(size=200),
            "alpha_b": rng.normal(size=200),
        }
        result = pool_correlation_guard(candidate, pool)
        assert isinstance(result, PoolCorrelationResult)
        assert result.passed is True
        assert result.max_corr < 0.85

    def test_pool_guard_identical_fails(self) -> None:
        """Identical signal should fail with max_corr ~= 1.0."""
        signal = np.arange(100, dtype=np.float64)
        result = pool_correlation_guard(signal, {"clone": signal})
        assert result.passed is False
        assert result.max_corr == pytest.approx(1.0, abs=1e-10)
        assert result.most_correlated_alpha == "clone"

    def test_pool_guard_negative_correlation(self) -> None:
        """r=-0.9 should fail (uses |correlation|)."""
        rng = np.random.default_rng(7)
        base = rng.normal(size=500)
        negated = -base + rng.normal(size=500) * 0.1  # strong negative corr
        result = pool_correlation_guard(base, {"neg_alpha": negated}, threshold=0.85)
        assert result.passed is False
        assert result.max_corr > 0.85
        assert result.correlations["neg_alpha"] > 0.85

    def test_pool_guard_empty_pool(self) -> None:
        """Empty pool should always pass."""
        result = pool_correlation_guard(np.array([1.0, 2.0, 3.0]), {})
        assert result.passed is True
        assert result.max_corr == 0.0
        assert result.most_correlated_alpha is None
        assert result.correlations == {}

    def test_pool_guard_nan_handling(self) -> None:
        """Signals with NaN values should be handled gracefully."""
        candidate = np.array([1.0, np.nan, 3.0, 4.0, 5.0])
        pool = {"with_nan": np.array([np.nan, 2.0, 3.0, np.nan, 5.0])}
        result = pool_correlation_guard(candidate, pool)
        # Should not raise; NaN replaced with 0
        assert isinstance(result, PoolCorrelationResult)
        assert not math.isnan(result.max_corr)

    def test_pool_guard_different_lengths(self) -> None:
        """Signals of different lengths should be truncated to minimum."""
        candidate = np.arange(100, dtype=np.float64)
        pool = {"short": np.arange(50, dtype=np.float64)}
        result = pool_correlation_guard(candidate, pool)
        # First 50 elements of both are identical -> corr = 1.0
        assert result.max_corr == pytest.approx(1.0, abs=1e-10)
        assert result.passed is False

    def test_pool_guard_constant_signal(self) -> None:
        """Constant signal should yield correlation = 0 (not NaN)."""
        candidate = np.ones(100)
        pool = {"varied": np.arange(100, dtype=np.float64)}
        result = pool_correlation_guard(candidate, pool)
        assert result.correlations["varied"] == 0.0
        assert result.passed is True
