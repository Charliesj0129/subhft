"""rv_ratio_regime — Gate Zero Diagnostic

Multi-scale realized volatility ratio: vrr = EW_var(ret, 5s) / EW_var(ret, 300s).

Gate Zero checks (all mandatory):
1. RV_5s non-degeneracy: >30% of ticks have non-zero return in 5s windows
2. vrr distribution across all days (mean, std, percentiles)
3. IC of vrr vs future return at horizons 30s, 60s, 120s, 300s, 600s
4. Detrended IC (5-min EMA local trend removal) at same horizons
5. CBS P&L conditioning: split CBS trades by vrr quantile
6. OOS validation on March data
7. Kill criterion: p >= 0.10 on CBS P&L conditioning

Usage:
    python -m research.alphas.rv_ratio_regime.gate_zero
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parents[3] / "research" / "data" / "raw" / "tmfd6"

# ---------- helpers ----------

def load_all_days() -> list[tuple[str, np.ndarray]]:
    """Load all daily .npy files, return list of (date_str, structured_array)."""
    files = sorted(DATA_DIR.glob("TMFD6_2026-*_l1.npy"))
    days = []
    for f in files:
        date_str = f.stem.split("_")[1]
        arr = np.load(f, allow_pickle=True)
        if len(arr) < 100:
            continue
        days.append((date_str, arr))
    return days


def compute_vrr(mid: np.ndarray, ts_ns: np.ndarray,
                hl_short_s: float = 5.0, hl_long_s: float = 300.0) -> np.ndarray:
    """Compute vrr = EW_var(ret, short) / EW_var(ret, long) tick-by-tick.

    Uses exponentially-weighted variance with half-life in seconds.
    Returns vrr array (same length as mid, first values are NaN during warm-up).
    """
    n = len(mid)
    vrr = np.full(n, np.nan)

    # Convert half-lives to per-tick decay using median tick interval
    dt_ns = np.diff(ts_ns)
    median_dt_s = float(np.median(dt_ns)) / 1e9
    if median_dt_s <= 0:
        return vrr

    alpha_short = 1.0 - math.exp(-math.log(2) * median_dt_s / hl_short_s)
    alpha_long = 1.0 - math.exp(-math.log(2) * median_dt_s / hl_long_s)

    # EW variance accumulators
    ew_mean_s = 0.0
    ew_var_s = 0.0
    ew_mean_l = 0.0
    ew_var_l = 0.0

    # Warm-up: need at least hl_long_s / median_dt_s ticks
    warmup_ticks = int(hl_long_s / median_dt_s) + 1

    for i in range(1, n):
        ret = float(mid[i]) - float(mid[i - 1])

        # Short EW variance
        delta_s = ret - ew_mean_s
        ew_mean_s += alpha_short * delta_s
        ew_var_s = (1.0 - alpha_short) * (ew_var_s + alpha_short * delta_s * delta_s)

        # Long EW variance
        delta_l = ret - ew_mean_l
        ew_mean_l += alpha_long * delta_l
        ew_var_l = (1.0 - alpha_long) * (ew_var_l + alpha_long * delta_l * delta_l)

        if i >= warmup_ticks and ew_var_l > 1e-15:
            vrr[i] = ew_var_s / ew_var_l

    return vrr


def compute_future_return(mid: np.ndarray, ts_ns: np.ndarray,
                          horizon_s: float) -> np.ndarray:
    """Compute forward return at given horizon (in seconds).

    For each tick i, find the first tick j where ts_ns[j] - ts_ns[i] >= horizon_s * 1e9,
    then fwd_ret[i] = mid[j] - mid[i].
    Returns NaN where horizon extends beyond data.
    """
    n = len(mid)
    fwd = np.full(n, np.nan)
    horizon_ns = int(horizon_s * 1e9)
    j = 0
    for i in range(n):
        while j < n and ts_ns[j] - ts_ns[i] < horizon_ns:
            j += 1
        if j < n:
            fwd[i] = float(mid[j]) - float(mid[i])
        j_save = j
        j = max(i + 1, j - 1)  # reset j slightly back for next i
        j = j_save  # actually keep j advancing (sorted ts)
    # Re-do properly with pointer scan
    fwd2 = np.full(n, np.nan)
    ptr = 0
    for i in range(n):
        if ptr <= i:
            ptr = i + 1
        while ptr < n and ts_ns[ptr] - ts_ns[i] < horizon_ns:
            ptr += 1
        if ptr < n:
            fwd2[i] = float(mid[ptr]) - float(mid[i])
    return fwd2


def compute_ic(signal: np.ndarray, fwd_ret: np.ndarray) -> tuple[float, float, int]:
    """Rank IC (Spearman) between signal and forward return.

    Returns (ic, p_value, n_valid).
    """
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    n = int(mask.sum())
    if n < 30:
        return (np.nan, 1.0, n)
    from scipy.stats import spearmanr
    ic, p = spearmanr(signal[mask], fwd_ret[mask])
    return (float(ic), float(p), n)


def detrend_return(mid: np.ndarray, ts_ns: np.ndarray,
                   fwd_ret: np.ndarray, ema_hl_s: float = 300.0) -> np.ndarray:
    """Remove 5-min EMA local trend from forward returns.

    Detrended_ret = fwd_ret - EMA(ret_1tick, 300s) * horizon_ticks_approx
    Simpler: just subtract the EMA of forward returns themselves.
    """
    n = len(fwd_ret)
    dt_ns = np.diff(ts_ns)
    median_dt_s = float(np.median(dt_ns)) / 1e9
    if median_dt_s <= 0:
        return fwd_ret.copy()

    alpha = 1.0 - math.exp(-math.log(2) * median_dt_s / ema_hl_s)

    ema = np.full(n, np.nan)
    ew = 0.0
    started = False
    for i in range(n):
        if np.isfinite(fwd_ret[i]):
            if not started:
                ew = fwd_ret[i]
                started = True
            else:
                ew = alpha * fwd_ret[i] + (1.0 - alpha) * ew
            ema[i] = ew

    return fwd_ret - ema


def simple_cbs_simulation(mid: np.ndarray, ts_ns: np.ndarray,
                          vrr: np.ndarray,
                          move_bps: float = 40.0,
                          lookback_s: float = 600.0,
                          hold_s: float = 300.0,
                          stop_bps: float = 15.0) -> list[dict]:
    """Simplified CBS trade simulation.

    Detect a move of `move_bps` within `lookback_s` → contrarian entry → hold for `hold_s`.
    Returns list of trade dicts with vrr_at_entry and pnl.
    """
    n = len(mid)
    lookback_ns = int(lookback_s * 1e9)
    hold_ns = int(hold_s * 1e9)
    trades = []

    # Pre-compute: for each tick, find the price `lookback_s` ago
    prev_ptr = 0
    i = 0
    cooldown_ns = 0

    for i in range(1, n):
        if ts_ns[i] < cooldown_ns:
            continue

        # Find tick at ts_ns[i] - lookback_ns
        while prev_ptr < i and ts_ns[i] - ts_ns[prev_ptr] > lookback_ns:
            prev_ptr += 1

        if prev_ptr >= i:
            continue

        move = float(mid[i]) - float(mid[prev_ptr])
        mid_val = float(mid[i])
        if mid_val <= 0:
            continue

        move_bp = abs(move) / mid_val * 10000.0

        if move_bp < move_bps:
            continue

        # Signal present — contrarian entry
        direction = -1.0 if move > 0 else 1.0  # contrarian

        # Find exit tick
        exit_idx = None
        for j in range(i + 1, n):
            if ts_ns[j] - ts_ns[i] >= hold_ns:
                exit_idx = j
                break
            # Stop loss check
            unrealized = direction * (float(mid[j]) - float(mid[i]))
            if mid_val > 0 and abs(unrealized) / mid_val * 10000.0 > stop_bps and unrealized < 0:
                exit_idx = j
                break

        if exit_idx is None:
            continue

        pnl = direction * (float(mid[exit_idx]) - float(mid[i]))
        vrr_val = vrr[i] if np.isfinite(vrr[i]) else np.nan

        trades.append({
            "entry_ts": ts_ns[i],
            "exit_ts": ts_ns[exit_idx],
            "direction": direction,
            "pnl_pts": pnl,
            "pnl_bps": pnl / mid_val * 10000.0 if mid_val > 0 else 0.0,
            "vrr_at_entry": vrr_val,
            "date": "",  # filled later
        })

        cooldown_ns = ts_ns[exit_idx] + int(10e9)  # 10s cooldown

    return trades


def main() -> None:
    print("=" * 70)
    print("rv_ratio_regime — Gate Zero Diagnostic")
    print("=" * 70)

    days = load_all_days()
    if not days:
        print("ERROR: No data files found")
        return

    dates = [d for d, _ in days]
    print(f"\nLoaded {len(days)} days: {dates[0]} to {dates[-1]}")

    # Split IS/OOS: March = OOS
    is_days = [(d, arr) for d, arr in days if d < "2026-03"]
    oos_days = [(d, arr) for d, arr in days if d >= "2026-03"]
    print(f"IS days: {len(is_days)}, OOS days: {len(oos_days)}")

    # ========== CHECK 1: RV_5s non-degeneracy ==========
    print("\n" + "=" * 50)
    print("CHECK 1: RV_5s Non-Degeneracy (>30% non-zero 5s returns)")
    print("=" * 50)

    total_ticks = 0
    nonzero_5s = 0
    for date_str, arr in days:
        mid = arr["mid_price"]
        ts = arr["local_ts"]
        n = len(mid)
        total_ticks += n
        # Check: for each tick, is the 5s-ahead return non-zero?
        ptr = 0
        for i in range(n):
            while ptr < n and ts[ptr] - ts[i] < int(5e9):
                ptr += 1
            if ptr < n and abs(float(mid[ptr]) - float(mid[i])) > 0.01:
                nonzero_5s += 1

    pct = nonzero_5s / max(total_ticks, 1) * 100
    status = "PASS" if pct > 30 else "FAIL"
    print(f"  Non-zero 5s returns: {nonzero_5s:,} / {total_ticks:,} = {pct:.1f}%  [{status}]")

    if status == "FAIL":
        print("  KILLED: RV_5s is degenerate on TMFD6")
        return

    # ========== Compute vrr for all days ==========
    print("\nComputing vrr for all days...")
    all_vrr = []
    all_mid = []
    all_ts = []
    all_dates = []
    day_boundaries = []  # (start_idx, end_idx, date)
    offset = 0

    for date_str, arr in days:
        mid = arr["mid_price"]
        ts = arr["local_ts"]
        vrr = compute_vrr(mid, ts)
        n = len(mid)
        all_vrr.append(vrr)
        all_mid.append(mid)
        all_ts.append(ts)
        all_dates.extend([date_str] * n)
        day_boundaries.append((offset, offset + n, date_str))
        offset += n

    vrr_cat = np.concatenate(all_vrr)
    mid_cat = np.concatenate(all_mid)
    ts_cat = np.concatenate(all_ts)
    valid_vrr = vrr_cat[np.isfinite(vrr_cat)]

    # ========== CHECK 2: vrr distribution ==========
    print("\n" + "=" * 50)
    print("CHECK 2: vrr Distribution")
    print("=" * 50)
    print(f"  Valid vrr values: {len(valid_vrr):,} / {len(vrr_cat):,}")
    print(f"  Mean:   {np.mean(valid_vrr):.4f}")
    print(f"  Std:    {np.std(valid_vrr):.4f}")
    print(f"  Median: {np.median(valid_vrr):.4f}")
    for p in [5, 10, 25, 50, 75, 90, 95]:
        print(f"  P{p:02d}:    {np.percentile(valid_vrr, p):.4f}")

    # ========== CHECK 3 & 4: IC at multiple horizons ==========
    print("\n" + "=" * 50)
    print("CHECK 3 & 4: IC and Detrended IC vs Future Return")
    print("=" * 50)

    horizons_s = [30, 60, 120, 300, 600]
    print(f"  {'Horizon':>8s}  {'Raw IC':>8s}  {'p-val':>8s}  {'Detr IC':>8s}  {'p-val':>8s}  {'N':>8s}")
    print(f"  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}  {'-'*8}")

    ic_results = {}
    for hz in horizons_s:
        # Compute per-day, then pool
        all_sig = []
        all_fwd = []
        all_fwd_detr = []
        for start, end, date_str in day_boundaries:
            mid_d = mid_cat[start:end]
            ts_d = ts_cat[start:end]
            vrr_d = vrr_cat[start:end]
            fwd_d = compute_future_return(mid_d, ts_d, hz)
            detr_d = detrend_return(mid_d, ts_d, fwd_d, ema_hl_s=300.0)
            all_sig.append(vrr_d)
            all_fwd.append(fwd_d)
            all_fwd_detr.append(detr_d)

        sig_pool = np.concatenate(all_sig)
        fwd_pool = np.concatenate(all_fwd)
        detr_pool = np.concatenate(all_fwd_detr)

        ic_raw, p_raw, n_raw = compute_ic(sig_pool, fwd_pool)
        ic_detr, p_detr, n_detr = compute_ic(sig_pool, detr_pool)

        ic_results[hz] = {
            "ic_raw": ic_raw, "p_raw": p_raw,
            "ic_detr": ic_detr, "p_detr": p_detr,
            "n": n_raw,
        }
        print(f"  {hz:>6d}s  {ic_raw:>+8.4f}  {p_raw:>8.4f}  {ic_detr:>+8.4f}  {p_detr:>8.4f}  {n_raw:>8,d}")

    # ========== CHECK 5: CBS P&L conditioning by vrr quantile ==========
    print("\n" + "=" * 50)
    print("CHECK 5: CBS P&L Conditioning by vrr Quantile")
    print("=" * 50)

    all_trades = []
    for date_str, arr in days:
        mid = arr["mid_price"]
        ts = arr["local_ts"]
        vrr = compute_vrr(mid, ts)
        trades = simple_cbs_simulation(mid, ts, vrr)
        for t in trades:
            t["date"] = date_str
        all_trades.extend(trades)

    print(f"  Total CBS trades: {len(all_trades)}")
    if len(all_trades) < 10:
        print("  WARNING: Too few trades for meaningful analysis")
    else:
        vrr_entries = np.array([t["vrr_at_entry"] for t in all_trades])
        pnl_entries = np.array([t["pnl_bps"] for t in all_trades])
        valid_mask = np.isfinite(vrr_entries)

        if valid_mask.sum() >= 10:
            vrr_valid = vrr_entries[valid_mask]
            pnl_valid = pnl_entries[valid_mask]

            # Tercile split
            q33 = np.percentile(vrr_valid, 33.3)
            q67 = np.percentile(vrr_valid, 66.7)

            low_mask = vrr_valid <= q33
            mid_mask = (vrr_valid > q33) & (vrr_valid <= q67)
            high_mask = vrr_valid > q67

            for label, mask in [("LOW vrr", low_mask), ("MID vrr", mid_mask), ("HIGH vrr", high_mask)]:
                n_t = int(mask.sum())
                if n_t > 0:
                    mean_pnl = float(np.mean(pnl_valid[mask]))
                    std_pnl = float(np.std(pnl_valid[mask]))
                    win_rate = float(np.mean(pnl_valid[mask] > 0)) * 100
                    print(f"  {label:>8s}: N={n_t:>3d}, mean={mean_pnl:>+6.2f} bps, "
                          f"std={std_pnl:>6.2f}, WR={win_rate:>5.1f}%")

            # T-test: low vrr vs high vrr
            if low_mask.sum() >= 5 and high_mask.sum() >= 5:
                from scipy.stats import ttest_ind
                t_stat, p_val = ttest_ind(pnl_valid[low_mask], pnl_valid[high_mask])
                print(f"\n  T-test (LOW vs HIGH): t={t_stat:+.3f}, p={p_val:.4f}")
                cbs_p = p_val
            else:
                cbs_p = 1.0
        else:
            cbs_p = 1.0

    # ========== CHECK 6: OOS validation (March) ==========
    print("\n" + "=" * 50)
    print("CHECK 6: OOS Validation (March 2026)")
    print("=" * 50)

    if not oos_days:
        print("  No OOS data available")
    else:
        oos_trades = []
        for date_str, arr in oos_days:
            mid = arr["mid_price"]
            ts = arr["local_ts"]
            vrr = compute_vrr(mid, ts)
            trades = simple_cbs_simulation(mid, ts, vrr)
            for t in trades:
                t["date"] = date_str
            oos_trades.extend(trades)

        print(f"  OOS CBS trades: {len(oos_trades)}")
        if len(oos_trades) >= 5:
            vrr_oos = np.array([t["vrr_at_entry"] for t in oos_trades])
            pnl_oos = np.array([t["pnl_bps"] for t in oos_trades])
            vm = np.isfinite(vrr_oos)
            if vm.sum() >= 5:
                vrr_v = vrr_oos[vm]
                pnl_v = pnl_oos[vm]
                med = np.median(vrr_v)
                low_m = vrr_v <= med
                high_m = vrr_v > med
                for label, mask in [("LOW vrr", low_m), ("HIGH vrr", high_m)]:
                    n_t = int(mask.sum())
                    if n_t > 0:
                        mean_pnl = float(np.mean(pnl_v[mask]))
                        print(f"  OOS {label}: N={n_t}, mean={mean_pnl:+.2f} bps")

        # OOS IC at 300s
        for date_str, arr in oos_days:
            mid = arr["mid_price"]
            ts = arr["local_ts"]
            vrr = compute_vrr(mid, ts)
            fwd = compute_future_return(mid, ts, 300.0)
            detr = detrend_return(mid, ts, fwd, 300.0)
            ic_raw, p_raw, n = compute_ic(vrr, fwd)
            ic_detr, p_detr, _ = compute_ic(vrr, detr)
            print(f"  {date_str}: IC_raw={ic_raw:+.4f} (p={p_raw:.3f}), "
                  f"IC_detr={ic_detr:+.4f} (p={p_detr:.3f}), N={n:,}")

    # ========== CHECK 7: Kill criterion ==========
    print("\n" + "=" * 50)
    print("CHECK 7: Kill Criterion (p >= 0.10 on CBS conditioning)")
    print("=" * 50)

    if len(all_trades) < 10:
        print("  INCONCLUSIVE: Too few CBS trades to evaluate")
        final = "INCONCLUSIVE"
    elif cbs_p < 0.10:
        print(f"  CBS conditioning p={cbs_p:.4f} < 0.10 → PASS")
        final = "PASS"
    else:
        print(f"  CBS conditioning p={cbs_p:.4f} >= 0.10 → FAIL (KILLED)")
        final = "FAIL"

    # ========== Summary ==========
    print("\n" + "=" * 70)
    print(f"GATE ZERO VERDICT: {final}")
    print("=" * 70)

    # Print IC summary
    print("\nIC Summary (all days pooled):")
    for hz in horizons_s:
        r = ic_results[hz]
        star = "*" if r["p_detr"] < 0.05 else ""
        print(f"  {hz:>4d}s: raw={r['ic_raw']:+.4f}  detrended={r['ic_detr']:+.4f}{star}")


if __name__ == "__main__":
    main()
