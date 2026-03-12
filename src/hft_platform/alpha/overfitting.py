"""Enhanced overfitting detection: Deflated Sharpe Ratio, PBO, pool correlation guard.

References:
- Bailey & Lopez de Prado (2014), "The Deflated Sharpe Ratio"
- Lopez de Prado (2014), "Probability of Backtest Overfitting"
- Lo (2002), "The Statistics of Sharpe Ratios"
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats

from hft_platform.alpha.pool import _safe_corr

# Euler-Mascheroni constant
_EULER_MASCHERONI: float = 0.5772156649015329


@dataclass(frozen=True)
class DeflatedSharpeResult:
    """Result of deflated Sharpe ratio computation.

    Attributes:
        dsr: P(SR > E[max]) in [0, 1]; > 0.95 is good.
        expected_max_sharpe: Expected maximum Sharpe under null.
        sr_adjusted: Non-normality adjusted Sharpe ratio.
        skewness: Sample skewness of OOS returns.
        excess_kurtosis: Sample excess kurtosis of OOS returns.
        se_sharpe: Standard error of the Sharpe estimator.
        n_obs: Number of observations used.
    """

    __slots__ = (
        "dsr",
        "expected_max_sharpe",
        "sr_adjusted",
        "skewness",
        "excess_kurtosis",
        "se_sharpe",
        "n_obs",
    )

    dsr: float
    expected_max_sharpe: float
    sr_adjusted: float
    skewness: float
    excess_kurtosis: float
    se_sharpe: float
    n_obs: int


@dataclass(frozen=True)
class PBOResult:
    """Result of probability of backtest overfitting computation.

    Attributes:
        pbo: Fraction of paths where OOS Sharpe <= 0, in [0, 1].
        logit_pbo: log(pbo / (1 - pbo)); < 0 is good.
        n_paths: Total number of CPCV paths evaluated.
        n_underperforming: Paths with OOS Sharpe <= 0.
    """

    __slots__ = ("pbo", "logit_pbo", "n_paths", "n_underperforming")

    pbo: float
    logit_pbo: float
    n_paths: int
    n_underperforming: int


@dataclass(frozen=True)
class PoolCorrelationResult:
    """Result of pool correlation guard check.

    Attributes:
        passed: True if max |correlation| < threshold.
        max_corr: Maximum absolute correlation observed.
        most_correlated_alpha: Alpha ID with highest |correlation|, or None.
        correlations: Mapping of alpha_id -> |correlation|.
        threshold: Threshold used for the check.
    """

    __slots__ = (
        "passed",
        "max_corr",
        "most_correlated_alpha",
        "correlations",
        "threshold",
    )

    passed: bool
    max_corr: float
    most_correlated_alpha: str | None
    correlations: dict[str, float]
    threshold: float


def deflated_sharpe_ratio(
    sharpe_oos: float,
    n_trials: int,
    oos_returns: np.ndarray,
    annualization_factor: float = 252.0,
) -> DeflatedSharpeResult:
    """Compute the Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio.

    Adjusts the observed Sharpe ratio for multiple testing bias and
    non-normality of returns.

    Args:
        sharpe_oos: Observed (annualized) out-of-sample Sharpe ratio.
        n_trials: Number of strategy trials / backtest configurations tested.
        oos_returns: 1-D array of OOS period returns.
        annualization_factor: Periods per year (252 for daily, 12 for monthly).

    Returns:
        DeflatedSharpeResult with DSR probability and diagnostics.
    """
    oos_returns = np.asarray(oos_returns, dtype=np.float64).ravel()
    n = len(oos_returns)

    # --- Step 1: moments ---
    skew = float(stats.skew(oos_returns, bias=False))
    kurt = float(stats.kurtosis(oos_returns, fisher=True, bias=False))

    # Per-period Sharpe (de-annualize for moment adjustments)
    sr_per_period = sharpe_oos / math.sqrt(annualization_factor)

    # --- Step 2: expected max Sharpe under null (i.i.d. normal) ---
    gamma = _EULER_MASCHERONI
    if n_trials <= 1:
        expected_max_sr = 0.0
    else:
        expected_max_sr = (1.0 - gamma) * stats.norm.ppf(1.0 - 1.0 / n_trials) + gamma * stats.norm.ppf(
            1.0 - 1.0 / (n_trials * math.e)
        )

    # Annualize expected max SR for comparison
    expected_max_sharpe = float(expected_max_sr * math.sqrt(annualization_factor))

    # --- Step 3: non-normality adjustment (Lo 2002) ---
    # Applied to per-period SR, then re-annualized
    correction = 1.0 - skew * sr_per_period / 3.0 + (kurt - 3.0) * sr_per_period**2 / 24.0
    if correction <= 0:
        # Extreme non-normality; clamp to avoid sqrt of negative
        sr_adjusted_per_period = sr_per_period
    else:
        sr_adjusted_per_period = sr_per_period / math.sqrt(correction)

    sr_adjusted = float(sr_adjusted_per_period * math.sqrt(annualization_factor))

    # --- Step 4: standard error of Sharpe estimator ---
    se_per_period = math.sqrt(
        (1.0 + 0.5 * sr_per_period**2 - skew * sr_per_period + (kurt / 4.0) * sr_per_period**2) / max(n - 1, 1)
    )
    se_sharpe = float(se_per_period * math.sqrt(annualization_factor))

    # --- Step 5: DSR = P(SR* > E[max(SR)]) ---
    if se_sharpe > 0:
        z = (sr_adjusted - expected_max_sharpe) / se_sharpe
        dsr = float(stats.norm.cdf(z))
    else:
        dsr = 1.0 if sr_adjusted > expected_max_sharpe else 0.0

    return DeflatedSharpeResult(
        dsr=dsr,
        expected_max_sharpe=expected_max_sharpe,
        sr_adjusted=sr_adjusted,
        skewness=skew,
        excess_kurtosis=kurt,
        se_sharpe=se_sharpe,
        n_obs=n,
    )


def probability_of_backtest_overfitting(
    is_sharpes: np.ndarray,
    oos_sharpes: np.ndarray,
) -> PBOResult:
    """Compute probability of backtest overfitting (Lopez de Prado 2014).

    Simplified single-strategy version: PBO = fraction of CPCV paths where
    the strategy's OOS Sharpe is <= 0 (underperforms risk-free).

    Args:
        is_sharpes: Shape (n_paths,) in-sample Sharpe per CPCV path.
        oos_sharpes: Shape (n_paths,) out-of-sample Sharpe per CPCV path.

    Returns:
        PBOResult with PBO probability and logit transform.
    """
    is_sharpes = np.asarray(is_sharpes, dtype=np.float64).ravel()
    oos_sharpes = np.asarray(oos_sharpes, dtype=np.float64).ravel()

    if len(is_sharpes) != len(oos_sharpes):
        msg = f"is_sharpes and oos_sharpes must have same length, got {len(is_sharpes)} and {len(oos_sharpes)}"
        raise ValueError(msg)

    n_paths = len(oos_sharpes)
    if n_paths == 0:
        msg = "oos_sharpes must not be empty"
        raise ValueError(msg)

    n_underperforming = int(np.sum(oos_sharpes <= 0))
    pbo = n_underperforming / n_paths

    if pbo <= 0.0:
        logit_pbo = -math.inf
    elif pbo >= 1.0:
        logit_pbo = math.inf
    else:
        logit_pbo = math.log(pbo / (1.0 - pbo))

    return PBOResult(
        pbo=pbo,
        logit_pbo=logit_pbo,
        n_paths=n_paths,
        n_underperforming=n_underperforming,
    )


def pool_correlation_guard(
    candidate_signals: np.ndarray,
    pool_signals: dict[str, np.ndarray],
    threshold: float = 0.85,
) -> PoolCorrelationResult:
    """Check if a candidate signal is too correlated with existing pool alphas.

    Computes Pearson correlation between the candidate and each pool signal,
    using absolute values so negative correlation is also flagged.

    Args:
        candidate_signals: Shape (T,) candidate signal time series.
        pool_signals: Mapping of alpha_id -> signal array shape (T,).
        threshold: Maximum allowed |correlation|; default 0.85.

    Returns:
        PoolCorrelationResult indicating whether the candidate passes.
    """
    candidate: np.ndarray[tuple[int], np.dtype[np.float64]] = np.asarray(candidate_signals, dtype=np.float64).ravel()
    np.nan_to_num(candidate, nan=0.0, copy=False)

    if not pool_signals:
        return PoolCorrelationResult(
            passed=True,
            max_corr=0.0,
            most_correlated_alpha=None,
            correlations={},
            threshold=threshold,
        )

    correlations: dict[str, float] = {}
    max_corr = 0.0
    most_correlated: str | None = None

    for alpha_id, signals in pool_signals.items():
        other: np.ndarray[tuple[int], np.dtype[np.float64]] = np.asarray(signals, dtype=np.float64).ravel()
        np.nan_to_num(other, nan=0.0, copy=False)

        # Truncate to minimum length
        min_len = min(len(candidate), len(other))
        if min_len < 2:
            correlations[alpha_id] = 0.0
            continue

        c = candidate[:min_len]
        o = other[:min_len]

        corr = abs(_safe_corr(c, o))

        correlations[alpha_id] = corr

        if corr > max_corr:
            max_corr = corr
            most_correlated = alpha_id

    return PoolCorrelationResult(
        passed=max_corr < threshold,
        max_corr=max_corr,
        most_correlated_alpha=most_correlated,
        correlations=correlations,
        threshold=threshold,
    )
