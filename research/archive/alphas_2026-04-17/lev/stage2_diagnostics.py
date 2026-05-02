"""
R34 Stage 2 Empirical Diagnostics
==================================
Three pre-gate diagnostics for LEV and L-COG candidates.

Diagnostic 1: LEV Proxy IC + Autocorrelation
Diagnostic 2: COG-QI_1 Correlation
Diagnostic 3: LEV vs OFI Correlation
"""

import glob
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
GOLDEN_DIR = Path("research/data/real/golden/TXFD6")
PRICE_SCALE = 1_000_000  # golden parquet convention
POINT_VALUE = 200  # NTD per point for TXFD6

# Forward return horizons (seconds)
FWD_HORIZONS_S = [30, 60, 300]

# AC lags (seconds)
AC_LAGS_S = [1, 5, 30, 60]

# Spread regime threshold (points) - median of all days used as split
SPREAD_REGIME_SPLIT_PTS = 100  # days with median spread > 100 pts = "wide"


def load_day(path: Path) -> pd.DataFrame:
    """Load a single golden parquet day."""
    df = pd.read_parquet(path)
    return df


def extract_book_arrays(ba_df: pd.DataFrame):
    """Extract L1-L5 bid/ask prices and volumes as numpy arrays from BidAsk rows."""
    n = len(ba_df)
    bid_prices = np.zeros((n, 5), dtype=np.int64)
    bid_vols = np.zeros((n, 5), dtype=np.int64)
    ask_prices = np.zeros((n, 5), dtype=np.int64)
    ask_vols = np.zeros((n, 5), dtype=np.int64)
    timestamps = ba_df["exch_ts"].values.astype(np.int64)

    for i, (_, row) in enumerate(ba_df.iterrows()):
        bp = np.asarray(row["bids_price"], dtype=np.int64)
        bv = np.asarray(row["bids_vol"], dtype=np.int64)
        ap = np.asarray(row["asks_price"], dtype=np.int64)
        av = np.asarray(row["asks_vol"], dtype=np.int64)
        lvls = min(5, len(bp))
        bid_prices[i, :lvls] = bp[:lvls]
        bid_vols[i, :lvls] = bv[:lvls]
        lvls_a = min(5, len(ap))
        ask_prices[i, :lvls_a] = ap[:lvls_a]
        ask_vols[i, :lvls_a] = av[:lvls_a]

    return timestamps, bid_prices, bid_vols, ask_prices, ask_vols


def compute_mid_price(bid_prices, ask_prices):
    """Mid price from L1 bid/ask (in scaled units)."""
    return (bid_prices[:, 0] + ask_prices[:, 0]) / 2.0


def compute_spread_pts(bid_prices, ask_prices):
    """Spread in points from L1."""
    return (ask_prices[:, 0] - bid_prices[:, 0]) / PRICE_SCALE


def compute_ofi_depth_norm(bid_prices, bid_vols, ask_prices, ask_vols):
    """
    LEV proxy: OFI depth-normalized (FeatureEngine feature [16]).
    OFI_L1 = delta(bid_vol_L1) - delta(ask_vol_L1) at L1,
    normalized by total depth L1-L5.
    """
    n = len(bid_vols)
    ofi = np.zeros(n)
    for i in range(1, n):
        # L1 OFI: change in bid vol minus change in ask vol
        # Account for price level changes
        if bid_prices[i, 0] == bid_prices[i - 1, 0]:
            d_bid = bid_vols[i, 0] - bid_vols[i - 1, 0]
        elif bid_prices[i, 0] > bid_prices[i - 1, 0]:
            d_bid = bid_vols[i, 0]  # new level, all volume is "added"
        else:
            d_bid = -bid_vols[i - 1, 0]  # level dropped

        if ask_prices[i, 0] == ask_prices[i - 1, 0]:
            d_ask = ask_vols[i, 0] - ask_vols[i - 1, 0]
        elif ask_prices[i, 0] < ask_prices[i - 1, 0]:
            d_ask = ask_vols[i, 0]
        else:
            d_ask = -ask_vols[i - 1, 0]

        raw_ofi = d_bid - d_ask
        total_depth = (
            bid_vols[i, :].sum() + ask_vols[i, :].sum()
        )
        if total_depth > 0:
            ofi[i] = raw_ofi / total_depth
    return ofi


