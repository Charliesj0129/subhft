"""
Round 17 Stage 2 Expanded: Non-overlapping IC analysis for 2330 → TMF lead-lag.

Loads aligned npz files, resamples to 1s bars, computes:
1. Pooled IC with non-overlapping windows
2. Per-day IC and sign consistency
3. TMFD6 self-prediction IC (control)
4. Incremental IC of 2330 over self-prediction (partial correlation)
5. Bootstrap 95% CI on pooled IC (day-level resampling)
6. Top/bottom quintile mean return (expected edge in bps)

Kill gates:
- IC >= 0.03 with p < 0.05 on non-overlapping data
- Sign consistent >= 70% of days
- Incremental IC over self > 0.02
- Net edge > 3 bps per trade (after 1.33 bps RT cost)
"""

import numpy as np
import pandas as pd
from pathlib import Path
from scipy import stats

BASE = Path(__file__).resolve().parent.parent.parent.parent.parent
DATA_DIR = BASE / "research" / "data" / "processed" / "tsmc_leadlag"

LOOKBACKS = [30, 60, 120, 300]
HORIZONS = [60, 120, 300, 600]

RT_COST_BPS = 1.33


def load_all_days() -> dict:
    """Load all aligned npz files, resample to 1s, return dict of DataFrames."""
    days = {}
    for f in sorted(DATA_DIR.glob("aligned_*.npz")):
        date_str = f.stem.replace("aligned_", "")
        data = np.load(f)
        stock = pd.DataFrame(data["stock"])
        futures = pd.DataFrame(data["futures"])

        stock["ts"] = pd.to_datetime(stock["local_ts"], unit="ns")
        futures["ts"] = pd.to_datetime(futures["local_ts"], unit="ns")
        stock = stock.set_index("ts").sort_index()
        futures = futures.set_index("ts").sort_index()

        # Resample to 1-second bars
        s1 = stock.resample("1s").last().dropna(subset=["mid_price"])
        f1 = futures.resample("1s").last().dropna(subset=["mid_price"])

        # Inner join on common timestamps
        common = s1.index.intersection(f1.index)
        if len(common) < 500:
            print(f"  {date_str}: SKIP ({len(common)} common bars)")
            continue

        df = pd.DataFrame(index=common)
        df["mid_stock"] = s1.loc[common, "mid_price"].values
        df["mid_fut"] = f1.loc[common, "mid_price"].values
        df["bid_qty_stock"] = s1.loc[common, "bid_qty"].values
        df["ask_qty_stock"] = s1.loc[common, "ask_qty"].values
        days[date_str] = df
        print(f"  {date_str}: {len(df)} aligned 1s bars")

    return days


def compute_non_overlapping_ic(
    signal: np.ndarray, fwd_ret: np.ndarray, step: int
) -> tuple:
    """Compute Spearman IC on non-overlapping samples (every `step` rows)."""
    idx = np.arange(0, len(signal), step)
    s = signal[idx]
    r = fwd_ret[idx]
    valid = np.isfinite(s) & np.isfinite(r)
    if valid.sum() < 20:
        return np.nan, np.nan, 0
    ic, pval = stats.spearmanr(s[valid], r[valid])
    return ic, pval, int(valid.sum())


def log_return(prices: np.ndarray, shift: int) -> np.ndarray:
    """Compute log return: log(price[i] / price[i - shift])."""
    ret = np.full(len(prices), np.nan)
    if shift > 0:
        valid = (prices[shift:] > 0) & (prices[:-shift] > 0)
        ret[shift:] = np.where(valid, np.log(prices[shift:] / prices[:-shift]), np.nan)
    return ret


def forward_return(prices: np.ndarray, horizon: int) -> np.ndarray:
    """Compute forward log return: log(price[i + horizon] / price[i])."""
    ret = np.full(len(prices), np.nan)
    n = len(prices)
    if horizon < n:
        valid = (prices[horizon:] > 0) & (prices[:n - horizon] > 0)
        ret[:n - horizon] = np.where(
            valid, np.log(prices[horizon:] / prices[:n - horizon]), np.nan
        )
    return ret


