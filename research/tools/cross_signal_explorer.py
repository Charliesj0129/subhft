"""Cross-signal interaction alpha exploration on real L1 data.

Vectorized computation of 15 interaction/combination alpha signals across all
symbols, with forward-return IC, autocorrelation, and cross-symbol consistency
metrics.  Each alpha combines two or more base signals (QI, OFI, tox_timescale,
tox_multiscale, adverse_asym, ofi_regime, vol_ratio) into a single composite.

Usage::

    python research/tools/cross_signal_explorer.py \
        --data-dir research/data/raw \
        --out research/results/cross_signal_exploration.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
from scipy.signal import lfilter
from structlog import get_logger

logger = get_logger("cross_signal_explorer")

# ---------------------------------------------------------------------------
# EMA constants (match existing codebase)
# ---------------------------------------------------------------------------
_EMA_ALPHA_4 = 0.2212
_EMA_ALPHA_8 = 0.1175
_EMA_ALPHA_16 = 0.0606
_EMA_ALPHA_32 = 0.0308
_EMA_ALPHA_64 = 0.0155

_EPS = 1e-8


def _ema(x: np.ndarray, alpha: float) -> np.ndarray:
    """Vectorized EMA using scipy.signal.lfilter. O(n)."""
    if len(x) == 0:
        return x.copy()
    b = np.array([alpha], dtype=np.float64)
    a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
    zi = np.array([x[0] * (1.0 - alpha)], dtype=np.float64)
    out, _ = lfilter(b, a, x, zi=zi)
    return np.asarray(out, dtype=np.float64)


# ---------------------------------------------------------------------------
# Base signal computation (shared across alphas)
# ---------------------------------------------------------------------------

def _compute_base_signals(
    bid_qty: np.ndarray,
    ask_qty: np.ndarray,
    mid: np.ndarray,
    spread: np.ndarray,
) -> dict[str, np.ndarray]:
    """Compute all base signals once, then combine in alpha functions."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])

    # tox_timescale: fast/slow QI divergence gated by spread excess
    fast_qi = _ema(qi, _EMA_ALPHA_4)
    slow_qi = _ema(qi, _EMA_ALPHA_32)
    divergence = fast_qi - slow_qi
    spread_ratio_64 = spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1.0)
    gate = np.minimum(np.clip(spread_ratio_64 - 1.0, 0.0, None) + 0.1, 1.0)
    tox_timescale = np.clip(divergence * gate, -1.0, 1.0)

    # tox_multiscale: sign(QI) * EMA8(vol * |QI| * spread_dev)
    volatility = _ema(np.abs(d_mid), _EMA_ALPHA_16)
    spread_dev = spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1.0)
    raw_multi = volatility * np.abs(qi) * spread_dev
    tox_multiscale = np.clip(np.sign(qi) * _ema(raw_multi, _EMA_ALPHA_8), -2.0, 2.0)

    # adverse_asym: 2nd-moment QI decomposition
    pos_sq = np.maximum(qi, 0.0) ** 2
    neg_sq = np.maximum(-qi, 0.0) ** 2
    ema_pos = _ema(pos_sq, _EMA_ALPHA_16)
    ema_neg = _ema(neg_sq, _EMA_ALPHA_16)
    denom_asym = ema_pos + ema_neg + _EPS
    adverse_asym = np.clip(
        _ema((ema_pos - ema_neg) / denom_asym, _EMA_ALPHA_8), -1.0, 1.0
    )

    # ofi_regime: EMA8(OFI) * clip(vol16/base64, 0.5, 2.0)
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    vol16 = _ema(np.abs(ofi), _EMA_ALPHA_16)
    base64 = _ema(np.abs(ofi), _EMA_ALPHA_64) + _EPS
    rf = np.clip(vol16 / base64, 0.5, 2.0)
    ofi_regime = ofi_ema8 * rf

    # vol_ratio: EMA8(d_mid^2) / max(EMA64(d_mid^2), eps)
    d_mid_sq = d_mid ** 2
    vol_ratio = _ema(d_mid_sq, _EMA_ALPHA_8) / np.maximum(
        _ema(d_mid_sq, _EMA_ALPHA_64), _EPS
    )

    return {
        "qi": qi,
        "ofi": ofi,
        "d_mid": d_mid,
        "tox_timescale": tox_timescale,
        "tox_multiscale": tox_multiscale,
        "adverse_asym": adverse_asym,
        "ofi_regime": ofi_regime,
        "vol_ratio": vol_ratio,
        "spread": spread,
        "spread_ema64": _ema(spread, _EMA_ALPHA_64),
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
    }


