"""MLOFI Microprice Correction — IC Decay Curve & Regression Analysis.

Stage 2 prototype for Candidate A: mlofi_microprice_correction.

Computes:
  1. MLOFI_integrated per tick (geometrically weighted multi-level OFI)
  2. IC decay curve at {250ms, 500ms, 1s, 2s, 5s, 10s, 30s, 60s}
  3. Regression coefficients (alpha, lambda, R-squared) per day
  4. Incremental IC (correction term vs L1 baseline)

Kill gate: If pooled IC(30s) < 0.015 on TXFD6, TERMINATE.

Usage:
    python -m research.alphas.mlofi_microprice.backtest_ic
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TW_TZ = timezone(timedelta(hours=8))
HORIZONS_MS = [250, 500, 1_000, 2_000, 5_000, 10_000, 30_000, 60_000]
HORIZONS_NS = [h * 1_000_000 for h in HORIZONS_MS]
LAMBDA_DEFAULT = 0.5
EMA_ALPHA = 1.0 - np.exp(-1.0 / 8.0)  # EMA window=8, matching FeatureEngine
N_LEVELS = 5
WARMUP_TICKS = 64
IC_KILL_GATE_30S = 0.015

# Regular trading hours (Taiwan futures): 08:45 - 13:45
MARKET_OPEN_H, MARKET_OPEN_M = 8, 45
MARKET_CLOSE_H, MARKET_CLOSE_M = 13, 45

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "l5_v2"


# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_l5_data(symbol: str) -> np.ndarray:
    """Load L5 numpy structured array for a symbol."""
    fname_map = {"TXFD6": "TXFE6_l5.npy", "TXFE6": "TXFE6_l5.npy", "2330": "2330_l5.npy"}
    fname = fname_map.get(symbol, f"{symbol}_l5.npy")
    path = DATA_DIR / fname
    if not path.exists():
        raise FileNotFoundError(f"L5 data not found: {path}")
    return np.load(str(path), mmap_mode="r")


def split_by_day(data: np.ndarray, dates: list[str]) -> dict[str, np.ndarray]:
    """Split data into per-day arrays using regular trading hours."""
    ts = data["timestamp_ns"]
    result: dict[str, np.ndarray] = {}
    for date_str in dates:
        y, m, d = map(int, date_str.split("-"))
        start = datetime(y, m, d, MARKET_OPEN_H, MARKET_OPEN_M, tzinfo=TW_TZ)
        end = datetime(y, m, d, MARKET_CLOSE_H, MARKET_CLOSE_M, tzinfo=TW_TZ)
        start_ns = int(start.timestamp() * 1e9)
        end_ns = int(end.timestamp() * 1e9)
        mask = (ts >= start_ns) & (ts < end_ns)
        day_data = data[mask]
        if len(day_data) > 100:  # skip tiny days
            result[date_str] = day_data
    return result


# ---------------------------------------------------------------------------
# MLOFI computation
# ---------------------------------------------------------------------------

def compute_mlofi_integrated(
    bids_vol: np.ndarray,  # shape (N, 5)
    asks_vol: np.ndarray,  # shape (N, 5)
    bids_price: np.ndarray,  # shape (N, 5)
    asks_price: np.ndarray,  # shape (N, 5)
    lam: float = LAMBDA_DEFAULT,
) -> np.ndarray:
    """Compute MLOFI_integrated per tick with EMA smoothing.

    Returns array of shape (N,) with MLOFI_integrated values.
    Includes BBO-shift guard: zero MLOFI when best bid/ask price changes.
    """
    n = len(bids_vol)
    # Geometric weights: w_k = lambda^(k-1) for k=1..5
    weights = np.array([lam ** k for k in range(N_LEVELS)], dtype=np.float64)

    # Delta quantities per level
    delta_bid = np.diff(bids_vol.astype(np.float64), axis=0)  # (N-1, 5)
    delta_ask = np.diff(asks_vol.astype(np.float64), axis=0)  # (N-1, 5)

    # BBO-shift guard: zero MLOFI when best bid or ask price changes
    bbo_shift = (
        (bids_price[1:, 0] != bids_price[:-1, 0])
        | (asks_price[1:, 0] != asks_price[:-1, 0])
    )

    # Per-level OFI
    ofi_per_level = delta_bid - delta_ask  # (N-1, 5)

    # Zero out levels where the specific level price changed (level shift)
    for k in range(N_LEVELS):
        level_shift = (
            (bids_price[1:, k] != bids_price[:-1, k])
            | (asks_price[1:, k] != asks_price[:-1, k])
        )
        ofi_per_level[level_shift, k] = 0.0

    # Zero all levels when BBO shifts (conservative approach)
    ofi_per_level[bbo_shift, :] = 0.0

    # Weighted sum
    mlofi_raw = ofi_per_level @ weights  # (N-1,)

    # EMA smoothing
    mlofi_ema = np.empty(n, dtype=np.float64)
    mlofi_ema[0] = 0.0
    val = 0.0
    for i in range(n - 1):
        raw = mlofi_raw[i]
        val += EMA_ALPHA * (raw - val)
        mlofi_ema[i + 1] = val

    # Zero warmup
    mlofi_ema[:WARMUP_TICKS] = 0.0

    return mlofi_ema


def compute_mid_price(bids_price: np.ndarray, asks_price: np.ndarray) -> np.ndarray:
    """Compute mid price from L1 bid/ask. Returns float64 array."""
    bid1 = bids_price[:, 0].astype(np.float64)
    ask1 = asks_price[:, 0].astype(np.float64)
    return (bid1 + ask1) / 2.0


def compute_weighted_mid(
    bids_price: np.ndarray,
    bids_vol: np.ndarray,
    asks_price: np.ndarray,
    asks_vol: np.ndarray,
) -> np.ndarray:
    """Compute L1 weighted microprice: (Qb*Pa + Qa*Pb) / (Qb + Qa)."""
    pb = bids_price[:, 0].astype(np.float64)
    pa = asks_price[:, 0].astype(np.float64)
    qb = bids_vol[:, 0].astype(np.float64)
    qa = asks_vol[:, 0].astype(np.float64)
    denom = qb + qa
    # Avoid division by zero
    safe_denom = np.where(denom > 0, denom, 1.0)
    return np.where(denom > 0, (qb * pa + qa * pb) / safe_denom, (pb + pa) / 2.0)


# ---------------------------------------------------------------------------
# Forward return computation
# ---------------------------------------------------------------------------

def compute_forward_returns(
    ts_ns: np.ndarray,
    mid: np.ndarray,
    horizons_ns: list[int],
) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons using nearest-tick lookup.

    Returns dict mapping horizon_ns -> forward_return array of shape (N,).
    NaN where horizon extends beyond data.
    """
    n = len(ts_ns)
    result: dict[int, np.ndarray] = {}

    for h_ns in horizons_ns:
        fwd = np.full(n, np.nan, dtype=np.float64)
        target_ts = ts_ns + h_ns
        # Use searchsorted for efficient lookup
        future_idx_raw = np.searchsorted(ts_ns, target_ts, side="left")
        # Compute valid BEFORE clipping (after clip, all < n is trivially True)
        valid = future_idx_raw < n
        # Clamp to valid range for safe indexing
        future_idx = np.clip(future_idx_raw, 0, n - 1)
        fwd[valid] = mid[future_idx[valid]] - mid[valid]
        # Mark as NaN if the actual timestamp is too far from target
        actual_ts = ts_ns[future_idx[valid]]
        # Allow up to 2x horizon tolerance for sparse data
        too_far = np.abs(actual_ts - target_ts[valid]) > h_ns
        fwd_valid_indices = np.where(valid)[0]
        fwd[fwd_valid_indices[too_far]] = np.nan
        result[h_ns] = fwd

    return result


