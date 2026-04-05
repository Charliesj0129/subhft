#!/usr/bin/env python3
"""
R25b Corrected Analysis: Large Single-Tick Order Flow on TMFD6

Strategy thesis: Single large trades (high volume per tick) that move price
indicate informed flow. Enter in the SAME direction, fixed SL + trailing TP.

This script queries ClickHouse directly and tests:
1. Event frequency by volume threshold
2. Single-tick price impact (large vs small)
3. Forward returns after large orders (direction-adjusted)
4. KS test (large volume vs random baseline)
5. Combined signal (large volume + price jump)
6. Regime split (March vs Jan/Feb)

Kill conditions:
- Mean fwd return < 4 pts at ALL horizons → KILL (below RT cost)
- < 100 qualifying events in March → insufficient data
- Large-volume ticks don't move price more than small ticks → thesis falsified
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import stats

# ---------------------------------------------------------------------------
# ClickHouse helper
# ---------------------------------------------------------------------------

SCALE = 1_000_000  # price_scaled -> points (CH stores as internal_scaled * CH_SCALE / price_scale)
LATENCY_NS = 36_000_000  # 36 ms entry latency
RT_COST_PTS = 4.0  # round-trip cost in points
HORIZONS_S = [5, 10, 30, 60, 120]
HORIZONS_NS = [h * 1_000_000_000 for h in HORIZONS_S]
VOL_THRESHOLDS = [5, 10, 20, 50, 100]

OUT_DIR = Path("outputs/team_artifacts/alpha-research-r25b")


def query_ch(sql: str) -> str:
    """Run a ClickHouse query via docker exec and return raw output."""
    r = subprocess.run(
        ["docker", "exec", "clickhouse", "clickhouse-client", "--query", sql],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if r.returncode != 0:
        print(f"[ERROR] ClickHouse query failed: {r.stderr}", file=sys.stderr)
        sys.exit(1)
    return r.stdout.strip()


def load_ticks() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Load all TMFD6 tick data: (price_pts, volume, exch_ts_ns, day_str)."""
    print("[1/6] Loading tick data from ClickHouse ...")
    raw = query_ch(
        "SELECT price_scaled, volume, exch_ts, "
        "toString(toDate(toDateTime64(exch_ts/1e9, 3))) as day "
        "FROM hft.market_data "
        "WHERE symbol = 'TMFD6' AND type = 'Tick' "
        "ORDER BY exch_ts "
        "FORMAT TabSeparated"
    )
    lines = raw.split("\n")
    n = len(lines)
    price = np.empty(n, dtype=np.float64)
    volume = np.empty(n, dtype=np.int64)
    ts = np.empty(n, dtype=np.int64)
    days = []
    for i, line in enumerate(lines):
        parts = line.split("\t")
        price[i] = int(parts[0]) / SCALE
        volume[i] = int(parts[1])
        ts[i] = int(parts[2])
        days.append(parts[3])
    days_arr = np.array(days)
    print(f"  Loaded {n:,} ticks, {len(set(days))} unique days")
    return price, volume, ts, days_arr


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def compute_forward_returns(
    price: np.ndarray,
    ts: np.ndarray,
    indices: np.ndarray,
    direction: np.ndarray,
) -> dict[int, np.ndarray]:
    """
    For each tick index, compute direction-adjusted forward return in points
    at each horizon, applying latency delay.
    Returns {horizon_s: array_of_returns}.
    """
    n_total = len(price)
    results = {h: np.full(len(indices), np.nan) for h in HORIZONS_S}

    # Build a pointer for each index to find the future price
    for k, idx in enumerate(indices):
        entry_ts = ts[idx] + LATENCY_NS
        # Find entry price (first tick at or after entry_ts)
        entry_j = np.searchsorted(ts, entry_ts, side="left")
        if entry_j >= n_total:
            continue
        entry_price = price[entry_j]

        for h_s, h_ns in zip(HORIZONS_S, HORIZONS_NS):
            exit_ts = entry_ts + h_ns
            exit_j = np.searchsorted(ts, exit_ts, side="left")
            if exit_j >= n_total:
                continue
            # Use the tick just before or at exit_ts
            if ts[exit_j] > exit_ts and exit_j > 0:
                exit_j -= 1
            exit_price = price[exit_j]
            raw_ret = exit_price - entry_price
            results[h_s][k] = raw_ret * direction[k]

    return results


