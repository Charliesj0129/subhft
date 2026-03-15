"""Large-scale volatility & regime alpha exploration on real L1 data.

Vectorized computation of 15 volatility/regime alpha signals across all symbols,
with forward-return IC, autocorrelation, and cross-symbol consistency metrics.

Usage::

    python research/tools/volatility_regime_explorer.py \
        --data-dir research/data/raw \
        --out research/results/volatility_regime_exploration.json
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

logger = get_logger("volatility_regime_explorer")

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
# Vectorized volatility/regime alpha formulas
# ---------------------------------------------------------------------------

def alpha_realized_vol_fast(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA8(d_mid^2), fast realized vol proxy."""
    return _ema(d_mid ** 2, _EMA_ALPHA_8)


def alpha_realized_vol_slow(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA64(d_mid^2), slow realized vol proxy."""
    return _ema(d_mid ** 2, _EMA_ALPHA_64)


def alpha_vol_ratio(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA8(d_mid^2) / max(EMA64(d_mid^2), eps), regime indicator."""
    fast = _ema(d_mid ** 2, _EMA_ALPHA_8)
    slow = _ema(d_mid ** 2, _EMA_ALPHA_64)
    return fast / np.maximum(slow, _EPS)


def alpha_vol_of_vol(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA16((fast_vol - EMA16(fast_vol))^2) where fast_vol = EMA8(d_mid^2)."""
    fast_vol = _ema(d_mid ** 2, _EMA_ALPHA_8)
    fast_vol_mean = _ema(fast_vol, _EMA_ALPHA_16)
    return _ema((fast_vol - fast_vol_mean) ** 2, _EMA_ALPHA_16)


def alpha_garch_proxy(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """GARCH(1,1) proxy: sigma2_t = 0.05*mean(d_mid^2) + 0.1*d_mid^2 + 0.85*sigma2_{t-1}."""
    d_mid_sq = d_mid ** 2
    omega = 0.05 * float(np.mean(d_mid_sq))
    # sigma2_t = omega + 0.1*d_mid^2 + 0.85*sigma2_{t-1}
    # This is an IIR filter: sigma2 = lfilter([omega, 0.1], [1, -0.85], ones_and_d_mid_sq)
    # Rewrite: sigma2_t - 0.85*sigma2_{t-1} = omega + 0.1*d_mid_sq_t
    # y_t = omega*1 + 0.1*x_t + 0.85*y_{t-1}
    # Using lfilter on d_mid_sq: b=[0.1], a=[1, -0.85], then add omega/(1-0.85)=omega/0.15 as DC offset
    # More precisely, just filter with the constant baked in:
    n = len(d_mid_sq)
    inp = omega * np.ones(n, dtype=np.float64) + 0.1 * d_mid_sq
    b_coeff = np.array([1.0], dtype=np.float64)
    a_coeff = np.array([1.0, -0.85], dtype=np.float64)
    zi = np.array([inp[0] / (1.0 - 0.85)], dtype=np.float64) * (1.0 - 0.85)
    out, _ = lfilter(b_coeff, a_coeff, inp, zi=zi)
    return np.asarray(out, dtype=np.float64)


def alpha_kl_regime_proxy(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """log(max(EMA8(d_mid^2), eps) / max(EMA64(d_mid^2), eps))."""
    fast = _ema(d_mid ** 2, _EMA_ALPHA_8)
    slow = _ema(d_mid ** 2, _EMA_ALPHA_64)
    return np.log(np.maximum(fast, _EPS) / np.maximum(slow, _EPS))


def alpha_vol_momentum(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA4(EMA8(d_mid^2)) - EMA32(EMA8(d_mid^2)), vol trend."""
    fast_vol = _ema(d_mid ** 2, _EMA_ALPHA_8)
    return _ema(fast_vol, _EMA_ALPHA_4) - _ema(fast_vol, _EMA_ALPHA_32)


def alpha_vol_breakout(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """clip((fast_vol - EMA64(fast_vol)) / max(std_of_vol, eps), -3, 3)."""
    fast_vol = _ema(d_mid ** 2, _EMA_ALPHA_8)
    slow_mean = _ema(fast_vol, _EMA_ALPHA_64)
    slow_sq = _ema(fast_vol ** 2, _EMA_ALPHA_64)
    std_vol = np.sqrt(np.maximum(slow_sq - slow_mean ** 2, 0.0))
    return np.clip((fast_vol - slow_mean) / np.maximum(std_vol, _EPS), -3.0, 3.0)


def alpha_depth_vol_coupling(
    bid_qty: np.ndarray, ask_qty: np.ndarray, d_mid: np.ndarray, **_: Any
) -> np.ndarray:
    """EMA16(|diff(depth)| * |d_mid|) / max(EMA16(|diff(depth)|) * EMA16(|d_mid|), eps)."""
    depth = bid_qty + ask_qty
    d_depth = np.abs(np.diff(depth, prepend=depth[0]))
    abs_d_mid = np.abs(d_mid)
    numerator = _ema(d_depth * abs_d_mid, _EMA_ALPHA_16)
    denominator = _ema(d_depth, _EMA_ALPHA_16) * _ema(abs_d_mid, _EMA_ALPHA_16)
    return numerator / np.maximum(denominator, _EPS)


def alpha_vol_signed_by_qi(
    bid_qty: np.ndarray, ask_qty: np.ndarray, d_mid: np.ndarray, **_: Any
) -> np.ndarray:
    """clip(vol_ratio * sign(QI), -3, 3)."""
    fast = _ema(d_mid ** 2, _EMA_ALPHA_8)
    slow = _ema(d_mid ** 2, _EMA_ALPHA_64)
    vol_ratio = fast / np.maximum(slow, _EPS)
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return np.clip(vol_ratio * np.sign(qi), -3.0, 3.0)


def alpha_vol_asymmetry(d_mid: np.ndarray, **_: Any) -> np.ndarray:
    """EMA16(max(d_mid,0)^2) - EMA16(max(-d_mid,0)^2), up vs down vol."""
    up_sq = np.maximum(d_mid, 0.0) ** 2
    dn_sq = np.maximum(-d_mid, 0.0) ** 2
    return _ema(up_sq, _EMA_ALPHA_16) - _ema(dn_sq, _EMA_ALPHA_16)


def alpha_vol_mean_revert(
    bid_qty: np.ndarray, ask_qty: np.ndarray, d_mid: np.ndarray, **_: Any
) -> np.ndarray:
    """clip((EMA8(d_mid^2) - EMA64(d_mid^2)) * sign(QI), -2, 2)."""
    fast = _ema(d_mid ** 2, _EMA_ALPHA_8)
    slow = _ema(d_mid ** 2, _EMA_ALPHA_64)
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return np.clip((fast - slow) * np.sign(qi), -2.0, 2.0)


def alpha_spread_vol_regime(
    spread: np.ndarray, d_mid: np.ndarray, **_: Any
) -> np.ndarray:
    """EMA16(spread * |d_mid|) / max(EMA64(spread * |d_mid|), eps)."""
    product = spread * np.abs(d_mid)
    fast = _ema(product, _EMA_ALPHA_16)
    slow = _ema(product, _EMA_ALPHA_64)
    return fast / np.maximum(slow, _EPS)


def alpha_inter_arrival_proxy(local_ts: np.ndarray, **_: Any) -> np.ndarray:
    """EMA16(d_ts^2) / max(EMA64(d_ts^2), eps) where d_ts = diff(local_ts)."""
    d_ts = np.diff(local_ts.astype(np.float64), prepend=local_ts[0]).astype(np.float64)
    d_ts_sq = d_ts ** 2
    fast = _ema(d_ts_sq, _EMA_ALPHA_16)
    slow = _ema(d_ts_sq, _EMA_ALPHA_64)
    return fast / np.maximum(slow, _EPS)


def alpha_vol_clustering(
    bid_qty: np.ndarray, ask_qty: np.ndarray, d_mid: np.ndarray, **_: Any
) -> np.ndarray:
    """EMA4((|d_mid| > 2*sqrt(EMA64(d_mid^2))).astype(float)) * sign(QI)."""
    slow_var = _ema(d_mid ** 2, _EMA_ALPHA_64)
    threshold = 2.0 * np.sqrt(np.maximum(slow_var, 0.0))
    extreme = (np.abs(d_mid) > threshold).astype(np.float64)
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return _ema(extreme, _EMA_ALPHA_4) * np.sign(qi)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "realized_vol_fast":      (alpha_realized_vol_fast, "EMA8(d_mid^2)"),
    "realized_vol_slow":      (alpha_realized_vol_slow, "EMA64(d_mid^2)"),
    "vol_ratio":              (alpha_vol_ratio, "fast/slow vol regime"),
    "vol_of_vol":             (alpha_vol_of_vol, "vol of vol (fast var)"),
    "garch_proxy":            (alpha_garch_proxy, "GARCH(1,1) approx"),
    "kl_regime_proxy":        (alpha_kl_regime_proxy, "log(fast_vol/slow_vol)"),
    "vol_momentum":           (alpha_vol_momentum, "EMA4-EMA32 of fast_vol"),
    "vol_breakout":           (alpha_vol_breakout, "z-score of fast_vol"),
    "depth_vol_coupling":     (alpha_depth_vol_coupling, "|d_depth|*|d_mid| coupling"),
    "vol_signed_by_qi":       (alpha_vol_signed_by_qi, "vol_ratio * sign(QI)"),
    "vol_asymmetry":          (alpha_vol_asymmetry, "up vs down vol"),
    "vol_mean_revert":        (alpha_vol_mean_revert, "(fast-slow vol)*sign(QI)"),
    "spread_vol_regime":      (alpha_spread_vol_regime, "spread*|d_mid| fast/slow"),
    "inter_arrival_proxy":    (alpha_inter_arrival_proxy, "d_ts^2 fast/slow"),
    "vol_clustering":         (alpha_vol_clustering, "extreme |d_mid| * sign(QI)"),
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
    """Run all volatility/regime alphas on one symbol's L1 data.

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
    local_ts = data["local_ts"]
    d_mid = np.diff(mid, prepend=mid[0])

    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(
                bid_qty=bid_qty,
                ask_qty=ask_qty,
                spread=spread,
                mid=mid,
                d_mid=d_mid,
                local_ts=local_ts,
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
    """Run volatility/regime alpha exploration across all symbols in data_dir."""
    base = Path(data_dir)
    if horizons is None:
        horizons = [50, 200, 1000, 5000]

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
        # Extract row count from results to avoid re-reading the file
        first_alpha = next(iter(sym_results.values()), None)
        n_rows = first_alpha["n_rows"] if first_alpha else 0
        logger.info("explored", symbol=sym, rows=n_rows, alphas=len(sym_results), elapsed_s=f"{elapsed:.1f}")

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
            hits = []
            for sym, sym_res in per_symbol.items():
                if alpha_id in sym_res and h_key in sym_res[alpha_id].get("horizons", {}):
                    m = sym_res[alpha_id]["horizons"][h_key]
                    ics.append(m["ic_mean"])
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
                sym_keys = [s for s in per_symbol if alpha_id in per_symbol[s]]
                agg[f"h{h}_best_sym"] = sym_keys[best_idx] if best_idx < len(sym_keys) else ""
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
    print(f"VOLATILITY/REGIME ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
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
    parser = argparse.ArgumentParser(description="Large-scale volatility/regime alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/volatility_regime_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