# ---------------------------------------------------------------------------
# IC computation
# ---------------------------------------------------------------------------

def spearman_ic(signal: np.ndarray, returns: np.ndarray) -> tuple[float, int]:
    """Compute Spearman rank IC between signal and returns.

    Returns (ic, n_valid). Skips NaN and warmup zeros.
    """
    valid = np.isfinite(signal) & np.isfinite(returns) & (signal != 0.0)
    n_valid = int(np.sum(valid))
    if n_valid < 30:
        return np.nan, n_valid
    ic, _ = sp_stats.spearmanr(signal[valid], returns[valid])
    return float(ic), n_valid


def newey_west_tstat(daily_ics: list[float], max_lag: int | None = None) -> float:
    """Compute Newey-West t-statistic for a series of daily ICs."""
    ics = np.array([x for x in daily_ics if np.isfinite(x)])
    n = len(ics)
    if n < 3:
        return np.nan
    mean_ic = np.mean(ics)
    if max_lag is None:
        max_lag = int(np.floor(n ** (1 / 3)))

    # Autocovariance
    gamma = np.zeros(max_lag + 1)
    demeaned = ics - mean_ic
    for j in range(max_lag + 1):
        gamma[j] = np.mean(demeaned[: n - j] * demeaned[j:])

    # Newey-West variance
    var_nw = gamma[0]
    for j in range(1, max_lag + 1):
        w = 1.0 - j / (max_lag + 1)
        var_nw += 2 * w * gamma[j]

    se = np.sqrt(var_nw / n)
    if se < 1e-15:
        return np.nan
    return mean_ic / se


# ---------------------------------------------------------------------------
# Regression
# ---------------------------------------------------------------------------