# ---------------------------------------------------------------------------
# 15 Cross-signal interaction alpha formulas
# ---------------------------------------------------------------------------

def alpha_qi_x_tox_timescale(b: dict[str, np.ndarray]) -> np.ndarray:
    """QI x tox_timescale interaction."""
    return np.clip(b["qi"] * b["tox_timescale"], -2.0, 2.0)


def alpha_qi_x_tox_multiscale(b: dict[str, np.ndarray]) -> np.ndarray:
    """QI x tox_multiscale interaction."""
    return np.clip(b["qi"] * b["tox_multiscale"], -2.0, 2.0)


def alpha_qi_x_adverse_asym(b: dict[str, np.ndarray]) -> np.ndarray:
    """QI x adverse_asym interaction."""
    return np.clip(b["qi"] * b["adverse_asym"], -2.0, 2.0)


def alpha_tox_timescale_x_multi(b: dict[str, np.ndarray]) -> np.ndarray:
    """tox_timescale x tox_multiscale interaction."""
    return np.clip(b["tox_timescale"] * b["tox_multiscale"], -2.0, 2.0)


def alpha_qi_squared_sign(b: dict[str, np.ndarray]) -> np.ndarray:
    """sign(QI) * QI^2 — convex amplification of imbalance."""
    qi = b["qi"]
    return np.clip(np.sign(qi) * qi ** 2, -1.0, 1.0)


def alpha_qi_cubed(b: dict[str, np.ndarray]) -> np.ndarray:
    """QI^3 — odd-power convex amplification."""
    return np.clip(b["qi"] ** 3, -1.0, 1.0)


def alpha_qi_tox_spread_triple(b: dict[str, np.ndarray]) -> np.ndarray:
    """QI x tox_multiscale x spread_ratio — triple interaction."""
    spread_ratio = b["spread"] / np.maximum(b["spread_ema64"], 1.0)
    return np.clip(b["qi"] * b["tox_multiscale"] * spread_ratio, -3.0, 3.0)


def alpha_pca_composite(b: dict[str, np.ndarray]) -> np.ndarray:
    """PCA-inspired weighted composite of base signals."""
    raw = (
        0.4 * _ema(b["qi"], _EMA_ALPHA_8)
        + 0.3 * b["tox_timescale"]
        + 0.2 * b["adverse_asym"]
        + 0.1 * b["ofi_regime"]
    )
    return np.clip(raw, -2.0, 2.0)


def alpha_max_signal(b: dict[str, np.ndarray]) -> np.ndarray:
    """Per-tick max of |signal| magnitudes, signed by majority vote."""
    qi_abs = np.abs(b["qi"])
    tt_abs = np.abs(b["tox_timescale"])
    tm_abs = np.abs(b["tox_multiscale"])
    magnitude = np.maximum(np.maximum(qi_abs, tt_abs), tm_abs)
    # Majority sign
    sign_sum = np.sign(b["qi"]) + np.sign(b["tox_timescale"]) + np.sign(b["tox_multiscale"])
    majority_sign = np.sign(sign_sum)
    # When sign_sum == 0, use QI sign as tiebreaker
    majority_sign = np.where(majority_sign == 0, np.sign(b["qi"]), majority_sign)
    return magnitude * majority_sign


def alpha_residual_qi_vs_ofi(b: dict[str, np.ndarray]) -> np.ndarray:
    """Residual QI after regressing out OFI: captures QI alpha beyond OFI."""
    qi_ema8 = _ema(b["qi"], _EMA_ALPHA_8)
    ofi_ema8 = _ema(b["ofi"], _EMA_ALPHA_8)
    beta = _ema(qi_ema8 * ofi_ema8, _EMA_ALPHA_32) / np.maximum(
        _ema(ofi_ema8 ** 2, _EMA_ALPHA_32), _EPS
    )
    residual = qi_ema8 - beta * ofi_ema8
    return np.clip(residual, -2.0, 2.0)


