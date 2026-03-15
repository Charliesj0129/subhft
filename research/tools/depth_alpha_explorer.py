"""Large-scale depth-dynamics alpha exploration on real L1 data.

Vectorized computation of 15 depth-related alpha signals across all symbols,
with forward-return IC, autocorrelation, and cross-symbol consistency metrics.

Usage::

    python research/tools/depth_alpha_explorer.py \
        --data-dir research/data/raw \
        --out research/results/depth_exploration.json
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

logger = get_logger("depth_explorer")

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
# Vectorized depth alpha formulas (15 signals)
# ---------------------------------------------------------------------------

def alpha_depth_momentum(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA16(delta_total_depth) / max(EMA64(|delta_total_depth|), eps)."""
    total = bid_qty + ask_qty
    dtotal = np.diff(total, prepend=total[0])
    num = _ema(dtotal, _EMA_ALPHA_16)
    denom = np.maximum(_ema(np.abs(dtotal), _EMA_ALPHA_64), _EPS)
    return num / denom


def alpha_depth_shock(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA4(min(delta_bid,0) - min(delta_ask,0)) / max(EMA32(|shock|), eps), -2, 2)."""
    dbid = np.diff(bid_qty, prepend=bid_qty[0])
    dask = np.diff(ask_qty, prepend=ask_qty[0])
    shock = np.minimum(dbid, 0.0) - np.minimum(dask, 0.0)
    num = _ema(shock, _EMA_ALPHA_4)
    denom = np.maximum(_ema(np.abs(shock), _EMA_ALPHA_32), _EPS)
    return np.clip(num / denom, -2.0, 2.0)


def alpha_depth_velocity_diff(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(delta_bid - delta_ask) / max(EMA32(|delta_bid - delta_ask|), eps), -2, 2)."""
    dbid = np.diff(bid_qty, prepend=bid_qty[0])
    dask = np.diff(ask_qty, prepend=ask_qty[0])
    diff = dbid - dask
    num = _ema(diff, _EMA_ALPHA_8)
    denom = np.maximum(_ema(np.abs(diff), _EMA_ALPHA_32), _EPS)
    return np.clip(num / denom, -2.0, 2.0)


def alpha_depth_ratio_log(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(log(max(bq,1) / max(aq,1))), -2, 2)."""
    ratio = np.log(np.maximum(bid_qty, 1.0) / np.maximum(ask_qty, 1.0))
    return np.clip(_ema(ratio, _EMA_ALPHA_8), -2.0, 2.0)


def alpha_queue_acceleration(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA4(EMA8(QI) - EMA32(QI)), -1, 1)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    fast = _ema(qi, _EMA_ALPHA_8)
    slow = _ema(qi, _EMA_ALPHA_32)
    return np.clip(_ema(fast - slow, _EMA_ALPHA_4), -1.0, 1.0)


def alpha_mean_revert_qi(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(-(QI - EMA64(QI)) / max(sqrt(EMA32((QI-EMA64(QI))^2)), eps), -2, 2)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_slow = _ema(qi, _EMA_ALPHA_64)
    dev = qi - qi_slow
    dev_var = _ema(dev ** 2, _EMA_ALPHA_32)
    denom = np.maximum(np.sqrt(dev_var), _EPS)
    return np.clip(-dev / denom, -2.0, 2.0)


def alpha_cross_ema_qi(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA4(QI) - EMA16(QI), -1, 1)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    fast = _ema(qi, _EMA_ALPHA_4)
    slow = _ema(qi, _EMA_ALPHA_16)
    return np.clip(fast - slow, -1.0, 1.0)


def alpha_vol_of_imbalance(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA16(QI^2) - EMA16(QI)^2. Variance of imbalance (unsigned)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    ema_sq = _ema(qi ** 2, _EMA_ALPHA_16)
    sq_ema = _ema(qi, _EMA_ALPHA_16) ** 2
    return ema_sq - sq_ema


def alpha_depth_ratio_momentum(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA4(log(bq/aq)) - EMA16(log(bq/aq)). Cross-over momentum of depth ratio."""
    ratio = np.log(np.maximum(bid_qty, 1.0) / np.maximum(ask_qty, 1.0))
    fast = _ema(ratio, _EMA_ALPHA_4)
    slow = _ema(ratio, _EMA_ALPHA_16)
    return fast - slow