def daily_regression(
    mlofi: np.ndarray,
    mid: np.ndarray,
) -> tuple[float, float, float]:
    """Fit delta_mid = alpha * MLOFI + epsilon via OLS.

    Returns (alpha, intercept, r_squared).
    """
    delta_mid = np.diff(mid)
    signal = mlofi[1:]  # align with diff

    valid = np.isfinite(signal) & np.isfinite(delta_mid) & (signal != 0.0)
    n = int(np.sum(valid))
    if n < 100:
        return np.nan, np.nan, np.nan

    x = signal[valid]
    y = delta_mid[valid]

    # OLS
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    cov_xy = np.mean((x - x_mean) * (y - y_mean))
    var_x = np.mean((x - x_mean) ** 2)

    if var_x < 1e-15:
        return np.nan, np.nan, np.nan

    alpha = cov_xy / var_x
    intercept = y_mean - alpha * x_mean

    y_hat = alpha * x + intercept
    ss_res = np.sum((y - y_hat) ** 2)
    ss_tot = np.sum((y - y_mean) ** 2)
    r_sq = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return float(alpha), float(intercept), float(r_sq)


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def analyze_asset(
    symbol: str,
    dates: list[str],
    lam: float = LAMBDA_DEFAULT,
) -> dict:
    """Run full IC decay + regression analysis for one asset."""
    print(f"\n{'='*70}")
    print(f"Analyzing {symbol} (lambda={lam})")
    print(f"{'='*70}")

    data = load_l5_data(symbol)
    days = split_by_day(data, dates)
    print(f"Loaded {len(data):,} total rows, {len(days)} trading days")

    # Per-day IC storage
    daily_ics: dict[int, list[float]] = {h: [] for h in HORIZONS_NS}
    daily_alphas: list[float] = []
    daily_r2: list[float] = []
    daily_r2_l1: list[float] = []
    daily_incremental_ics: dict[int, list[float]] = {h: [] for h in HORIZONS_NS}
    daily_dates: list[str] = []

    # Process most recent days first (per feedback_backtest_recency_bias)
    sorted_dates = sorted(days.keys(), reverse=True)

    for date_str in sorted_dates:
        day_data = days[date_str]
        n = len(day_data)
        daily_dates.append(date_str)

        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]

        # Check for zero prices (incomplete L5 data)
        l5_valid = np.sum((bids_price[:, 4] > 0) & (asks_price[:, 4] > 0))
        l5_coverage = l5_valid / n * 100

        mid = compute_mid_price(bids_price, asks_price)
        wmid = compute_weighted_mid(bids_price, bids_vol, asks_price, asks_vol)

        mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)

        fwd_rets = compute_forward_returns(ts_ns, mid, HORIZONS_NS)

        print(f"\n--- {date_str} ({n:,} rows, L5 coverage={l5_coverage:.1f}%) ---")

        # IC at each horizon
        for h_ns in HORIZONS_NS:
            ic, n_valid = spearman_ic(mlofi, fwd_rets[h_ns])
            daily_ics[h_ns].append(ic)
            h_ms = h_ns // 1_000_000
            print(f"  IC({h_ms:>5}ms): {ic:+.4f}  (n={n_valid:,})")

        # Regression
        alpha_coef, intercept, r_sq = daily_regression(mlofi, mid)
        daily_alphas.append(alpha_coef)
        daily_r2.append(r_sq)
        print(f"  Regression: alpha={alpha_coef:+.4f}, R2={r_sq:.6f}")

        # L1-only regression (weighted mid prediction)
        l1_imbalance = compute_l1_imbalance(bids_vol, asks_vol)
        _, _, r_sq_l1 = daily_regression(l1_imbalance, mid)
        daily_r2_l1.append(r_sq_l1)
        print(f"  L1-only R2={r_sq_l1:.6f}")

        # Incremental IC: correction term vs residual returns
        if np.isfinite(alpha_coef):
            correction = alpha_coef * mlofi
            l1_pred = compute_l1_microprice_prediction(bids_price, bids_vol, asks_price, asks_vol, mid)
            for h_ns in HORIZONS_NS:
                residual_ret = fwd_rets[h_ns] - l1_pred
                ic_incr, _ = spearman_ic(correction, residual_ret)
                daily_incremental_ics[h_ns].append(ic_incr)
        else:
            for h_ns in HORIZONS_NS:
                daily_incremental_ics[h_ns].append(np.nan)

    # Pooled results
    print(f"\n{'='*70}")
    print(f"POOLED RESULTS — {symbol}")
    print(f"{'='*70}")

    print("\nIC Decay Curve (pooled mean +/- std, NW t-stat):")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'Std IC':>10} {'NW t':>8} {'N days':>8}")
    pooled_ics: dict[int, float] = {}
    for h_ns in HORIZONS_NS:
        ics = daily_ics[h_ns]
        valid_ics = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid_ics) if valid_ics else np.nan
        std_ic = np.std(valid_ics) if len(valid_ics) > 1 else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        pooled_ics[h_ns] = mean_ic
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {std_ic:>10.4f} {t_stat:>8.2f} {len(valid_ics):>8}")

    # Kill gate
    ic_30s = pooled_ics.get(30_000_000_000, np.nan)
    print(f"\n*** KILL GATE: IC(30s) = {ic_30s:+.4f} (threshold={IC_KILL_GATE_30S}) ***")
    if np.isfinite(ic_30s) and abs(ic_30s) < IC_KILL_GATE_30S:
        print("*** RESULT: FAIL — IC(30s) below kill gate. TERMINATE direction. ***")
    elif np.isfinite(ic_30s):
        print(f"*** RESULT: PASS — |IC(30s)| = {abs(ic_30s):.4f} >= {IC_KILL_GATE_30S} ***")
    else:
        print("*** RESULT: INCONCLUSIVE — IC(30s) is NaN ***")

    # Regression stability
    valid_alphas = [a for a in daily_alphas if np.isfinite(a)]
    if len(valid_alphas) >= 2:
        mean_alpha = np.mean(valid_alphas)
        std_alpha = np.std(valid_alphas)
        cv_alpha = abs(std_alpha / mean_alpha) * 100 if abs(mean_alpha) > 1e-15 else float("inf")
        print(f"\nRegression alpha: mean={mean_alpha:+.4f}, std={std_alpha:.4f}, CV={cv_alpha:.1f}%")
        if cv_alpha > 50:
            print("  *** WARNING: CV(alpha) > 50% — coefficient instability flagged ***")

        # R-squared improvement
        valid_r2 = [r for r in daily_r2 if np.isfinite(r)]
        valid_r2_l1 = [r for r in daily_r2_l1 if np.isfinite(r)]
        if valid_r2 and valid_r2_l1:
            mean_r2 = np.mean(valid_r2)
            mean_r2_l1 = np.mean(valid_r2_l1)
            improvement = (mean_r2 - mean_r2_l1) / mean_r2_l1 * 100 if mean_r2_l1 > 0 else float("inf")
            print(f"  MLOFI R2={mean_r2:.6f}, L1-only R2={mean_r2_l1:.6f}, improvement={improvement:+.1f}%")

    # Incremental IC
    print("\nIncremental IC (MLOFI correction term vs residual after L1):")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8}")
    for h_ns in HORIZONS_NS:
        ics = daily_incremental_ics[h_ns]
        valid_ics = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid_ics) if valid_ics else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f}")

    # TWSE sign check
    valid_alphas_arr = np.array(valid_alphas)
    neg_count = np.sum(valid_alphas_arr < 0)
    print(f"\nTWSE sign check: {neg_count}/{len(valid_alphas)} days have alpha < 0 (expected: CONTRARIAN / negative)")

    return {
        "symbol": symbol,
        "daily_ics": daily_ics,
        "pooled_ics": pooled_ics,
        "daily_alphas": daily_alphas,
        "daily_r2": daily_r2,
        "daily_r2_l1": daily_r2_l1,
        "daily_incremental_ics": daily_incremental_ics,
        "daily_dates": daily_dates,
    }


def compute_l1_imbalance(
    bids_vol: np.ndarray,
    asks_vol: np.ndarray,
) -> np.ndarray:
    """Compute L1 order imbalance with EMA smoothing (same as MLOFI pipeline)."""
    n = len(bids_vol)
    qb = bids_vol[:, 0].astype(np.float64)
    qa = asks_vol[:, 0].astype(np.float64)
    denom = qb + qa
    safe_denom = np.where(denom > 0, denom, 1.0)
    imb_raw = np.where(denom > 0, (qb - qa) / safe_denom, 0.0)

    # EMA smooth
    imb_ema = np.empty(n, dtype=np.float64)
    val = 0.0
    for i in range(n):
        val += EMA_ALPHA * (imb_raw[i] - val)
        imb_ema[i] = val
    imb_ema[:WARMUP_TICKS] = 0.0
    return imb_ema


def compute_l1_microprice_prediction(
    bids_price: np.ndarray,
    bids_vol: np.ndarray,
    asks_price: np.ndarray,
    asks_vol: np.ndarray,
    mid: np.ndarray,
) -> np.ndarray:
    """Compute L1 microprice - mid price (the L1 correction term).

    This is used to compute residual returns for incremental IC analysis.
    """
    wmid = compute_weighted_mid(bids_price, bids_vol, asks_price, asks_vol)
    return wmid - mid


# ---------------------------------------------------------------------------
# Lambda grid search
# ---------------------------------------------------------------------------

