"""Large-scale spread & microprice alpha exploration on real L1 data.

Vectorized computation of 15 spread/microprice alpha signals across all symbols,
with forward-return IC, autocorrelation, and cross-symbol consistency metrics.

Usage::

    python research/tools/spread_microprice_explorer.py \
        --data-dir research/data/raw \
        --out research/results/spread_microprice_exploration.json
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

logger = get_logger("spread_microprice_explorer")

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
# Shared helpers
# ---------------------------------------------------------------------------

def _microprice(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
) -> np.ndarray:
    """Compute microprice from L1 bid/ask prices and quantities."""
    total_qty = bid_qty + ask_qty + _EPS
    return (bid_px * ask_qty + ask_px * bid_qty) / total_qty


# ---------------------------------------------------------------------------
# Vectorized spread/microprice alpha formulas
# ---------------------------------------------------------------------------

def alpha_spread_mean_revert(
    spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread mean-reversion: z-score of spread deviation from EMA64."""
    ema64 = _ema(spread, _EMA_ALPHA_64)
    ema64_sq = _ema(spread ** 2, _EMA_ALPHA_64)
    variance = ema64_sq - ema64 ** 2
    std = np.sqrt(np.maximum(variance, 0.0))
    raw = -(spread - ema64) / np.maximum(std, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_spread_recovery(
    spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread recovery speed: EMA16 of normalized negative spread changes."""
    d_spread = np.diff(spread, prepend=spread[0])
    abs_d_ema = _ema(np.abs(d_spread), _EMA_ALPHA_32)
    raw = -d_spread / np.maximum(abs_d_ema, _EPS)
    return np.clip(_ema(raw, _EMA_ALPHA_16), -2.0, 2.0)


def alpha_microprice_raw(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Microprice adjustment: microprice - mid (unnormalized)."""
    microprice = _microprice(bid_px, ask_px, bid_qty, ask_qty)
    return microprice - mid


def alpha_microprice_momentum(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    mid: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Microprice momentum: EMA8 of normalized microprice changes."""
    microprice = _microprice(bid_px, ask_px, bid_qty, ask_qty)
    d_micro = np.diff(microprice, prepend=microprice[0])
    spread_price = spread / 10000.0 * mid
    raw = d_micro / np.maximum(spread_price, _EPS)
    return np.clip(_ema(raw, _EMA_ALPHA_8), -2.0, 2.0)


def alpha_microprice_reversion(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    **_: Any,
) -> np.ndarray:
    """Microprice reversion from EMA32, normalized by volatility."""
    microprice = _microprice(bid_px, ask_px, bid_qty, ask_qty)
    ema32 = _ema(microprice, _EMA_ALPHA_32)
    d_micro = np.diff(microprice, prepend=microprice[0])
    vol = _ema(np.abs(d_micro), _EMA_ALPHA_16)
    raw = -(microprice - ema32) / np.maximum(vol, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_price_level_revert(
    mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Mid-price mean-reversion from EMA64, normalized by tick volatility."""
    ema64 = _ema(mid, _EMA_ALPHA_64)
    d_mid = np.diff(mid, prepend=mid[0])
    vol = _ema(np.abs(d_mid), _EMA_ALPHA_16)
    raw = -(mid - ema64) / np.maximum(vol, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_tick_pressure(
    bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Tick direction weighted by total depth relative to EMA64 depth."""
    d_mid = np.diff(mid, prepend=mid[0])
    total_qty = bid_qty + ask_qty
    ema64_qty = _ema(total_qty, _EMA_ALPHA_64)
    raw = np.sign(d_mid) * total_qty / np.maximum(ema64_qty, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_spread_vol(
    spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread variance: EMA16(spread^2) - EMA16(spread)^2."""
    ema16_sq = _ema(spread ** 2, _EMA_ALPHA_16)
    ema16 = _ema(spread, _EMA_ALPHA_16)
    return ema16_sq - ema16 ** 2


def alpha_spread_regime(
    spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread regime indicator: current spread / EMA64 spread."""
    ema64 = _ema(spread, _EMA_ALPHA_64)
    return spread / np.maximum(ema64, _EPS)


def alpha_microprice_spread_ratio(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    mid: np.ndarray, **_: Any,
) -> np.ndarray:
    """Microprice deviation as fraction of bid-ask spread."""
    microprice = _microprice(bid_px, ask_px, bid_qty, ask_qty)
    raw_spread = ask_px - bid_px
    raw = (microprice - mid) / np.maximum(raw_spread, _EPS)
    return np.clip(raw, -1.0, 1.0)


def alpha_bid_ask_drift(
    mid: np.ndarray, bid_px: np.ndarray, ask_px: np.ndarray, **_: Any,
) -> np.ndarray:
    """EMA8 of mid drift normalized by raw spread."""
    d_mid = np.diff(mid, prepend=mid[0])
    raw_spread = ask_px - bid_px
    raw = _ema(d_mid, _EMA_ALPHA_8) / np.maximum(raw_spread, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_spread_qi_divergence(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Coupling between spread changes and queue imbalance direction."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_spread = np.diff(spread, prepend=spread[0])
    abs_d_spread_ema = _ema(np.abs(d_spread), _EMA_ALPHA_32)
    raw = _ema(d_spread * qi, _EMA_ALPHA_8) / np.maximum(abs_d_spread_ema, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_microprice_acceleration(
    bid_px: np.ndarray, ask_px: np.ndarray,
    bid_qty: np.ndarray, ask_qty: np.ndarray,
    **_: Any,
) -> np.ndarray:
    """Microprice acceleration: fast EMA minus slow EMA of microprice changes."""
    microprice = _microprice(bid_px, ask_px, bid_qty, ask_qty)
    d_micro = np.diff(microprice, prepend=microprice[0])
    fast = _ema(d_micro, _EMA_ALPHA_4)
    slow = _ema(d_micro, _EMA_ALPHA_16)
    return np.clip(fast - slow, -1.0, 1.0)


def alpha_spread_compression_signal(
    bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Spread compression gated by absolute queue imbalance."""
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    d_spread = np.diff(spread, prepend=spread[0])
    compress = np.maximum(-d_spread, 0.0) * np.abs(qi)
    abs_d_spread_ema = _ema(np.abs(d_spread), _EMA_ALPHA_32)
    raw = _ema(compress, _EMA_ALPHA_8) / np.maximum(abs_d_spread_ema, _EPS)
    return np.clip(raw, -2.0, 2.0)


def alpha_price_reversal_spread(
    mid: np.ndarray, spread: np.ndarray, **_: Any,
) -> np.ndarray:
    """Prior tick reversal weighted by spread excess."""
    d_mid = np.diff(mid, prepend=mid[0])
    ema64_spread = _ema(spread, _EMA_ALPHA_64)
    spread_excess = spread / np.maximum(ema64_spread, 1.0) - 1.0
    # Use np.roll for shift(1) — prior tick's sign and spread excess
    prior_sign = np.roll(np.sign(d_mid), 1)
    prior_spread_excess = np.roll(spread_excess, 1)
    prior_sign[0] = 0.0
    prior_spread_excess[0] = 0.0
    raw = -prior_sign * prior_spread_excess
    return np.clip(_ema(raw, _EMA_ALPHA_8), -1.0, 1.0)


# ---------------------------------------------------------------------------
# Alpha registry
# ---------------------------------------------------------------------------
ALPHA_REGISTRY: dict[str, tuple[callable, str]] = {
    "spread_mean_revert":          (alpha_spread_mean_revert, "spread z-score reversion"),
    "spread_recovery":             (alpha_spread_recovery, "normalized spread recovery speed"),
    "microprice_raw":              (alpha_microprice_raw, "microprice - mid (unnorm)"),
    "microprice_momentum":         (alpha_microprice_momentum, "EMA8 norm microprice diff"),
    "microprice_reversion":        (alpha_microprice_reversion, "microprice revert from EMA32"),
    "price_level_revert":          (alpha_price_level_revert, "mid reversion from EMA64"),
    "tick_pressure":               (alpha_tick_pressure, "sign(dmid) x depth/ema64_depth"),
    "spread_vol":                  (alpha_spread_vol, "spread variance (unsigned)"),
    "spread_regime":               (alpha_spread_regime, "spread/ema64 regime indicator"),
    "microprice_spread_ratio":     (alpha_microprice_spread_ratio, "(micro-mid)/raw_spread"),
    "bid_ask_drift":               (alpha_bid_ask_drift, "ema8(dmid)/raw_spread"),
    "spread_qi_divergence":        (alpha_spread_qi_divergence, "dspread x QI coupling"),
    "microprice_acceleration":     (alpha_microprice_acceleration, "fast-slow microprice diff"),
    "spread_compression_signal":   (alpha_spread_compression_signal, "compress x |QI| gated"),
    "price_reversal_spread":       (alpha_price_reversal_spread, "prior-tick reversal x spread"),
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
    """Run all spread/microprice alphas on one symbol's L1 data.

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

    # Forward returns
    fwd_rets = _compute_forward_returns(mid, horizons)

    results: dict[str, Any] = {}
    for alpha_id, (alpha_fn, desc) in ALPHA_REGISTRY.items():
        try:
            signal = alpha_fn(
                bid_qty=bid_qty, ask_qty=ask_qty,
                spread=spread, mid=mid,
                bid_px=bid_px, ask_px=ask_px,
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
    """Run spread/microprice alpha exploration across all symbols in data_dir."""
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
        # Extract row count from first alpha result to avoid re-loading the file
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
    print(f"SPREAD/MICROPRICE ALPHA LEADERBOARD — {output['total_symbols']} symbols, {output['total_alphas']} alphas")
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

        print(f" {entry['alpha_id']:<29} {ic:>+10.5f} {ir:>8.2f} {hit*100:>6.1f}% "
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
    parser = argparse.ArgumentParser(description="Large-scale spread/microprice alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw", help="Directory with per-symbol L1 data")
    parser.add_argument("--out", default="research/results/spread_microprice_exploration.json", help="Output JSON")
    parser.add_argument("--horizons", default="50,200,1000,5000", help="Forward return horizons (ticks)")
    args = parser.parse_args()

    horizons = [int(h) for h in args.horizons.split(",")]
    output = run_exploration(data_dir=args.data_dir, horizons=horizons, out_path=args.out)
    print_leaderboard(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