def alpha_qi_vol_gated(b: dict[str, np.ndarray]) -> np.ndarray:
    """EMA8(QI) gated by high-volatility regime (vol_ratio > 1.5)."""
    qi_ema8 = _ema(b["qi"], _EMA_ALPHA_8)
    gate = (b["vol_ratio"] > 1.5).astype(np.float64)
    return qi_ema8 * gate


def alpha_momentum_agreement(b: dict[str, np.ndarray]) -> np.ndarray:
    """EMA8(QI) x sign(momentum) — QI confirmed by price momentum."""
    qi_ema8 = _ema(b["qi"], _EMA_ALPHA_8)
    fast_mid = _ema(b["d_mid"], _EMA_ALPHA_4)
    slow_mid = _ema(b["d_mid"], _EMA_ALPHA_32)
    momentum_sign = np.sign(fast_mid - slow_mid)
    return np.clip(qi_ema8 * momentum_sign, -1.0, 1.0)


def alpha_spread_gated_composite(b: dict[str, np.ndarray]) -> np.ndarray:
    """(QI + tox_timescale) / 2, active only when spread exceeds its EMA64."""
    composite = 0.5 * b["qi"] + 0.5 * b["tox_timescale"]
    gate = (b["spread"] > b["spread_ema64"]).astype(np.float64)
    return np.clip(composite * gate, -2.0, 2.0)


def alpha_anti_correlated_blend(b: dict[str, np.ndarray]) -> np.ndarray:
    """EMA8(QI) minus scaled OFI depth ratio — orthogonal blend."""
    qi_ema8 = _ema(b["qi"], _EMA_ALPHA_8)
    ofi_depth = np.abs(b["ofi"]) / (b["bid_qty"] + b["ask_qty"] + _EPS)
    ofi_depth_ema16 = _ema(ofi_depth, _EMA_ALPHA_16)
    return np.clip(qi_ema8 - 0.3 * ofi_depth_ema16, -2.0, 2.0)


def alpha_ensemble_vote(b: dict[str, np.ndarray]) -> np.ndarray:
    """Sign-vote of three base signals: QI, tox_timescale, ofi_regime."""
    vote = (
        np.sign(_ema(b["qi"], _EMA_ALPHA_8))
        + np.sign(b["tox_timescale"])
        + np.sign(b["ofi_regime"])
    ) / 3.0
    return np.clip(vote, -1.0, 1.0)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "qi_x_tox_timescale":       (alpha_qi_x_tox_timescale, "QI * tox_timescale"),
    "qi_x_tox_multiscale":      (alpha_qi_x_tox_multiscale, "QI * tox_multiscale"),
    "qi_x_adverse_asym":        (alpha_qi_x_adverse_asym, "QI * adverse_asym"),
    "tox_timescale_x_multi":    (alpha_tox_timescale_x_multi, "tox_timescale * tox_multiscale"),
    "qi_squared_sign":          (alpha_qi_squared_sign, "sign(QI) * QI^2"),
    "qi_cubed":                 (alpha_qi_cubed, "QI^3 convex"),
    "qi_tox_spread_triple":     (alpha_qi_tox_spread_triple, "QI * tox_multi * spread_ratio"),
    "pca_composite":            (alpha_pca_composite, "0.4QI+0.3tox_ts+0.2adv+0.1ofi"),
    "max_signal":               (alpha_max_signal, "max(|QI|,|tox_ts|,|tox_ms|) signed"),
    "residual_qi_vs_ofi":       (alpha_residual_qi_vs_ofi, "QI - beta*OFI residual"),
    "qi_vol_gated":             (alpha_qi_vol_gated, "EMA8(QI) in high-vol only"),
    "momentum_agreement":       (alpha_momentum_agreement, "EMA8(QI) * sign(momentum)"),
    "spread_gated_composite":   (alpha_spread_gated_composite, "(QI+tox_ts)/2 wide-spread"),
    "anti_correlated_blend":    (alpha_anti_correlated_blend, "EMA8(QI) - 0.3*OFI_depth"),
    "ensemble_vote":            (alpha_ensemble_vote, "sign-vote QI+tox_ts+ofi_reg"),
}


