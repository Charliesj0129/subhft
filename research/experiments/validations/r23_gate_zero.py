"""R23 Gate Zero: Detrended autocorrelation on TMFD6/TXFD6 at MF horizons.

Tests whether mid-price returns at 1h/2h/4h show significant autocorrelation
(trending or reverting), which is the prerequisite for Candidate A
(regime-conditional trend following).

Methodology:
1. Load daily L1 .npy files for each symbol
2. Resample mid_price to fixed intervals (1min bars)
3. Compute returns at 1h/2h/4h horizons
4. Detrend: remove intraday seasonality (per-minute mean return)
5. Measure lag-1 autocorrelation of detrended returns at each horizon
6. Compute IC: rank correlation of lagged return vs forward return
7. Report per-day, pooled, with bootstrap 95% CI

Pass threshold: detrended IC >= 0.020 at any horizon.
"""

from __future__ import annotations

import glob
import os
import sys
from pathlib import Path

import numpy as np
from scipy import stats  # type: ignore[import-untyped]


def load_daily_files(symbol: str) -> list[tuple[str, np.ndarray]]:
    """Load all daily L1 .npy files for a symbol, return (date, data) pairs."""
    base_dir = Path(f"research/data/raw/{symbol.lower()}")
    files = sorted(glob.glob(str(base_dir / f"{symbol}_2026-*_l1.npy")))
    result = []
    for f in files:
        date_str = Path(f).stem.split("_")[1]  # e.g. "2026-01-28"
        data = np.load(f)
        result.append((date_str, data))
    return result


