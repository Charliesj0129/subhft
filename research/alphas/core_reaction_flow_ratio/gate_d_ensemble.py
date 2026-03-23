"""Gate D ensemble lift test for core_reaction_flow_ratio.

1. Ensemble lift: does adding core_reaction improve composite IC?
2. Orthogonality matrix: pairwise signal correlations
3. IS/OOS on 12 informative symbols
4. Document RING_SIZE=1000 as fixed

Usage:
    python research/alphas/core_reaction_flow_ratio/gate_d_ensemble.py
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

from research.alphas.core_reaction_flow_ratio.impl import (
    CoreReactionFlowRatioAlpha,
    _InterArrivalRing,
)
from research.alphas.multilevel_ofi.impl import MultilevelOfiAlpha
from research.alphas.hawkes_ofi_impact.impl import HawkesOfiImpactAlpha
from research.alphas.flow_toxicity_ratio.impl import FlowToxicityRatioAlpha
from research.alphas.ofi_depth_divergence.impl import OfiDepthDivergenceAlpha

_DATA_DIR = _ROOT / "research" / "data" / "real" / "golden"
_SATURATION_THRESHOLD = 0.9

# 12 informative symbols (from reanalysis)
_INFORMATIVE_SYMBOLS = [
    "1101", "1301", "1303", "1326", "2002", "2303",
    "2308", "2317", "2327", "2330", "2408", "2409",
]

_MIN_IC_CHUNKS = 10


def _load_combined(symbol: str) -> dict | None:
    """Load both Tick and BidAsk data for a symbol."""
    sym_dir = _DATA_DIR / symbol
    if not sym_dir.exists():
        return None

    def _pad5(col_list: list) -> NDArray:
        out = np.zeros((len(col_list), 5), dtype=np.float64)
        for j, row in enumerate(col_list):
            k = min(len(row), 5)
            out[j, :k] = row[:k]
        return out

    all_tick_ts, all_tick_px, all_tick_vol = [], [], []
    all_ba_bp, all_ba_bv, all_ba_ap, all_ba_av = [], [], [], []

    for pf in sorted(sym_dir.glob("*.parquet")):
        table = pq.read_table(pf)
        df = table.to_pandas()

        ticks = df[(df["type"] == "Tick") & (df["price_scaled"] > 0)]
        if not ticks.empty:
            all_tick_ts.append(ticks["exch_ts"].values)
            all_tick_px.append(ticks["price_scaled"].values)
            all_tick_vol.append(ticks["volume"].values)

        ba = df[df["type"] == "BidAsk"]
        if not ba.empty:
            all_ba_bp.append(_pad5(ba["bids_price"].tolist()))
            all_ba_bv.append(_pad5(ba["bids_vol"].tolist()))
            all_ba_ap.append(_pad5(ba["asks_price"].tolist()))
            all_ba_av.append(_pad5(ba["asks_vol"].tolist()))

    if not all_tick_ts or not all_ba_bp:
        return None

    tick_ts = np.concatenate(all_tick_ts)
    tick_px = np.concatenate(all_tick_px)
    tick_vol = np.concatenate(all_tick_vol)
    order = np.argsort(tick_ts)
    tick_ts, tick_px, tick_vol = tick_ts[order], tick_px[order], tick_vol[order]

    return {
        "tick_ts": tick_ts, "tick_px": tick_px, "tick_vol": tick_vol,
        "ba_bp": np.concatenate(all_ba_bp), "ba_bv": np.concatenate(all_ba_bv),
        "ba_ap": np.concatenate(all_ba_ap), "ba_av": np.concatenate(all_ba_av),
    }


def _gen_bidask_signals(bp: NDArray, bv: NDArray, ap: NDArray, av: NDArray) -> dict[str, NDArray]:
    """Generate signals from BidAsk data for: multilevel_ofi, ofi_depth_divergence, hawkes_ofi, flow_toxicity."""
    n = len(bp)
    ml = MultilevelOfiAlpha()
    dd = OfiDepthDivergenceAlpha()
    ho = HawkesOfiImpactAlpha()
    ft = FlowToxicityRatioAlpha()

    sig_ml = np.zeros(n)
    sig_dd = np.zeros(n)
    sig_ho = np.zeros(n)
    sig_ft = np.zeros(n)

    prev_bid = 0.0
    prev_ask = 0.0

    for i in range(n):
        bids = np.column_stack([bp[i], bv[i]])
        asks = np.column_stack([ap[i], av[i]])
        sig_ml[i] = ml.update(bids=bids, asks=asks)
        sig_dd[i] = dd.update(bids=bids, asks=asks)

        bid_qty = float(bv[i, 0])
        ask_qty = float(av[i, 0])
        sig_ho[i] = ho.update(bid_qty, ask_qty)

        ofi_raw = (bid_qty - prev_bid) - (ask_qty - prev_ask)
        sig_ft[i] = ft.update(ofi_raw, bid_qty, ask_qty)
        prev_bid = bid_qty
        prev_ask = ask_qty

    return {"multilevel_ofi": sig_ml, "ofi_depth_div": sig_dd, "hawkes_ofi": sig_ho, "flow_tox": sig_ft}


def _gen_tick_signals(ts: NDArray, px: NDArray, vol: NDArray) -> NDArray:
    """Generate core_reaction signal from tick data."""
    n = len(ts)
    alpha = CoreReactionFlowRatioAlpha()
    signals = np.zeros(n)
    for i in range(n):
        signals[i] = alpha.update(int(ts[i]), float(px[i]), float(vol[i]))
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


def run_ensemble_test() -> dict:
    print("=" * 70)
    print("CORE_REACTION_FLOW_RATIO — Gate D Ensemble Lift Test")
    print("=" * 70)
    print(f"  Symbols: {len(_INFORMATIVE_SYMBOLS)} informative (saturated excluded)")
    print(f"  RING_SIZE = 1000 (fixed, not optimizable)")

    results: dict = {"ring_size": 1000, "ring_size_note": "Fixed parameter, not optimized"}

    # Collect signals for all symbols
    all_corr_pairs: list[tuple[str, str, float]] = []
    alpha_names = ["multilevel_ofi", "ofi_depth_div", "hawkes_ofi", "flow_tox", "core_reaction"]

    # Per-symbol IC at horizon 50
    ic_standalone: dict[str, list[float]] = {n: [] for n in alpha_names}
    ic_ensemble: list[float] = []
    ic_ensemble_no_cr: list[float] = []

    # Correlation accumulators
    corr_matrix_accum: dict[tuple[str, str], list[float]] = {}
    for i, a in enumerate(alpha_names):
        for j, b in enumerate(alpha_names):
            if i < j:
                corr_matrix_accum[(a, b)] = []

    for sym in _INFORMATIVE_SYMBOLS:
        print(f"\n--- {sym} ---")
        data = _load_combined(sym)
        if data is None:
            print("  SKIP: no data")
            continue

        # BidAsk signals
        ba_sigs = _gen_bidask_signals(data["ba_bp"], data["ba_bv"], data["ba_ap"], data["ba_av"])
        mid_ba = (data["ba_bp"][:, 0] + data["ba_ap"][:, 0]) / 2.0
        n_ba = len(mid_ba)

        # Tick signals (core_reaction)
        cr_sig_tick = _gen_tick_signals(data["tick_ts"], data["tick_px"], data["tick_vol"])

        # Resample core_reaction to BidAsk timeline (nearest tick signal for each BA event)
        # Simple: use the last value before each BA timestamp
        cr_sig_ba = np.zeros(n_ba)
        tick_idx = 0
        n_tick = len(data["tick_ts"])
        # Since both are sorted, we can walk through
        # But BidAsk doesn't have timestamps in our current loading...
        # Use index-based approximation: map by position ratio
        if n_tick > 0:
            ratio = n_tick / n_ba
            for i in range(n_ba):
                ti = min(int(i * ratio), n_tick - 1)
                cr_sig_ba[i] = cr_sig_tick[ti]

        ba_sigs["core_reaction"] = cr_sig_ba

        # IS/OOS split
        split = int(n_ba * 0.6)
        oos_mid = mid_ba[split:]

        # Per-alpha IC@50 (OOS, non-overlapping)
        for name in alpha_names:
            sig = ba_sigs[name][split:]
            ic, _, nc = _ic_nonoverlap(sig, oos_mid, 50)
            if nc >= _MIN_IC_CHUNKS:
                ic_standalone[name].append(ic)
            print(f"  {name:20s}: IC@50 = {ic:>7.4f} (n={nc})")

        # Ensemble: equal-weight combination of all 5 alphas
        oos_sigs = {n: ba_sigs[n][split:] for n in alpha_names}
        # Normalize each signal to zero-mean, unit-std before combining
        normed = {}
        for name, sig in oos_sigs.items():
            std = np.std(sig)
            if std > 1e-12:
                normed[name] = (sig - np.mean(sig)) / std
            else:
                normed[name] = np.zeros_like(sig)

        ensemble_all = sum(normed[n] for n in alpha_names) / len(alpha_names)
        ensemble_no_cr = sum(normed[n] for n in alpha_names if n != "core_reaction") / (len(alpha_names) - 1)

        ic_all, _, nc_all = _ic_nonoverlap(ensemble_all, oos_mid, 50)
        ic_no, _, nc_no = _ic_nonoverlap(ensemble_no_cr, oos_mid, 50)
        if nc_all >= _MIN_IC_CHUNKS:
            ic_ensemble.append(ic_all)
        if nc_no >= _MIN_IC_CHUNKS:
            ic_ensemble_no_cr.append(ic_no)
        print(f"  {'ensemble_all':20s}: IC@50 = {ic_all:>7.4f}")
        print(f"  {'ensemble_no_cr':20s}: IC@50 = {ic_no:>7.4f}")

        # Pairwise correlations (OOS)
        for i, a in enumerate(alpha_names):
            for j, b in enumerate(alpha_names):
                if i < j:
                    sa, sb = oos_sigs[a], oos_sigs[b]
                    if np.std(sa) > 1e-12 and np.std(sb) > 1e-12:
                        corr = float(np.corrcoef(sa, sb)[0, 1])
                        if math.isfinite(corr):
                            corr_matrix_accum[(a, b)].append(corr)

    # Summary
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    print("\n[1] Standalone IC@50 (non-overlapping, OOS, informative symbols)")
    for name in alpha_names:
        vals = ic_standalone[name]
        if vals:
            m = float(np.mean(vals))
            pp = sum(1 for v in vals if v > 0) / len(vals)
            print(f"  {name:20s}: mean={m:>7.4f}  %pos={pp:.0%}  (n={len(vals)})")
            results[f"standalone_ic_{name}"] = m

    print("\n[2] Ensemble Lift")
    if ic_ensemble and ic_ensemble_no_cr:
        m_all = float(np.mean(ic_ensemble))
        m_no = float(np.mean(ic_ensemble_no_cr))
        lift = m_all - m_no
        results["ensemble_all_ic"] = m_all
        results["ensemble_no_cr_ic"] = m_no
        results["ensemble_lift"] = lift
        print(f"  Ensemble (all 5):       IC@50 = {m_all:.4f}")
        print(f"  Ensemble (w/o core_rx): IC@50 = {m_no:.4f}")
        print(f"  Lift from core_reaction: {lift:+.4f}")
        print(f"  Lift reduces variance:   {'YES' if np.std(ic_ensemble) < np.std(ic_ensemble_no_cr) else 'NO'}")

    print("\n[3] Orthogonality Matrix (mean pairwise correlation)")
    print(f"  {'':20s}", end="")
    for b in alpha_names:
        print(f" {b[:8]:>8s}", end="")
    print()
    corr_means: dict[tuple[str, str], float] = {}
    for (a, b), vals in corr_matrix_accum.items():
        if vals:
            corr_means[(a, b)] = float(np.mean(vals))
    for i, a in enumerate(alpha_names):
        print(f"  {a:20s}", end="")
        for j, b in enumerate(alpha_names):
            if i == j:
                print(f" {'1.000':>8s}", end="")
            elif (a, b) in corr_means:
                print(f" {corr_means[(a,b)]:>8.3f}", end="")
            elif (b, a) in corr_means:
                print(f" {corr_means[(b,a)]:>8.3f}", end="")
            else:
                print(f" {'---':>8s}", end="")
        print()

    # core_reaction max correlation with any other alpha
    cr_corrs = [v for (a, b), v in corr_means.items() if "core_reaction" in a or "core_reaction" in b]
    if cr_corrs:
        max_corr = max(abs(c) for c in cr_corrs)
        results["cr_max_abs_corr"] = max_corr
        print(f"\n  core_reaction max |corr| with others: {max_corr:.3f}")

    print(f"\n[4] RING_SIZE = 1000 (documented as fixed)")
    print(f"  Not optimizable — moment estimator needs ~1000 samples for stable m2.")
    print(f"  Symbol filter: exclude where max(n_buy, n_sell) > {_SATURATION_THRESHOLD}")

    # Verdict
    print(f"\n{'='*60}")
    print("VERDICT")
    print(f"{'='*60}")
    standalone_cr = results.get("standalone_ic_core_reaction", 0)
    lift = results.get("ensemble_lift", 0)
    max_corr = results.get("cr_max_abs_corr", 1.0)

    checks = [
        ("Standalone IC@50 > 0", standalone_cr > 0, f"{standalone_cr:.4f}"),
        ("Ensemble lift > 0", lift > 0, f"{lift:+.4f}"),
        ("Max |corr| < 0.5 (orthogonal)", max_corr < 0.5, f"{max_corr:.3f}"),
    ]
    all_pass = True
    for name, passed, val in checks:
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  [{status}] {name} = {val}")

    results["gate_d_ensemble_passed"] = all_pass
    tier = "ENSEMBLE" if all_pass else "DEPRECATED"
    results["recommended_tier"] = tier
    print(f"\n  Recommended tier: {tier}")

    out_file = _ROOT / "research" / "experiments" / "runs" / "core_reaction_gate_d_ensemble.json"
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Saved to: {out_file}")

    return results


if __name__ == "__main__":
    run_ensemble_test()