def partial_correlation(x: np.ndarray, y: np.ndarray, z: np.ndarray) -> float:
    """Partial rank correlation of x and y controlling for z."""
    valid = np.isfinite(x) & np.isfinite(y) & np.isfinite(z)
    if valid.sum() < 20:
        return np.nan
    x, y, z = x[valid], y[valid], z[valid]
    # Rank
    rx = stats.rankdata(x)
    ry = stats.rankdata(y)
    rz = stats.rankdata(z)
    # Residualize x and y on z
    from numpy.polynomial.polynomial import polyfit, polyval
    cx = polyfit(rz, rx, 1)
    cy = polyfit(rz, ry, 1)
    res_x = rx - polyval(rz, cx)
    res_y = ry - polyval(rz, cy)
    corr, _ = stats.spearmanr(res_x, res_y)
    return corr


def quintile_analysis(signal: np.ndarray, fwd_ret: np.ndarray, step: int) -> dict:
    """Compute mean forward return by signal quintile (non-overlapping)."""
    idx = np.arange(0, len(signal), step)
    s = signal[idx]
    r = fwd_ret[idx]
    valid = np.isfinite(s) & np.isfinite(r)
    s, r = s[valid], r[valid]
    if len(s) < 50:
        return {}
    try:
        quintiles = pd.qcut(s, 5, labels=False, duplicates="drop")
    except ValueError:
        return {}
    result = {}
    unique_q = sorted(set(q for q in quintiles if not np.isnan(q)))
    for i, q in enumerate(unique_q):
        mask = quintiles == q
        if mask.sum() > 0:
            label = f"Q{i + 1}"
            result[label] = {
                "mean_ret_bps": float(np.mean(r[mask]) * 1e4),
                "n": int(mask.sum()),
            }
    return result


