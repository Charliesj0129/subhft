"""Large-scale toxicity II alpha exploration on real L1 data.

Second wave of toxicity/informed-flow alpha signals — 15 NEW formulas
complementing the original toxicity_alpha_explorer.py.

Covers: VPIN proxy, regime detection, adverse selection timing,
depth depletion, flash crash, stealth trading, quote stuffing,
and multi-signal composites.

Usage::

    python research/tools/toxicity_ii_explorer.py \
        --data-dir research/data/raw \
        --out research/results/toxicity_ii_exploration.json
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

logger = get_logger("toxicity_ii_explorer")

# ---------------------------------------------------------------------------
# EMA constants (matching codebase)
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
# 15 NEW toxicity alpha formulas
# ---------------------------------------------------------------------------


def alpha_vpin_proxy(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """VPIN proxy: EMA32(|EMA4(QI)| x (bq+aq)) / max(EMA32(bq+aq), eps)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_ema4 = _ema(qi, _EMA_ALPHA_4)
    total = bid_qty + ask_qty
    numerator = _ema(np.abs(qi_ema4) * total, _EMA_ALPHA_32)
    denominator = np.maximum(_ema(total, _EMA_ALPHA_32), _EPS)
    return numerator / denominator


def alpha_toxicity_regime_detector(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Regime detector: EMA4(|QI|) / max(EMA64(|QI|), eps)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_abs = np.abs(qi)
    fast = _ema(qi_abs, _EMA_ALPHA_4)
    slow = np.maximum(_ema(qi_abs, _EMA_ALPHA_64), _EPS)
    return fast / slow


def alpha_informed_clustering_v2(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Informed clustering v2: EMA8((|QI|>1.5*sqrt(EMA64(QI^2))).float) x sign(EMA4(QI))."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_sq_ema64 = _ema(qi ** 2, _EMA_ALPHA_64)
    threshold = 1.5 * np.sqrt(qi_sq_ema64)
    extreme = (np.abs(qi) > threshold).astype(np.float64)
    intensity = _ema(extreme, _EMA_ALPHA_8)
    direction = np.sign(_ema(qi, _EMA_ALPHA_4))
    return np.clip(intensity * direction, -1.0, 1.0)


def alpha_adverse_selection_timing(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Adverse selection timing: EMA8(|QI| x EMA16(|QI|)) x sign(QI)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_abs = np.abs(qi)
    qi_abs_ema16 = _ema(qi_abs, _EMA_ALPHA_16)
    raw = _ema(qi_abs * qi_abs_ema16, _EMA_ALPHA_8)
    return np.clip(raw * np.sign(qi), -2.0, 2.0)


def alpha_depth_depletion_toxicity(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Depth depletion: directional queue depletion aligned with QI.

    max(-diff(bq),0)*(QI>0) + max(-diff(aq),0)*(QI<0), directed by sign(QI).
    """
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    dbq = np.diff(bid_qty, prepend=bid_qty[0])
    daq = np.diff(ask_qty, prepend=ask_qty[0])
    bid_depletion = np.maximum(-dbq, 0.0) * (qi > 0).astype(np.float64)
    ask_depletion = np.maximum(-daq, 0.0) * (qi < 0).astype(np.float64)
    raw = _ema(bid_depletion + ask_depletion, _EMA_ALPHA_8) * np.sign(qi)
    return np.clip(raw, -2.0, 2.0)


def alpha_toxic_spread_momentum(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Toxic spread momentum: EMA8(diff(spread) x sign(EMA8(OFI)))."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    d_spread = np.diff(spread, prepend=spread[0])
    raw = _ema(d_spread * np.sign(ofi_ema8), _EMA_ALPHA_8)
    return np.clip(raw, -2.0, 2.0)


def alpha_flash_crash_indicator(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Flash crash: EMA4((|d_mid|>2*sqrt(EMA64(d_mid^2))).float) x sign(QI)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_mid = np.diff(mid, prepend=mid[0])
    d_mid_sq_ema64 = _ema(d_mid ** 2, _EMA_ALPHA_64)
    threshold = 2.0 * np.sqrt(d_mid_sq_ema64)
    extreme = (np.abs(d_mid) > threshold).astype(np.float64)
    intensity = _ema(extreme, _EMA_ALPHA_4)
    return np.clip(intensity * np.sign(qi), -1.0, 1.0)


def alpha_informed_arrival_rate(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Informed arrival rate: EMA8((|diff(QI)|>EMA32(|diff(QI)|)).float)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    dqi = np.abs(np.diff(qi, prepend=qi[0]))
    threshold = _ema(dqi, _EMA_ALPHA_32)
    extreme = (dqi > threshold).astype(np.float64)
    return np.clip(_ema(extreme, _EMA_ALPHA_8), 0.0, 1.0)


def alpha_toxicity_half_life(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Toxicity half-life: 1 / EMA16(1/max(|QI-EMA64(QI)|, 0.01)). Decay speed."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_ema64 = _ema(qi, _EMA_ALPHA_64)
    deviation = np.maximum(np.abs(qi - qi_ema64), 0.01)
    inv_dev = 1.0 / deviation
    ema_inv = _ema(inv_dev, _EMA_ALPHA_16)
    return np.clip(1.0 / ema_inv, 0.0, 2.0)


def alpha_multi_tox_composite(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Multi-toxicity composite: 0.4*EMA8(QI) + 0.3*ofi_regime + 0.2*tox_multi + 0.1*adverse_asym."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)

    # Component 1: EMA8(QI)
    c1 = _ema(qi, _EMA_ALPHA_8)

    # Component 2: ofi_regime
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    vol16 = _ema(np.abs(ofi), _EMA_ALPHA_16)
    base64 = _ema(np.abs(ofi), _EMA_ALPHA_64) + _EPS
    c2 = ofi_ema8 * np.clip(vol16 / base64, 0.5, 2.0)

    # Component 3: tox_multiscale
    d_mid = np.diff(mid, prepend=mid[0])
    volatility = _ema(np.abs(d_mid), _EMA_ALPHA_16)
    spread_dev = spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1.0)
    c3 = np.sign(qi) * _ema(volatility * np.abs(qi) * spread_dev, _EMA_ALPHA_8)

    # Component 4: adverse_flow_asymmetry
    pos_sq = np.maximum(qi, 0.0) ** 2
    neg_sq = np.maximum(-qi, 0.0) ** 2
    ema_pos = _ema(pos_sq, _EMA_ALPHA_16)
    ema_neg = _ema(neg_sq, _EMA_ALPHA_16)
    c4 = _ema((ema_pos - ema_neg) / (ema_pos + ema_neg + _EPS), _EMA_ALPHA_8)

    raw = 0.4 * c1 + 0.3 * c2 + 0.2 * c3 + 0.1 * c4
    return np.clip(raw, -2.0, 2.0)


def alpha_toxicity_asymmetry_ratio(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Asymmetry ratio: EMA16(max(QI,0)*|d_mid|)/(EMA16(max(QI,0)*|d_mid|)+EMA16(max(-QI,0)*|d_mid|)+eps) - 0.5."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_mid = np.abs(np.diff(mid, prepend=mid[0]))
    pos_impact = _ema(np.maximum(qi, 0.0) * d_mid, _EMA_ALPHA_16)
    neg_impact = _ema(np.maximum(-qi, 0.0) * d_mid, _EMA_ALPHA_16)
    return pos_impact / (pos_impact + neg_impact + _EPS) - 0.5


def alpha_stealth_trading_proxy(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Stealth trading: large price move with small OFI = hidden informed flow.

    (|d_mid|/max(|EMA8(OFI)|,eps)) * (|QI|<EMA64(|QI|)).float * sign(d_mid).
    """
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])
    ofi_ema8 = np.maximum(np.abs(_ema(ofi, _EMA_ALPHA_8)), _EPS)
    qi_abs = np.abs(qi)
    qi_abs_ema64 = _ema(qi_abs, _EMA_ALPHA_64)
    stealth_gate = (qi_abs < qi_abs_ema64).astype(np.float64)
    raw = (np.abs(d_mid) / ofi_ema8) * stealth_gate * np.sign(d_mid)
    return np.clip(raw, -2.0, 2.0)


def alpha_toxicity_momentum(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Toxicity momentum: fast/slow toxicity level divergence x sign(QI).

    tox_level = EMA8(|QI| x spread / max(EMA64(spread), 1)).
    """
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    spread_norm = spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1.0)
    tox_level = _ema(np.abs(qi) * spread_norm, _EMA_ALPHA_8)
    fast = _ema(tox_level, _EMA_ALPHA_4)
    slow = _ema(tox_level, _EMA_ALPHA_32)
    return np.clip((fast - slow) * np.sign(qi), -1.0, 1.0)


def alpha_quote_stuffing_proxy(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray,
    local_ts: np.ndarray | None = None, **_: Any,
) -> np.ndarray:
    """Quote stuffing: high update rate with small price moves = manipulative.

    Uses local_ts if available, otherwise falls back to tick index spacing.
    """
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_mid = np.diff(mid, prepend=mid[0])

    if local_ts is not None and len(local_ts) == len(mid):
        dt = np.diff(local_ts.astype(np.float64), prepend=local_ts[0].astype(np.float64))
        dt = np.maximum(dt, 1.0)
        rate = 1e9 / dt  # updates per second (assuming ns timestamps)
    else:
        # Fallback: use constant rate proxy
        rate = np.ones_like(mid, dtype=np.float64)

    d_mid_abs = np.abs(d_mid)
    d_mid_ema64 = _ema(d_mid_abs, _EMA_ALPHA_64)
    small_move_gate = (d_mid_abs < d_mid_ema64).astype(np.float64)
    rate_ema32 = np.maximum(_ema(rate, _EMA_ALPHA_32), _EPS)
    raw = _ema(rate, _EMA_ALPHA_8) * small_move_gate * np.sign(qi) / rate_ema32
    return np.clip(raw, -2.0, 2.0)


def alpha_toxicity_vol_coupling(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Toxicity-volatility coupling: joint intensity normalized by marginals.

    EMA16(|QI| x d_mid^2) / max(EMA16(|QI|) x EMA16(d_mid^2), eps) x sign(QI).
    """
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_mid = np.diff(mid, prepend=mid[0])
    d_mid_sq = d_mid ** 2
    qi_abs = np.abs(qi)
    joint = _ema(qi_abs * d_mid_sq, _EMA_ALPHA_16)
    marginal = _ema(qi_abs, _EMA_ALPHA_16) * _ema(d_mid_sq, _EMA_ALPHA_16)
    denom = np.maximum(marginal, _EPS)
    return np.clip((joint / denom) * np.sign(qi), -2.0, 2.0)


# ---------------------------------------------------------------------------
# Alpha registry — 15 new alphas
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "vpin_proxy":                (alpha_vpin_proxy, "VPIN-inspired vol toxicity"),
    "toxicity_regime_detector":  (alpha_toxicity_regime_detector, "fast/slow |QI| ratio"),
    "informed_clustering_v2":    (alpha_informed_clustering_v2, "extreme QI burst directed"),
    "adverse_selection_timing":  (alpha_adverse_selection_timing, "|QI|*EMA16(|QI|) directed"),
    "depth_depletion_toxicity":  (alpha_depth_depletion_toxicity, "directional queue drain"),
    "toxic_spread_momentum":     (alpha_toxic_spread_momentum, "d_spread*sign(OFI)"),
    "flash_crash_indicator":     (alpha_flash_crash_indicator, "extreme d_mid burst"),
    "informed_arrival_rate":     (alpha_informed_arrival_rate, "|dQI| exceeds EMA32"),
    "toxicity_half_life":        (alpha_toxicity_half_life, "QI deviation decay speed"),
    "multi_tox_composite":       (alpha_multi_tox_composite, "4-signal weighted blend"),
    "toxicity_asymmetry_ratio":  (alpha_toxicity_asymmetry_ratio, "directional impact ratio"),
    "stealth_trading_proxy":     (alpha_stealth_trading_proxy, "large move, small OFI"),
    "toxicity_momentum":         (alpha_toxicity_momentum, "fast/slow tox_level div"),
    "quote_stuffing_proxy":      (alpha_quote_stuffing_proxy, "high rate, small move"),
    "toxicity_vol_coupling":     (alpha_toxicity_vol_coupling, "|QI|*vol^2 joint/marginal"),
}


# ---------------------------------------------------------------------------
# Metrics computation (shared infrastructure)
# ---------------------------------------------------------------------------

def _compute_forward_returns(mid: np.ndarray, horizons: list[int]) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons (in ticks)."""
    ret = {}
    for h in horizons:
        fwd = np.empty_like(mid)
        fwd[:-h] = (mid[h:] - mid[:-h]) / (mid[:-h] + _EPS)
        fwd[-h:] = 0.0
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
    """Signal turnover: mean |d_signal| / mean |signal|."""
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
    """Run all toxicity II alphas on one symbol's L1 data."""
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

    # Optional: local_ts for quote_stuffing_proxy
    local_ts: np.ndarray | None = None
    if "local_ts" in data.dtype.names:
        local_ts = data["local_ts"]

    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(
                bid_qty=bid_qty, ask_qty=ask_qty,
                spread=spread, mid=mid, local_ts=local_ts,
            )
        except Exception as e:
            logger.warning("alpha_failed", alpha=alpha_id, error=str(e))
            continue

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
    """Run toxicity II alpha exploration across all symbols in data_dir."""
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
            daily = sorted(sym_dir.glob(f"{sym}_*_l1.npy"))
            daily = [f for f in daily if "all" not in f.name]
            if daily:
                largest = max(daily, key=lambda f: f.stat().st_size)
                all_files.append((sym, str(largest)))

    logger.info("found_symbols", count=len(all_files))

    per_symbol: dict[str, dict] = {}
    t0 = time.monotonic()

    for sym, fpath in all_files:
        logger.info("exploring", symbol=sym, path=fpath)
        t_sym = time.monotonic()
        sym_results = explore_symbol(fpath, horizons)
        elapsed = time.monotonic() - t_sym
        per_symbol[sym] = sym_results
        n = np.load(fpath).shape[0]
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

        for h in horizons:
            h_key = str(h)
            ics = []
            irs = []
            hits = []
            for sym, sym_res in per_symbol.items():
                if alpha_id in sym_res and h_key in sym_res[alpha_id].get("horizons", {}):
                    m = sym_res[alpha_id]["horizons"][h_key]
                    ics.append(m["ic_mean"])
                    irs.append(m["ic_ir"])
                    hits.append(m["hit_rate"])

            if ics:
                ic_arr = np.array(ics)
                agg[f"h{h}_ic_mean"] = float(ic_arr.mean())
                agg[f"h{h}_ic_std"] = float(ic_arr.std())
                agg[f"h{h}_ic_ir"] = float(ic_arr.mean() / (ic_arr.std() + _EPS))
                agg[f"h{h}_hit_mean"] = float(np.mean(hits))
                agg[f"h{h}_syms_positive"] = int((ic_arr > 0).sum())
                agg[f"h{h}_syms_total"] = len(ics)
                best_idx = int(np.argmax(np.abs(ic_arr)))
                syms_list = list(per_symbol.keys())
                agg[f"h{h}_best_sym"] = syms_list[best_idx] if best_idx < len(syms_list) else ""
                agg[f"h{h}_best_ic"] = float(ic_arr[best_idx])

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

    print(f"\n{'='*120}")
    print(f"TOXICITY II ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
    print(f"{'='*120}")

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
    parser = argparse.ArgumentParser(description="Large-scale toxicity II alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/toxicity_ii_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
