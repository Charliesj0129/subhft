"""LOB Kinetic Energy — IC validation on TXFD6 L1 real data.

Deliverables (per task assignment):
  1. Compute KE_bid, KE_ask, LOB_momentum from L1 BidAskEvent data
  2. Measure pooled IC on TXFD6 data at h=10/50/200
  3. Measure collinearity with existing features (depth_imbalance_ppm,
     l1_imbalance_ppm, ofi_l1_raw) — Challenger DC-2
  4. Per-level contribution analysis — Challenger DC-1
  5. Integer overflow stress testing

Usage:
    python -m research.alphas.lob_kinetic_energy.validate_ic
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import structlog

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.alphas.lob_kinetic_energy.impl import LobKineticEnergyAlpha

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DATA_DIR = Path("research/data/raw/txfd6")
HORIZONS = (10, 50, 200)  # forward return horizons (in ticks)
OUTPUT_DIR = Path("research/experiments/validations/lob_kinetic_energy")

# Price scale factor (TXFD6 L1 data has raw prices, e.g. 33391.0 = 3339.1 pts)
# In our L1 data the prices are float (actual index points * 10)
# Forward returns are computed from mid_price changes


def _load_all_l1_files() -> list[np.ndarray]:
    """Load per-day L1 npy files (not the concatenated 'all' file, for per-day IC)."""
    files = sorted(DATA_DIR.glob("TXFD6_*_l1.npy"))
    files = [f for f in files if "all" not in f.name]
    if not files:
        raise FileNotFoundError(f"No L1 files found in {DATA_DIR}")
    days = []
    for f in files:
        d = np.load(f, allow_pickle=True)
        if len(d) > 100:  # skip very short days
            days.append(d)
    return days


def _compute_ofi_l1(data: np.ndarray) -> np.ndarray:
    """Compute L1 OFI from BBO changes (Cont et al. 2014 decomposition)."""
    n = len(data)
    ofi = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        bb = data["bid_px"][i]
        ba = data["ask_px"][i]
        bq = data["bid_qty"][i]
        aq = data["ask_qty"][i]
        pbb = data["bid_px"][i - 1]
        pba = data["ask_px"][i - 1]
        pbq = data["bid_qty"][i - 1]
        paq = data["ask_qty"][i - 1]

        if bb > pbb:
            b_flow = bq
        elif bb == pbb:
            b_flow = bq - pbq
        else:
            b_flow = -pbq

        if ba > pba:
            a_flow = -paq
        elif ba == pba:
            a_flow = aq - paq
        else:
            a_flow = aq

        ofi[i] = b_flow - a_flow
    return ofi


def _compute_depth_imbalance(data: np.ndarray) -> np.ndarray:
    """depth_imbalance = (bid_qty - ask_qty) / (bid_qty + ask_qty + eps)."""
    bq = data["bid_qty"].astype(np.float64)
    aq = data["ask_qty"].astype(np.float64)
    total = bq + aq
    return np.where(total > 0, (bq - aq) / total, 0.0)


def _compute_l1_imbalance(data: np.ndarray) -> np.ndarray:
    """l1_imbalance = bid_qty / (bid_qty + ask_qty + eps)."""
    bq = data["bid_qty"].astype(np.float64)
    aq = data["ask_qty"].astype(np.float64)
    total = bq + aq
    return np.where(total > 0, bq / total, 0.5)


def _compute_forward_returns(mid: np.ndarray, h: int) -> np.ndarray:
    """Forward return at horizon h: mid[i+h] - mid[i]."""
    n = len(mid)
    fwd = np.full(n, np.nan, dtype=np.float64)
    if h < n:
        fwd[:n - h] = mid[h:] - mid[:n - h]
    return fwd


def _pearson_ic(signal: np.ndarray, fwd_ret: np.ndarray) -> tuple[float, int]:
    """Pooled Pearson IC between signal and forward returns.

    Returns (ic, n_valid).
    """
    mask = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    n = int(mask.sum())
    if n < 30:
        return 0.0, n
    s = signal[mask]
    r = fwd_ret[mask]
    s_z = s - s.mean()
    r_z = r - r.mean()
    denom = np.sqrt((s_z ** 2).sum() * (r_z ** 2).sum())
    if denom < 1e-30:
        return 0.0, n
    return float((s_z * r_z).sum() / denom), n


def _run_alpha_on_day(data: np.ndarray) -> dict[str, np.ndarray]:
    """Run LobKineticEnergyAlpha on a single day of L1 data.

    Returns dict with signal arrays for full (L1), skip_l1 variants,
    plus raw KE_bid, KE_ask, momentum.
    """
    n = len(data)

    alpha_full = LobKineticEnergyAlpha(active_depth=1, skip_l1=False)
    # No skip_l1 variant for L1-only data (only 1 level)

    signals = np.zeros(n, dtype=np.float64)
    ke_bid = np.zeros(n, dtype=np.float64)
    ke_ask = np.zeros(n, dtype=np.float64)

    for i in range(n):
        bp = float(data["bid_px"][i])
        ap = float(data["ask_px"][i])
        bq = float(data["bid_qty"][i])
        aq = float(data["ask_qty"][i])

        signals[i] = alpha_full.update(
            bid_px=bp, ask_px=ap, bid_qty=bq, ask_qty=aq
        )
        ke_bid[i] = alpha_full.get_ke_bid()
        ke_ask[i] = alpha_full.get_ke_ask()

    return {
        "signal": signals,
        "ke_bid": ke_bid,
        "ke_ask": ke_ask,
    }


def _run_alpha_l5_sim(data: np.ndarray) -> dict[str, np.ndarray]:
    """Simulate L5 by constructing synthetic deeper levels from L1 data.

    This creates synthetic L2-L5 levels at fixed offsets from BBO with
    quantities scaled from L1 depth. This is for DC-1 per-level analysis only.
    """
    n = len(data)

    # Run separate single-level alphas to measure per-level contribution
    per_level_signals = {}
    for level in range(1, 6):
        alpha = LobKineticEnergyAlpha(active_depth=5, skip_l1=False)
        sigs = np.zeros(n, dtype=np.float64)

        for i in range(n):
            bp = float(data["bid_px"][i])
            ap = float(data["ask_px"][i])
            bq = float(data["bid_qty"][i])
            aq = float(data["ask_qty"][i])

            # Construct synthetic L5 book
            # Deeper levels have quantities decaying by ~0.7 per level
            bids = np.zeros((5, 2), dtype=np.float64)
            asks = np.zeros((5, 2), dtype=np.float64)
            for lv in range(5):
                decay = 0.7 ** lv
                bids[lv] = [bp - lv, bq * decay]
                asks[lv] = [ap + lv, aq * decay]

            sigs[i] = alpha.update(bids=bids, asks=asks)

        per_level_signals[f"l1_to_l{level}"] = sigs

    return per_level_signals


def _integer_overflow_test() -> dict[str, str]:
    """Test that extreme values don't cause overflow or NaN.

    Addresses Execution concern: integer overflow testing.
    """
    results = {}

    # Test 1: Max TXFD6 realistic values
    # Price ~23000 * 10000 = 230_000_000 (fits i64)
    # Qty up to 10_000 contracts
    alpha = LobKineticEnergyAlpha()
    bp, ap = 230_000_000.0, 230_010_000.0
    bids = np.array([[bp - i * 10_000, 10_000.0] for i in range(5)], dtype=np.float64)
    asks = np.array([[ap + i * 10_000, 10_000.0] for i in range(5)], dtype=np.float64)

    for _ in range(20):
        alpha.update(bids=bids, asks=asks)
    # Change quantities dramatically
    bids2 = np.array([[bp - i * 10_000, 10_000.0 + 5_000 * i] for i in range(5)], dtype=np.float64)
    sig = alpha.update(bids=bids2, asks=asks)
    results["max_realistic"] = "PASS" if np.isfinite(sig) else "FAIL"

    # Test 2: Extreme quantities (stress test)
    alpha2 = LobKineticEnergyAlpha()
    extreme_qty = 1e15  # unrealistic but tests overflow
    bids_e = np.array([[100 - i, extreme_qty] for i in range(5)], dtype=np.float64)
    asks_e = np.array([[101 + i, extreme_qty] for i in range(5)], dtype=np.float64)
    for _ in range(20):
        alpha2.update(bids=bids_e, asks=asks_e)
    bids_e2 = np.array([[100 - i, extreme_qty * 1.01] for i in range(5)], dtype=np.float64)
    sig2 = alpha2.update(bids=bids_e2, asks=asks_e)
    results["extreme_qty_1e15"] = "PASS" if np.isfinite(sig2) else "FAIL"

    # Test 3: Zero quantities (division safety)
    alpha3 = LobKineticEnergyAlpha()
    bids_z = np.array([[100 - i, 0.0] for i in range(5)], dtype=np.float64)
    asks_z = np.array([[101 + i, 0.0] for i in range(5)], dtype=np.float64)
    for _ in range(30):
        sig3 = alpha3.update(bids=bids_z, asks=asks_z)
    results["zero_qty"] = "PASS" if np.isfinite(sig3) else "FAIL"

    # Test 4: Alternating extreme changes
    alpha4 = LobKineticEnergyAlpha()
    for i in range(50):
        qty = 1e12 if i % 2 == 0 else 1.0
        b = np.array([[100 - j, qty] for j in range(5)], dtype=np.float64)
        a = np.array([[101 + j, qty] for j in range(5)], dtype=np.float64)
        sig4 = alpha4.update(bids=b, asks=a)
    results["alternating_extreme"] = "PASS" if np.isfinite(sig4) else "FAIL"

    # Test 5: KE computation with large velocity * large qty
    # v = 1e12 - 1 = ~1e12, q = 1e12, KE = 0.5 * q * v^2 = 0.5 * 1e12 * 1e24 = 5e35
    # This exceeds float64 range? No, float64 max is ~1.8e308. Safe.
    alpha5 = LobKineticEnergyAlpha()
    b1 = np.array([[100 - j, 1.0] for j in range(5)], dtype=np.float64)
    a1 = np.array([[101 + j, 1.0] for j in range(5)], dtype=np.float64)
    alpha5.update(bids=b1, asks=a1)
    b2 = np.array([[100 - j, 1e12] for j in range(5)], dtype=np.float64)
    sig5 = alpha5.update(bids=b2, asks=a1)
    results["large_velocity_x_qty"] = "PASS" if np.isfinite(sig5) else "FAIL"

    return results


def main() -> None:
    logger.info("lob_ke_validation_start")

    # --- Integer overflow test (Execution concern) ---
    overflow_results = _integer_overflow_test()
    logger.info("overflow_test_complete", results=overflow_results)
    all_pass = all(v == "PASS" for v in overflow_results.values())
    if not all_pass:
        logger.error("overflow_test_failed", results=overflow_results)

    # --- Load data ---
    days = _load_all_l1_files()
    logger.info("data_loaded", n_days=len(days), total_rows=sum(len(d) for d in days))

    # --- Run alpha and compute features per day ---
    all_signals: list[np.ndarray] = []
    all_fwd_returns: dict[int, list[np.ndarray]] = {h: [] for h in HORIZONS}
    all_ofi: list[np.ndarray] = []
    all_depth_imb: list[np.ndarray] = []
    all_l1_imb: list[np.ndarray] = []
    per_day_ic: dict[int, list[float]] = {h: [] for h in HORIZONS}

    for day_idx, data in enumerate(days):
        mid = data["mid_price"].astype(np.float64)

        # Run alpha
        result = _run_alpha_on_day(data)
        sig = result["signal"]

        # Compute existing features for collinearity
        ofi = _compute_ofi_l1(data)
        depth_imb = _compute_depth_imbalance(data)
        l1_imb = _compute_l1_imbalance(data)

        all_signals.append(sig)
        all_ofi.append(ofi)
        all_depth_imb.append(depth_imb)
        all_l1_imb.append(l1_imb)

        # Forward returns at each horizon
        for h in HORIZONS:
            fwd = _compute_forward_returns(mid, h)
            all_fwd_returns[h].append(fwd)

            # Per-day IC
            ic_val, n_valid = _pearson_ic(sig, fwd)
            per_day_ic[h].append(ic_val)
            logger.info(
                "day_ic",
                day=day_idx,
                horizon=h,
                ic=round(ic_val, 6),
                n_valid=n_valid,
                n_rows=len(data),
            )

    # --- Pooled IC (concatenate all days) ---
    pooled_sig = np.concatenate(all_signals)
    pooled_ic_results = {}
    for h in HORIZONS:
        pooled_fwd = np.concatenate(all_fwd_returns[h])
        ic_val, n_valid = _pearson_ic(pooled_sig, pooled_fwd)
        pooled_ic_results[h] = {
            "pooled_ic": round(ic_val, 6),
            "n_valid": n_valid,
            "per_day_mean_ic": round(float(np.mean(per_day_ic[h])), 6),
            "per_day_std_ic": round(float(np.std(per_day_ic[h])), 6),
            "per_day_median_ic": round(float(np.median(per_day_ic[h])), 6),
        }
        logger.info("pooled_ic", horizon=h, **pooled_ic_results[h])

    # --- Collinearity analysis (Challenger DC-2) ---
    pooled_ofi = np.concatenate(all_ofi)
    pooled_depth_imb = np.concatenate(all_depth_imb)
    pooled_l1_imb = np.concatenate(all_l1_imb)

    # Only use rows where signal is nonzero (past warmup)
    mask = pooled_sig != 0.0
    sig_m = pooled_sig[mask]
    ofi_m = pooled_ofi[mask]
    dimb_m = pooled_depth_imb[mask]
    l1imb_m = pooled_l1_imb[mask]

    def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
        if len(a) < 30 or a.std() < 1e-30 or b.std() < 1e-30:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    collinearity = {
        "ke_momentum_vs_ofi_l1": round(_safe_corr(sig_m, ofi_m), 4),
        "ke_momentum_vs_depth_imbalance": round(_safe_corr(sig_m, dimb_m), 4),
        "ke_momentum_vs_l1_imbalance": round(_safe_corr(sig_m, l1imb_m), 4),
        "n_samples": int(mask.sum()),
        "dc2_threshold": 0.7,
    }
    collinearity["dc2_pass_ofi"] = abs(collinearity["ke_momentum_vs_ofi_l1"]) < 0.7
    collinearity["dc2_pass_depth_imb"] = abs(collinearity["ke_momentum_vs_depth_imbalance"]) < 0.7
    collinearity["dc2_pass_l1_imb"] = abs(collinearity["ke_momentum_vs_l1_imbalance"]) < 0.7
    collinearity["dc2_all_pass"] = all([
        collinearity["dc2_pass_ofi"],
        collinearity["dc2_pass_depth_imb"],
        collinearity["dc2_pass_l1_imb"],
    ])
    logger.info("collinearity_dc2", **collinearity)

    # --- Per-level contribution analysis (Challenger DC-1) ---
    # Use the day with most qty variance for DC-1 (not first day which may be flat)
    dc1_results = {}
    if days:
        # Pick day with highest bid_qty std (most depth variation)
        day_stds = [float(d["bid_qty"].std()) for d in days]
        best_day_idx = int(np.argmax(day_stds))
        first_day = days[best_day_idx]
        logger.info("dc1_day_selected", day_idx=best_day_idx, bid_qty_std=day_stds[best_day_idx])
        mid = first_day["mid_price"].astype(np.float64)
        fwd_50 = _compute_forward_returns(mid, 50)

        # Run with different active_depth settings
        for ad in [1, 2, 3, 5]:
            alpha = LobKineticEnergyAlpha(active_depth=ad)
            n = len(first_day)
            sigs = np.zeros(n, dtype=np.float64)

            for i in range(n):
                bp = float(first_day["bid_px"][i])
                ap = float(first_day["ask_px"][i])
                bq = float(first_day["bid_qty"][i])
                aq = float(first_day["ask_qty"][i])

                # Construct synthetic multi-level book
                bids = np.zeros((5, 2), dtype=np.float64)
                asks = np.zeros((5, 2), dtype=np.float64)
                for lv in range(5):
                    decay = 0.7 ** lv
                    bids[lv] = [bp - lv, bq * decay]
                    asks[lv] = [ap + lv, aq * decay]

                sigs[i] = alpha.update(bids=bids, asks=asks)

            ic_val, n_valid = _pearson_ic(sigs, fwd_50)
            dc1_results[f"active_depth_{ad}"] = {
                "ic_h50": round(ic_val, 6),
                "n_valid": n_valid,
            }

        # Also test skip_l1
        alpha_skip = LobKineticEnergyAlpha(active_depth=5, skip_l1=True)
        sigs_skip = np.zeros(len(first_day), dtype=np.float64)
        for i in range(len(first_day)):
            bp = float(first_day["bid_px"][i])
            ap = float(first_day["ask_px"][i])
            bq = float(first_day["bid_qty"][i])
            aq = float(first_day["ask_qty"][i])
            bids = np.zeros((5, 2), dtype=np.float64)
            asks = np.zeros((5, 2), dtype=np.float64)
            for lv in range(5):
                decay = 0.7 ** lv
                bids[lv] = [bp - lv, bq * decay]
                asks[lv] = [ap + lv, aq * decay]
            sigs_skip[i] = alpha_skip.update(bids=bids, asks=asks)

        ic_skip, n_skip = _pearson_ic(sigs_skip, fwd_50)
        dc1_results["skip_l1"] = {"ic_h50": round(ic_skip, 6), "n_valid": n_skip}

        logger.info("per_level_dc1", results=dc1_results)

    # --- Signal statistics ---
    nonzero_mask = pooled_sig != 0.0
    sig_nz = pooled_sig[nonzero_mask]
    signal_stats = {
        "n_total": len(pooled_sig),
        "n_nonzero": int(nonzero_mask.sum()),
        "pct_nonzero": round(100.0 * nonzero_mask.sum() / len(pooled_sig), 2),
        "mean": round(float(sig_nz.mean()), 6) if len(sig_nz) > 0 else 0.0,
        "std": round(float(sig_nz.std()), 6) if len(sig_nz) > 0 else 0.0,
        "min": round(float(sig_nz.min()), 6) if len(sig_nz) > 0 else 0.0,
        "max": round(float(sig_nz.max()), 6) if len(sig_nz) > 0 else 0.0,
        "p5": round(float(np.percentile(sig_nz, 5)), 6) if len(sig_nz) > 0 else 0.0,
        "p95": round(float(np.percentile(sig_nz, 95)), 6) if len(sig_nz) > 0 else 0.0,
    }
    logger.info("signal_stats", **signal_stats)

    # --- Assemble full report ---
    report = {
        "alpha_id": "lob_kinetic_energy",
        "data": {
            "n_days": len(days),
            "total_rows": sum(len(d) for d in days),
            "data_source": "TXFD6 L1 npy files",
            "note": "L1 only — L5 IC will differ when real multi-level data available",
        },
        "pooled_ic": pooled_ic_results,
        "collinearity_dc2": collinearity,
        "per_level_dc1": dc1_results,
        "signal_stats": signal_stats,
        "overflow_tests": overflow_results,
        "methodology": {
            "ic_type": "Pearson (pooled across all days)",
            "horizons_ticks": list(HORIZONS),
            "warmup_ticks": 16,
            "note": "Per-day IC also computed but pooled IC is authoritative (lessons from Round 14)",
        },
    }

    # --- Save ---
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "ic_validation_results.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info("report_saved", path=str(out_path))

    # --- Summary ---
    print("\n" + "=" * 70)
    print("LOB Kinetic Energy — IC Validation Summary")
    print("=" * 70)
    print(f"Data: {len(days)} days, {sum(len(d) for d in days):,} rows (L1)")
    print()
    print("Pooled IC (authoritative):")
    for h in HORIZONS:
        r = pooled_ic_results[h]
        print(f"  h={h:>3}: IC={r['pooled_ic']:+.6f}  (per-day mean={r['per_day_mean_ic']:+.6f} +/- {r['per_day_std_ic']:.6f})")
    print()
    print("Collinearity (DC-2, threshold < 0.7):")
    print(f"  vs ofi_l1_raw:          r={collinearity['ke_momentum_vs_ofi_l1']:+.4f}  {'PASS' if collinearity['dc2_pass_ofi'] else 'FAIL'}")
    print(f"  vs depth_imbalance_ppm: r={collinearity['ke_momentum_vs_depth_imbalance']:+.4f}  {'PASS' if collinearity['dc2_pass_depth_imb'] else 'FAIL'}")
    print(f"  vs l1_imbalance_ppm:    r={collinearity['ke_momentum_vs_l1_imbalance']:+.4f}  {'PASS' if collinearity['dc2_pass_l1_imb'] else 'FAIL'}")
    print()
    print("Per-level analysis (DC-1, synthetic L5, h=50):")
    for k, v in dc1_results.items():
        print(f"  {k:>15}: IC={v['ic_h50']:+.6f}")
    print()
    print("Overflow tests:")
    for k, v in overflow_results.items():
        print(f"  {k:>25}: {v}")
    print()
    print(f"Signal stats: mean={signal_stats['mean']:+.6f}, std={signal_stats['std']:.6f}, "
          f"nonzero={signal_stats['pct_nonzero']:.1f}%")
    print("=" * 70)
    print(f"Report saved to: {out_path}")


if __name__ == "__main__":
    main()
