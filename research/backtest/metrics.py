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


def _pearson(s: np.ndarray, r: np.ndarray) -> float:
    """Single-pass Pearson correlation — avoids np.corrcoef 2×n matrix allocation."""
    sx = s - s.mean()
    rx = r - r.mean()
    denom = float(np.sqrt((sx @ sx) * (rx @ rx)))
    if denom < 1e-12:
        return float("nan")
    return float(sx @ rx) / denom


def compute_ic(signals: Iterable[float], forward_returns: Iterable[float], buckets: int = 20) -> tuple[float, float, np.ndarray]:
    sig = np.asarray(list(signals), dtype=np.float64)
    fut = np.asarray(list(forward_returns), dtype=np.float64)
    n = min(sig.size, fut.size)
    if n < 8:
        return 0.0, 0.0, np.asarray([], dtype=np.float64)

    sig = sig[:n]
    fut = fut[:n]
    chunk = max(8, n // max(buckets, 1))
    ic_values: list[float] = []
    for start in range(0, n - chunk + 1, chunk):
        s = sig[start : start + chunk]
        r = fut[start : start + chunk]
        corr = _pearson(s, r)
        if np.isfinite(corr):
            ic_values.append(corr)

    if not ic_values:
        return 0.0, 0.0, np.asarray([], dtype=np.float64)

    series = np.asarray(ic_values, dtype=np.float64)
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
    for lag in range(1, min(max_lag + 1, n // 2)):
        cov = float(np.mean((arr[:-lag] - mean) * (arr[lag:] - mean)))
        acf = cov / var
        if acf < 0.5:
            return lag
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
