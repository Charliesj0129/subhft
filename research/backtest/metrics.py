from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True, slots=True)
class ICChunkState:
    """Accumulated state for incremental IC computation.

    Stores previously computed per-chunk IC values so that expanding
    the window only requires computing IC for the *new* chunks.
    """

    ic_values: np.ndarray  # 1-D float64 array of per-chunk IC values
    n_samples_used: int  # total samples consumed so far
    chunk_size: int  # chunk width used during computation


@dataclass(frozen=True, slots=True)
class IncrementalMetricsState:
    """Snapshot of intermediate accumulators for ``compute_metrics_incremental``.

    Callers should treat this as opaque — pass it back into
    ``compute_metrics_incremental`` together with the next data slice.
    """

    n: int
    sum_ret: float
    sum_ret_sq: float
    sum_neg_ret_sq: float
    neg_count: int
    peak: float
    max_dd: float
    sum_abs_diff: float
    diff_count: int
    last_signal: float | None
    last_equity: float | None
    ic_state: ICChunkState | None


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


def compute_ic_incremental(
    signals: Iterable[float],
    forward_returns: Iterable[float],
    buckets: int = 20,
    prev_state: ICChunkState | None = None,
) -> tuple[float, float, np.ndarray, ICChunkState | None]:
    """IC computation that reuses prior chunk results when expanding the window.

    When *prev_state* is ``None`` this behaves identically to
    :func:`compute_ic` (plus returning the reusable state as the 4th
    element).  When *prev_state* is supplied, only the new chunks that
    were not present in the previous call are computed, and their IC
    values are appended to the prior results.

    Returns
    -------
    (ic_mean, ic_std, ic_series, state)
        The first three elements match :func:`compute_ic`.
        *state* is an :class:`ICChunkState` that can be passed as
        ``prev_state`` to a subsequent call with a larger window.
    """
    sig = np.asarray(list(signals), dtype=np.float64)
    fut = np.asarray(list(forward_returns), dtype=np.float64)
    n = min(sig.size, fut.size)
    if n < 8:
        return 0.0, 0.0, np.asarray([], dtype=np.float64), None

    sig = sig[:n]
    fut = fut[:n]
    chunk = max(8, n // max(buckets, 1))

    # If prev_state exists but chunk size changed, we cannot reuse.
    if prev_state is not None and prev_state.chunk_size != chunk:
        prev_state = None

    n_chunks = (n - chunk) // chunk + 1
    if n_chunks < 1:
        return 0.0, 0.0, np.asarray([], dtype=np.float64), None

    # Determine how many chunks are already computed.
    start_chunk = 0
    prior_ic: np.ndarray = np.asarray([], dtype=np.float64)
    if prev_state is not None:
        prior_n_chunks = prev_state.ic_values.size
        if prior_n_chunks <= n_chunks:
            start_chunk = prior_n_chunks
            prior_ic = prev_state.ic_values

    if start_chunk < n_chunks:
        new_n_chunks = n_chunks - start_chunk
        offset = start_chunk * chunk
        n_usable_new = new_n_chunks * chunk
        sig_chunks = sig[offset : offset + n_usable_new].reshape(new_n_chunks, chunk)
        fut_chunks = fut[offset : offset + n_usable_new].reshape(new_n_chunks, chunk)

        sig_centered = sig_chunks - sig_chunks.mean(axis=1, keepdims=True)
        fut_centered = fut_chunks - fut_chunks.mean(axis=1, keepdims=True)
        num = np.einsum("ij,ij->i", sig_centered, fut_centered)
        sig_ss = np.einsum("ij,ij->i", sig_centered, sig_centered)
        fut_ss = np.einsum("ij,ij->i", fut_centered, fut_centered)
        denom = np.sqrt(sig_ss * fut_ss)
        zero_mask = denom < 1e-12
        safe_denom = np.where(zero_mask, 1.0, denom)
        new_ic = np.where(zero_mask, np.nan, num / safe_denom)

        ic_all = np.concatenate([prior_ic, new_ic]) if prior_ic.size > 0 else new_ic
    else:
        ic_all = prior_ic

    state = ICChunkState(
        ic_values=ic_all,
        n_samples_used=n,
        chunk_size=chunk,
    )

    series = ic_all[np.isfinite(ic_all)]
    if series.size == 0:
        return 0.0, 0.0, np.asarray([], dtype=np.float64), state

    return float(np.mean(series)), float(np.std(series)), series, state


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


def compute_metrics_incremental(
    prev_state: IncrementalMetricsState | None,
    new_equity: Iterable[float],
    new_signals: Iterable[float] | None = None,
    new_forward_returns: Iterable[float] | None = None,
    annualization_factor: float = 252.0,
    ic_buckets: int = 20,
) -> tuple[dict[str, Any], IncrementalMetricsState]:
    """Compute metrics for an expanded window without reprocessing history.

    This merges the accumulators in *prev_state* with the statistics
    derived from *new_equity* / *new_signals* / *new_forward_returns*
    so that the returned metrics approximate the full-window result.

    Parameters
    ----------
    prev_state:
        State returned by a previous call (or ``None`` for the first
        slice).
    new_equity:
        Equity curve values for the *new* slice only.
    new_signals:
        Signal values for the new slice (needed for turnover and IC).
    new_forward_returns:
        Forward returns for the new slice (needed for IC).
    annualization_factor:
        Passed through to Sharpe / Sortino.
    ic_buckets:
        Bucket count for IC computation.

    Returns
    -------
    (metrics_dict, new_state)
        *metrics_dict* contains ``sharpe``, ``sortino``, ``max_drawdown``,
        ``turnover``, ``ic_mean``, ``ic_std``.
        *new_state* should be passed as ``prev_state`` to the next call.
    """
    eq = np.asarray(list(new_equity), dtype=np.float64)

    # Unpack prior accumulators (or zero-initialise).
    _empty = IncrementalMetricsState(
        n=0, sum_ret=0.0, sum_ret_sq=0.0, sum_neg_ret_sq=0.0,
        neg_count=0, peak=0.0, max_dd=0.0, sum_abs_diff=0.0,
        diff_count=0, last_signal=None, last_equity=None, ic_state=None,
    )
    ps = prev_state if prev_state is not None else _empty
    n_ret = ps.n
    sum_ret = ps.sum_ret
    sum_ret_sq = ps.sum_ret_sq
    sum_neg_ret_sq = ps.sum_neg_ret_sq
    neg_count = ps.neg_count
    peak = ps.peak
    max_dd = ps.max_dd
    sum_abs_diff = ps.sum_abs_diff
    diff_count = ps.diff_count
    last_signal: float | None = ps.last_signal
    last_equity: float | None = ps.last_equity
    ic_state: ICChunkState | None = ps.ic_state

    # Bridge return between prev last equity and first new equity
    if eq.size > 0:
        if last_equity is not None and last_equity != 0.0:
            bridge_ret = (eq[0] - last_equity) / last_equity
            if np.isfinite(bridge_ret):
                sum_ret += bridge_ret
                sum_ret_sq += bridge_ret * bridge_ret
                if bridge_ret < 0.0:
                    sum_neg_ret_sq += bridge_ret * bridge_ret
                    neg_count += 1
                n_ret += 1

        # Returns within new slice
        if eq.size >= 2:
            base = eq[:-1]
            delta = np.diff(eq)
            rets = np.divide(delta, base, out=np.zeros_like(delta), where=base != 0)
            finite_mask = np.isfinite(rets)
            rets_clean = rets[finite_mask]
            sum_ret += float(np.sum(rets_clean))
            sum_ret_sq += float(np.sum(rets_clean * rets_clean))
            neg_rets = rets_clean[rets_clean < 0.0]
            sum_neg_ret_sq += float(np.sum(neg_rets * neg_rets))
            neg_count += int(neg_rets.size)
            n_ret += int(rets_clean.size)

        # Vectorized max drawdown update across new slice
        if peak > 0:
            peaks = np.maximum(peak, np.maximum.accumulate(eq))
        else:
            peaks = np.maximum.accumulate(eq)
        dd = np.divide(eq - peaks, peaks, out=np.zeros_like(eq), where=peaks != 0)
        slice_min_dd = float(np.min(dd)) if dd.size > 0 else 0.0
        max_dd = min(max_dd, slice_min_dd)
        peak = float(peaks[-1]) if peaks.size > 0 else peak

        last_equity = float(eq[-1])

    # --- turnover from signals ---
    sig_arr: np.ndarray | None = None
    if new_signals is not None:
        sig_arr = np.asarray(list(new_signals), dtype=np.float64)
        if sig_arr.size > 0:
            if last_signal is not None:
                diffs = np.empty(sig_arr.size, dtype=np.float64)
                diffs[0] = abs(sig_arr[0] - last_signal)
                if sig_arr.size > 1:
                    diffs[1:] = np.abs(np.diff(sig_arr))
            elif sig_arr.size >= 2:
                diffs = np.abs(np.diff(sig_arr))
            else:
                diffs = np.asarray([], dtype=np.float64)
            sum_abs_diff += float(np.sum(diffs))
            diff_count += int(diffs.size)
            last_signal = float(sig_arr[-1])

    # --- IC incremental (compute only new-slice chunks, merge with prior) ---
    ic_mean = 0.0
    ic_std = 0.0
    if sig_arr is not None and new_forward_returns is not None:
        fwd_arr = np.asarray(list(new_forward_returns), dtype=np.float64)
        ic_mean, ic_std, _, ic_state = compute_ic_incremental(
            sig_arr, fwd_arr, buckets=ic_buckets, prev_state=ic_state,
        )

    # --- derive final metrics ---
    sharpe = 0.0
    sortino = 0.0
    if n_ret >= 2:
        mean_ret = sum_ret / n_ret
        var_ret = sum_ret_sq / n_ret - mean_ret * mean_ret
        std_ret = var_ret ** 0.5 if var_ret > 0 else 0.0
        if std_ret > 0:
            sharpe = mean_ret / std_ret * (annualization_factor ** 0.5)
        if neg_count >= 2:
            ds_var = sum_neg_ret_sq / neg_count
            ds_std = ds_var ** 0.5 if ds_var > 0 else 0.0
            if ds_std > 1e-15:
                sortino = mean_ret / ds_std * (annualization_factor ** 0.5)

    turnover = (sum_abs_diff / diff_count) if diff_count > 0 else 0.0

    state = IncrementalMetricsState(
        n=n_ret,
        sum_ret=sum_ret,
        sum_ret_sq=sum_ret_sq,
        sum_neg_ret_sq=sum_neg_ret_sq,
        neg_count=neg_count,
        peak=peak,
        max_dd=max_dd,
        sum_abs_diff=sum_abs_diff,
        diff_count=diff_count,
        last_signal=last_signal,
        last_equity=last_equity,
        ic_state=ic_state,
    )

    return {
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "turnover": turnover,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
    }, state
