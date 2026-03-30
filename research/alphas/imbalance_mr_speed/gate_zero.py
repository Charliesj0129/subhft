"""imbalance_mr_speed — Gate Zero Diagnostic (vectorized).

Online OU fit to LOB imbalance -> mean-reversion speed as regime detector.

Usage:
    python -m research.alphas.imbalance_mr_speed.gate_zero
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

DATA_DIR = Path(__file__).resolve().parents[3] / "research" / "data" / "raw" / "tmfd6"


def load_all_days() -> list[tuple[str, np.ndarray]]:
    files = sorted(DATA_DIR.glob("TMFD6_2026-*_l1.npy"))
    days = []
    for f in files:
        date_str = f.stem.split("_")[1]
        arr = np.load(f, allow_pickle=True)
        if len(arr) < 100:
            continue
        days.append((date_str, arr))
    return days


def compute_imbalance(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    total = bid_qty + ask_qty
    return np.where(total > 0, (bid_qty - ask_qty) / total, 0.0)


def compute_mr_speed_vectorized(imb: np.ndarray, median_dt_s: float,
                                ew_hl_s: float = 60.0) -> np.ndarray:
    """Compute mr_speed using vectorized EW accumulation."""
    n = len(imb)
    mr_speed = np.full(n, np.nan)
    if median_dt_s <= 0:
        return mr_speed
    alpha = 1.0 - math.exp(-math.log(2) * median_dt_s / ew_hl_s)
    decay = 1.0 - alpha
    warmup = int(ew_hl_s / median_dt_s) + 1

    # x = imb[:-1], y = imb[1:]
    x = imb[:-1].astype(np.float64)
    y = imb[1:].astype(np.float64)

    # EW accumulators via loop (unavoidable for online EW)
    ew_x = 0.0
    ew_y = 0.0
    ew_xx = 0.0
    ew_xy = 0.0

    for i in range(len(x)):
        xi = x[i]
        yi = y[i]
        ew_x = decay * ew_x + alpha * xi
        ew_y = decay * ew_y + alpha * yi
        ew_xx = decay * ew_xx + alpha * xi * xi
        ew_xy = decay * ew_xy + alpha * xi * yi

        if i >= warmup:
            var_x = ew_xx - ew_x * ew_x
            if var_x > 1e-10:
                beta = (ew_xy - ew_x * ew_y) / var_x
                if 0.0 < beta < 1.0:
                    mr_speed[i + 1] = -math.log(beta) / median_dt_s
                elif beta >= 1.0:
                    mr_speed[i + 1] = 0.0

    return mr_speed


def compute_future_return_vec(mid: np.ndarray, ts_ns: np.ndarray,
                              horizon_s: float) -> np.ndarray:
    """Vectorized forward return using searchsorted."""
    horizon_ns = int(horizon_s * 1e9)
    target_ts = ts_ns + horizon_ns
    idx = np.searchsorted(ts_ns, target_ts, side="left")
    idx = np.clip(idx, 0, len(mid) - 1)
    fwd = mid[idx].astype(np.float64) - mid.astype(np.float64)
    # Mark entries where horizon goes beyond data
    fwd[idx >= len(mid) - 1] = np.nan
    # Also mark where we didn't actually reach the horizon
    actual_dt = ts_ns[idx] - ts_ns
    fwd[actual_dt < horizon_ns * 0.5] = np.nan  # too short
    return fwd


def compute_ic(signal: np.ndarray, fwd_ret: np.ndarray) -> tuple[float, float, int]:
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    n = int(mask.sum())
    if n < 30:
        return (np.nan, 1.0, n)
    from scipy.stats import spearmanr
    # Subsample if too large for speed
    if n > 500_000:
        idx = np.where(mask)[0]
        idx = np.random.default_rng(42).choice(idx, 500_000, replace=False)
        ic, p = spearmanr(signal[idx], fwd_ret[idx])
    else:
        ic, p = spearmanr(signal[mask], fwd_ret[mask])
    return (float(ic), float(p), n)


def detrend_return_vec(fwd_ret: np.ndarray, ts_ns: np.ndarray,
                       ema_hl_s: float = 300.0) -> np.ndarray:
    n = len(fwd_ret)
    median_dt_s = float(np.median(np.diff(ts_ns))) / 1e9
    if median_dt_s <= 0:
        return fwd_ret.copy()
    alpha = 1.0 - math.exp(-math.log(2) * median_dt_s / ema_hl_s)
    ema = np.full(n, np.nan)
    ew = 0.0
    started = False
    for i in range(n):
        if np.isfinite(fwd_ret[i]):
            if not started:
                ew = float(fwd_ret[i])
                started = True
            else:
                ew = alpha * float(fwd_ret[i]) + (1.0 - alpha) * ew
            ema[i] = ew
    return fwd_ret - ema


def simple_cbs_trades(mid, ts_ns, signal,
                      move_bps=40.0, lookback_s=600.0,
                      hold_s=300.0, stop_bps=15.0):
    """Simplified CBS with signal tagging. Returns (pnl_bps_array, signal_array)."""
    n = len(mid)
    lookback_ns = int(lookback_s * 1e9)
    hold_ns = int(hold_s * 1e9)
    pnl_list = []
    sig_list = []
    prev_ptr = 0
    cooldown_ns = 0

    for i in range(1, n):
        if ts_ns[i] < cooldown_ns:
            continue
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

        direction = -1.0 if move > 0 else 1.0
        exit_idx = None
        for j in range(i + 1, n):
            if ts_ns[j] - ts_ns[i] >= hold_ns:
                exit_idx = j
                break
            unr = direction * (float(mid[j]) - float(mid[i]))
            if mid_val > 0 and abs(unr) / mid_val * 10000.0 > stop_bps and unr < 0:
                exit_idx = j
                break
        if exit_idx is None:
            continue

        pnl = direction * (float(mid[exit_idx]) - float(mid[i]))
        pnl_bps = pnl / mid_val * 10000.0
        sig_val = float(signal[i]) if np.isfinite(signal[i]) else np.nan
        pnl_list.append(pnl_bps)
        sig_list.append(sig_val)
        cooldown_ns = ts_ns[exit_idx] + int(10e9)

    return np.array(pnl_list), np.array(sig_list)


def main() -> None:
    print("=" * 70, flush=True)
    print("imbalance_mr_speed — Gate Zero Diagnostic", flush=True)
    print("=" * 70, flush=True)

    days = load_all_days()
    if not days:
        print("ERROR: No data files found")
        return

    dates = [d for d, _ in days]
    print(f"\nLoaded {len(days)} days: {dates[0]} to {dates[-1]}", flush=True)

    is_days = [(d, arr) for d, arr in days if d < "2026-03"]
    oos_days = [(d, arr) for d, arr in days if d >= "2026-03"]
    print(f"IS days: {len(is_days)}, OOS days: {len(oos_days)}", flush=True)

    # ========== CHECK 1: Imbalance CV ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 1: Imbalance Non-Degeneracy", flush=True)

    all_imb_stds = []
    all_imb_means = []
    for _, arr in days:
        imb = compute_imbalance(arr["bid_qty"], arr["ask_qty"])
        all_imb_stds.append(np.std(imb))
        all_imb_means.append(np.mean(np.abs(imb)))

    mean_std = np.mean(all_imb_stds)
    mean_abs = np.mean(all_imb_means)
    cv_global = mean_std / max(mean_abs, 1e-10)
    print(f"  Imbalance std (avg across days): {mean_std:.4f}", flush=True)
    print(f"  Imbalance |mean| (avg): {mean_abs:.4f}", flush=True)
    print(f"  Global CV: {cv_global:.3f}", flush=True)
    status = "PASS" if mean_std > 0.15 else "FAIL"
    print(f"  Status: {status} (std > 0.15 required)", flush=True)

    if status == "FAIL":
        print("  KILLED: Imbalance has insufficient variation")
        return

    # ========== Compute mr_speed per day ==========
    print("\nComputing mr_speed per day...", flush=True)

    day_results = []
    for date_str, arr in days:
        mid = arr["mid_price"]
        ts = arr["local_ts"]
        imb = compute_imbalance(arr["bid_qty"], arr["ask_qty"])
        med_dt_s = float(np.median(np.diff(ts))) / 1e9
        mr = compute_mr_speed_vectorized(imb, med_dt_s, ew_hl_s=60.0)
        day_results.append({
            "date": date_str, "mid": mid, "ts": ts, "imb": imb,
            "mr": mr, "n": len(mid), "med_dt_s": med_dt_s,
        })
        valid = np.isfinite(mr)
        print(f"  {date_str}: N={len(mid):>6,d}, valid_mr={valid.sum():>6,d}, "
              f"med_mr={np.median(mr[valid]):.4f}" if valid.any() else
              f"  {date_str}: N={len(mid):>6,d}, NO valid mr_speed", flush=True)

    # ========== CHECK 2: mr_speed distribution ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 2: mr_speed Distribution", flush=True)

    all_mr = np.concatenate([d["mr"] for d in day_results])
    valid_mr = all_mr[np.isfinite(all_mr)]
    print(f"  Valid: {len(valid_mr):,} / {len(all_mr):,}", flush=True)
    if len(valid_mr) > 0:
        print(f"  Mean:   {np.mean(valid_mr):.4f}", flush=True)
        print(f"  Std:    {np.std(valid_mr):.4f}", flush=True)
        for p in [5, 10, 25, 50, 75, 90, 95]:
            print(f"  P{p:02d}:    {np.percentile(valid_mr, p):.4f}", flush=True)

    # ========== CHECK 3 & 4: IC ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 3 & 4: IC and Detrended IC", flush=True)

    horizons_s = [60, 120, 300, 600]
    print(f"  {'Hz':>6s}  {'Raw IC':>8s}  {'p':>8s}  {'Detr IC':>8s}  {'p':>8s}  {'N':>8s}", flush=True)

    ic_results = {}
    for hz in horizons_s:
        sigs = []
        fwds = []
        fwds_d = []
        for d in day_results:
            fwd = compute_future_return_vec(d["mid"], d["ts"], hz)
            detr = detrend_return_vec(fwd, d["ts"], 300.0)
            sigs.append(d["mr"])
            fwds.append(fwd)
            fwds_d.append(detr)

        sig_p = np.concatenate(sigs)
        fwd_p = np.concatenate(fwds)
        detr_p = np.concatenate(fwds_d)

        ic_raw, p_raw, n_raw = compute_ic(sig_p, fwd_p)
        ic_detr, p_detr, _ = compute_ic(sig_p, detr_p)
        ic_results[hz] = {"ic_raw": ic_raw, "p_raw": p_raw,
                          "ic_detr": ic_detr, "p_detr": p_detr, "n": n_raw}
        print(f"  {hz:>4d}s  {ic_raw:>+8.4f}  {p_raw:>8.4f}  {ic_detr:>+8.4f}  {p_detr:>8.4f}  {n_raw:>8,d}",
              flush=True)

    # ========== CHECK 5: Incremental over ret_autocov ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 5: Incremental Value over ret_autocov_5s", flush=True)

    # Compute ret_autocov for each day
    for d in day_results:
        mid = d["mid"].astype(np.float64)
        ret = np.diff(mid, prepend=mid[0])
        ret_lag = np.roll(ret, 1)
        ret_lag[0] = 0.0
        # EW autocovariance
        n = len(mid)
        if d["med_dt_s"] <= 0:
            d["autocov"] = np.zeros(n)
            continue
        alpha_ac = 1.0 - math.exp(-math.log(2) * d["med_dt_s"] / 5.0)
        autocov = np.zeros(n)
        ew_rr = 0.0
        ew_r = 0.0
        warmup_ac = int(5.0 / d["med_dt_s"]) + 1
        for i in range(2, n):
            r = float(ret[i])
            rl = float(ret[i - 1])
            ew_rr = (1 - alpha_ac) * ew_rr + alpha_ac * r * rl
            ew_r = (1 - alpha_ac) * ew_r + alpha_ac * r
            if i >= warmup_ac:
                autocov[i] = ew_rr - ew_r * ew_r
        d["autocov"] = autocov

    all_ac = np.concatenate([d["autocov"] for d in day_results])

    # Correlation between mr_speed and ret_autocov
    mask = np.isfinite(all_mr) & (all_ac != 0)
    if mask.sum() > 1000:
        from scipy.stats import spearmanr
        sample = np.random.default_rng(42).choice(np.where(mask)[0],
                                                   min(200000, mask.sum()), replace=False)
        rho, _ = spearmanr(all_mr[sample], all_ac[sample])
        print(f"  Corr(mr_speed, ret_autocov): rho={rho:+.4f}", flush=True)

        # Partial IC at 300s
        fwd_300 = np.concatenate([
            compute_future_return_vec(d["mid"], d["ts"], 300.0) for d in day_results
        ])
        triple = mask & np.isfinite(fwd_300)
        if triple.sum() > 1000:
            from scipy.stats import rankdata
            sample2 = np.random.default_rng(42).choice(np.where(triple)[0],
                                                        min(200000, triple.sum()), replace=False)
            mr_r = rankdata(all_mr[sample2])
            ac_r = rankdata(all_ac[sample2])
            fwd_r = fwd_300[sample2]
            # Residualize mr on autocov
            A = np.column_stack([np.ones(len(mr_r)), ac_r])
            coef = np.linalg.lstsq(A, mr_r, rcond=None)[0]
            resid = mr_r - A @ coef
            ic_resid, p_resid, _ = compute_ic(resid, fwd_r)
            ic_mr, p_mr, _ = compute_ic(all_mr[sample2], fwd_r)
            ic_ac, p_ac, _ = compute_ic(all_ac[sample2], fwd_r)
            print(f"  IC(mr_speed, fwd_300s):    {ic_mr:+.4f} (p={p_mr:.4f})", flush=True)
            print(f"  IC(ret_autocov, fwd_300s): {ic_ac:+.4f} (p={p_ac:.4f})", flush=True)
            print(f"  IC(mr_resid, fwd_300s):    {ic_resid:+.4f} (p={p_resid:.4f})", flush=True)

    # ========== CHECK 6: CBS conditioning ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 6: CBS P&L Conditioning", flush=True)

    all_pnl = []
    all_sig = []
    for d in day_results:
        pnl, sig = simple_cbs_trades(d["mid"], d["ts"], d["mr"])
        all_pnl.append(pnl)
        all_sig.append(sig)

    pnl_arr = np.concatenate(all_pnl) if all_pnl else np.array([])
    sig_arr = np.concatenate(all_sig) if all_sig else np.array([])
    print(f"  Total CBS trades: {len(pnl_arr)}", flush=True)
    cbs_p = 1.0

    if len(pnl_arr) >= 10:
        vm = np.isfinite(sig_arr)
        if vm.sum() >= 10:
            pv = pnl_arr[vm]
            sv = sig_arr[vm]
            q33 = np.percentile(sv, 33.3)
            q67 = np.percentile(sv, 66.7)
            low = sv <= q33
            mid_ = (sv > q33) & (sv <= q67)
            high = sv > q67
            for label, m in [("LOW mr", low), ("MID mr", mid_), ("HIGH mr", high)]:
                nt = int(m.sum())
                if nt > 0:
                    mp = float(np.mean(pv[m]))
                    wr = float(np.mean(pv[m] > 0)) * 100
                    print(f"  {label:>8s}: N={nt:>3d}, mean={mp:>+6.2f} bps, WR={wr:.1f}%",
                          flush=True)
            if low.sum() >= 5 and high.sum() >= 5:
                from scipy.stats import ttest_ind
                # Hypothesis: HIGH mr = fast MR = range-bound = CBS better
                t_stat, cbs_p = ttest_ind(pv[high], pv[low])
                print(f"\n  T-test (HIGH vs LOW mr): t={t_stat:+.3f}, p={cbs_p:.4f}", flush=True)

    # ========== CHECK 7: OOS ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 7: OOS (March)", flush=True)

    oos_results = [d for d in day_results if d["date"] >= "2026-03"]
    for d in oos_results:
        fwd = compute_future_return_vec(d["mid"], d["ts"], 300.0)
        detr = detrend_return_vec(fwd, d["ts"], 300.0)
        ic_r, p_r, n = compute_ic(d["mr"], fwd)
        ic_d, p_d, _ = compute_ic(d["mr"], detr)
        print(f"  {d['date']}: IC_raw={ic_r:+.4f} (p={p_r:.3f}), "
              f"IC_detr={ic_d:+.4f} (p={p_d:.3f}), N={n:,}", flush=True)

    # ========== CHECK 8: Kill criterion ==========
    print("\n" + "=" * 50, flush=True)
    print("CHECK 8: Kill Criterion", flush=True)

    any_sig = any(ic_results[hz]["p_detr"] < 0.05 for hz in horizons_s)
    if not any_sig:
        final = "FAIL"
        print(f"  KILLED: No significant detrended IC")
    elif cbs_p < 0.10:
        final = "PASS"
        print(f"  Detrended IC significant + CBS conditioning p={cbs_p:.4f} < 0.10")
    else:
        final = "CONDITIONAL PASS"
        print(f"  Detrended IC significant, CBS conditioning p={cbs_p:.4f} >= 0.10")

    print("\n" + "=" * 70, flush=True)
    print(f"GATE ZERO VERDICT: {final}", flush=True)
    print("=" * 70, flush=True)


if __name__ == "__main__":
    main()
