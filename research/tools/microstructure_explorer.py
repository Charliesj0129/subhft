"""Large-scale microstructure alpha exploration on real L1 tick data.

Vectorized computation of 15 microstructure alpha signals across all symbols,
with forward-return IC, autocorrelation, and cross-symbol consistency metrics.

Usage::

    python research/tools/microstructure_explorer.py \
        --data-dir research/data/raw \
        --out research/results/microstructure_exploration.json
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

logger = get_logger("microstructure_explorer")

# ---------------------------------------------------------------------------
# EMA constants
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
# Vectorized microstructure alpha formulas
# ---------------------------------------------------------------------------

def alpha_kyle_lambda_proxy(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Kyle's lambda proxy: price impact coefficient.
    clip(EMA32(OFI * d_mid) / max(EMA32(OFI^2), eps), -2, 2)."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])
    numer = _ema(ofi * d_mid, _EMA_ALPHA_32)
    denom = np.maximum(_ema(ofi ** 2, _EMA_ALPHA_32), _EPS)
    return np.clip(numer / denom, -2.0, 2.0)


def alpha_transient_impact(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Instantaneous impact: clip(d_mid / max(|EMA8(OFI)|, eps), -2, 2)."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])
    denom = np.maximum(np.abs(_ema(ofi, _EMA_ALPHA_8)), _EPS)
    return np.clip(d_mid / denom, -2.0, 2.0)


def alpha_markov_lob_inertia(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """LOB state inertia via Markov transition probability.
    EMA8((sign(QI[t]) == sign(QI[t-1])).astype(float)) - 0.5."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    sign_qi = np.sign(qi)
    same_sign = (sign_qi[1:] == sign_qi[:-1]).astype(np.float64)
    same_sign = np.concatenate([[0.0], same_sign])
    return _ema(same_sign, _EMA_ALPHA_8) - 0.5


