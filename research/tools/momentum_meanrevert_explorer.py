"""Large-scale momentum & mean-reversion alpha exploration on real L1 data.

Vectorized computation of 15 momentum/mean-reversion alpha signals across all
symbols, with forward-return IC, autocorrelation, and cross-symbol consistency
metrics.

Usage::

    python research/tools/momentum_meanrevert_explorer.py \
        --data-dir research/data/raw \
        --out research/results/momentum_meanrevert_exploration.json
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from collections.abc import Callable
from typing import Any

import numpy as np
from scipy.signal import lfilter
from structlog import get_logger

logger = get_logger("momentum_meanrevert_explorer")

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
# Vectorized momentum & mean-reversion alpha formulas
# ---------------------------------------------------------------------------

def alpha_price_momentum_fast(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Fast price momentum: EMA4(d_mid) normalized by EMA64(|d_mid|)."""
    d_mid = np.diff(mid, prepend=mid[0])
    num = _ema(d_mid, _EMA_ALPHA_4)
    denom = np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_64), _EPS)
    return np.clip(num / denom, -3.0, 3.0)


def alpha_price_momentum_slow(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Slow price momentum: EMA32(d_mid) normalized by EMA64(|d_mid|)."""
    d_mid = np.diff(mid, prepend=mid[0])
    num = _ema(d_mid, _EMA_ALPHA_32)
    denom = np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_64), _EPS)
    return np.clip(num / denom, -3.0, 3.0)


def alpha_momentum_divergence(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Fast-slow momentum divergence: (EMA4 - EMA32) / EMA64(|d_mid|)."""
    d_mid = np.diff(mid, prepend=mid[0])
    fast = _ema(d_mid, _EMA_ALPHA_4)
    slow = _ema(d_mid, _EMA_ALPHA_32)
    denom = np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_64), _EPS)
    return np.clip((fast - slow) / denom, -3.0, 3.0)


def alpha_mean_revert_zscore(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Mean-reversion z-score: -(mid - EMA64(mid)) / rolling_std."""
    ema64_mid = _ema(mid, _EMA_ALPHA_64)
    ema64_mid_sq = _ema(mid ** 2, _EMA_ALPHA_64)
    var = ema64_mid_sq - ema64_mid ** 2
    std = np.maximum(np.sqrt(np.maximum(var, 0.0)), _EPS)
    return np.clip(-(mid - ema64_mid) / std, -3.0, 3.0)


def alpha_trend_strength(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Trend strength (Sharpe-like): EMA8(d_mid) / sqrt(EMA8(d_mid^2))."""
    d_mid = np.diff(mid, prepend=mid[0])
    num = _ema(d_mid, _EMA_ALPHA_8)
    denom = np.maximum(np.sqrt(_ema(d_mid ** 2, _EMA_ALPHA_8) + _EPS), _EPS)
    return np.clip(num / denom, -2.0, 2.0)


def alpha_momentum_reversal(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Momentum reversal: fires when fast momentum exceeds 2x volatility."""
    d_mid = np.diff(mid, prepend=mid[0])
    fast = _ema(d_mid, _EMA_ALPHA_4)
    vol = np.sqrt(_ema(d_mid ** 2, _EMA_ALPHA_64))
    extreme = (np.abs(fast) > 2.0 * vol).astype(np.float64)
    return np.clip(-np.sign(fast) * extreme, -1.0, 1.0)


def alpha_acceleration(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Acceleration: (EMA4 - EMA8) / EMA32(|d_mid|)."""
    d_mid = np.diff(mid, prepend=mid[0])
    fast = _ema(d_mid, _EMA_ALPHA_4)
    med = _ema(d_mid, _EMA_ALPHA_8)
    denom = np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_32), _EPS)
    return np.clip((fast - med) / denom, -2.0, 2.0)