def lambda_search(
    symbol: str,
    dates: list[str],
    lambdas: list[float] | None = None,
    target_horizon_ns: int = 5_000_000_000,  # 5s default
) -> None:
    """Grid search over lambda values for optimal MLOFI weighting."""
    if lambdas is None:
        lambdas = [0.3, 0.4, 0.5, 0.6, 0.7]

    data = load_l5_data(symbol)
    days = split_by_day(data, dates)

    print(f"\nLambda grid search for {symbol} at horizon={target_horizon_ns // 1_000_000}ms")
    print(f"{'Lambda':>8} {'Mean IC':>10} {'NW t':>8}")

    for lam in lambdas:
        day_ics: list[float] = []
        for date_str in sorted(days.keys()):
            day_data = days[date_str]
            bids_price = day_data["bids_price"]
            bids_vol = day_data["bids_vol"]
            asks_price = day_data["asks_price"]
            asks_vol = day_data["asks_vol"]
            ts_ns = day_data["timestamp_ns"]
            mid = compute_mid_price(bids_price, asks_price)
            mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)
            fwd = compute_forward_returns(ts_ns, mid, [target_horizon_ns])
            ic, _ = spearman_ic(mlofi, fwd[target_horizon_ns])
            day_ics.append(ic)

        valid = [x for x in day_ics if np.isfinite(x)]
        mean_ic = np.mean(valid) if valid else np.nan
        t = newey_west_tstat(day_ics)
        print(f"  {lam:>6.2f} {mean_ic:>+10.4f} {t:>8.2f}")


# ---------------------------------------------------------------------------
# Realized spread per fill analysis
# ---------------------------------------------------------------------------

def compute_realized_spread_per_fill(
    ts_ns: np.ndarray,
    mid: np.ndarray,
    mlofi: np.ndarray,
    threshold: float = 0.5,
    horizon_ns: int = 30_000_000_000,  # 30s
) -> dict:
    """Compute realized spread per fill using MLOFI-adjusted microprice.

    'Fill' is simulated: when |MLOFI| > threshold, assume we would quote.
    Realized spread = price_at_fill - mid_price(t + 30s).
    Positive = maker captured spread, negative = adverse selection.
    """
    n = len(mlofi)
    valid_signals = np.abs(mlofi) > threshold
    valid_signals[:WARMUP_TICKS] = False

    target_ts = ts_ns + horizon_ns
    future_idx_raw = np.searchsorted(ts_ns, target_ts, side="left")
    has_future = future_idx_raw < n
    future_idx = np.clip(future_idx_raw, 0, n - 1)

    fill_mask = valid_signals & has_future
    if np.sum(fill_mask) < 10:
        return {"n_fills": 0, "mean_rs": np.nan, "std_rs": np.nan}

    mid_at_fill = mid[fill_mask]
    mid_future = mid[future_idx[fill_mask]]
    direction = np.sign(mlofi[fill_mask])  # positive MLOFI = BUY (pro-cyclical)

    # Pro-cyclical: positive MLOFI = buy, negative = sell
    # Realized spread for buyer: mid_future - mid_at_fill (profit if price goes up)
    # Realized spread for seller: mid_at_fill - mid_future (profit if price goes down)
    realized_spread = direction * (mid_future - mid_at_fill)

    return {
        "n_fills": int(np.sum(fill_mask)),
        "mean_rs": float(np.mean(realized_spread)),
        "std_rs": float(np.std(realized_spread)),
        "median_rs": float(np.median(realized_spread)),
    }


# ---------------------------------------------------------------------------
# Challenge 1: Non-overlapping IC
# ---------------------------------------------------------------------------

def spearman_ic_nonoverlap(
    signal: np.ndarray,
    ts_ns: np.ndarray,
    mid: np.ndarray,
    horizon_ns: int,
) -> tuple[float, int]:
    """Compute Spearman IC on non-overlapping return windows.

    Subsamples every `horizon_ns` nanoseconds so that forward returns do not
    overlap.  This removes autocorrelation inflation that plagues tick-level IC.

    Returns (ic, n_valid).
    """
    n = len(ts_ns)
    if n < 100:
        return np.nan, 0

    # Build non-overlapping index: start from first valid tick, step by horizon
    indices: list[int] = []
    next_ts = ts_ns[WARMUP_TICKS] if WARMUP_TICKS < n else ts_ns[0]
    i = WARMUP_TICKS
    while i < n:
        if ts_ns[i] >= next_ts:
            indices.append(i)
            next_ts = ts_ns[i] + horizon_ns
        i += 1

    if len(indices) < 30:
        return np.nan, len(indices)

    idx = np.array(indices)
    sig = signal[idx]

    # Compute forward return at each sampled point
    target_ts = ts_ns[idx] + horizon_ns
    future_idx_raw = np.searchsorted(ts_ns, target_ts, side="left")
    valid = future_idx_raw < n
    future_idx = np.clip(future_idx_raw, 0, n - 1)

    fwd = np.full(len(idx), np.nan, dtype=np.float64)
    fwd[valid] = mid[future_idx[valid]] - mid[idx[valid]]

    # Filter tolerance: actual timestamp within 2x horizon
    actual_ts = ts_ns[future_idx[valid]]
    too_far = np.abs(actual_ts - target_ts[valid]) > horizon_ns
    valid_indices = np.where(valid)[0]
    fwd[valid_indices[too_far]] = np.nan

    # Final filter
    ok = np.isfinite(sig) & np.isfinite(fwd) & (sig != 0.0)
    n_ok = int(np.sum(ok))
    if n_ok < 30:
        return np.nan, n_ok

    ic, _ = sp_stats.spearmanr(sig[ok], fwd[ok])
    return float(ic), n_ok


# ---------------------------------------------------------------------------
# Challenge 2: Daily trend decontamination
# ---------------------------------------------------------------------------