def compute_raw_ofi(bid_prices, bid_vols, ask_prices, ask_vols):
    """Raw OFI (unnormalized) for correlation comparison."""
    n = len(bid_vols)
    ofi = np.zeros(n)
    for i in range(1, n):
        if bid_prices[i, 0] == bid_prices[i - 1, 0]:
            d_bid = bid_vols[i, 0] - bid_vols[i - 1, 0]
        elif bid_prices[i, 0] > bid_prices[i - 1, 0]:
            d_bid = bid_vols[i, 0]
        else:
            d_bid = -bid_vols[i - 1, 0]

        if ask_prices[i, 0] == ask_prices[i - 1, 0]:
            d_ask = ask_vols[i, 0] - ask_vols[i - 1, 0]
        elif ask_prices[i, 0] < ask_prices[i - 1, 0]:
            d_ask = ask_vols[i, 0]
        else:
            d_ask = -ask_vols[i - 1, 0]

        ofi[i] = d_bid - d_ask
    return ofi


def compute_cog(bid_prices, bid_vols, ask_prices, ask_vols):
    """
    Center of Gravity for ask side (buy-COG) and bid side (sell-COG).
    COG = sum(price_i * qty_i) / sum(qty_i) for L1-L5.
    Returns buy_cog, sell_cog arrays.
    """
    n = len(bid_vols)
    buy_cog = np.zeros(n)
    sell_cog = np.zeros(n)

    for i in range(n):
        # Ask-side COG (buy COG)
        aq = ask_vols[i, :]
        total_aq = aq.sum()
        if total_aq > 0:
            buy_cog[i] = np.dot(ask_prices[i, :].astype(np.float64), aq.astype(np.float64)) / total_aq
        else:
            buy_cog[i] = ask_prices[i, 0]

        # Bid-side COG (sell COG)
        bq = bid_vols[i, :]
        total_bq = bq.sum()
        if total_bq > 0:
            sell_cog[i] = np.dot(bid_prices[i, :].astype(np.float64), bq.astype(np.float64)) / total_bq
        else:
            sell_cog[i] = bid_prices[i, 0]

    return buy_cog, sell_cog


def compute_qi_1(bid_vols, ask_vols):
    """Queue imbalance at L1: (bid_vol - ask_vol) / (bid_vol + ask_vol)."""
    bv = bid_vols[:, 0].astype(np.float64)
    av = ask_vols[:, 0].astype(np.float64)
    total = bv + av
    qi = np.where(total > 0, (bv - av) / total, 0.0)
    return qi


def ts_to_seconds(ts):
    """Convert nanosecond timestamps to seconds (float)."""
    return ts.astype(np.float64) / 1e9


def compute_forward_return(mid, timestamps_s, horizon_s):
    """
    Compute forward return (in points) at a given horizon.
    For each observation, find the first timestamp >= t + horizon_s.
    """
    n = len(mid)
    fwd_ret = np.full(n, np.nan)
    j = 0
    for i in range(n):
        target_t = timestamps_s[i] + horizon_s
        while j < n and timestamps_s[j] < target_t:
            j += 1
        if j < n:
            fwd_ret[i] = (mid[j] - mid[i]) / PRICE_SCALE  # in points
        j_save = j
        j = max(i + 1, j - 10)  # backtrack slightly for next iteration
        if j > j_save:
            j = j_save
    return fwd_ret