def alpha_total_depth_zscore(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """(total - EMA64(total)) / max(sqrt(EMA64(total^2) - EMA64(total)^2), eps)."""
    total = bid_qty + ask_qty
    ema_total = _ema(total, _EMA_ALPHA_64)
    ema_total_sq = _ema(total ** 2, _EMA_ALPHA_64)
    var = ema_total_sq - ema_total ** 2
    denom = np.maximum(np.sqrt(np.maximum(var, 0.0)), _EPS)
    return (total - ema_total) / denom


def alpha_asymmetric_depth_growth(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip((EMA8(max(dbid,0)) - EMA8(max(dask,0))) / max(EMA8(max(dbid,0)) + EMA8(max(dask,0)), eps), -1, 1)."""
    dbid = np.diff(bid_qty, prepend=bid_qty[0])
    dask = np.diff(ask_qty, prepend=ask_qty[0])
    bid_growth = _ema(np.maximum(dbid, 0.0), _EMA_ALPHA_8)
    ask_growth = _ema(np.maximum(dask, 0.0), _EMA_ALPHA_8)
    denom = np.maximum(bid_growth + ask_growth, _EPS)
    return np.clip((bid_growth - ask_growth) / denom, -1.0, 1.0)


def alpha_depth_persistence(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(QI[t]*QI[t-1]) / max(EMA8(QI^2), eps), -1, 1)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    qi_lag = np.roll(qi, 1)
    qi_lag[0] = qi[0]
    product = qi * qi_lag
    num = _ema(product, _EMA_ALPHA_8)
    denom = np.maximum(_ema(qi ** 2, _EMA_ALPHA_8), _EPS)
    return np.clip(num / denom, -1.0, 1.0)


def alpha_depth_curvature(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip(EMA8(diff(diff(total))) / max(EMA32(|diff(diff(total))|), eps), -2, 2)."""
    total = bid_qty + ask_qty
    d1 = np.diff(total, prepend=total[0])
    d2 = np.diff(d1, prepend=d1[0])
    num = _ema(d2, _EMA_ALPHA_8)
    denom = np.maximum(_ema(np.abs(d2), _EMA_ALPHA_32), _EPS)
    return np.clip(num / denom, -2.0, 2.0)


def alpha_bid_ask_velocity_ratio(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """EMA8(|delta_bid|) / max(EMA8(|delta_bid|) + EMA8(|delta_ask|), eps) - 0.5."""
    dbid = np.abs(np.diff(bid_qty, prepend=bid_qty[0]))
    dask = np.abs(np.diff(ask_qty, prepend=ask_qty[0]))
    ema_dbid = _ema(dbid, _EMA_ALPHA_8)
    ema_dask = _ema(dask, _EMA_ALPHA_8)
    denom = np.maximum(ema_dbid + ema_dask, _EPS)
    return ema_dbid / denom - 0.5


def alpha_depth_surprise(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """clip((|delta_total - EMA8(delta_total)| * sign(QI)) / max(EMA16(|delta_total|), eps), -2, 2)."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    total = bid_qty + ask_qty
    dtotal = np.diff(total, prepend=total[0])
    ema_dtotal = _ema(dtotal, _EMA_ALPHA_8)
    surprise = np.abs(dtotal - ema_dtotal) * np.sign(qi)
    denom = np.maximum(_ema(np.abs(dtotal), _EMA_ALPHA_16), _EPS)
    return np.clip(surprise / denom, -2.0, 2.0)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "depth_momentum":           (alpha_depth_momentum, "EMA16(dtotal)/EMA64(|dtotal|)"),
    "depth_shock":              (alpha_depth_shock, "neg-depth asym shock"),
    "depth_velocity_diff":      (alpha_depth_velocity_diff, "EMA8(dbid-dask) normalized"),
    "depth_ratio_log":          (alpha_depth_ratio_log, "EMA8(log(bq/aq))"),
    "queue_acceleration":       (alpha_queue_acceleration, "EMA4(fast_QI-slow_QI)"),
    "mean_revert_qi":           (alpha_mean_revert_qi, "-(QI-EMA64)/std mean-rev"),
    "cross_ema_qi":             (alpha_cross_ema_qi, "EMA4(QI)-EMA16(QI)"),
    "vol_of_imbalance":         (alpha_vol_of_imbalance, "QI variance (unsigned)"),
    "depth_ratio_momentum":     (alpha_depth_ratio_momentum, "fast-slow log(bq/aq)"),
    "total_depth_zscore":       (alpha_total_depth_zscore, "total depth z-score"),
    "asymmetric_depth_growth":  (alpha_asymmetric_depth_growth, "pos-delta asym"),
    "depth_persistence":        (alpha_depth_persistence, "QI[t]*QI[t-1] / QI^2"),
    "depth_curvature":          (alpha_depth_curvature, "2nd-diff total depth"),
    "bid_ask_velocity_ratio":   (alpha_bid_ask_velocity_ratio, "|dbid|/(|dbid|+|dask|)-0.5"),
    "depth_surprise":           (alpha_depth_surprise, "|dtotal-ema|*sign(QI)"),
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
    """Run all depth alphas on one symbol's L1 data.

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
    """Run depth alpha exploration across all symbols in data_dir."""
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
    print(f"DEPTH ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
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
    parser = argparse.ArgumentParser(description="Large-scale depth-dynamics alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/depth_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
