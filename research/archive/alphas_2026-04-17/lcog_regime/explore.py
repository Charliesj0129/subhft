"""
L-COG C3 Exploration: COG-Regime Conditioned KDJ vs Unconditioned KDJ
=====================================================================
Loads TXFD6 golden parquet (10 days), builds event bars from BidAsk data,
computes KDJ(QI_1) and COG regime indicator, then compares:
  1. IC of unconditioned KDJ(QI_1) → forward return
  2. IC of COG-conditioned KDJ → forward return
  3. Per-regime IC split (COG>0 vs COG<0)

Data: research/data/real/golden/TXFD6/*.parquet
Prices are x1,000,000 scale in golden parquet.
"""

import sys
from pathlib import Path

import numpy as np
from scipy import stats

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GOLDEN_DIR = Path("research/data/real/golden/TXFD6")
PRICE_SCALE = 1_000_000  # golden parquet convention
POINT_VALUE = 200  # NTD per point for TXFD6
EVENT_BAR_N = 500  # every N BidAsk events → ~1 min bars
WARMUP_BARS = 14  # max(KDJ_K_PERIOD, COG_EMA_PERIOD) + 5 = 14, aligned with impl.py
KDJ_K_PERIOD = 9
KDJ_D_PERIOD = 3
COG_EMA_PERIOD = 9
FWD_HORIZONS = [1, 3, 5, 10]  # bar horizons (fwd_3 ≈ 2.8 min)
BOOK_LEVELS = 5


# ---------------------------------------------------------------------------
# Data Loading (reuse stage2 pattern for golden parquet)
# ---------------------------------------------------------------------------
def load_bidask_day(path: Path):
    """Load a single golden parquet day, return BidAsk rows only."""
    import pandas as pd
    df = pd.read_parquet(path)
    ba_df = df[df["type"] == "BidAsk"].reset_index(drop=True)
    tk_df = df[df["type"] == "Tick"].reset_index(drop=True)
    return ba_df, tk_df


def extract_book_arrays(ba_df):
    """Extract L1-L5 bid/ask prices and volumes as numpy arrays."""
    n = len(ba_df)
    bid_prices = np.zeros((n, BOOK_LEVELS), dtype=np.int64)
    bid_vols = np.zeros((n, BOOK_LEVELS), dtype=np.int64)
    ask_prices = np.zeros((n, BOOK_LEVELS), dtype=np.int64)
    ask_vols = np.zeros((n, BOOK_LEVELS), dtype=np.int64)
    timestamps = ba_df["exch_ts"].values.astype(np.int64)

    for i, (_, row) in enumerate(ba_df.iterrows()):
        bp = np.asarray(row["bids_price"], dtype=np.int64)
        bv = np.asarray(row["bids_vol"], dtype=np.int64)
        ap = np.asarray(row["asks_price"], dtype=np.int64)
        av = np.asarray(row["asks_vol"], dtype=np.int64)
        lvls = min(BOOK_LEVELS, len(bp))
        bid_prices[i, :lvls] = bp[:lvls]
        bid_vols[i, :lvls] = bv[:lvls]
        lvls_a = min(BOOK_LEVELS, len(ap))
        ask_prices[i, :lvls_a] = ap[:lvls_a]
        ask_vols[i, :lvls_a] = av[:lvls_a]

    return timestamps, bid_prices, bid_vols, ask_prices, ask_vols


def subsample_bidask(ba_df, max_rows=700000):
    """Subsample BidAsk rows if too many, keeping even spacing.
    Cap raised to 700K to ensure no TXFD6 day (max ~570K) is subsampled.
    """
    if len(ba_df) <= max_rows:
        return ba_df
    step = len(ba_df) // max_rows
    print(f"  WARNING: subsampling {len(ba_df)} -> ~{max_rows} rows (step={step})")
    return ba_df.iloc[::step].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Bar Builder (event bars from BidAsk snapshots)