def compute_ic_detrended(
    signal: np.ndarray,
    fwd_ret: np.ndarray,
    ts_ns: np.ndarray | None = None,
    window_s: int = 300,
) -> tuple[float, int]:
    """Compute Spearman IC after removing local trend from forward returns.

    Subtracting a constant (day mean) from returns does not change Spearman
    rank correlation.  Instead, we subtract a rolling 5-minute local mean of
    forward returns so that the *ranks* change: ticks that merely follow the
    local drift are devalued relative to ticks with genuine signal.

    If ts_ns is None, falls back to index-based windowing (every N ticks).
    """
    n = len(signal)
    valid_mask = np.isfinite(fwd_ret) & np.isfinite(signal) & (signal != 0.0)
    n_valid = int(np.sum(valid_mask))
    if n_valid < 30:
        return np.nan, n_valid

    detrended = fwd_ret.copy()

    if ts_ns is not None:
        # Time-based windowing: subtract rolling local mean in `window_s` windows
        window_ns = int(window_s * 1_000_000_000)
        # For efficiency, use bin-based approach
        t0 = ts_ns[0]
        bin_idx = ((ts_ns - t0) // window_ns).astype(np.int64)
        n_bins = int(bin_idx[-1]) + 1 if len(bin_idx) > 0 else 0

        for b in range(n_bins):
            mask_bin = bin_idx == b
            combined = mask_bin & valid_mask
            if np.sum(combined) > 1:
                local_mean = np.nanmean(fwd_ret[combined])
                detrended[combined] -= local_mean
    else:
        # Index-based fallback: ~600 tick window
        win = max(100, n // 50)
        for start in range(0, n, win):
            end = min(start + win, n)
            chunk_mask = np.zeros(n, dtype=bool)
            chunk_mask[start:end] = True
            combined = chunk_mask & valid_mask
            if np.sum(combined) > 1:
                local_mean = np.nanmean(fwd_ret[combined])
                detrended[combined] -= local_mean

    ok = np.isfinite(signal) & np.isfinite(detrended) & (signal != 0.0)
    n_ok = int(np.sum(ok))
    if n_ok < 30:
        return np.nan, n_ok

    ic, _ = sp_stats.spearmanr(signal[ok], detrended[ok])
    return float(ic), n_ok


# ---------------------------------------------------------------------------
# Challenge 3: L1-only vs L2-L5-only MLOFI
# ---------------------------------------------------------------------------

def compute_mlofi_l1_only(
    bids_vol: np.ndarray,
    asks_vol: np.ndarray,
    bids_price: np.ndarray,
    asks_price: np.ndarray,
) -> np.ndarray:
    """Compute MLOFI using L1 only (weight=[1,0,0,0,0])."""
    return compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=0.0)


def compute_mlofi_deep_only(
    bids_vol: np.ndarray,
    asks_vol: np.ndarray,
    bids_price: np.ndarray,
    asks_price: np.ndarray,
    lam: float = LAMBDA_DEFAULT,
) -> np.ndarray:
    """Compute MLOFI using L2-L5 only (zero L1 weight).

    weights = [0, lam, lam^2, lam^3, lam^4]
    """
    n = len(bids_vol)
    weights = np.array([0.0] + [lam ** k for k in range(1, N_LEVELS)], dtype=np.float64)

    delta_bid = np.diff(bids_vol.astype(np.float64), axis=0)
    delta_ask = np.diff(asks_vol.astype(np.float64), axis=0)

    bbo_shift = (
        (bids_price[1:, 0] != bids_price[:-1, 0])
        | (asks_price[1:, 0] != asks_price[:-1, 0])
    )

    ofi_per_level = delta_bid - delta_ask
    for k in range(N_LEVELS):
        level_shift = (
            (bids_price[1:, k] != bids_price[:-1, k])
            | (asks_price[1:, k] != asks_price[:-1, k])
        )
        ofi_per_level[level_shift, k] = 0.0
    ofi_per_level[bbo_shift, :] = 0.0

    mlofi_raw = ofi_per_level @ weights

    mlofi_ema = np.empty(n, dtype=np.float64)
    mlofi_ema[0] = 0.0
    val = 0.0
    for i in range(n - 1):
        val += EMA_ALPHA * (mlofi_raw[i] - val)
        mlofi_ema[i + 1] = val
    mlofi_ema[:WARMUP_TICKS] = 0.0

    return mlofi_ema


# ---------------------------------------------------------------------------
# Challenge 4: OOS incremental IC (rolling day-behind alpha)
# ---------------------------------------------------------------------------

def compute_oos_incremental_ic(
    days_sorted: list[str],
    day_data_map: dict[str, np.ndarray],
    lam: float = LAMBDA_DEFAULT,
    horizons_ns: list[int] | None = None,
) -> dict[int, list[tuple[str, float, int]]]:
    """Compute incremental IC using rolling OOS alpha coefficient.

    For day t, use day t-1's regression alpha. Skip day 0.
    Returns dict of horizon -> list of (date, ic, n_valid).
    """
    if horizons_ns is None:
        horizons_ns = [5_000_000_000, 30_000_000_000]

    result: dict[int, list[tuple[str, float, int]]] = {h: [] for h in horizons_ns}
    prev_alpha: float | None = None

    # Process in chronological order for rolling
    for date_str in days_sorted:
        day_data = day_data_map[date_str]
        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]

        mid = compute_mid_price(bids_price, asks_price)
        mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)

        # Today's regression alpha (for next day's OOS use)
        today_alpha, _, _ = daily_regression(mlofi, mid)

        if prev_alpha is not None and np.isfinite(prev_alpha):
            # OOS: use yesterday's alpha
            correction = prev_alpha * mlofi
            l1_pred = compute_l1_microprice_prediction(bids_price, bids_vol, asks_price, asks_vol, mid)
            fwd_rets = compute_forward_returns(ts_ns, mid, horizons_ns)

            for h_ns in horizons_ns:
                residual_ret = fwd_rets[h_ns] - l1_pred
                ic_incr, n_valid = spearman_ic(correction, residual_ret)
                result[h_ns].append((date_str, float(ic_incr) if np.isfinite(ic_incr) else np.nan, n_valid))
        else:
            for h_ns in horizons_ns:
                result[h_ns].append((date_str, np.nan, 0))

        prev_alpha = today_alpha

    return result


# ---------------------------------------------------------------------------
# Challenge 5: Conditional TXFD6 IC (L5 coverage filter)
# ---------------------------------------------------------------------------