def summarize_returns(rets: np.ndarray) -> dict:
    valid = rets[~np.isnan(rets)]
    if len(valid) == 0:
        return {"n": 0, "mean": None, "median": None, "std": None}
    return {
        "n": int(len(valid)),
        "mean": round(float(np.mean(valid)), 4),
        "median": round(float(np.median(valid)), 4),
        "std": round(float(np.std(valid)), 4),
    }


def regime_mask(days: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return boolean masks for jan_feb and march."""
    march = np.array([d >= "2026-03" for d in days])
    jan_feb = ~march
    return jan_feb, march


# ---------------------------------------------------------------------------
# Main analyses
# ---------------------------------------------------------------------------

def main() -> None:
    price, volume, ts, days = load_ticks()
    jan_feb_mask, march_mask = regime_mask(days)
    n = len(price)

    # Price change per tick
    dprice = np.zeros(n)
    dprice[1:] = price[1:] - price[:-1]

    # Day boundaries: reset dprice at first tick of each day
    day_boundaries = np.where(days[1:] != days[:-1])[0] + 1
    dprice[day_boundaries] = 0.0
    dprice[0] = 0.0

    results = {}

    # =======================================================================
    # Analysis 1: Event Frequency by Volume Threshold
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 1: Event Frequency by Volume Threshold")
    print("=" * 70)

    freq_table = {}
    unique_days = np.unique(days)
    jan_feb_days = [d for d in unique_days if d < "2026-03"]
    march_days = [d for d in unique_days if d >= "2026-03"]

    for thresh in VOL_THRESHOLDS:
        mask = volume >= thresh
        jf_count = int(np.sum(mask & jan_feb_mask))
        mar_count = int(np.sum(mask & march_mask))
        jf_per_day = jf_count / max(len(jan_feb_days), 1)
        mar_per_day = mar_count / max(len(march_days), 1)
        freq_table[thresh] = {
            "jan_feb_total": jf_count,
            "jan_feb_per_day": round(jf_per_day, 1),
            "march_total": mar_count,
            "march_per_day": round(mar_per_day, 1),
        }
        print(f"  vol >= {thresh:>3d}: Jan/Feb {jf_count:>5d} ({jf_per_day:>6.1f}/day)  "
              f"March {mar_count:>5d} ({mar_per_day:>6.1f}/day)")

    results["analysis_1_frequency"] = freq_table

    # =======================================================================
    # Analysis 2: Single-Tick Price Impact
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 2: Single-Tick Price Impact")
    print("=" * 70)

    impact_table = {}
    for label, mask in [("vol==1", volume == 1), ("vol>=10", volume >= 10),
                        ("vol>=20", volume >= 20), ("vol>=50", volume >= 50)]:
        dp = np.abs(dprice[mask])
        n_ticks = int(np.sum(mask))
        mean_impact = float(np.mean(dp))
        frac_ge1 = float(np.mean(dp >= 1.0))
        frac_ge2 = float(np.mean(dp >= 2.0))
        impact_table[label] = {
            "count": n_ticks,
            "mean_abs_impact_pts": round(mean_impact, 4),
            "frac_ge_1pt": round(frac_ge1, 4),
            "frac_ge_2pt": round(frac_ge2, 4),
        }
        print(f"  {label:>8s}: n={n_ticks:>8d}  mean|dp|={mean_impact:.4f} pts  "
              f">=1pt: {frac_ge1:.2%}  >=2pt: {frac_ge2:.2%}")

    results["analysis_2_price_impact"] = impact_table

    # Check kill condition: large orders don't move price more
    small_mean = impact_table["vol==1"]["mean_abs_impact_pts"]
    large_mean = impact_table["vol>=10"]["mean_abs_impact_pts"]
    if large_mean <= small_mean:
        print("  *** KILL SIGNAL: Large-volume ticks do NOT move price more than small ***")
        results["kill_price_impact"] = True
    else:
        ratio = large_mean / max(small_mean, 1e-9)
        print(f"  Large/small impact ratio: {ratio:.2f}x")
        results["kill_price_impact"] = False

    # =======================================================================
    # Analysis 3: Forward Returns After Large Orders
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 3: Forward Returns (Direction-Adjusted, 36ms Latency)")
    print("=" * 70)

    fwd_table = {}
    for label, vol_thresh in [("vol>=10", 10), ("vol>=20", 20), ("vol>=50", 50)]:
        mask = (volume >= vol_thresh) & (dprice != 0)
        indices = np.where(mask)[0]
        direction = np.sign(dprice[indices])

        print(f"\n  --- {label} with price change (n={len(indices)}) ---")
        fwd = compute_forward_returns(price, ts, indices, direction)
        horizon_results = {}
        for h in HORIZONS_S:
            s = summarize_returns(fwd[h])
            horizon_results[f"{h}s"] = s
            mean_str = f"{s['mean']:.4f}" if s['mean'] is not None else "N/A"
            med_str = f"{s['median']:.4f}" if s['median'] is not None else "N/A"
            verdict = ""
            if s['mean'] is not None:
                verdict = "PASS" if s['mean'] >= RT_COST_PTS else "FAIL (<4pts RT cost)"
            print(f"    {h:>3d}s: n={s['n']:>5d}  mean={mean_str:>8s}  "
                  f"med={med_str:>8s}  std={s.get('std',''):>8}  {verdict}")

        fwd_table[label] = horizon_results

    # Unconditional baseline
    print("\n  --- BASELINE: all ticks with dprice != 0 ---")
    base_mask = dprice != 0
    base_idx = np.where(base_mask)[0]
    base_dir = np.sign(dprice[base_idx])
    base_fwd = compute_forward_returns(price, ts, base_idx[:10000], base_dir[:10000])
    baseline_results = {}
    for h in HORIZONS_S:
        s = summarize_returns(base_fwd[h])
        baseline_results[f"{h}s"] = s
        mean_str = f"{s['mean']:.4f}" if s['mean'] is not None else "N/A"
        print(f"    {h:>3d}s: n={s['n']:>5d}  mean={mean_str:>8s}")

    fwd_table["baseline_all_ticks"] = baseline_results
    results["analysis_3_forward_returns"] = fwd_table

    # =======================================================================
    # Analysis 4: KS Test
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 4: KS Test (Large Volume vs Random Baseline)")
    print("=" * 70)

    ks_table = {}
    for label, vol_thresh in [("vol>=10", 10), ("vol>=20", 20), ("vol>=50", 50)]:
        mask = (volume >= vol_thresh) & (dprice != 0)
        indices = np.where(mask)[0]
        direction = np.sign(dprice[indices])
        fwd_large = compute_forward_returns(price, ts, indices, direction)

        # Random baseline: same size sample from all ticks with dprice != 0
        rng = np.random.default_rng(42)
        sample_size = min(len(indices), len(base_idx))
        rand_idx = rng.choice(base_idx, size=sample_size, replace=False)
        rand_dir = np.sign(dprice[rand_idx])
        fwd_rand = compute_forward_returns(price, ts, rand_idx, rand_dir)

        print(f"\n  --- {label} vs random (n={sample_size}) ---")
        ks_results = {}
        for h in HORIZONS_S:
            large_vals = fwd_large[h][~np.isnan(fwd_large[h])]
            rand_vals = fwd_rand[h][~np.isnan(fwd_rand[h])]
            if len(large_vals) < 10 or len(rand_vals) < 10:
                print(f"    {h:>3d}s: insufficient data")
                continue
            ks_stat, p_val = stats.ks_2samp(large_vals, rand_vals)
            # Cohen's d
            pooled_std = np.sqrt((np.var(large_vals) + np.var(rand_vals)) / 2)
            cohens_d = (np.mean(large_vals) - np.mean(rand_vals)) / pooled_std if pooled_std > 0 else 0
            ks_results[f"{h}s"] = {
                "ks_stat": round(float(ks_stat), 4),
                "p_value": float(p_val),
                "cohens_d": round(float(cohens_d), 4),
                "large_mean_pts": round(float(np.mean(large_vals)), 4),
                "random_mean_pts": round(float(np.mean(rand_vals)), 4),
            }
            sig = "***" if p_val < 0.001 else "**" if p_val < 0.01 else "*" if p_val < 0.05 else "ns"
            print(f"    {h:>3d}s: KS={ks_stat:.4f} p={p_val:.4e} {sig}  "
                  f"Cohen's d={cohens_d:.4f}  "
                  f"large={np.mean(large_vals):.4f} vs rand={np.mean(rand_vals):.4f} pts")

        ks_table[label] = ks_results

    results["analysis_4_ks_test"] = ks_table

    # =======================================================================
    # Analysis 5: Combined Signal (Large Volume + Price Jump >= 2 pts)
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 5: Combined Signal (Volume + Price Jump)")
    print("=" * 70)

    combined_table = {}
    for vol_thresh in [10, 20, 50]:
        mask = (volume >= vol_thresh) & (np.abs(dprice) >= 2.0)
        indices = np.where(mask)[0]
        n_events = len(indices)
        n_march = int(np.sum(march_mask[indices])) if n_events > 0 else 0

        print(f"\n  vol>={vol_thresh} AND |dp|>=2pts: {n_events} events ({n_march} in March)")

        if n_events < 5:
            print("    Too few events, skipping")
            combined_table[f"vol>={vol_thresh}_dp>=2"] = {"count": n_events, "march_count": n_march}
            continue

        direction = np.sign(dprice[indices])
        fwd = compute_forward_returns(price, ts, indices, direction)
        horizon_results = {"count": n_events, "march_count": n_march}
        for h in HORIZONS_S:
            s = summarize_returns(fwd[h])
            horizon_results[f"{h}s"] = s
            mean_str = f"{s['mean']:.4f}" if s['mean'] is not None else "N/A"
            verdict = ""
            if s['mean'] is not None:
                verdict = "PASS" if s['mean'] >= RT_COST_PTS else "FAIL"
            print(f"    {h:>3d}s: n={s['n']:>5d}  mean={mean_str:>8s}  {verdict}")

        combined_table[f"vol>={vol_thresh}_dp>=2"] = horizon_results

    results["analysis_5_combined_signal"] = combined_table

    # =======================================================================
    # Analysis 6: Regime Split (March vs Jan/Feb)
    # =======================================================================
    print("\n" + "=" * 70)
    print("ANALYSIS 6: Regime Split — vol>=10, dprice!=0")
    print("=" * 70)

    regime_table = {}
    for regime_name, rmask in [("jan_feb", jan_feb_mask), ("march", march_mask)]:
        mask = (volume >= 10) & (dprice != 0) & rmask
        indices = np.where(mask)[0]
        direction = np.sign(dprice[indices])
        print(f"\n  --- {regime_name} (n={len(indices)}) ---")
        if len(indices) < 5:
            print("    Too few events")
            regime_table[regime_name] = {"count": len(indices)}
            continue
        fwd = compute_forward_returns(price, ts, indices, direction)
        horizon_results = {"count": int(len(indices))}
        for h in HORIZONS_S:
            s = summarize_returns(fwd[h])
            horizon_results[f"{h}s"] = s
            mean_str = f"{s['mean']:.4f}" if s['mean'] is not None else "N/A"
            print(f"    {h:>3d}s: n={s['n']:>5d}  mean={mean_str:>8s}")
        regime_table[regime_name] = horizon_results

    results["analysis_6_regime_split"] = regime_table

    # =======================================================================
    # Final Verdict
    # =======================================================================
    print("\n" + "=" * 70)
    print("FINAL VERDICT")
    print("=" * 70)

    # Check kill conditions
    kills = []

    # Kill 1: price impact
    if results.get("kill_price_impact"):
        kills.append("Large-volume ticks do NOT move price more than small-volume ticks")

    # Kill 2: forward returns all below cost
    best_mean = -999
    for label in ["vol>=10", "vol>=20", "vol>=50"]:
        fwd_data = results["analysis_3_forward_returns"].get(label, {})
        for h in HORIZONS_S:
            h_data = fwd_data.get(f"{h}s", {})
            m = h_data.get("mean")
            if m is not None and m > best_mean:
                best_mean = m
    if best_mean < RT_COST_PTS:
        kills.append(f"Best mean fwd return ({best_mean:.4f} pts) < 4 pts RT cost at ALL horizons")

    # Kill 3: insufficient March events for combined signal
    march_combined = results["analysis_5_combined_signal"].get("vol>=10_dp>=2", {})
    if march_combined.get("march_count", 0) < 100:
        kills.append(
            f"Combined signal (vol>=10 + dp>=2) has only "
            f"{march_combined.get('march_count', 0)} March events (<100)"
        )

    if kills:
        verdict = "KILL"
        print(f"  Verdict: **KILL**")
        for k in kills:
            print(f"    - {k}")
    else:
        verdict = "PROCEED"
        print(f"  Verdict: **PROCEED** to next stage")

    results["verdict"] = verdict
    results["kill_reasons"] = kills

    # Save results
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / "stage3b_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to {out_path}")


if __name__ == "__main__":
    main()