# ---------------------------------------------------------------------------
def build_event_bars(timestamps, bid_prices, bid_vols, ask_prices, ask_vols, n=EVENT_BAR_N):
    """
    Build event bars from BidAsk snapshots.
    Each bar aggregates N consecutive BidAsk events.
    Returns list of bar dicts with OHLC of QI, COG, mid, etc.
    """
    total = len(timestamps)
    bars = []

    for start in range(0, total - n + 1, n):
        end = start + n
        # Filter out zero-price rows within bar
        bp = bid_prices[start:end]
        bv = bid_vols[start:end]
        ap = ask_prices[start:end]
        av = ask_vols[start:end]
        ts = timestamps[start:end]

        valid = (bp[:, 0] > 0) & (ap[:, 0] > 0)
        if valid.sum() < 10:
            continue

        # Mid price (in points, float)
        mid = (bp[valid, 0].astype(np.float64) + ap[valid, 0].astype(np.float64)) / 2.0 / PRICE_SCALE

        # QI_1: (bid_vol - ask_vol) / (bid_vol + ask_vol) at L1
        bv1 = bv[valid, 0].astype(np.float64)
        av1 = av[valid, 0].astype(np.float64)
        total_vol = bv1 + av1
        qi = np.where(total_vol > 0, (bv1 - av1) / total_vol, 0.0)

        # COG for bid and ask sides (L1-L5)
        # COG = sum(price_i * qty_i) / sum(qty_i) for each side, in points
        bp_f = bp[valid].astype(np.float64) / PRICE_SCALE
        bv_f = bv[valid].astype(np.float64)
        ap_f = ap[valid].astype(np.float64) / PRICE_SCALE
        av_f = av[valid].astype(np.float64)

        # Vectorized COG: dot product per row
        bv_sum = bv_f.sum(axis=1)
        av_sum = av_f.sum(axis=1)
        cog_bid = np.where(
            bv_sum > 0,
            np.sum(bp_f * bv_f, axis=1) / bv_sum,
            bp_f[:, 0],
        )
        cog_ask = np.where(
            av_sum > 0,
            np.sum(ap_f * av_f, axis=1) / av_sum,
            ap_f[:, 0],
        )
        cog_mid = (cog_bid + cog_ask) / 2.0
        cog_dev = cog_mid - mid  # positive = resting demand heavier

        bar = {
            "bar_ts": ts[valid][-1],
            "mid_open": mid[0],
            "mid_close": mid[-1],
            "mid_high": mid.max(),
            "mid_low": mid.min(),
            "qi_open": qi[0],
            "qi_close": qi[-1],
            "qi_high": qi.max(),
            "qi_low": qi.min(),
            "cog_dev_mean": cog_dev.mean(),
            "cog_dev_close": cog_dev[-1],
            # Snapshot at bar close for COG computation
            "bid_prices_close": bp[valid][-1].copy(),
            "bid_vols_close": bv[valid][-1].copy(),
            "ask_prices_close": ap[valid][-1].copy(),
            "ask_vols_close": av[valid][-1].copy(),
        }
        bars.append(bar)

    return bars


# ---------------------------------------------------------------------------
# KDJ Indicator
# ---------------------------------------------------------------------------
def compute_kdj(qi_close, qi_high, qi_low, k_period=KDJ_K_PERIOD, d_period=KDJ_D_PERIOD):
    """Compute KDJ on QI_1 bar series. Returns K, D, J arrays."""
    n = len(qi_close)
    k_vals = np.full(n, 50.0)
    d_vals = np.full(n, 50.0)

    for i in range(k_period - 1, n):
        lo = qi_low[max(0, i - k_period + 1): i + 1].min()
        hi = qi_high[max(0, i - k_period + 1): i + 1].max()
        if hi - lo > 1e-12:
            rsv = (qi_close[i] - lo) / (hi - lo) * 100.0
        else:
            rsv = 50.0
        k_vals[i] = 2.0 / 3.0 * (k_vals[i - 1] if i > 0 else 50.0) + 1.0 / 3.0 * rsv
        d_vals[i] = 2.0 / 3.0 * (d_vals[i - 1] if i > 0 else 50.0) + 1.0 / 3.0 * k_vals[i]

    j_vals = 3.0 * k_vals - 2.0 * d_vals
    return k_vals, d_vals, j_vals