def analyze_conditional_l5(
    day_data: np.ndarray,
    lam: float = LAMBDA_DEFAULT,
    horizons_ns: list[int] | None = None,
    coverage_threshold: float = 0.8,
) -> dict[int, tuple[float, int, int]]:
    """Compute IC on TXFD6 subset with high L5 coverage.

    Filters ticks where L5 bid and ask prices are all > 0.
    Returns dict of horizon -> (ic, n_valid, n_total_in_subset).
    """
    if horizons_ns is None:
        horizons_ns = [5_000_000_000, 30_000_000_000]

    bids_price = day_data["bids_price"]
    asks_price = day_data["asks_price"]
    bids_vol = day_data["bids_vol"]
    asks_vol = day_data["asks_vol"]
    ts_ns = day_data["timestamp_ns"]

    # L5 coverage: all 5 levels have non-zero prices on both sides
    l5_bid_ok = np.all(bids_price > 0, axis=1)
    l5_ask_ok = np.all(asks_price > 0, axis=1)
    l5_mask = l5_bid_ok & l5_ask_ok

    n_total = len(day_data)
    n_l5 = int(np.sum(l5_mask))

    if n_l5 < 100:
        return {h: (np.nan, 0, n_l5) for h in horizons_ns}

    # Compute MLOFI on full data first (EMA needs continuity)
    mid = compute_mid_price(bids_price, asks_price)
    mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)
    fwd_rets = compute_forward_returns(ts_ns, mid, horizons_ns)

    result: dict[int, tuple[float, int, int]] = {}
    for h_ns in horizons_ns:
        sig = mlofi[l5_mask]
        ret = fwd_rets[h_ns][l5_mask]
        ic, n_valid = spearman_ic(sig, ret)
        result[h_ns] = (ic, n_valid, n_l5)

    return result


# ---------------------------------------------------------------------------
# Stage 2b extended analysis runner
# ---------------------------------------------------------------------------

def run_stage2b_analysis(symbol: str, dates: list[str], lam: float = LAMBDA_DEFAULT) -> dict:
    """Run all Stage 2b analyses for a symbol.

    Returns dict with all results for artifact generation.
    """
    print(f"\n{'='*70}")
    print(f"STAGE 2b EXTENDED ANALYSIS — {symbol}")
    print(f"{'='*70}")

    data = load_l5_data(symbol)
    days = split_by_day(data, dates)
    sorted_dates = sorted(days.keys(), reverse=True)  # most recent first
    chrono_dates = sorted(days.keys())  # chronological for OOS
    print(f"Loaded {len(data):,} rows, {len(days)} trading days")

    results: dict = {"symbol": symbol, "n_days": len(days)}

    # ---- Challenge 1: Non-overlapping IC ----
    print(f"\n--- Challenge 1: Non-Overlapping IC ---")
    nonoverlap_horizons = [30_000_000_000, 60_000_000_000]  # 30s, 60s
    daily_nonoverlap_ics: dict[int, list[float]] = {h: [] for h in nonoverlap_horizons}
    daily_nonoverlap_detail: dict[int, list[tuple[str, float, int]]] = {h: [] for h in nonoverlap_horizons}

    for date_str in sorted_dates:
        day_data = days[date_str]
        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]
        mid = compute_mid_price(bids_price, asks_price)
        mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)

        for h_ns in nonoverlap_horizons:
            ic, n_valid = spearman_ic_nonoverlap(mlofi, ts_ns, mid, h_ns)
            daily_nonoverlap_ics[h_ns].append(ic)
            daily_nonoverlap_detail[h_ns].append((date_str, ic, n_valid))
            h_ms = h_ns // 1_000_000
            print(f"  {date_str} IC_nonoverlap({h_ms}ms): {ic:+.4f}  (n={n_valid})")

    print(f"\nPooled Non-Overlapping IC:")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8} {'N days':>8}")
    results["nonoverlap_ics"] = {}
    for h_ns in nonoverlap_horizons:
        ics = daily_nonoverlap_ics[h_ns]
        valid = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid) if valid else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f} {len(valid):>8}")
        results["nonoverlap_ics"][h_ms] = {"mean": float(mean_ic), "nw_t": float(t_stat), "n_days": len(valid)}

    results["nonoverlap_detail"] = {
        h // 1_000_000: [(d, float(ic) if np.isfinite(ic) else None, n) for d, ic, n in v]
        for h, v in daily_nonoverlap_detail.items()
    }

    # ---- Challenge 2: Detrended IC ----
    print(f"\n--- Challenge 2: Detrended IC ---")
    detrend_horizons = HORIZONS_NS
    daily_detrend_ics: dict[int, list[float]] = {h: [] for h in detrend_horizons}
    daily_detrend_detail: dict[int, list[tuple[str, float, int]]] = {h: [] for h in detrend_horizons}

    for date_str in sorted_dates:
        day_data = days[date_str]
        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]
        mid = compute_mid_price(bids_price, asks_price)
        mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)
        fwd_rets = compute_forward_returns(ts_ns, mid, detrend_horizons)

        for h_ns in detrend_horizons:
            ic, n_valid = compute_ic_detrended(mlofi, fwd_rets[h_ns], ts_ns=ts_ns)
            daily_detrend_ics[h_ns].append(ic)
            daily_detrend_detail[h_ns].append((date_str, ic, n_valid))

    print(f"\nPooled Detrended IC:")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8} {'N days':>8}")
    results["detrended_ics"] = {}
    for h_ns in detrend_horizons:
        ics = daily_detrend_ics[h_ns]
        valid = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid) if valid else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f} {len(valid):>8}")
        results["detrended_ics"][h_ms] = {"mean": float(mean_ic), "nw_t": float(t_stat), "n_days": len(valid)}

    results["detrended_detail"] = {
        h // 1_000_000: [(d, float(ic) if np.isfinite(ic) else None, n) for d, ic, n in v]
        for h, v in daily_detrend_detail.items()
    }

    # ---- Challenge 3: L1-only vs L2-L5-only IC ----
    print(f"\n--- Challenge 3: L1-only vs L2-L5-only IC ---")
    decomp_horizons = [5_000_000_000, 30_000_000_000]
    daily_l1_ics: dict[int, list[float]] = {h: [] for h in decomp_horizons}
    daily_deep_ics: dict[int, list[float]] = {h: [] for h in decomp_horizons}

    for date_str in sorted_dates:
        day_data = days[date_str]
        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]
        mid = compute_mid_price(bids_price, asks_price)
        fwd_rets = compute_forward_returns(ts_ns, mid, decomp_horizons)

        mlofi_l1 = compute_mlofi_l1_only(bids_vol, asks_vol, bids_price, asks_price)
        mlofi_deep = compute_mlofi_deep_only(bids_vol, asks_vol, bids_price, asks_price, lam=lam)

        for h_ns in decomp_horizons:
            ic_l1, _ = spearman_ic(mlofi_l1, fwd_rets[h_ns])
            ic_deep, _ = spearman_ic(mlofi_deep, fwd_rets[h_ns])
            daily_l1_ics[h_ns].append(ic_l1)
            daily_deep_ics[h_ns].append(ic_deep)

    print(f"\nPooled L1-only vs L2-L5-only IC:")
    print(f"{'Horizon':>10} {'L1 IC':>10} {'L1 t':>8} {'Deep IC':>10} {'Deep t':>8}")
    results["l1_vs_deep_ics"] = {}
    for h_ns in decomp_horizons:
        l1_valid = [x for x in daily_l1_ics[h_ns] if np.isfinite(x)]
        deep_valid = [x for x in daily_deep_ics[h_ns] if np.isfinite(x)]
        l1_mean = np.mean(l1_valid) if l1_valid else np.nan
        deep_mean = np.mean(deep_valid) if deep_valid else np.nan
        l1_t = newey_west_tstat(daily_l1_ics[h_ns])
        deep_t = newey_west_tstat(daily_deep_ics[h_ns])
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {l1_mean:>+10.4f} {l1_t:>8.2f} {deep_mean:>+10.4f} {deep_t:>8.2f}")
        results["l1_vs_deep_ics"][h_ms] = {
            "l1_mean": float(l1_mean), "l1_t": float(l1_t),
            "deep_mean": float(deep_mean), "deep_t": float(deep_t),
        }

    # ---- Challenge 4: OOS incremental IC ----
    print(f"\n--- Challenge 4: OOS Incremental IC (rolling day-behind alpha) ---")
    oos_results = compute_oos_incremental_ic(chrono_dates, days, lam=lam)
    print(f"\nOOS Incremental IC:")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8} {'N days':>8}")
    results["oos_incremental_ics"] = {}
    for h_ns in [5_000_000_000, 30_000_000_000]:
        entries = oos_results[h_ns]
        ics = [ic for _, ic, _ in entries if np.isfinite(ic)]
        mean_ic = np.mean(ics) if ics else np.nan
        t_stat = newey_west_tstat([ic for _, ic, _ in entries])
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f} {len(ics):>8}")
        results["oos_incremental_ics"][h_ms] = {"mean": float(mean_ic), "nw_t": float(t_stat), "n_days": len(ics)}
        # Per-day detail
        for date_str, ic, n_valid in entries:
            if np.isfinite(ic):
                print(f"    {date_str}: IC={ic:+.4f} (n={n_valid})")

    # ---- Corrected realized spread ----
    print(f"\n--- Corrected Realized Spread (pro-cyclical) ---")
    daily_rs: list[dict] = []
    for date_str in sorted_dates:
        day_data = days[date_str]
        mid = compute_mid_price(day_data["bids_price"], day_data["asks_price"])
        mlofi = compute_mlofi_integrated(
            day_data["bids_vol"], day_data["asks_vol"],
            day_data["bids_price"], day_data["asks_price"], lam=lam,
        )
        rs = compute_realized_spread_per_fill(day_data["timestamp_ns"], mid, mlofi)
        rs["date"] = date_str
        daily_rs.append(rs)
        if rs["n_fills"] > 0:
            print(f"  {date_str}: n={rs['n_fills']:,}, mean_rs={rs['mean_rs']:+.2f}, median_rs={rs['median_rs']:+.2f}")

    results["realized_spread"] = daily_rs

    # ---- Original overlapping IC for comparison ----
    print(f"\n--- Original Overlapping IC (for comparison) ---")
    daily_overlap_ics: dict[int, list[float]] = {h: [] for h in HORIZONS_NS}
    for date_str in sorted_dates:
        day_data = days[date_str]
        bids_price = day_data["bids_price"]
        bids_vol = day_data["bids_vol"]
        asks_price = day_data["asks_price"]
        asks_vol = day_data["asks_vol"]
        ts_ns = day_data["timestamp_ns"]
        mid = compute_mid_price(bids_price, asks_price)
        mlofi = compute_mlofi_integrated(bids_vol, asks_vol, bids_price, asks_price, lam=lam)
        fwd_rets = compute_forward_returns(ts_ns, mid, HORIZONS_NS)
        for h_ns in HORIZONS_NS:
            ic, _ = spearman_ic(mlofi, fwd_rets[h_ns])
            daily_overlap_ics[h_ns].append(ic)

    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8}")
    results["overlap_ics"] = {}
    for h_ns in HORIZONS_NS:
        ics = daily_overlap_ics[h_ns]
        valid = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid) if valid else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f}")
        results["overlap_ics"][h_ms] = {"mean": float(mean_ic), "nw_t": float(t_stat)}

    return results