def run():
    print("Loading aligned data...")
    days = load_all_days()
    print(f"\nLoaded {len(days)} days\n")

    if not days:
        print("NO DATA")
        return

    # ======================================================================
    # Per-config analysis
    # ======================================================================
    results = []

    for lb in LOOKBACKS:
        for h in HORIZONS:
            step = max(lb, h)
            day_ics = []
            day_ics_self = []
            day_ics_incremental = []

            pooled_signal = []
            pooled_fwd = []
            pooled_self_signal = []

            for date_str, df in days.items():
                mid_s = df["mid_stock"].values
                mid_f = df["mid_fut"].values

                # Signal: 2330 past return
                sig = log_return(mid_s, lb)
                # Forward: TMF forward return
                fwd = forward_return(mid_f, h)
                # Self: TMF past return (control)
                self_sig = log_return(mid_f, lb)

                # Non-overlapping IC
                ic, pval, n = compute_non_overlapping_ic(sig, fwd, step)
                ic_self, _, n_self = compute_non_overlapping_ic(self_sig, fwd, step)

                # Incremental (partial correlation)
                idx = np.arange(0, len(sig), step)
                s_sub = sig[idx]
                f_sub = fwd[idx]
                sf_sub = self_sig[idx]
                inc_ic = partial_correlation(s_sub, f_sub, sf_sub)

                day_ics.append(ic)
                day_ics_self.append(ic_self)
                day_ics_incremental.append(inc_ic)

                pooled_signal.append(sig)
                pooled_fwd.append(fwd)
                pooled_self_signal.append(self_sig)

            # Pooled
            all_sig = np.concatenate(pooled_signal)
            all_fwd = np.concatenate(pooled_fwd)
            all_self = np.concatenate(pooled_self_signal)

            pooled_ic, pooled_pval, pooled_n = compute_non_overlapping_ic(
                all_sig, all_fwd, step
            )
            pooled_self_ic, _, _ = compute_non_overlapping_ic(
                all_self, all_fwd, step
            )

            # Incremental IC pooled
            idx_p = np.arange(0, len(all_sig), step)
            pooled_inc = partial_correlation(
                all_sig[idx_p], all_fwd[idx_p], all_self[idx_p]
            )

            # Sign consistency
            valid_ics = [ic for ic in day_ics if not np.isnan(ic)]
            if valid_ics and not np.isnan(pooled_ic):
                sign_consistent = sum(
                    1 for ic in valid_ics if np.sign(ic) == np.sign(pooled_ic)
                ) / len(valid_ics)
            else:
                sign_consistent = np.nan

            # Bootstrap CI (day-level resampling)
            boot_ics = []
            valid_day_data = [
                (pooled_signal[i], pooled_fwd[i])
                for i in range(len(pooled_signal))
            ]
            rng = np.random.default_rng(42)
            for _ in range(1000):
                boot_idx = rng.choice(len(valid_day_data), len(valid_day_data), replace=True)
                b_sig = np.concatenate([valid_day_data[j][0] for j in boot_idx])
                b_fwd = np.concatenate([valid_day_data[j][1] for j in boot_idx])
                b_ic, _, _ = compute_non_overlapping_ic(b_sig, b_fwd, step)
                boot_ics.append(b_ic)
            boot_ics = [x for x in boot_ics if not np.isnan(x)]
            ci_lo = np.percentile(boot_ics, 2.5) if boot_ics else np.nan
            ci_hi = np.percentile(boot_ics, 97.5) if boot_ics else np.nan

            # Quintile analysis (pooled)
            quints = quintile_analysis(all_sig, all_fwd, step)
            sorted_keys = sorted(quints.keys())
            q1_ret = quints[sorted_keys[0]]["mean_ret_bps"] if sorted_keys else np.nan
            q_last_ret = quints[sorted_keys[-1]]["mean_ret_bps"] if sorted_keys else np.nan
            ls_edge = q_last_ret - q1_ret if not (np.isnan(q_last_ret) or np.isnan(q1_ret)) else np.nan

            results.append({
                "lb": lb, "h": h,
                "pooled_ic": pooled_ic, "pooled_pval": pooled_pval, "pooled_n": pooled_n,
                "pooled_self_ic": pooled_self_ic,
                "pooled_inc_ic": pooled_inc,
                "sign_pct": sign_consistent,
                "ci_lo": ci_lo, "ci_hi": ci_hi,
                "q1_bps": q1_ret, "q5_bps": q_last_ret, "ls_edge_bps": ls_edge,
                "day_ics": valid_ics,
                "n_days": len(valid_ics),
            })

    # ======================================================================
    # Print results
    # ======================================================================
    print("=" * 90)
    print("EXPANDED IC RESULTS (Non-overlapping windows, 22 days)")
    print("=" * 90)

    print(f"\n{'LB':>4} {'H':>4} {'IC':>7} {'p':>7} {'N':>6} "
          f"{'Self':>7} {'Inc':>7} {'Sign%':>6} "
          f"{'CI_lo':>7} {'CI_hi':>7} {'Q1':>7} {'Q5':>7} {'LS':>7} {'Days':>5}")
    print("-" * 90)

    for r in results:
        marker = ""
        if abs(r["pooled_ic"]) >= 0.03 and r["pooled_pval"] < 0.05:
            marker = " ***"
        elif abs(r["pooled_ic"]) >= 0.02:
            marker = " **"
        elif abs(r["pooled_ic"]) >= 0.01:
            marker = " *"

        print(
            f"{r['lb']:>4} {r['h']:>4} "
            f"{r['pooled_ic']:>+7.4f} {r['pooled_pval']:>7.4f} {r['pooled_n']:>6} "
            f"{r['pooled_self_ic']:>+7.4f} "
            f"{r['pooled_inc_ic']:>+7.4f} "
            f"{r['sign_pct']:>6.1%} "
            f"{r['ci_lo']:>+7.4f} {r['ci_hi']:>+7.4f} "
            f"{r['q1_bps']:>+7.2f} {r['q5_bps']:>+7.2f} {r['ls_edge_bps']:>+7.2f} "
            f"{r['n_days']:>5}{marker}"
        )

    # ======================================================================
    # Kill gate summary
    # ======================================================================
    print(f"\n{'=' * 90}")
    print("KILL GATE ASSESSMENT")
    print(f"{'=' * 90}")

    best = max(results, key=lambda r: abs(r["pooled_ic"]) if not np.isnan(r["pooled_ic"]) else 0)
    print(f"\nBest config: LB={best['lb']}s H={best['h']}s")
    print(f"  Pooled IC: {best['pooled_ic']:+.4f} (p={best['pooled_pval']:.4f})")
    print(f"  Self IC:   {best['pooled_self_ic']:+.4f}")
    print(f"  Incr IC:   {best['pooled_inc_ic']:+.4f}")
    print(f"  Sign %:    {best['sign_pct']:.1%} ({best['n_days']} days)")
    print(f"  95% CI:    [{best['ci_lo']:+.4f}, {best['ci_hi']:+.4f}]")
    print(f"  L/S edge:  {best['ls_edge_bps']:+.2f} bps (Q5-Q1)")
    print(f"  Net edge:  {best['ls_edge_bps'] - RT_COST_BPS:+.2f} bps (after {RT_COST_BPS} bps RT cost)")

    # Check each gate
    gates = {}

    # Gate 1: IC >= 0.03 with p < 0.05
    gate1_pass = any(
        abs(r["pooled_ic"]) >= 0.03 and r["pooled_pval"] < 0.05
        for r in results if not np.isnan(r["pooled_ic"])
    )
    gates["IC >= 0.03, p < 0.05"] = gate1_pass

    # Gate 2: Sign consistent >= 70%
    gate2_pass = any(
        r["sign_pct"] >= 0.70
        for r in results if not np.isnan(r["sign_pct"])
    )
    gates["Sign >= 70%"] = gate2_pass

    # Gate 3: Incremental IC > 0.02
    gate3_pass = any(
        r["pooled_inc_ic"] > 0.02
        for r in results if not np.isnan(r["pooled_inc_ic"])
    )
    gates["Incremental IC > 0.02"] = gate3_pass

    # Gate 4: Net edge > 3 bps
    gate4_pass = any(
        r["ls_edge_bps"] - RT_COST_BPS > 3.0
        for r in results if not np.isnan(r["ls_edge_bps"])
    )
    gates["Net edge > 3 bps"] = gate4_pass

    print(f"\nKill Gate Results:")
    for gate, passed in gates.items():
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {gate}")

    all_fail = not any(gates.values())
    if all_fail:
        print("\n>>> ALL KILL GATES FAILED — REJECT this direction")
    else:
        n_pass = sum(gates.values())
        print(f"\n>>> {n_pass}/{len(gates)} gates passed")

    # ======================================================================
    # Per-day detail for best config
    # ======================================================================
    print(f"\n{'=' * 90}")
    print(f"PER-DAY IC for best config (LB={best['lb']}s, H={best['h']}s)")
    print(f"{'=' * 90}")

    sorted_days = sorted(days.keys())
    step = max(best["lb"], best["h"])
    for date_str in sorted_days:
        df = days[date_str]
        mid_s = df["mid_stock"].values
        mid_f = df["mid_fut"].values
        sig = log_return(mid_s, best["lb"])
        fwd = forward_return(mid_f, best["h"])
        self_sig = log_return(mid_f, best["lb"])
        ic, pval, n = compute_non_overlapping_ic(sig, fwd, step)
        ic_self, _, _ = compute_non_overlapping_ic(self_sig, fwd, step)
        print(f"  {date_str}: IC={ic:+.4f} (p={pval:.3f}, n={n}), Self={ic_self:+.4f}")

    return results


if __name__ == "__main__":
    results = run()