# ---------------------------------------------------------------------------
# COG Regime + Conditioned KDJ
# ---------------------------------------------------------------------------
def compute_cog_regime(cog_dev_series, ema_period=COG_EMA_PERIOD):
    """Compute EMA-smoothed COG deviation and regime sign."""
    n = len(cog_dev_series)
    cog_ema = np.zeros(n)
    alpha = 2.0 / (ema_period + 1)
    cog_ema[0] = cog_dev_series[0]
    for i in range(1, n):
        cog_ema[i] = alpha * cog_dev_series[i] + (1.0 - alpha) * cog_ema[i - 1]
    regime = np.sign(cog_ema)
    return cog_ema, regime


def condition_kdj_by_regime(k_vals, regime):
    """
    Apply regime conditioning to KDJ_K signal.
    - Aligned (regime * KDJ direction > 0): keep full signal
    - Misaligned: attenuate deviation from 50 by 50%
    """
    n = len(k_vals)
    conditioned = np.zeros(n)
    for i in range(n):
        kdj_dir = 1.0 if k_vals[i] > 50.0 else -1.0
        if regime[i] * kdj_dir > 0:
            conditioned[i] = k_vals[i]
        else:
            conditioned[i] = 50.0 + (k_vals[i] - 50.0) * 0.5
    return conditioned


# ---------------------------------------------------------------------------
# IC Computation
# ---------------------------------------------------------------------------
def compute_fwd_returns(mid_close, horizons=FWD_HORIZONS):
    """Forward returns at multiple bar horizons (in points)."""
    n = len(mid_close)
    fwd = {}
    for h in horizons:
        ret = np.full(n, np.nan)
        if h < n:
            ret[:n - h] = mid_close[h:] - mid_close[:n - h]
        fwd[f"fwd_{h}"] = ret
    return fwd


def spearman_ic(signal, fwd):
    """Spearman rank IC, ignoring NaN."""
    mask = np.isfinite(signal) & np.isfinite(fwd)
    if mask.sum() < 20:
        return np.nan
    r, _ = stats.spearmanr(signal[mask], fwd[mask])
    return r


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def compute_median_spread_pts(bid_prices, ask_prices):
    """Compute median L1 spread in points from raw book arrays."""
    bp1 = bid_prices[:, 0].astype(np.float64)
    ap1 = ask_prices[:, 0].astype(np.float64)
    valid = (bp1 > 0) & (ap1 > 0)
    if valid.sum() == 0:
        return np.nan
    spread = (ap1[valid] - bp1[valid]) / PRICE_SCALE
    return float(np.median(spread))