def compute_autocorrelation(signal, timestamps_s, lag_s):
    """
    Compute autocorrelation of signal at a given lag (seconds).
    Resample to 1-second bars first for clean AC computation.
    """
    # Create 1-second binned signal
    t_start = timestamps_s[0]
    t_end = timestamps_s[-1]
    n_bins = int(t_end - t_start) + 1
    if n_bins < 2 * lag_s:
        return np.nan

    bins = np.zeros(n_bins)
    counts = np.zeros(n_bins)
    for i in range(len(signal)):
        b = int(timestamps_s[i] - t_start)
        if 0 <= b < n_bins:
            bins[b] += signal[i]
            counts[b] += 1

    # Average per bin
    mask = counts > 0
    bins[mask] /= counts[mask]

    # Only use bins with data
    valid_bins = bins[mask]
    if len(valid_bins) < 2 * lag_s:
        return np.nan

    # Compute AC at lag
    x = valid_bins[:-lag_s]
    y = valid_bins[lag_s:]
    if len(x) < 10:
        return np.nan
    corr, _ = stats.pearsonr(x, y)
    return corr


def compute_ic(signal, fwd_ret):
    """Rank IC (Spearman correlation) between signal and forward return."""
    valid = ~np.isnan(fwd_ret) & ~np.isnan(signal)
    if valid.sum() < 30:
        return np.nan
    corr, _ = stats.spearmanr(signal[valid], fwd_ret[valid])
    return corr