# ---------------------------------------------------------------------------
# Metrics computation
# ---------------------------------------------------------------------------

def _compute_forward_returns(mid: np.ndarray, horizons: list[int]) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons (in ticks)."""
    ret = {}
    for h in horizons:
        fwd = np.empty_like(mid)
        fwd[:-h] = (mid[h:] - mid[:-h]) / (mid[:-h] + _EPS)
        fwd[-h:] = 0.0  # pad end
        ret[h] = fwd
    return ret


def _compute_ic(signal: np.ndarray, fwd_ret: np.ndarray, n_chunks: int = 20) -> tuple[float, float]:
    """Compute mean IC and IC stability (IC_mean / IC_std).

    IC is rank correlation computed on non-overlapping chunks.
    """
    valid = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    sig_v = signal[valid]
    ret_v = fwd_ret[valid]

    if len(sig_v) < 1000:
        return 0.0, 0.0

    chunk_size = len(sig_v) // n_chunks
    if chunk_size < 50:
        return 0.0, 0.0

    ics = []
    for i in range(n_chunks):
        s = sig_v[i * chunk_size : (i + 1) * chunk_size]
        r = ret_v[i * chunk_size : (i + 1) * chunk_size]
        # Spearman rank correlation
        rank_s = np.argsort(np.argsort(s)).astype(np.float64)
        rank_r = np.argsort(np.argsort(r)).astype(np.float64)
        rank_s -= rank_s.mean()
        rank_r -= rank_r.mean()
        denom = np.sqrt((rank_s ** 2).sum() * (rank_r ** 2).sum())
        if denom < _EPS:
            ics.append(0.0)
        else:
            ics.append(float((rank_s * rank_r).sum() / denom))

    ic_arr = np.array(ics)
    ic_mean = float(ic_arr.mean())
    ic_std = float(ic_arr.std()) + _EPS
    return ic_mean, ic_mean / ic_std


def _compute_autocorr(signal: np.ndarray, lag: int = 1) -> float:
    """Lag-1 autocorrelation."""
    valid = np.isfinite(signal) & (signal != 0.0)
    s = signal[valid]
    if len(s) < lag + 100:
        return 0.0
    s = s - s.mean()
    c0 = np.dot(s, s)
    if c0 < _EPS:
        return 0.0
    c1 = np.dot(s[:-lag], s[lag:])
    return float(c1 / c0)


def _compute_turnover(signal: np.ndarray) -> float:
    """Signal turnover: mean |delta_signal| / mean |signal|."""
    valid = np.isfinite(signal) & (signal != 0.0)
    s = signal[valid]
    if len(s) < 100:
        return 0.0
    ds = np.abs(np.diff(s))
    return float(ds.mean() / (np.abs(s).mean() + _EPS))


def _compute_hit_rate(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Fraction of ticks where sign(signal) == sign(forward_return)."""
    valid = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0) & (fwd_ret != 0.0)
    if valid.sum() < 100:
        return 0.5
    return float((np.sign(signal[valid]) == np.sign(fwd_ret[valid])).mean())


# ---------------------------------------------------------------------------
# Per-symbol exploration
# ---------------------------------------------------------------------------