def process_day(pf, cog_ema_period=COG_EMA_PERIOD):
    """
    Process one day's parquet file. Returns (day_str, day_ic, pooled_arrays, median_spread)
    or None if day is skipped.
    """
    day_str = pf.stem
    print(f"\n--- {day_str} ---")

    ba_df, tk_df = load_bidask_day(pf)
    raw_rows = len(ba_df)
    if raw_rows < EVENT_BAR_N * 10:
        print(f"  Skipping: only {raw_rows} BidAsk rows")
        return None

    # No subsampling -- use all rows for correct bar resolution
    print(f"  BidAsk rows (raw): {raw_rows}")

    timestamps, bid_prices, bid_vols, ask_prices, ask_vols = extract_book_arrays(ba_df)

    median_spread = compute_median_spread_pts(bid_prices, ask_prices)
    print(f"  Median L1 spread: {median_spread:.1f} pts")

    bars = build_event_bars(timestamps, bid_prices, bid_vols, ask_prices, ask_vols, n=EVENT_BAR_N)
    n_bars = len(bars)
    if n_bars < WARMUP_BARS + 30:
        print(f"  Skipping: only {n_bars} bars")
        return None

    print(f"  Bars: {n_bars}")

    mid_close = np.array([b["mid_close"] for b in bars])
    qi_close = np.array([b["qi_close"] for b in bars])
    qi_high = np.array([b["qi_high"] for b in bars])
    qi_low = np.array([b["qi_low"] for b in bars])
    cog_dev_mean = np.array([b["cog_dev_mean"] for b in bars])

    k_vals, d_vals, j_vals = compute_kdj(qi_close, qi_high, qi_low)
    cog_ema, regime = compute_cog_regime(cog_dev_mean, ema_period=cog_ema_period)
    kdj_cond = condition_kdj_by_regime(k_vals, regime)
    fwd = compute_fwd_returns(mid_close)

    s = WARMUP_BARS
    k_trim = k_vals[s:]
    cond_trim = kdj_cond[s:]
    regime_trim = regime[s:]

    print(f"  {'Signal':<25} | " + " | ".join(f"fwd_{h:>2}" for h in FWD_HORIZONS))
    print(f"  {'-' * 25}-+-" + "-+-".join("-" * 6 for _ in FWD_HORIZONS))

    day_ic = {"day": day_str, "n_bars": n_bars - WARMUP_BARS, "median_spread": median_spread}

    for label, sig in [("KDJ_K (unconditioned)", k_trim), ("KDJ_K (COG-conditioned)", cond_trim)]:
        ics = []
        for h in FWD_HORIZONS:
            fwd_trim = fwd[f"fwd_{h}"][s:]
            ic = spearman_ic(sig, fwd_trim)
            ics.append(ic)
        ic_str = " | ".join(f"{ic:+.4f}" if not np.isnan(ic) else "   NaN" for ic in ics)
        print(f"  {label:<25} | {ic_str}")
        day_ic[label] = {f"fwd_{h}": ics[i] for i, h in enumerate(FWD_HORIZONS)}

    for regime_label, rmask in [("COG>0 (demand)", regime_trim > 0), ("COG<0 (supply)", regime_trim < 0)]:
        if rmask.sum() < 20:
            print(f"  KDJ_K | {regime_label:<15}: too few bars ({rmask.sum()})")
            continue
        ics = []
        for h in FWD_HORIZONS:
            fwd_trim = fwd[f"fwd_{h}"][s:]
            ic = spearman_ic(k_trim[rmask], fwd_trim[rmask])
            ics.append(ic)
        ic_str = " | ".join(f"{ic:+.4f}" if not np.isnan(ic) else "   NaN" for ic in ics)
        print(f"  KDJ_K | {regime_label:<15} | {ic_str}")
        day_ic[f"KDJ_K|{regime_label}"] = {f"fwd_{h}": ics[i] for i, h in enumerate(FWD_HORIZONS)}

    n_demand = (regime_trim > 0).sum()
    n_supply = (regime_trim < 0).sum()
    n_neutral = (regime_trim == 0).sum()
    print(f"  Regime balance: demand={n_demand}, supply={n_supply}, neutral={n_neutral}")

    pooled_arrays = {
        "kdj_k": k_trim,
        "kdj_cond": cond_trim,
        "regime": regime_trim,
    }
    for h in FWD_HORIZONS:
        pooled_arrays[f"fwd_{h}"] = fwd[f"fwd_{h}"][s:]

    return day_str, day_ic, pooled_arrays, median_spread