def resample_to_bars(data: np.ndarray, bar_seconds: int = 60) -> np.ndarray:
    """Resample tick-level mid_price to fixed-interval bars (last price in bar).

    Returns structured array with fields: (ts_ns, mid_price, bar_idx).
    """
    ts = data["local_ts"]
    mid = data["mid_price"]

    if len(ts) == 0:
        return np.array([], dtype=[("ts_ns", "i8"), ("mid_price", "f8"), ("bar_idx", "i4")])

    bar_ns = bar_seconds * 1_000_000_000
    # Align to bar boundaries
    t0 = ts[0]
    bar_indices = ((ts - t0) // bar_ns).astype(np.int32)
    max_bar = bar_indices[-1]

    bars = []
    for b in range(max_bar + 1):
        mask = bar_indices == b
        if np.any(mask):
            idx = np.where(mask)[0][-1]  # last tick in bar
            bars.append((ts[idx], mid[idx], b))

    if not bars:
        return np.array([], dtype=[("ts_ns", "i8"), ("mid_price", "f8"), ("bar_idx", "i4")])

    return np.array(bars, dtype=[("ts_ns", "i8"), ("mid_price", "f8"), ("bar_idx", "i4")])


def compute_returns_at_horizon(bars: np.ndarray, horizon_bars: int) -> np.ndarray:
    """Compute log returns at a given horizon (in number of bars).

    Returns array of (return, bar_idx) for non-overlapping windows.
    """
    mid = bars["mid_price"]
    n = len(mid)
    if n < 2 * horizon_bars:
        return np.array([], dtype=[("ret", "f8"), ("bar_idx", "i4")])

    results = []
    for i in range(0, n - horizon_bars, horizon_bars):
        if mid[i] > 0 and mid[i + horizon_bars] > 0:
            ret = np.log(mid[i + horizon_bars] / mid[i])
            results.append((ret, bars["bar_idx"][i]))

    if not results:
        return np.array([], dtype=[("ret", "f8"), ("bar_idx", "i4")])

    return np.array(results, dtype=[("ret", "f8"), ("bar_idx", "i4")])


def detrend_returns_causal(returns: np.ndarray, window: int = 5) -> np.ndarray:
    """Remove local trend using CAUSAL-ONLY rolling mean (no look-ahead).

    rolling_mean[i] = mean(ret[i-window+1 : i+1])  — past only.
    First (window-1) elements use available history only.
    """
    ret = returns["ret"].copy()
    if len(ret) < 3:
        return ret

    rolling_mean = np.zeros_like(ret)
    cumsum = np.cumsum(np.insert(ret, 0, 0))
    for i in range(len(ret)):
        lo = max(0, i - window + 1)
        rolling_mean[i] = (cumsum[i + 1] - cumsum[lo]) / (i + 1 - lo)

    return ret - rolling_mean


def lag1_autocorrelation(x: np.ndarray) -> float:
    """Compute lag-1 autocorrelation."""
    if len(x) < 3:
        return np.nan
    return np.corrcoef(x[:-1], x[1:])[0, 1]


def rank_ic(signal: np.ndarray, target: np.ndarray) -> float:
    """Compute Spearman rank IC between signal and target."""
    if len(signal) < 3:
        return np.nan
    corr, _ = stats.spearmanr(signal, target)
    return corr


def bootstrap_ci(values: np.ndarray, n_boot: int = 5000, ci: float = 0.95) -> tuple[float, float]:
    """Bootstrap confidence interval for the mean."""
    if len(values) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(42)
    means = np.array([
        np.mean(rng.choice(values, size=len(values), replace=True))
        for _ in range(n_boot)
    ])
    alpha = (1 - ci) / 2
    return (np.quantile(means, alpha), np.quantile(means, 1 - alpha))


def analyze_variant(
    daily_data: list[tuple[str, np.ndarray]],
    hz_bars: int,
    detrend_fn: callable | None,
    variant_name: str,
) -> dict:
    """Analyze a single variant (raw or detrended) for a given horizon.

    Uses PER-DAY IC only (no cross-day pooling) to avoid overnight gap issue (GZ-C2).
    """
    daily_ics = []
    daily_autocorrs = []
    total_obs = 0

    for date_str, data in daily_data:
        bars = resample_to_bars(data, bar_seconds=60)
        if len(bars) < 2 * hz_bars:
            continue

        returns = compute_returns_at_horizon(bars, hz_bars)
        if len(returns) < 4:
            continue

        if detrend_fn is not None:
            rets = detrend_fn(returns)
        else:
            rets = returns["ret"].copy()

        # Lag-1 autocorrelation (within day only)
        ac = lag1_autocorrelation(rets)

        # IC: lagged return predicts forward return (within day only)
        if len(rets) >= 4:
            ic = rank_ic(rets[:-1], rets[1:])
        else:
            ic = np.nan

        if not np.isnan(ac):
            daily_autocorrs.append(ac)
        if not np.isnan(ic):
            daily_ics.append(ic)

        total_obs += len(rets)

    ac_arr = np.array(daily_autocorrs)
    ic_arr = np.array(daily_ics)

    mean_ac = np.mean(ac_arr) if len(ac_arr) > 0 else np.nan
    mean_ic = np.mean(ic_arr) if len(ic_arr) > 0 else np.nan

    ac_ci = bootstrap_ci(ac_arr) if len(ac_arr) >= 3 else (np.nan, np.nan)
    ic_ci = bootstrap_ci(ic_arr) if len(ic_arr) >= 3 else (np.nan, np.nan)

    if len(ic_arr) >= 3:
        t_stat, p_val = stats.ttest_1samp(ic_arr, 0)
    else:
        t_stat, p_val = np.nan, np.nan

    sign_pct = (np.mean(ic_arr < 0) * 100) if len(ic_arr) > 0 else np.nan  # % negative for reversion

    return {
        "variant": variant_name,
        "n_days": len(daily_ics),
        "n_obs_total": total_obs,
        "mean_daily_ac": mean_ac,
        "ac_ci_95": ac_ci,
        "mean_daily_ic": mean_ic,
        "ic_ci_95": ic_ci,
        "t_stat": t_stat,
        "p_value": p_val,
        "sign_neg_pct": sign_pct,
    }


def run_gate_zero(symbol: str, horizons_minutes: list[int]) -> dict:
    """Run Gate Zero diagnostic for a single symbol with THREE variants."""
    print(f"\n{'='*60}")
    print(f"Gate Zero v2: {symbol}")
    print(f"{'='*60}")

    daily_data = load_daily_files(symbol)
    if not daily_data:
        print(f"  NO DATA for {symbol}")
        return {}

    print(f"  Loaded {len(daily_data)} trading days")
    print(f"  Using PER-DAY IC only (no cross-day pooling, fixes GZ-C2)")

    results = {}

    for hz_min in horizons_minutes:
        hz_bars = hz_min
        hz_label = f"{hz_min}min"
        if hz_min >= 60:
            hz_label = f"{hz_min // 60}h"

        print(f"\n  --- Horizon: {hz_label} ---")

        variants = [
            (None, "RAW"),
            (lambda r: detrend_returns_causal(r, window=5), "CAUSAL_5"),
            (lambda r: detrend_returns_causal(r, window=10), "CAUSAL_10"),
        ]

        hz_results = {}
        for detrend_fn, vname in variants:
            r = analyze_variant(daily_data, hz_bars, detrend_fn, vname)
            hz_results[vname] = r

            ci_str = f"[{r['ic_ci_95'][0]:+.4f}, {r['ic_ci_95'][1]:+.4f}]"
            pass_str = "YES" if abs(r["mean_daily_ic"]) >= 0.020 else "NO"
            print(
                f"    {vname:<12} N={r['n_days']:<3} obs={r['n_obs_total']:<5} "
                f"IC={r['mean_daily_ic']:+.4f} {ci_str:<24} "
                f"AC={r['mean_daily_ac']:+.4f} p={r['p_value']:.4f} "
                f"sign_neg={r['sign_neg_pct']:.0f}% PASS={pass_str}"
            )

        results[hz_label] = hz_results

    return results


def main() -> None:
    os.chdir(Path(__file__).resolve().parents[3])  # project root

    horizons = [15, 30, 60, 120, 240]  # minutes

    all_results = {}
    for symbol in ["TMFD6", "TXFD6"]:
        all_results[symbol] = run_gate_zero(symbol, horizons)

    # Summary — focus on RAW (no detrending artifact) as ground truth
    print(f"\n{'='*60}")
    print("GATE ZERO v2 SUMMARY")
    print(f"{'='*60}")
    print(f"Pass threshold: |IC| >= 0.020 at any horizon")
    print(f"PRIMARY: RAW returns (no detrending artifact possible)")
    print(f"SECONDARY: CAUSAL_5 detrending (past-only, no look-ahead)")
    print()

    any_raw_pass = False
    for symbol in ["TMFD6", "TXFD6"]:
        print(f"\n{symbol}:")
        print(f"  {'Horizon':<8} | {'RAW IC':<10} {'RAW p':<8} | {'CAUSAL5 IC':<12} {'CAUSAL5 p':<10} | {'CAUSAL10 IC':<13} {'CAUSAL10 p':<10}")
        print(f"  {'-'*95}")
        for hz_label, hz_results in all_results[symbol].items():
            raw = hz_results.get("RAW", {})
            c5 = hz_results.get("CAUSAL_5", {})
            c10 = hz_results.get("CAUSAL_10", {})

            raw_ic = raw.get("mean_daily_ic", np.nan)
            c5_ic = c5.get("mean_daily_ic", np.nan)
            c10_ic = c10.get("mean_daily_ic", np.nan)

            if abs(raw_ic) >= 0.020:
                any_raw_pass = True

            print(
                f"  {hz_label:<8} | {raw_ic:+.4f}     {raw.get('p_value', np.nan):<8.4f} | "
                f"{c5_ic:+.4f}       {c5.get('p_value', np.nan):<10.4f} | "
                f"{c10_ic:+.4f}        {c10.get('p_value', np.nan):<10.4f}"
            )

    print(f"\n{'='*60}")
    if any_raw_pass:
        print("GATE ZERO v2: PASS on RAW returns (no detrending artifact)")
        print("Signal is genuine, not a centered-MA artifact.")
    else:
        print("GATE ZERO v2: FAIL on RAW returns")
        print("Previous result was likely a centered-MA artifact (GZ-C1).")
        print("Candidate A is KILLED.")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