def count_divergences(mid, lev, timestamps_s, window_s=30):
    """
    Count events where price makes a new 30s high/low but LEV is declining.
    Returns count per trading day.
    """
    n = len(mid)
    divergence_count = 0

    # Use 30-second rolling window
    j_start = 0
    for i in range(1, n):
        # Advance window start
        while j_start < i and timestamps_s[i] - timestamps_s[j_start] > window_s:
            j_start += 1

        if j_start >= i:
            continue

        window_mid = mid[j_start:i + 1]
        window_lev = lev[j_start:i + 1]

        if len(window_mid) < 5:
            continue

        # New high: current mid is max of window
        is_new_high = mid[i] >= window_mid.max()
        # New low: current mid is min of window
        is_new_low = mid[i] <= window_mid.min()

        if is_new_high or is_new_low:
            # Check if LEV is declining (compare last 25% of window to first 25%)
            quarter = max(1, len(window_lev) // 4)
            lev_recent = np.mean(window_lev[-quarter:])
            lev_early = np.mean(window_lev[:quarter])

            if is_new_high and lev_recent < lev_early - 0.001:
                divergence_count += 1
            elif is_new_low and lev_recent > lev_early + 0.001:
                divergence_count += 1

    return divergence_count


def subsample_bidask(ba_df, max_rows=50000):
    """Subsample BidAsk rows if too many, keeping even spacing."""
    if len(ba_df) <= max_rows:
        return ba_df
    step = len(ba_df) // max_rows
    return ba_df.iloc[::step].reset_index(drop=True)


def run_diagnostics():
    """Main entry point."""
    parquet_files = sorted(GOLDEN_DIR.glob("*.parquet"))
    if not parquet_files:
        print("ERROR: No TXFD6 golden parquet files found!")
        sys.exit(1)

    print(f"Found {len(parquet_files)} TXFD6 golden days")
    print("=" * 80)

    # Per-day results storage
    day_results = []

    for pf in parquet_files:
        day_str = pf.stem
        print(f"\n--- Processing {day_str} ---")

        df = load_day(pf)
        ba_df = df[df["type"] == "BidAsk"].reset_index(drop=True)

        if len(ba_df) < 100:
            print(f"  Skipping: only {len(ba_df)} BidAsk rows")
            continue

        # Subsample large days for speed
        ba_df = subsample_bidask(ba_df, max_rows=50000)
        print(f"  Using {len(ba_df)} BidAsk rows")

        # Extract arrays
        timestamps, bid_prices, bid_vols, ask_prices, ask_vols = extract_book_arrays(ba_df)
        timestamps_s = ts_to_seconds(timestamps)

        # Filter out rows where L1 prices are zero
        valid_mask = (bid_prices[:, 0] > 0) & (ask_prices[:, 0] > 0)
        if valid_mask.sum() < 100:
            print(f"  Skipping: only {valid_mask.sum()} valid L1 rows")
            continue

        timestamps = timestamps[valid_mask]
        timestamps_s = timestamps_s[valid_mask]
        bid_prices = bid_prices[valid_mask]
        bid_vols = bid_vols[valid_mask]
        ask_prices = ask_prices[valid_mask]
        ask_vols = ask_vols[valid_mask]

        # Compute features
        mid = compute_mid_price(bid_prices, ask_prices)
        spread_pts = compute_spread_pts(bid_prices, ask_prices)
        median_spread = np.median(spread_pts)
        lev_proxy = compute_ofi_depth_norm(bid_prices, bid_vols, ask_prices, ask_vols)
        raw_ofi = compute_raw_ofi(bid_prices, bid_vols, ask_prices, ask_vols)
        buy_cog, sell_cog = compute_cog(bid_prices, bid_vols, ask_prices, ask_vols)
        qi_1 = compute_qi_1(bid_vols, ask_vols)

        print(f"  Median spread: {median_spread:.1f} pts")

        # --- Diagnostic 1: AC ---
        ac_results = {}
        for lag in AC_LAGS_S:
            ac = compute_autocorrelation(lev_proxy, timestamps_s, lag)
            ac_results[lag] = ac
            print(f"  LEV AC(lag={lag}s): {ac:.4f}" if not np.isnan(ac) else f"  LEV AC(lag={lag}s): NaN")

        # --- Diagnostic 1: IC ---
        ic_results = {}
        for hz in FWD_HORIZONS_S:
            fwd_ret = compute_forward_return(mid, timestamps_s, hz)
            ic = compute_ic(lev_proxy, fwd_ret)
            ic_results[hz] = ic
            print(f"  LEV IC(fwd={hz}s): {ic:.4f}" if not np.isnan(ic) else f"  LEV IC(fwd={hz}s): NaN")

        # --- Diagnostic 1: Divergence count (for C3 Absorption Score) ---
        div_count = count_divergences(mid, lev_proxy, timestamps_s, window_s=30)
        print(f"  LEV-price divergences (30s window): {div_count}")

        # --- Diagnostic 2: COG-QI_1 correlation ---
        # COG regime indicator: sign(buy_cog - mid) for ask side
        cog_regime = np.sign(buy_cog - mid)
        qi_direction = np.sign(qi_1)
        # Rank correlation between COG-mid deviation and QI_1
        cog_deviation = (buy_cog - mid) / PRICE_SCALE  # in points
        valid_cog = (np.abs(qi_1) > 0.001) & (np.abs(cog_deviation) > 0)
        if valid_cog.sum() > 30:
            cog_qi_corr, _ = stats.spearmanr(cog_deviation[valid_cog], qi_1[valid_cog])
        else:
            cog_qi_corr = np.nan
        print(f"  COG-QI_1 rank corr: {cog_qi_corr:.4f}" if not np.isnan(cog_qi_corr) else "  COG-QI_1 rank corr: NaN")

        # --- Diagnostic 3: LEV vs raw OFI correlation ---
        valid_ofi = (np.abs(raw_ofi) > 0) & (np.abs(lev_proxy) > 0)
        if valid_ofi.sum() > 30:
            lev_ofi_corr, _ = stats.pearsonr(lev_proxy[valid_ofi], raw_ofi[valid_ofi])
        else:
            lev_ofi_corr = np.nan
        print(f"  LEV vs raw OFI corr: {lev_ofi_corr:.4f}" if not np.isnan(lev_ofi_corr) else "  LEV vs raw OFI corr: NaN")

        day_results.append({
            "day": day_str,
            "n_rows": len(timestamps),
            "median_spread_pts": median_spread,
            "is_wide_spread": median_spread > SPREAD_REGIME_SPLIT_PTS,
            "ac": ac_results,
            "ic": ic_results,
            "div_count": div_count,
            "cog_qi_corr": cog_qi_corr,
            "lev_ofi_corr": lev_ofi_corr,
        })

    # =====================================================================
    # Aggregate results and verdicts
    # =====================================================================
    print("\n" + "=" * 80)
    print("AGGREGATE RESULTS")
    print("=" * 80)

    if not day_results:
        print("ERROR: No valid days processed!")
        sys.exit(1)

    # --- Diagnostic 1: AC ---
    print("\n[D1] LEV Autocorrelation")
    for lag in AC_LAGS_S:
        acs = [d["ac"][lag] for d in day_results if not np.isnan(d["ac"].get(lag, np.nan))]
        if acs:
            print(f"  Lag {lag}s: mean={np.mean(acs):.4f}, median={np.median(acs):.4f}, "
                  f"min={np.min(acs):.4f}, max={np.max(acs):.4f} (N={len(acs)} days)")
        else:
            print(f"  Lag {lag}s: NO DATA")

    ac_5s_values = [d["ac"][5] for d in day_results if not np.isnan(d["ac"].get(5, np.nan))]
    ac_5s_mean = np.mean(ac_5s_values) if ac_5s_values else np.nan
    ac_5s_kill = not np.isnan(ac_5s_mean) and ac_5s_mean < 0.02
    print(f"\n  VERDICT AC(5s): mean={ac_5s_mean:.4f} {'< 0.02 -> KILL ALL LEV' if ac_5s_kill else '>= 0.02 -> PASS'}")

    # --- Diagnostic 1: IC by spread regime ---
    print("\n[D1] LEV IC by Spread Regime")
    wide_days = [d for d in day_results if d["is_wide_spread"]]
    tight_days = [d for d in day_results if not d["is_wide_spread"]]

    print(f"  Wide spread days ({len(wide_days)}): {[d['day'] for d in wide_days]}")
    print(f"  Tight spread days ({len(tight_days)}): {[d['day'] for d in tight_days]}")

    ic_regime_kill = False
    for hz in FWD_HORIZONS_S:
        wide_ics = [d["ic"][hz] for d in wide_days if not np.isnan(d["ic"].get(hz, np.nan))]
        tight_ics = [d["ic"][hz] for d in tight_days if not np.isnan(d["ic"].get(hz, np.nan))]
        wide_mean = np.mean(wide_ics) if wide_ics else np.nan
        tight_mean = np.mean(tight_ics) if tight_ics else np.nan
        ratio = abs(wide_mean / tight_mean) if (not np.isnan(tight_mean) and abs(tight_mean) > 1e-6) else np.nan

        print(f"  Fwd {hz}s: wide IC={wide_mean:.4f}, tight IC={tight_mean:.4f}, ratio={ratio:.2f}"
              if not np.isnan(ratio) else
              f"  Fwd {hz}s: wide IC={wide_mean if not np.isnan(wide_mean) else 'N/A'}, "
              f"tight IC={tight_mean if not np.isnan(tight_mean) else 'N/A'}")

        if not np.isnan(ratio) and ratio > 3.0:
            ic_regime_kill = True
            print(f"    -> Ratio > 3x: C1 (LEV-Classic) would be KILLED")

    print(f"\n  VERDICT IC regime: {'KILL C1 (IC ratio > 3x)' if ic_regime_kill else 'PASS (IC ratio <= 3x)'}")

    # --- Diagnostic 1: Divergence count ---
    print("\n[D1] LEV-Price Divergences (C3 Absorption Score)")
    for d in day_results:
        duration_hrs = (ts_to_seconds(np.array([0]))[0] +
                       (d["n_rows"] / max(1, d["n_rows"])) * 4.5)  # approximate
        print(f"  {d['day']}: {d['div_count']} divergences")

    div_counts = [d["div_count"] for d in day_results]
    mean_div = np.mean(div_counts)
    div_kill = mean_div < 5
    print(f"\n  VERDICT divergences: mean={mean_div:.1f}/day "
          f"{'< 5 -> KILL C3' if div_kill else '>= 5 -> PASS'}")

    # --- Diagnostic 2: COG-QI_1 ---
    print("\n[D2] COG-QI_1 Correlation")
    cog_qi_corrs = [d["cog_qi_corr"] for d in day_results if not np.isnan(d["cog_qi_corr"])]
    if cog_qi_corrs:
        cog_qi_mean = np.mean(cog_qi_corrs)
        print(f"  Mean corr: {cog_qi_mean:.4f} (N={len(cog_qi_corrs)} days)")
        for d in day_results:
            if not np.isnan(d["cog_qi_corr"]):
                print(f"    {d['day']}: {d['cog_qi_corr']:.4f}")
        cog_kill = cog_qi_mean > 0.7
        print(f"\n  VERDICT: {'KILL L-COG C3 (corr > 0.7, redundant with QI_1)' if cog_kill else 'PASS (corr <= 0.7)'}")
    else:
        cog_qi_mean = np.nan
        cog_kill = False
        print("  NO DATA")

    # --- Diagnostic 3: LEV vs OFI ---
    print("\n[D3] LEV vs Raw OFI Correlation")
    lev_ofi_corrs = [d["lev_ofi_corr"] for d in day_results if not np.isnan(d["lev_ofi_corr"])]
    if lev_ofi_corrs:
        lev_ofi_mean = np.mean(lev_ofi_corrs)
        print(f"  Mean corr: {lev_ofi_mean:.4f} (N={len(lev_ofi_corrs)} days)")
        for d in day_results:
            if not np.isnan(d["lev_ofi_corr"]):
                print(f"    {d['day']}: {d['lev_ofi_corr']:.4f}")
        lev_ofi_kill = lev_ofi_mean > 0.9
        print(f"\n  VERDICT: {'LEV is rescaled OFI (corr > 0.9)' if lev_ofi_kill else 'LEV has independent info (corr <= 0.9)'}")
    else:
        lev_ofi_mean = np.nan
        lev_ofi_kill = False
        print("  NO DATA")

    # =====================================================================
    # Final Summary
    # =====================================================================
    print("\n" + "=" * 80)
    print("FINAL VERDICT SUMMARY")
    print("=" * 80)

    print(f"\n[D1-AC]  LEV AC(5s) = {ac_5s_mean:.4f}  ->  {'KILL ALL LEV (AC < 0.02)' if ac_5s_kill else 'PASS'}")
    print(f"[D1-IC]  IC regime ratio         ->  {'KILL C1 LEV-Classic (ratio > 3x)' if ic_regime_kill else 'PASS'}")
    print(f"[D1-DIV] Divergences = {mean_div:.1f}/day  ->  {'KILL C3 Absorption (< 5/day)' if div_kill else 'PASS'}")
    print(f"[D2]     COG-QI_1 corr = {cog_qi_mean:.4f}  ->  {'KILL L-COG C3 (> 0.7)' if cog_kill else 'PASS'}")
    print(f"[D3]     LEV-OFI corr = {lev_ofi_mean:.4f}   ->  {'LEV ~ rescaled OFI' if lev_ofi_kill else 'LEV has independent info'}")

    # Overall
    all_lev_killed = ac_5s_kill
    print(f"\nOVERALL LEV STATUS: {'ALL KILLED (AC too low)' if all_lev_killed else 'SOME CANDIDATES SURVIVE'}")
    if not all_lev_killed:
        c1_alive = not ic_regime_kill
        c3_alive = not div_kill
        print(f"  C1 (LEV-Classic): {'ALIVE' if c1_alive else 'KILLED'}")
        print(f"  C3 (Absorption Score): {'ALIVE' if c3_alive else 'KILLED'}")

    print(f"\nL-COG C3 STATUS: {'KILLED (redundant with QI_1)' if cog_kill else 'ALIVE'}")

    return {
        "ac_5s_mean": ac_5s_mean,
        "ac_5s_kill": ac_5s_kill,
        "ic_regime_kill": ic_regime_kill,
        "div_mean": mean_div,
        "div_kill": div_kill,
        "cog_qi_mean": cog_qi_mean,
        "cog_kill": cog_kill,
        "lev_ofi_mean": lev_ofi_mean,
        "lev_ofi_kill": lev_ofi_kill,
        "day_results": day_results,
    }


if __name__ == "__main__":
    results = run_diagnostics()