def run_txfd6_conditional_analysis(dates: list[str], lam: float = LAMBDA_DEFAULT) -> dict:
    """Run Challenge 5: Conditional TXFD6 IC on high-L5-coverage ticks."""
    print(f"\n{'='*70}")
    print(f"CHALLENGE 5: Conditional TXFD6 IC (L5 coverage > 80%)")
    print(f"{'='*70}")

    data = load_l5_data("TXFD6")
    days = split_by_day(data, dates)
    sorted_dates = sorted(days.keys(), reverse=True)

    horizons = [5_000_000_000, 30_000_000_000]
    daily_cond_ics: dict[int, list[float]] = {h: [] for h in horizons}
    daily_detail: list[dict] = []

    for date_str in sorted_dates:
        day_data = days[date_str]
        cond = analyze_conditional_l5(day_data, lam=lam, horizons_ns=horizons)
        n_total = len(day_data)
        detail = {"date": date_str, "n_total": n_total}
        for h_ns in horizons:
            ic, n_valid, n_l5 = cond[h_ns]
            daily_cond_ics[h_ns].append(ic)
            h_ms = h_ns // 1_000_000
            detail[f"ic_{h_ms}ms"] = float(ic) if np.isfinite(ic) else None
            detail[f"n_l5_{h_ms}ms"] = n_l5
            print(f"  {date_str} IC({h_ms}ms) L5-only: {ic:+.4f}  (n_l5={n_l5}/{n_total}, {n_l5/n_total*100:.1f}%)")
        daily_detail.append(detail)

    print(f"\nPooled Conditional IC (L5-coverage > 80%):")
    print(f"{'Horizon':>10} {'Mean IC':>10} {'NW t':>8}")
    results: dict = {}
    for h_ns in horizons:
        ics = daily_cond_ics[h_ns]
        valid = [x for x in ics if np.isfinite(x)]
        mean_ic = np.mean(valid) if valid else np.nan
        t_stat = newey_west_tstat(ics)
        h_ms = h_ns // 1_000_000
        print(f"  {h_ms:>7}ms {mean_ic:>+10.4f} {t_stat:>8.2f}")
        results[h_ms] = {"mean": float(mean_ic), "nw_t": float(t_stat)}

    results["detail"] = daily_detail
    return results


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