def alpha_bollinger_signal(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Bollinger band signal: (mid - EMA32(mid)) / (2 * rolling_std)."""
    ema32_mid = _ema(mid, _EMA_ALPHA_32)
    ema32_mid_sq = _ema(mid ** 2, _EMA_ALPHA_32)
    var = ema32_mid_sq - ema32_mid ** 2
    std = np.maximum(2.0 * np.sqrt(np.maximum(var, 0.0)), _EPS)
    return np.clip((mid - ema32_mid) / std, -3.0, 3.0)


def alpha_depth_weighted_momentum(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any
) -> np.ndarray:
    """Depth-weighted momentum: EMA8(d_mid * QI) / EMA32(|d_mid|)."""
    d_mid = np.diff(mid, prepend=mid[0])
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    num = _ema(d_mid * qi, _EMA_ALPHA_8)
    denom = np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_32), _EPS)
    return np.clip(num / denom, -2.0, 2.0)


def alpha_hurst_proxy(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Hurst exponent proxy: EMA32(EMA4(d_mid)^2) / EMA32(EMA32(d_mid)^2)."""
    d_mid = np.diff(mid, prepend=mid[0])
    fast_sq = _ema(d_mid, _EMA_ALPHA_4) ** 2
    slow_sq = _ema(d_mid, _EMA_ALPHA_32) ** 2
    num = _ema(fast_sq, _EMA_ALPHA_32)
    denom = np.maximum(_ema(slow_sq, _EMA_ALPHA_32), _EPS)
    return np.clip(num / denom, 0.0, 5.0)


def alpha_mean_revert_ofi_gated(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any
) -> np.ndarray:
    """Flow-confirmed mean reversion: zscore * sign(EMA8(OFI))."""
    # mean_revert_zscore
    ema64_mid = _ema(mid, _EMA_ALPHA_64)
    ema64_mid_sq = _ema(mid ** 2, _EMA_ALPHA_64)
    var = ema64_mid_sq - ema64_mid ** 2
    std = np.maximum(np.sqrt(np.maximum(var, 0.0)), _EPS)
    zscore = np.clip(-(mid - ema64_mid) / std, -3.0, 3.0)
    # OFI
    ofi = np.diff(bid_qty, prepend=bid_qty[0]) - np.diff(ask_qty, prepend=ask_qty[0])
    ofi_sign = np.sign(_ema(ofi, _EMA_ALPHA_8))
    return np.clip(zscore * ofi_sign, -3.0, 3.0)


def alpha_multi_timescale_trend(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Multi-timescale trend agreement: (sign(EMA4) + sign(EMA16) + sign(EMA64)) / 3."""
    d_mid = np.diff(mid, prepend=mid[0])
    s4 = np.sign(_ema(d_mid, _EMA_ALPHA_4))
    s16 = np.sign(_ema(d_mid, _EMA_ALPHA_16))
    s64 = np.sign(_ema(d_mid, _EMA_ALPHA_64))
    return (s4 + s16 + s64) / 3.0


def alpha_return_autocorr(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Return autocorrelation: EMA16(d_mid[t] * d_mid[t-1]) / EMA16(d_mid^2)."""
    d_mid = np.diff(mid, prepend=mid[0])
    d_mid_lag = np.roll(d_mid, 1)
    d_mid_lag[0] = 0.0
    num = _ema(d_mid * d_mid_lag, _EMA_ALPHA_16)
    denom = np.maximum(_ema(d_mid ** 2, _EMA_ALPHA_16), _EPS)
    return np.clip(num / denom, -1.0, 1.0)


def alpha_momentum_fatigue(mid: np.ndarray, **_: Any) -> np.ndarray:
    """Momentum fatigue: momentum scaled down by mean-reversion proximity."""
    d_mid = np.diff(mid, prepend=mid[0])
    # momentum component
    mom = _ema(d_mid, _EMA_ALPHA_8) / np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_64), _EPS)
    # mean_revert_zscore magnitude
    ema64_mid = _ema(mid, _EMA_ALPHA_64)
    ema64_mid_sq = _ema(mid ** 2, _EMA_ALPHA_64)
    var = ema64_mid_sq - ema64_mid ** 2
    std = np.maximum(np.sqrt(np.maximum(var, 0.0)), _EPS)
    zscore_abs = np.abs((mid - ema64_mid) / std)
    fatigue = 1.0 - zscore_abs / 3.0
    return np.clip(mom * fatigue, -2.0, 2.0)


def alpha_spread_gated_momentum(mid: np.ndarray, spread: np.ndarray, **_: Any) -> np.ndarray:
    """Spread-gated momentum: momentum fires only when spread is above average."""
    d_mid = np.diff(mid, prepend=mid[0])
    mom = _ema(d_mid, _EMA_ALPHA_8) / np.maximum(_ema(np.abs(d_mid), _EMA_ALPHA_64), _EPS)
    gate = (spread > _ema(spread, _EMA_ALPHA_64)).astype(np.float64)
    return np.clip(mom * gate, -2.0, 2.0)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[Callable[..., np.ndarray], str]] = {
    "price_momentum_fast":      (alpha_price_momentum_fast, "EMA4(d_mid)/EMA64(|d_mid|)"),
    "price_momentum_slow":      (alpha_price_momentum_slow, "EMA32(d_mid)/EMA64(|d_mid|)"),
    "momentum_divergence":      (alpha_momentum_divergence, "fast-slow EMA divergence"),
    "mean_revert_zscore":       (alpha_mean_revert_zscore, "-(mid-EMA64)/rolling_std"),
    "trend_strength":           (alpha_trend_strength, "Sharpe-like EMA8 trend"),
    "momentum_reversal":        (alpha_momentum_reversal, "extreme momentum reversal"),
    "acceleration":             (alpha_acceleration, "(EMA4-EMA8)/EMA32 accel"),
    "bollinger_signal":         (alpha_bollinger_signal, "Bollinger band z-score"),
    "depth_weighted_momentum":  (alpha_depth_weighted_momentum, "EMA8(d_mid*QI)/EMA32"),
    "hurst_proxy":              (alpha_hurst_proxy, "fast/slow var ratio"),
    "mean_revert_ofi_gated":    (alpha_mean_revert_ofi_gated, "zscore*sign(OFI) flow-confirmed"),
    "multi_timescale_trend":    (alpha_multi_timescale_trend, "3-scale sign agreement"),
    "return_autocorr":          (alpha_return_autocorr, "EMA16(r[t]*r[t-1])/var"),
    "momentum_fatigue":         (alpha_momentum_fatigue, "momentum*(1-|zscore|/3)"),
    "spread_gated_momentum":    (alpha_spread_gated_momentum, "momentum when spread>avg"),
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
    """Run all momentum/mean-reversion alphas on one symbol's L1 data.

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

    # Forward returns
    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(bid_qty=bid_qty, ask_qty=ask_qty, spread=spread, mid=mid)
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
    """Run momentum/mean-reversion alpha exploration across all symbols in data_dir."""
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
        # Extract row count from any alpha result to avoid re-loading the file
        n = next((v["n_rows"] for v in sym_results.values()), 0)
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
                syms_list = [s for s in per_symbol if alpha_id in per_symbol[s]]
                agg[f"h{h}_best_sym"] = syms_list[best_idx] if best_idx < len(syms_list) else ""
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
    print(f"MOMENTUM / MEAN-REVERSION ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
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
    parser = argparse.ArgumentParser(description="Large-scale momentum & mean-reversion alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/momentum_meanrevert_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