def alpha_amihud_proxy(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Amihud illiquidity proxy: normalised |d_mid|/depth.
    clip(|d_mid| / max(bid_qty+ask_qty, eps) / max(EMA64(...), eps), -3, 3)."""
    d_mid = np.diff(mid, prepend=mid[0])
    total_depth = bid_qty + ask_qty + _EPS
    raw = np.abs(d_mid) / total_depth
    baseline = np.maximum(_ema(raw, _EMA_ALPHA_64), _EPS)
    return np.clip(raw / baseline, -3.0, 3.0)


def alpha_adverse_selection_cost(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Adverse selection cost: clip(EMA16(|d_mid| * sign(EMA8(OFI))), -2, 2)."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    raw = np.abs(d_mid) * np.sign(ofi_ema8)
    return np.clip(_ema(raw, _EMA_ALPHA_16), -2.0, 2.0)


def alpha_information_share(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray,
    bid_px: np.ndarray, ask_px: np.ndarray, **_: Any,
) -> np.ndarray:
    """Information share: microprice variance / mid variance.
    clip(EMA32(diff(microprice)^2) / max(EMA32(d_mid^2), eps), 0, 3)."""
    microprice = (bid_px * ask_qty + ask_px * bid_qty) / (bid_qty + ask_qty + _EPS)
    d_micro = np.diff(microprice, prepend=microprice[0])
    d_mid = np.diff(mid, prepend=mid[0])
    numer = _ema(d_micro ** 2, _EMA_ALPHA_32)
    denom = np.maximum(_ema(d_mid ** 2, _EMA_ALPHA_32), _EPS)
    return np.clip(numer / denom, 0.0, 3.0)


def alpha_quote_intensity(
    local_ts: np.ndarray, **_: Any,
) -> np.ndarray:
    """Quote arrival rate ratio.
    clip(EMA64(1e9/max(dt,1)) / max(EMA64(EMA64(1e9/max(dt,1))), eps), 0, 5)."""
    dt = np.diff(local_ts.astype(np.float64), prepend=local_ts[0])
    dt = np.maximum(dt, 1.0)
    rate = 1e9 / dt
    fast = _ema(rate, _EMA_ALPHA_64)
    slow = np.maximum(_ema(fast, _EMA_ALPHA_64), _EPS)
    return np.clip(fast / slow, 0.0, 5.0)


def alpha_depth_resilience(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Depth resilience: replenishment vs depletion ratio.
    clip(EMA8(max(d_depth,0)) / max(EMA32(max(-d_depth,0)), eps), 0, 3)."""
    total = bid_qty + ask_qty
    d_total = np.diff(total, prepend=total[0])
    replenish = np.maximum(d_total, 0.0)
    deplete = np.maximum(-d_total, 0.0)
    numer = _ema(replenish, _EMA_ALPHA_8)
    denom = np.maximum(_ema(deplete, _EMA_ALPHA_32), _EPS)
    return np.clip(numer / denom, 0.0, 3.0)


def alpha_spread_impact_ratio(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread-impact coupling: clip(EMA16(d_spread * sign(EMA8(OFI))), -2, 2)."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_spread = np.diff(spread, prepend=spread[0])
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    raw = d_spread * np.sign(ofi_ema8)
    return np.clip(_ema(raw, _EMA_ALPHA_16), -2.0, 2.0)


def alpha_permanent_impact(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Permanent impact component.
    clip(EMA64(d_mid * sign(EMA8(OFI))) / max(EMA64(|EMA8(OFI)|), eps), -2, 2)."""
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    d_mid = np.diff(mid, prepend=mid[0])
    ofi_ema8 = _ema(ofi, _EMA_ALPHA_8)
    numer = _ema(d_mid * np.sign(ofi_ema8), _EMA_ALPHA_64)
    denom = np.maximum(_ema(np.abs(ofi_ema8), _EMA_ALPHA_64), _EPS)
    return np.clip(numer / denom, -2.0, 2.0)


def alpha_temporary_impact(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Temporary impact = transient - permanent, clipped [-2,2]."""
    transient = alpha_transient_impact(bid_qty=bid_qty, ask_qty=ask_qty, mid=mid)
    permanent = alpha_permanent_impact(bid_qty=bid_qty, ask_qty=ask_qty, mid=mid)
    return np.clip(transient - permanent, -2.0, 2.0)


def alpha_liquidity_provision_asym(
    bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any,
) -> np.ndarray:
    """Asymmetric liquidity provision.
    clip((EMA8(max(d_bid,0)) - EMA8(max(d_ask,0))) /
         max(EMA8(max(d_bid,0)) + EMA8(max(d_ask,0)), eps), -1, 1)."""
    d_bid = np.diff(bid_qty, prepend=bid_qty[0])
    d_ask = np.diff(ask_qty, prepend=ask_qty[0])
    bid_add = _ema(np.maximum(d_bid, 0.0), _EMA_ALPHA_8)
    ask_add = _ema(np.maximum(d_ask, 0.0), _EMA_ALPHA_8)
    denom = np.maximum(bid_add + ask_add, _EPS)
    return np.clip((bid_add - ask_add) / denom, -1.0, 1.0)


def alpha_price_discovery_speed(
    mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Price discovery speed: inverse of return autocorrelation magnitude.
    clip(1 / max(|EMA16(d_mid[t]*d_mid[t-1]) / max(EMA16(d_mid^2), eps)|, 0.01), 0, 10)."""
    d_mid = np.diff(mid, prepend=mid[0])
    d_mid_lag = np.roll(d_mid, 1)
    d_mid_lag[0] = 0.0
    autocov = _ema(d_mid * d_mid_lag, _EMA_ALPHA_16)
    variance = np.maximum(_ema(d_mid ** 2, _EMA_ALPHA_16), _EPS)
    rho = autocov / variance
    return np.clip(1.0 / np.maximum(np.abs(rho), 0.01), 0.0, 10.0)


def alpha_queue_position_value(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Queue position value: (depth * spread) normalised by its EMA64.
    (bid_qty+ask_qty)*spread / max(EMA64((bid_qty+ask_qty)*spread), eps)."""
    total = bid_qty + ask_qty
    raw = total * spread
    baseline = np.maximum(_ema(raw, _EMA_ALPHA_64), _EPS)
    return raw / baseline


def alpha_depth_weighted_impact(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Depth-weighted price impact.
    clip(EMA8(d_mid*(bid_qty+ask_qty)) / max(EMA32(|d_mid|*(bid_qty+ask_qty)), eps), -2, 2)."""
    d_mid = np.diff(mid, prepend=mid[0])
    total = bid_qty + ask_qty
    numer = _ema(d_mid * total, _EMA_ALPHA_8)
    denom = np.maximum(_ema(np.abs(d_mid) * total, _EMA_ALPHA_32), _EPS)
    return np.clip(numer / denom, -2.0, 2.0)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "kyle_lambda_proxy":       (alpha_kyle_lambda_proxy, "price impact coefficient"),
    "transient_impact":        (alpha_transient_impact, "instantaneous impact"),
    "markov_lob_inertia":      (alpha_markov_lob_inertia, "LOB state transition prob"),
    "amihud_proxy":            (alpha_amihud_proxy, "Amihud illiquidity ratio"),
    "adverse_selection_cost":  (alpha_adverse_selection_cost, "adverse selection cost"),
    "information_share":       (alpha_information_share, "microprice info share"),
    "quote_intensity":         (alpha_quote_intensity, "quote arrival rate ratio"),
    "depth_resilience":        (alpha_depth_resilience, "replenish/deplete ratio"),
    "spread_impact_ratio":     (alpha_spread_impact_ratio, "spread-OFI coupling"),
    "permanent_impact":        (alpha_permanent_impact, "permanent impact component"),
    "temporary_impact":        (alpha_temporary_impact, "transient - permanent"),
    "liquidity_provision_asym": (alpha_liquidity_provision_asym, "asym liquidity provision"),
    "price_discovery_speed":   (alpha_price_discovery_speed, "inverse return autocorr"),
    "queue_position_value":    (alpha_queue_position_value, "depth*spread normalised"),
    "depth_weighted_impact":   (alpha_depth_weighted_impact, "depth-weighted price impact"),
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
    """Run all microstructure alphas on one symbol's L1 data.

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
    bid_px = data["bid_px"].astype(np.float64)
    ask_px = data["ask_px"].astype(np.float64)
    local_ts = data["local_ts"]

    # Forward returns
    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(
                bid_qty=bid_qty, ask_qty=ask_qty, spread=spread, mid=mid,
                bid_px=bid_px, ask_px=ask_px, local_ts=local_ts,
            )
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
    """Run microstructure alpha exploration across all symbols in data_dir."""
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
        first = next(iter(sym_results.values()), {})
        n = first.get("n_rows", 0)
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
                # Best symbol
                best_idx = int(np.argmax(np.abs(ic_arr)))
                agg[f"h{h}_best_sym"] = list(per_symbol.keys())[best_idx] if best_idx < len(per_symbol) else ""
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
    print(f"MICROSTRUCTURE ALPHA LEADERBOARD -- {output['total_symbols']} symbols, {output['total_alphas']} alphas")
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
    print("TOP 5 -- Multi-Horizon IC Profile")
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
    parser = argparse.ArgumentParser(description="Large-scale microstructure alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/microstructure_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