def explore_symbol(
    data_path: str,
    horizons: list[int] | None = None,
) -> dict[str, Any]:
    """Run all cross-signal alphas on one symbol's L1 data.

    Returns per-alpha metrics dict.
    """
    if horizons is None:
        horizons = [50, 200, 1000, 5000]

    data = np.load(data_path)
    n = len(data)
    if n < 2000:
        return {}

    bid_qty = data["bid_qty"].astype(np.float64)
    ask_qty = data["ask_qty"].astype(np.float64)
    mid = data["mid_price"].astype(np.float64)
    spread = data["spread_bps"].astype(np.float64)

    # Compute base signals once
    base = _compute_base_signals(bid_qty, ask_qty, mid, spread)

    # Forward returns
    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(base)
        except Exception as e:
            logger.warning("alpha_failed", alpha=alpha_id, error=str(e))
            continue

        # Warmup: skip first 500 ticks
        warmup = 500
        sig_w = signal[warmup:]

        alpha_result: dict[str, Any] = {
            "description": desc,
            "n_rows": n,
            "signal_mean": float(np.nanmean(sig_w)),
            "signal_std": float(np.nanstd(sig_w)),
            "signal_abs_mean": float(np.nanmean(np.abs(sig_w))),
            "acf_1": _compute_autocorr(sig_w, 1),
            "turnover": _compute_turnover(sig_w),
            "horizons": {},
        }

        for h in horizons:
            fwd = fwd_rets[h][warmup:]
            ic_mean, ic_ir = _compute_ic(sig_w, fwd)
            hit = _compute_hit_rate(sig_w, fwd)
            alpha_result["horizons"][str(h)] = {
                "ic_mean": ic_mean,
                "ic_ir": ic_ir,
                "hit_rate": hit,
            }

        results[alpha_id] = alpha_result

    return results


# ---------------------------------------------------------------------------
# Batch exploration across all symbols
# ---------------------------------------------------------------------------

def run_exploration(
    data_dir: str,
    horizons: list[int] | None = None,
    out_path: str | None = None,
) -> dict[str, Any]:
    """Run cross-signal alpha exploration across all symbols in data_dir."""
    base = Path(data_dir)
    if horizons is None:
        horizons = [50, 200, 1000, 5000]

    # Find all concatenated L1 files
    all_files: list[tuple[str, str]] = []
    for sym_dir in sorted(base.iterdir()):
        if not sym_dir.is_dir():
            continue
        sym = sym_dir.name.upper()
        concat_f = sym_dir / f"{sym}_all_l1.npy"
        if concat_f.exists():
            all_files.append((sym, str(concat_f)))
        else:
            # Try individual day files
            daily = sorted(sym_dir.glob(f"{sym}_*_l1.npy"))
            daily = [f for f in daily if "all" not in f.name]
            if daily:
                # Use largest single day for quick exploration
                largest = max(daily, key=lambda f: f.stat().st_size)
                all_files.append((sym, str(largest)))

    logger.info("found_symbols", count=len(all_files))

    # Run exploration
    per_symbol: dict[str, dict] = {}
    t0 = time.monotonic()

    for sym, fpath in all_files:
        logger.info("exploring", symbol=sym, path=fpath)
        t_sym = time.monotonic()
        sym_results = explore_symbol(fpath, horizons)
        elapsed = time.monotonic() - t_sym
        per_symbol[sym] = sym_results
        # Extract row count from first alpha result to avoid reloading the file
        n = next(iter(sym_results.values()), {}).get("n_rows", 0) if sym_results else 0
        logger.info("explored", symbol=sym, rows=n, alphas=len(sym_results), elapsed_s=f"{elapsed:.1f}")

    total_elapsed = time.monotonic() - t0
    logger.info("exploration_complete", symbols=len(per_symbol), elapsed_s=f"{total_elapsed:.1f}")

    # ---------------------------------------------------------------------------
    # Cross-symbol aggregation
    # ---------------------------------------------------------------------------
    alpha_ids = list(ALPHA_REGISTRY.keys())
    leaderboard: list[dict[str, Any]] = []

    for alpha_id in alpha_ids:
        agg: dict[str, Any] = {"alpha_id": alpha_id, "description": ALPHA_REGISTRY[alpha_id][1]}

        # Collect per-symbol ICs
        for h in horizons:
            h_key = str(h)
            ics = []
            hits = []
            ic_syms = []
            for sym, sym_res in per_symbol.items():
                if alpha_id in sym_res and h_key in sym_res[alpha_id].get("horizons", {}):
                    m = sym_res[alpha_id]["horizons"][h_key]
                    ics.append(m["ic_mean"])
                    hits.append(m["hit_rate"])
                    ic_syms.append(sym)

            if ics:
                ic_arr = np.array(ics)
                agg[f"h{h}_ic_mean"] = float(ic_arr.mean())
                agg[f"h{h}_ic_std"] = float(ic_arr.std())
                agg[f"h{h}_ic_ir"] = float(ic_arr.mean() / (ic_arr.std() + _EPS))
                agg[f"h{h}_hit_mean"] = float(np.mean(hits))
                agg[f"h{h}_syms_positive"] = int((ic_arr > 0).sum())
                agg[f"h{h}_syms_total"] = len(ics)
                # Best symbol
                best_idx = int(np.argmax(np.abs(ic_arr)))
                agg[f"h{h}_best_sym"] = ic_syms[best_idx]
                agg[f"h{h}_best_ic"] = float(ic_arr[best_idx])

        # Aggregated signal stats
        acfs = []
        turnovers = []
        for sym, sym_res in per_symbol.items():
            if alpha_id in sym_res:
                acfs.append(sym_res[alpha_id]["acf_1"])
                turnovers.append(sym_res[alpha_id]["turnover"])
        if acfs:
            agg["acf_1_mean"] = float(np.mean(acfs))
            agg["turnover_mean"] = float(np.mean(turnovers))

        leaderboard.append(agg)

    # Sort by best IC at horizon 1000
    leaderboard.sort(key=lambda x: abs(x.get("h1000_ic_mean", 0)), reverse=True)

    output = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_symbols": len(per_symbol),
        "total_alphas": len(alpha_ids),
        "horizons": horizons,
        "elapsed_s": total_elapsed,
        "leaderboard": leaderboard,
        "per_symbol": per_symbol,
    }

    if out_path:
        out_p = Path(out_path)
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
        logger.info("results_saved", path=out_path)

    return output


