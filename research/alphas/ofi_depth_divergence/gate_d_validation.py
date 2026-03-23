"""Gate D statistical validation for ofi_depth_divergence (optimized).

1. Non-overlapping IC at all horizons
2. IS/OOS split (60/40)
3. EMA sensitivity sweep (vectorized)
4. Walk-forward 3-fold temporal CV

Usage:
    python -m research.alphas.ofi_depth_divergence.gate_d_validation
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
import os as _os  # noqa: E402
_os.chdir(str(_ROOT))

from research.alphas.ofi_depth_divergence.impl import (  # noqa: E402
    OfiDepthDivergenceAlpha,
    _WARMUP_TICKS,
)

_DATA_DIR = _ROOT / "research" / "data" / "real" / "golden"
_IC_HORIZONS = [50, 200, 1000, 5000]

_STOCK_SYMBOLS = [
    "1101", "1102", "1216", "1301", "1303", "1326", "1402",
    "2002", "2201", "2207", "2301", "2303", "2308", "2317",
    "2327", "2330", "2345", "2354", "2357", "2379", "2382",
    "2395", "2408", "2409", "2412", "2454",
]

_MIN_IC_CHUNKS = 10


def _load_symbol(symbol: str) -> tuple[NDArray, NDArray, NDArray, NDArray] | None:
    sym_dir = _DATA_DIR / symbol
    if not sym_dir.exists():
        return None
    parquet_files = sorted(sym_dir.glob("*.parquet"))
    if not parquet_files:
        return None

    def _pad5(col_list: list) -> NDArray:
        out = np.zeros((len(col_list), 5), dtype=np.float64)
        for j, row in enumerate(col_list):
            k = min(len(row), 5)
            out[j, :k] = row[:k]
        return out

    all_bp, all_bv, all_ap, all_av = [], [], [], []
    for pf in parquet_files:
        table = pq.read_table(pf, columns=["type", "bids_price", "bids_vol", "asks_price", "asks_vol"])
        df = table.to_pandas()
        ba = df[df["type"] == "BidAsk"]
        if ba.empty:
            continue
        all_bp.append(_pad5(ba["bids_price"].tolist()))
        all_bv.append(_pad5(ba["bids_vol"].tolist()))
        all_ap.append(_pad5(ba["asks_price"].tolist()))
        all_av.append(_pad5(ba["asks_vol"].tolist()))

    if not all_bp:
        return None
    return np.concatenate(all_bp), np.concatenate(all_bv), np.concatenate(all_ap), np.concatenate(all_av)


def _precompute_band_ofi(bp: NDArray, bv: NDArray, ap: NDArray, av: NDArray) -> tuple[NDArray, NDArray, NDArray]:
    """Precompute per-tick shallow and deep OFI + mid prices (vectorized)."""
    n = len(bp)
    # Per-level deltas
    delta_bv = np.diff(bv, axis=0, prepend=0)  # (n, 5) — first row is full value from zero
    delta_av = np.diff(av, axis=0, prepend=0)
    ofi_per_level = delta_bv - delta_av  # (n, 5)

    # Band OFI with normalization
    shallow = (ofi_per_level[:, 0] + ofi_per_level[:, 1]) / 2.0
    deep = (ofi_per_level[:, 2] + ofi_per_level[:, 3] + ofi_per_level[:, 4]) / 3.0
    mid = (bp[:, 0] + ap[:, 0]) / 2.0
    return shallow, deep, mid


def _vectorized_ema(data: NDArray, alpha: float) -> NDArray:
    """Compute EMA of 1-D array using loop (scipy.signal.lfilter alternative)."""
    n = len(data)
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    out[0] = data[0]
    for i in range(1, n):
        out[i] = out[i - 1] + alpha * (data[i] - out[i - 1])
    return out


def _gen_signals_from_bands(
    shallow: NDArray, deep: NDArray, ema_fast_w: float = 4.0, ema_slow_w: float = 32.0,
) -> NDArray:
    """Generate signals from precomputed band OFI using specified EMA windows."""
    alpha_fast = 1.0 - math.exp(-1.0 / ema_fast_w)
    alpha_slow = 1.0 - math.exp(-1.0 / ema_slow_w)
    alpha_out = 1.0 - math.exp(-1.0 / 8.0)

    sf = _vectorized_ema(shallow, alpha_fast)
    ss = _vectorized_ema(shallow, alpha_slow)
    df = _vectorized_ema(deep, alpha_fast)
    ds = _vectorized_ema(deep, alpha_slow)

    shallow_momentum = sf - ss
    deep_momentum = df - ds
    raw_div = shallow_momentum - deep_momentum  # negated: shallow - deep
    signal = _vectorized_ema(raw_div, alpha_out)

    # Warmup suppression
    signal[:_WARMUP_TICKS] = 0.0
    # Clip
    np.clip(signal, -2.0, 2.0, out=signal)
    return signal


def _gen_signals_alpha(bp: NDArray, bv: NDArray, ap: NDArray, av: NDArray) -> NDArray:
    """Generate signals using actual alpha class (for reference comparison)."""
    alpha = OfiDepthDivergenceAlpha()
    n = len(bp)
    signals = np.zeros(n, dtype=np.float64)
    for i in range(n):
        bids = np.column_stack([bp[i], bv[i]])
        asks = np.column_stack([ap[i], av[i]])
        signals[i] = alpha.update(bids=bids, asks=asks)
    return signals


def _ic_nonoverlap(signals: NDArray, mid: NDArray, horizon: int, chunk: int = 500) -> tuple[float, float, int]:
    n = len(signals)
    step = chunk + horizon
    if n < step + horizon:
        return 0.0, 1.0, 0
    px = np.maximum(mid, 1.0)
    ics: list[float] = []
    for start in range(0, n - horizon - chunk, step):
        end = start + chunk
        if end + horizon > n:
            break
        s = signals[start:end]
        r = (mid[start + horizon:end + horizon] - mid[start:end]) / px[start:end]
        if np.std(s) < 1e-12 or np.std(r) < 1e-12:
            continue
        rs = np.argsort(np.argsort(s)).astype(np.float64)
        rr = np.argsort(np.argsort(r)).astype(np.float64)
        ic = float(np.corrcoef(rs, rr)[0, 1])
        if math.isfinite(ic):
            ics.append(ic)
    if not ics:
        return 0.0, 1.0, 0
    return float(np.mean(ics)), float(np.std(ics)), len(ics)


def run_validation() -> dict:
    print("=" * 70)
    print("OFI_DEPTH_DIVERGENCE — Gate D Validation (Optimized)")
    print("=" * 70)

    # Preload and precompute band OFI for all symbols
    print("\nLoading and precomputing...")
    sym_bands: dict[str, tuple[NDArray, NDArray, NDArray]] = {}
    for sym in _STOCK_SYMBOLS:
        data = _load_symbol(sym)
        if data is None:
            continue
        bp, bv, ap, av = data
        if len(bp) < 500:
            continue
        shallow, deep, mid = _precompute_band_ofi(bp, bv, ap, av)
        sym_bands[sym] = (shallow, deep, mid)
    print(f"  {len(sym_bands)} symbols loaded")

    results: dict = {}

    # =================================================================
    # 1. Non-overlapping IC (full dataset, default params)
    # =================================================================
    print(f"\n[1] Non-overlapping IC (full dataset)")
    pooled_full: dict[int, list[float]] = {h: [] for h in _IC_HORIZONS}

    for sym, (shallow, deep, mid) in sym_bands.items():
        signals = _gen_signals_from_bands(shallow, deep)
        for h in _IC_HORIZONS:
            ic, _, nc = _ic_nonoverlap(signals, mid, h)
            if nc >= _MIN_IC_CHUNKS:
                pooled_full[h].append(ic)

    print("  Horizon  Mean IC   Std     %Pos  n_sym")
    for h in _IC_HORIZONS:
        vals = pooled_full[h]
        if vals:
            m = float(np.mean(vals))
            s = float(np.std(vals))
            pp = sum(1 for v in vals if v > 0) / len(vals)
            results[f"full_ic_{h}"] = m
            results[f"full_ic_{h}_std"] = s
            results[f"full_ic_{h}_pctpos"] = pp
            print(f"  {h:>5d}    {m:>7.4f}   {s:.4f}  {pp:.0%}    {len(vals)}")

    # =================================================================
    # 2. IS/OOS split (60/40)
    # =================================================================
    print(f"\n[2] IS/OOS Split (60/40)")
    pooled_is: dict[int, list[float]] = {h: [] for h in _IC_HORIZONS}
    pooled_oos: dict[int, list[float]] = {h: [] for h in _IC_HORIZONS}

    for sym, (shallow, deep, mid) in sym_bands.items():
        signals = _gen_signals_from_bands(shallow, deep)
        n = len(signals)
        split = int(n * 0.6)
        for h in _IC_HORIZONS:
            ic_is, _, nc_is = _ic_nonoverlap(signals[:split], mid[:split], h)
            ic_oos, _, nc_oos = _ic_nonoverlap(signals[split:], mid[split:], h)
            if nc_is >= _MIN_IC_CHUNKS:
                pooled_is[h].append(ic_is)
            if nc_oos >= _MIN_IC_CHUNKS:
                pooled_oos[h].append(ic_oos)

    print("  Horizon  IS IC    OOS IC   Gap     OOS/IS   OOS%Pos  n_is  n_oos")
    for h in _IC_HORIZONS:
        vis, voos = pooled_is[h], pooled_oos[h]
        if vis and voos:
            mis, moos = float(np.mean(vis)), float(np.mean(voos))
            gap = mis - moos
            ratio = moos / mis if abs(mis) > 1e-8 else 0.0
            pp_oos = sum(1 for v in voos if v > 0) / len(voos)
            results[f"is_ic_{h}"] = mis
            results[f"oos_ic_{h}"] = moos
            results[f"oos_is_ratio_{h}"] = ratio
            print(f"  {h:>5d}    {mis:>7.4f}  {moos:>7.4f}  {gap:>6.4f}  {ratio:>6.1%}   {pp_oos:.0%}     {len(vis):>3d}   {len(voos):>3d}")

    # =================================================================
    # 3. EMA sensitivity sweep (vectorized — fast)
    # =================================================================
    print(f"\n[3] EMA Sensitivity Sweep (IC@50, non-overlapping)")
    fast_windows = [2, 4, 8]
    slow_windows = [16, 32, 64]

    print("         slow=16   slow=32   slow=64")
    ema_results: dict[str, float] = {}
    for fw in fast_windows:
        row = f"  fast={fw:d}  "
        for sw in slow_windows:
            ics_combo: list[float] = []
            for sym, (shallow, deep, mid) in sym_bands.items():
                signals = _gen_signals_from_bands(shallow, deep, ema_fast_w=fw, ema_slow_w=sw)
                ic, _, nc = _ic_nonoverlap(signals, mid, 50)
                if nc >= _MIN_IC_CHUNKS:
                    ics_combo.append(ic)
            mean_ic = float(np.mean(ics_combo)) if ics_combo else 0.0
            row += f" {mean_ic:>7.4f}  "
            ema_results[f"ema_{fw}_{sw}"] = mean_ic
        print(row)

    results["ema_sweep"] = ema_results
    n_positive = sum(1 for v in ema_results.values() if v > 0)
    print(f"  {n_positive}/9 combinations have positive IC@50")
    results["ema_n_positive"] = n_positive

    # =================================================================
    # 4. Walk-forward 3-fold temporal CV
    # =================================================================
    print(f"\n[4] Walk-Forward 3-Fold Temporal CV (IC@50)")
    fold_ics: list[float] = []

    for sym, (shallow, deep, mid) in sym_bands.items():
        n = len(shallow)
        if n < 3000:
            continue
        signals = _gen_signals_from_bands(shallow, deep)
        fold_size = n // 3
        for fold in range(3):
            start = fold * fold_size
            end = min(start + fold_size, n)
            ic, _, nc = _ic_nonoverlap(signals[start:end], mid[start:end], 50)
            if nc >= 5:
                fold_ics.append(ic)

    if fold_ics:
        wf_mean = float(np.mean(fold_ics))
        wf_std = float(np.std(fold_ics))
        wf_min = float(np.min(fold_ics))
        wf_pct_pos = sum(1 for v in fold_ics if v > 0) / len(fold_ics)
        results["wf_ic50_mean"] = wf_mean
        results["wf_ic50_std"] = wf_std
        results["wf_ic50_min"] = wf_min
        results["wf_pct_positive"] = wf_pct_pos
        print(f"  Mean IC@50: {wf_mean:.4f}  Std: {wf_std:.4f}  Min: {wf_min:.4f}  %Pos: {wf_pct_pos:.0%}  (n_folds={len(fold_ics)})")

    # =================================================================
    # VERDICT
    # =================================================================
    print(f"\n{'='*60}")
    print("GATE D VERDICT")
    print(f"{'='*60}")

    checks = []
    full_ic50 = results.get("full_ic_50", 0)
    checks.append(("Non-overlapping IC@50 > 0.02", full_ic50 > 0.02, f"{full_ic50:.4f}"))

    oos_ratio = results.get("oos_is_ratio_50", 0)
    checks.append(("OOS IC@50 > 50% of IS IC@50", oos_ratio > 0.5, f"{oos_ratio:.1%}"))

    checks.append(("EMA sweep >= 7/9 positive", n_positive >= 7, f"{n_positive}/9"))

    wf_mean = results.get("wf_ic50_mean", 0)
    checks.append(("Walk-forward IC@50 > 0", wf_mean > 0, f"{wf_mean:.4f}"))

    all_pass = True
    for name, passed, val in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name} = {val}")

    results["gate_d_passed"] = all_pass
    print(f"\n  Gate D: {'PASS' if all_pass else 'FAIL'}")

    out_file = _ROOT / "research" / "experiments" / "runs" / "ofi_depth_divergence_gate_d.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Saved to: {out_file}")

    return results


if __name__ == "__main__":
    run_validation()