TXFD6_DATES = [
    "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06", "2026-03-09",
    "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13", "2026-03-16",
    "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20", "2026-03-23",
]

STOCK_2330_DATES = [
    "2026-03-02", "2026-03-03", "2026-03-04", "2026-03-05", "2026-03-06",
    "2026-03-09", "2026-03-10", "2026-03-11", "2026-03-12", "2026-03-13",
    "2026-03-16", "2026-03-17", "2026-03-18", "2026-03-19", "2026-03-20",
    "2026-03-23", "2026-03-24",
]


def main() -> None:
    """Stage 2b: Run bug-fixed analysis with all challenger/execution diagnostics."""
    mode = sys.argv[1] if len(sys.argv) > 1 else "stage2b"

    if mode == "stage2a":
        # Original Stage 2a analysis (preserved for reference)
        print("MLOFI Microprice Correction — Stage 2a IC Analysis")
        print("=" * 70)
        txfd6_results = analyze_asset("TXFD6", TXFD6_DATES, lam=LAMBDA_DEFAULT)
        stock_results = analyze_asset("2330", STOCK_2330_DATES, lam=LAMBDA_DEFAULT)
        return

    # ---- Stage 2b: Full bug-fixed + challenger diagnostics ----
    print("MLOFI Microprice Correction — Stage 2b IC Analysis (Bug-Fixed)")
    print("=" * 70)
    print("Bugs fixed: (1) impl.py hot-path allocation, (2) forward return valid mask,")
    print("            (3) realized spread direction (contrarian -> pro-cyclical)")
    print("Challenges: (1) non-overlapping IC, (2) detrended IC, (3) L1 vs deep,")
    print("            (4) OOS incremental IC, (5) conditional TXFD6 L5 IC")
    print()

    # Run Stage 2a baseline first (with bug fixes applied)
    print("\n" + "=" * 70)
    print("PHASE 1: Bug-fixed baseline IC (overlapping, for comparison)")
    print("=" * 70)
    txfd6_baseline = analyze_asset("TXFD6", TXFD6_DATES, lam=LAMBDA_DEFAULT)
    stock_baseline = analyze_asset("2330", STOCK_2330_DATES, lam=LAMBDA_DEFAULT)

    # Run Stage 2b extended analyses on 2330 (the viable asset)
    print("\n" + "=" * 70)
    print("PHASE 2: Stage 2b Extended Analysis — 2330")
    print("=" * 70)
    stock_2b = run_stage2b_analysis("2330", STOCK_2330_DATES, lam=LAMBDA_DEFAULT)

    # Challenge 5: Conditional TXFD6
    txfd6_cond = run_txfd6_conditional_analysis(TXFD6_DATES, lam=LAMBDA_DEFAULT)

    # ---- Summary ----
    print(f"\n{'='*70}")
    print("STAGE 2b SUMMARY")
    print(f"{'='*70}")

    # Non-overlapping vs overlapping comparison
    print("\n2330 IC Comparison: Overlapping vs Non-Overlapping")
    print(f"{'Horizon':>10} {'Overlap IC':>12} {'NonOverlap IC':>14} {'Drop %':>8}")
    for h_ms in [30000, 60000]:
        ov = stock_2b.get("overlap_ics", {}).get(h_ms, {}).get("mean", np.nan)
        no = stock_2b.get("nonoverlap_ics", {}).get(h_ms, {}).get("mean", np.nan)
        drop = ((ov - no) / abs(ov) * 100) if (np.isfinite(ov) and abs(ov) > 1e-6 and np.isfinite(no)) else np.nan
        print(f"  {h_ms:>7}ms {ov:>+12.4f} {no:>+14.4f} {drop:>+8.1f}%")

    # Detrended comparison
    print("\n2330 IC Comparison: Raw vs Detrended")
    print(f"{'Horizon':>10} {'Raw IC':>10} {'Detrend IC':>12} {'Drop %':>8}")
    for h_ms_val in [250, 500, 1000, 2000, 5000, 10000, 30000, 60000]:
        raw = stock_2b.get("overlap_ics", {}).get(h_ms_val, {}).get("mean", np.nan)
        det = stock_2b.get("detrended_ics", {}).get(h_ms_val, {}).get("mean", np.nan)
        drop = ((raw - det) / abs(raw) * 100) if (np.isfinite(raw) and abs(raw) > 1e-6 and np.isfinite(det)) else np.nan
        print(f"  {h_ms_val:>7}ms {raw:>+10.4f} {det:>+12.4f} {drop:>+8.1f}%")

    # Kill gate assessment
    print("\n--- KILL GATE ASSESSMENT ---")
    ic_30s_overlap = stock_2b.get("overlap_ics", {}).get(30000, {}).get("mean", np.nan)
    ic_30s_nonoverlap = stock_2b.get("nonoverlap_ics", {}).get(30000, {}).get("mean", np.nan)
    ic_30s_detrended = stock_2b.get("detrended_ics", {}).get(30000, {}).get("mean", np.nan)
    nw_t_nonoverlap = stock_2b.get("nonoverlap_ics", {}).get(30000, {}).get("nw_t", np.nan)

    print(f"  2330 IC(30s) overlapping:     {ic_30s_overlap:+.4f}")
    print(f"  2330 IC(30s) non-overlapping:  {ic_30s_nonoverlap:+.4f}  (NW t={nw_t_nonoverlap:.2f})")
    print(f"  2330 IC(30s) detrended:        {ic_30s_detrended:+.4f}")
    print(f"  Kill gate threshold:           {IC_KILL_GATE_30S}")

    if np.isfinite(ic_30s_nonoverlap) and abs(ic_30s_nonoverlap) >= IC_KILL_GATE_30S:
        print(f"  RESULT: PASS (non-overlapping |IC| = {abs(ic_30s_nonoverlap):.4f} >= {IC_KILL_GATE_30S})")
    elif np.isfinite(ic_30s_nonoverlap):
        print(f"  RESULT: FAIL (non-overlapping |IC| = {abs(ic_30s_nonoverlap):.4f} < {IC_KILL_GATE_30S})")
    else:
        print(f"  RESULT: INCONCLUSIVE")


if __name__ == "__main__":
    main()