def print_leaderboard(output: dict[str, Any]) -> None:
    """Pretty-print the alpha leaderboard."""
    lb = output["leaderboard"]
    horizons = output["horizons"]

    # Header
    print(f"\n{'='*120}")
    print(f"CROSS-SIGNAL ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
    print(f"{'='*120}")

    # Pick the best horizon for ranking
    best_h = 1000

    print(f"\n{'Alpha':<30} {'IC@{}'.format(best_h):>10} {'IC_IR':>8} {'Hit%':>7} "
          f"{'ACF-1':>7} {'Turn':>7} {'Syms+':>7} {'Best':>8} {'BestIC':>8}  Description")
    print("-" * 120)

    for entry in lb:
        h_key = f"h{best_h}"
        ic = entry.get(f"{h_key}_ic_mean", 0)
        ir = entry.get(f"{h_key}_ic_ir", 0)
        hit = entry.get(f"{h_key}_hit_mean", 0.5)
        acf = entry.get("acf_1_mean", 0)
        turn = entry.get("turnover_mean", 0)
        pos = entry.get(f"{h_key}_syms_positive", 0)
        tot = entry.get(f"{h_key}_syms_total", 0)
        best_sym = entry.get(f"{h_key}_best_sym", "")
        best_ic = entry.get(f"{h_key}_best_ic", 0)

        # Star for top performers
        star = "*" if abs(ic) > 0.02 and ir > 1.5 else " "

        print(f"{star}{entry['alpha_id']:<29} {ic:>+10.5f} {ir:>8.2f} {hit*100:>6.1f}% "
              f"{acf:>7.3f} {turn:>7.3f} {pos:>3}/{tot:<3} {best_sym:>8} {best_ic:>+8.4f}  {entry['description']}")

    # Multi-horizon view for top 5
    print(f"\n{'='*120}")
    print("TOP 5 — Multi-Horizon IC Profile")
    print(f"{'='*120}")
    top5 = lb[:5]
    print(f"{'Alpha':<30}", end="")
    for h in horizons:
        print(f" {'IC@'+str(h):>10}", end="")
    print()
    print("-" * (30 + 11 * len(horizons)))
    for entry in top5:
        print(f"{entry['alpha_id']:<30}", end="")
        for h in horizons:
            ic = entry.get(f"h{h}_ic_mean", 0)
            print(f" {ic:>+10.5f}", end="")
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-signal interaction alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/cross_signal_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