def pooled_analysis(day_results, pooled_list, label_prefix=""):
    """Run pooled IC analysis across accumulated days. Returns results dict."""
    all_kdj = np.concatenate([p["kdj_k"] for p in pooled_list])
    all_cond = np.concatenate([p["kdj_cond"] for p in pooled_list])
    all_regime = np.concatenate([p["regime"] for p in pooled_list])
    all_fwd = {f"fwd_{h}": np.concatenate([p[f"fwd_{h}"] for p in pooled_list]) for h in FWD_HORIZONS}

    N = len(all_kdj)
    se = 1.0 / np.sqrt(N) if N > 0 else np.nan

    print(f"\nTotal bars (post-warmup): {N}")
    print(f"SE (1/sqrt(N)): {se:.4f}")
    print(f"Regime: demand={int((all_regime > 0).sum())}, supply={int((all_regime < 0).sum())}, "
          f"neutral={int((all_regime == 0).sum())}")

    print(f"\n{'Signal':<30} | " + " | ".join(f"fwd_{h:>2}" for h in FWD_HORIZONS))
    print(f"{'-' * 30}-+-" + "-+-".join("-" * 7 for _ in FWD_HORIZONS))

    ic_results = {}
    for label, sig in [
        ("KDJ_K (unconditioned)", all_kdj),
        ("KDJ_K (COG-conditioned)", all_cond),
    ]:
        ics = [spearman_ic(sig, all_fwd[f"fwd_{h}"]) for h in FWD_HORIZONS]
        ic_str = " | ".join(f"{ic:+.4f}" if not np.isnan(ic) else "    NaN" for ic in ics)
        print(f"{label:<30} | {ic_str}")
        ic_results[label] = {f"fwd_{h}": ics[i] for i, h in enumerate(FWD_HORIZONS)}

    for regime_label, rmask in [("COG>0 (demand)", all_regime > 0), ("COG<0 (supply)", all_regime < 0)]:
        if rmask.sum() < 30:
            continue
        ics = [spearman_ic(all_kdj[rmask], all_fwd[f"fwd_{h}"][rmask]) for h in FWD_HORIZONS]
        ic_str = " | ".join(f"{ic:+.4f}" if not np.isnan(ic) else "    NaN" for ic in ics)
        print(f"KDJ_K | {regime_label:<18} | {ic_str}")
        ic_results[f"KDJ_K|{regime_label}"] = {f"fwd_{h}": ics[i] for i, h in enumerate(FWD_HORIZONS)}

    # Summary with statistical significance
    ic_uncond = spearman_ic(all_kdj, all_fwd["fwd_3"])
    ic_cond = spearman_ic(all_cond, all_fwd["fwd_3"])
    delta = ic_cond - ic_uncond if not (np.isnan(ic_cond) or np.isnan(ic_uncond)) else np.nan
    delta_over_se = delta / se if (not np.isnan(delta) and se > 0) else np.nan

    ic_demand = spearman_ic(
        all_kdj[all_regime > 0], all_fwd["fwd_3"][all_regime > 0]
    ) if (all_regime > 0).sum() > 30 else np.nan
    ic_supply = spearman_ic(
        all_kdj[all_regime < 0], all_fwd["fwd_3"][all_regime < 0]
    ) if (all_regime < 0).sum() > 30 else np.nan

    print(f"\n  Unconditioned KDJ IC (fwd_3): {ic_uncond:+.4f}")
    print(f"  COG-Conditioned KDJ IC (fwd_3): {ic_cond:+.4f}")
    if not np.isnan(delta):
        print(f"  Delta (cond - uncond): {delta:+.4f}  |  delta/SE = {delta_over_se:+.2f}")
    else:
        print("  Delta: NaN")
    if not np.isnan(ic_demand):
        print(f"  Per-regime demand IC (fwd_3): {ic_demand:+.4f}")
    if not np.isnan(ic_supply):
        print(f"  Per-regime supply IC (fwd_3): {ic_supply:+.4f}")

    # Day consistency
    n_improved = 0
    n_total = 0
    for dr in day_results:
        u = dr.get("KDJ_K (unconditioned)", {}).get("fwd_3", np.nan)
        c = dr.get("KDJ_K (COG-conditioned)", {}).get("fwd_3", np.nan)
        if not np.isnan(u) and not np.isnan(c):
            n_total += 1
            if c > u:
                n_improved += 1
    print(f"  Day consistency: conditioned > unconditioned in {n_improved}/{n_total} days")

    return {
        "N": N,
        "se": se,
        "ic_uncond_fwd3": ic_uncond,
        "ic_cond_fwd3": ic_cond,
        "delta": delta,
        "delta_over_se": delta_over_se,
        "ic_demand_fwd3": ic_demand,
        "ic_supply_fwd3": ic_supply,
        "n_improved_days": n_improved,
        "n_total_days": n_total,
        "ic_results": ic_results,
        "day_results": day_results,
    }


