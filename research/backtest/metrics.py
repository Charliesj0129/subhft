from __future__ import annotations

from typing import Iterable

import numpy as np


def compute_sharpe(equity_curve: Iterable[float], annualization_factor: float = 252.0) -> float:
    values = np.asarray(list(equity_curve), dtype=np.float64)
    if values.size < 2:
        return 0.0

    base = values[:-1]
    delta = np.diff(values)
    returns = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
    returns = returns[np.isfinite(returns)]
    if returns.size < 2:
        return 0.0

    std = float(np.std(returns))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std * np.sqrt(annualization_factor))


def compute_ic(
    signals: Iterable[float],
    forward_returns: Iterable[float],
    buckets: int = 20,
) -> tuple[float, float, np.ndarray]:
    sig = np.asarray(list(signals), dtype=np.float64)
    fut = np.asarray(list(forward_returns), dtype=np.float64)
    n = min(sig.size, fut.size)
    if n < 8:
        return 0.0, 0.0, np.asarray([], dtype=np.float64)

    sig = sig[:n]
    fut = fut[:n]
    chunk = max(8, n // max(buckets, 1))
    n_chunks = (n - chunk) // chunk + 1
    if n_chunks < 1:
        return 0.0, 0.0, np.asarray([], dtype=np.float64)

    # Reshape into (n_chunks, chunk) — contiguous, no per-chunk allocation
    n_usable = n_chunks * chunk
    sig_chunks = sig[:n_usable].reshape(n_chunks, chunk)
    fut_chunks = fut[:n_usable].reshape(n_chunks, chunk)

    # Vectorized Pearson across all chunks simultaneously (axis=1)
    sig_centered = sig_chunks - sig_chunks.mean(axis=1, keepdims=True)
    fut_centered = fut_chunks - fut_chunks.mean(axis=1, keepdims=True)
    num = np.einsum("ij,ij->i", sig_centered, fut_centered)
    sig_ss = np.einsum("ij,ij->i", sig_centered, sig_centered)
    fut_ss = np.einsum("ij,ij->i", fut_centered, fut_centered)
    denom = np.sqrt(sig_ss * fut_ss)
    # Where denom < 1e-12, result is nan (consistent with Pearson undefined case)
    zero_mask = denom < 1e-12
    safe_denom = np.where(zero_mask, 1.0, denom)  # avoid divide-by-zero warning
    ic_all = np.where(zero_mask, np.nan, num / safe_denom)

    # Filter non-finite values
    series = ic_all[np.isfinite(ic_all)]
    if series.size == 0:
        return 0.0, 0.0, np.asarray([], dtype=np.float64)

    return float(np.mean(series)), float(np.std(series)), series


def compute_ic_ttest(ic_series: np.ndarray) -> tuple[float, float]:
    """t-test on IC series: H₀ mean IC = 0.

    Returns (t_stat, p_value).  One-sided p-value (H₁: μ > 0).
    """
    from scipy.stats import ttest_1samp

    arr = np.asarray(ic_series, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    if arr.size < 3:
        return 0.0, 1.0
    t_stat, p_two = ttest_1samp(arr, 0.0)
    # One-sided: reject H₀ if mean > 0
    p_one = float(p_two) / 2.0 if float(t_stat) > 0 else 1.0 - float(p_two) / 2.0
    return float(t_stat), p_one


def compute_ic_halflife(signals: np.ndarray, max_lag: int = 50) -> int:
    """Estimate signal half-life via autocorrelation decay.

    Returns the first lag k where autocorrelation drops below 0.5.
    If never drops, returns max_lag.
    """
    arr = np.asarray(signals, dtype=np.float64)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 8:
        return 0
    mean = arr.mean()
    var = float(np.var(arr))
    if var < 1e-15:
        return 0

    # Vectorized autocovariance for all lags at once via np.correlate
    centered = arr - mean
    n_lags = min(max_lag, n // 2 - 1)
    if n_lags < 1:
        return max_lag
    # Full autocorrelation via np.correlate; extract positive lags 1..n_lags
    full_corr = np.correlate(centered, centered, mode="full")
    # full_corr has length 2*n-1; midpoint is lag=0 at index n-1
    # Lags 1..n_lags are at indices n, n+1, ..., n+n_lags-1
    lag_covs = full_corr[n : n + n_lags] / n  # mean covariance (divide by n, matching np.mean)
    acf = lag_covs / var

    # Find first lag where ACF < 0.5
    below = np.where(acf < 0.5)[0]
    if below.size > 0:
        return int(below[0]) + 1  # +1 because lag indices are 0-based (lag=1 is index 0)
    return max_lag


def compute_sortino(equity_curve: Iterable[float], annualization_factor: float = 252.0) -> float:
    """Sortino ratio — penalises downside volatility only."""
    values = np.asarray(list(equity_curve), dtype=np.float64)
    if values.size < 2:
        return 0.0
    base = values[:-1]
    delta = np.diff(values)
    returns = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
    returns = returns[np.isfinite(returns)]
    if returns.size < 2:
        return 0.0
    neg = returns[returns < 0.0]
    downside_std = float(np.std(neg)) if neg.size >= 2 else 1e-9
    if downside_std < 1e-15:
        return 0.0
    return float(np.mean(returns) / downside_std * np.sqrt(annualization_factor))


def compute_cvar(equity_curve: Iterable[float], alpha: float = 0.05) -> float:
    """CVaR (Expected Shortfall) at the given alpha quantile."""
    values = np.asarray(list(equity_curve), dtype=np.float64)
    if values.size < 2:
        return 0.0
    base = values[:-1]
    delta = np.diff(values)
    returns = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
    returns = returns[np.isfinite(returns)]
    if returns.size < 2:
        return 0.0
    cutoff = float(np.quantile(returns, alpha))
    tail = returns[returns <= cutoff]
    if tail.size == 0:
        return cutoff
    return float(np.mean(tail))


def compute_turnover(signals: Iterable[float]) -> float:
    arr = np.asarray(list(signals), dtype=np.float64)
    if arr.size < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(arr))))


def compute_max_drawdown(equity_curve: Iterable[float]) -> float:
    values = np.asarray(list(equity_curve), dtype=np.float64)
    if values.size == 0:
        return 0.0
    peaks = np.maximum.accumulate(values)
    dd = np.divide(values - peaks, peaks, out=np.zeros_like(values), where=peaks != 0)
    return float(np.min(dd))


def compute_capacity(
    signals: Iterable[float],
    volume: Iterable[float],
    participation_limit: float = 0.02,
) -> float:
    sig = np.asarray(list(signals), dtype=np.float64)
    vol = np.asarray(list(volume), dtype=np.float64)
    n = min(sig.size, vol.size)
    if n < 2:
        return 0.0

    sig = sig[:n]
    vol = np.maximum(vol[:n], 1.0)
    trade_demand = np.abs(np.diff(sig, prepend=sig[0]))
    usage_ratio = trade_demand / vol
    p95 = float(np.quantile(usage_ratio, 0.95))
    if p95 <= 0.0:
        return float(participation_limit / 1e-9)
    return float(participation_limit / p95)