def run_exploration():
    """Run the full COG-conditioned vs unconditioned KDJ comparison on March-only days."""
    parquet_files = sorted(GOLDEN_DIR.glob("*.parquet"))
    if not parquet_files:
        print("ERROR: No TXFD6 golden parquet files found!")
        sys.exit(1)

    print(f"Found {len(parquet_files)} TXFD6 golden days total")

    # Filter to March+ days only (tight-spread regime)
    march_files = [pf for pf in parquet_files if pf.stem >= "2026-03"]
    feb_files = [pf for pf in parquet_files if pf.stem < "2026-03"]
    print(f"Excluding {len(feb_files)} Feb days (wide-spread regime)")
    print(f"Using {len(march_files)} March/April days (tight-spread regime)")
    print(f"March days: {[pf.stem for pf in march_files]}")
    print("=" * 80)

    day_results = []
    pooled_list = []

    for pf in march_files:
        result = process_day(pf, cog_ema_period=COG_EMA_PERIOD)
        if result is None:
            continue
        _day_str, day_ic, pooled_arrays, _median_spread = result
        day_results.append(day_ic)
        pooled_list.append(pooled_arrays)

    # =====================================================================
    # Pooled Results (March-only, COG_EMA=9)
    # =====================================================================
    print("\n" + "=" * 80)
    print("POOLED RESULTS — MARCH-ONLY (COG_EMA_PERIOD=9)")
    print("=" * 80)

    if not day_results:
        print("ERROR: No valid March days!")
        sys.exit(1)

    results = pooled_analysis(day_results, pooled_list)

    # =====================================================================
    # COG_EMA Sensitivity Sweep
    # =====================================================================
    print("\n" + "=" * 80)
    print("COG_EMA SENSITIVITY SWEEP")
    print("=" * 80)

    ema_periods = [5, 9, 15, 30]
    ema_ic_uncond = {}  # keyed by ema_period
    ema_ic_cond = {}

    for ema_p in ema_periods:
        print(f"\n--- COG_EMA_PERIOD = {ema_p} ---")
        sweep_day_results = []
        sweep_pooled = []
        for pf in march_files:
            result = process_day(pf, cog_ema_period=ema_p)
            if result is None:
                continue
            _ds, dic, pa, _ms = result
            sweep_day_results.append(dic)
            sweep_pooled.append(pa)
        if not sweep_pooled:
            print("  No valid days")
            continue
        sr = pooled_analysis(sweep_day_results, sweep_pooled, label_prefix=f"EMA={ema_p}")
        ema_ic_uncond[ema_p] = sr["ic_uncond_fwd3"]
        ema_ic_cond[ema_p] = sr["ic_cond_fwd3"]

    # Sensitivity summary
    print("\n" + "=" * 80)
    print("COG_EMA SENSITIVITY SUMMARY (fwd_3)")
    print("=" * 80)
    print(f"  {'EMA_PERIOD':<12} | {'IC_uncond':>10} | {'IC_cond':>10} | {'delta':>10}")
    print(f"  {'-'*12}-+-{'-'*10}-+-{'-'*10}-+-{'-'*10}")
    cond_ics_all = []
    for ema_p in ema_periods:
        u = ema_ic_uncond.get(ema_p, np.nan)
        c = ema_ic_cond.get(ema_p, np.nan)
        d = c - u if not (np.isnan(c) or np.isnan(u)) else np.nan
        if not np.isnan(c):
            cond_ics_all.append(c)
        print(f"  {ema_p:<12} | {u:+.4f}     | {c:+.4f}     | {d:+.4f}" if not np.isnan(d) else
              f"  {ema_p:<12} |    NaN     |    NaN     |    NaN")

    # Check >50% IC variation across EMA periods
    if len(cond_ics_all) >= 2:
        ic_range = max(cond_ics_all) - min(cond_ics_all)
        ic_mean = np.mean(cond_ics_all)
        ic_var_pct = (ic_range / abs(ic_mean) * 100) if abs(ic_mean) > 1e-6 else np.inf
        print(f"\n  Conditioned IC range: {ic_range:.4f}, mean: {ic_mean:+.4f}, variation: {ic_var_pct:.1f}%")
        if ic_var_pct > 50:
            print("  ** FLAG: >50% IC variation across COG_EMA periods -> OVERFIT RISK **")
        else:
            print("  COG_EMA sensitivity OK (<50% variation)")
    else:
        ic_var_pct = np.nan
        print("  Insufficient data for sensitivity check")

    # =====================================================================
    # Kill/Proceed Verdict
    # =====================================================================
    print("\n" + "=" * 80)
    print("KILL / PROCEED VERDICT")
    print("=" * 80)

    ic_uncond = results["ic_uncond_fwd3"]
    delta = results["delta"]
    delta_over_se = results["delta_over_se"]
    N = results["N"]

    kill_reasons = []

    # Kill trigger 1: March-only unconditioned KDJ IC < 0.05
    if not np.isnan(ic_uncond) and abs(ic_uncond) < 0.05:
        kill_reasons.append(f"March-only unconditioned KDJ IC = {ic_uncond:+.4f} < 0.05 -> R32b IC inflation concern")
        print(f"  [FAIL] Unconditioned KDJ IC = {ic_uncond:+.4f} (< 0.05 threshold)")
    else:
        print(f"  [PASS] Unconditioned KDJ IC = {ic_uncond:+.4f} (>= 0.05)")

    # Kill trigger 2: delta < 1 SE
    if not np.isnan(delta_over_se) and abs(delta_over_se) < 1.0:
        kill_reasons.append(f"Conditioned-unconditioned delta/SE = {delta_over_se:+.2f} < 1.0 -> COG adds nothing")
        print(f"  [FAIL] Delta/SE = {delta_over_se:+.2f} (< 1.0)")
    elif not np.isnan(delta_over_se):
        print(f"  [PASS] Delta/SE = {delta_over_se:+.2f} (>= 1.0)")

    # Kill trigger 3: COG_EMA sensitivity > 50%
    if not np.isnan(ic_var_pct) and ic_var_pct > 50:
        kill_reasons.append(f"COG_EMA IC variation = {ic_var_pct:.1f}% > 50% -> overfit")
        print(f"  [FAIL] COG_EMA sensitivity = {ic_var_pct:.1f}% (> 50%)")
    elif not np.isnan(ic_var_pct):
        print(f"  [PASS] COG_EMA sensitivity = {ic_var_pct:.1f}% (<= 50%)")

    if kill_reasons:
        print(f"\n  VERDICT: **KILL** L-COG C3")
        for r in kill_reasons:
            print(f"    - {r}")
    else:
        print(f"\n  VERDICT: **PROCEED** to Gate B")

    results["kill_reasons"] = kill_reasons
    results["ema_sensitivity_pct"] = ic_var_pct
    results["ema_ic_cond"] = ema_ic_cond
    results["ema_ic_uncond"] = ema_ic_uncond
    return results


if __name__ == "__main__":
    results = run_exploration()
