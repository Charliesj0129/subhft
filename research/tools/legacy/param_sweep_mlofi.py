"""Stage 5: Parameter sweep for mlofi_gradient (gradient-only).

Sweeps EMA_level × EMA_output on 2330 IS data, validates on OOS and 2317.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr

_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT))

# --- Parameters to sweep ---
EMA_LEVELS = [4, 8, 16, 32]
EMA_OUTPUTS = [2, 4, 8, 16, 32]
WARMUP = 64
CLIP = 2.0
CONVEXITY_WEIGHT = 0.0  # gradient-only

# Gradient weights (centered levels)
_GRAD_W = np.array([-2.0, -1.0, 0.0, 1.0, 2.0], dtype=np.float64)
_GRAD_D = 10.0

HORIZONS_NS = {
    "100ms": 100_000_000,
    "500ms": 500_000_000,
    "1s": 1_000_000_000,
    "5s": 5_000_000_000,
    "30s": 30_000_000_000,
}
LATENCY_NS = 36_000_000  # 36ms broker RTT


def _ema_alpha(span: int) -> float:
    return 1.0 - math.exp(-1.0 / span)


def _load_l5(path: str) -> np.ndarray:
    return np.load(path)


def _replay_raw_gradient(data: np.ndarray) -> np.ndarray:
    """Replay to get raw per-tick gradient (before any EMA)."""
    n = len(data)
    raw_grad = np.zeros(n, dtype=np.float64)
    prev_bid = np.zeros(5, dtype=np.float64)
    prev_ask = np.zeros(5, dtype=np.float64)

    for i in range(n):
        row = data[i]
        bid_vol = row["bids_vol"].astype(np.float64)
        ask_vol = row["asks_vol"].astype(np.float64)

        delta_bid = bid_vol - prev_bid
        delta_ask = ask_vol - prev_ask
        mlofi = delta_bid - delta_ask

        raw_grad[i] = float(np.dot(_GRAD_W, mlofi)) / _GRAD_D

        prev_bid[:] = bid_vol
        prev_ask[:] = ask_vol

    return raw_grad


def _apply_ema_chain(raw: np.ndarray, ema_level: int, ema_output: int,
                     warmup: int, clip: float) -> np.ndarray:
    """Apply double EMA (level then output) to raw gradient series."""
    n = len(raw)
    alpha_l = _ema_alpha(ema_level)
    alpha_o = _ema_alpha(ema_output)

    # First EMA: level smoothing on raw gradient
    smoothed = np.zeros(n, dtype=np.float64)
    smoothed[0] = raw[0]
    for i in range(1, n):
        smoothed[i] = smoothed[i - 1] + alpha_l * (raw[i] - smoothed[i - 1])

    # Second EMA: output smoothing
    signal = np.zeros(n, dtype=np.float64)
    signal[0] = smoothed[0]
    for i in range(1, n):
        signal[i] = signal[i - 1] + alpha_o * (smoothed[i] - signal[i - 1])

    # Warmup and clip
    signal[:warmup] = 0.0
    np.clip(signal, -clip, clip, out=signal)
    return signal


def _compute_forward_returns(data: np.ndarray, horizon_ns: int) -> np.ndarray:
    """Compute forward mid-price return at given horizon with latency offset."""
    ts = data["timestamp_ns"]
    bp = data["bids_price"][:, 0].astype(np.float64)
    ap = data["asks_price"][:, 0].astype(np.float64)
    mid = (bp + ap) * 0.5

    n = len(ts)
    ret = np.full(n, np.nan, dtype=np.float64)
    target_ts = ts + LATENCY_NS + horizon_ns

    j = 0
    for i in range(n):
        while j < n and ts[j] < target_ts[i]:
            j += 1
        if j < n:
            ret[i] = mid[j] - mid[i]
        j_save = j
        j = max(j - 1, i + 1)  # reset slightly for next
        j = j_save

    return ret


def _compute_ic(signal: np.ndarray, ret: np.ndarray) -> tuple[float, float]:
    """Rank IC and Newey-West t-stat."""
    mask = ~np.isnan(ret) & (signal != 0.0)
    s = signal[mask]
    r = ret[mask]
    if len(s) < 100:
        return 0.0, 0.0
    ic, _ = spearmanr(s, r)

    # Newey-West N_eff
    rho = np.corrcoef(s[:-1], s[1:])[0, 1]
    rho = min(max(rho, -0.999), 0.999)
    n_eff = len(s) * (1 - rho) / (1 + rho)
    n_eff = max(n_eff, 2)
    t_nw = ic * math.sqrt(n_eff) / math.sqrt(max(1 - ic * ic, 1e-10))
    return ic, t_nw


def main():
    import structlog
    log = structlog.get_logger()

    data_dir = _PROJECT / "research" / "data" / "l5_v2"

    # Load data
    log.info("loading_data")
    d2330 = _load_l5(str(data_dir / "2330_l5.npy"))
    d2317 = _load_l5(str(data_dir / "2317_l5.npy"))

    # Detect day boundaries for IS/OOS split
    def _day_indices(data):
        ts = data["timestamp_ns"]
        days = np.unique(ts // (24 * 3600 * 1_000_000_000))
        return days

    days_2330 = _day_indices(d2330)
    n_days = len(days_2330)
    is_days = int(n_days * 0.7)
    day_boundary = days_2330[is_days]
    ts_2330 = d2330["timestamp_ns"]
    is_mask_2330 = (ts_2330 // (24 * 3600 * 1_000_000_000)) < day_boundary
    oos_mask_2330 = ~is_mask_2330

    log.info("data_loaded", n_2330=len(d2330), n_2317=len(d2317),
             n_days=n_days, is_days=is_days, oos_days=n_days - is_days)

    # Pre-compute raw gradient (one replay)
    log.info("computing_raw_gradient", symbol="2330")
    raw_2330 = _replay_raw_gradient(d2330)

    # Pre-compute forward returns for 1s and 5s
    log.info("computing_forward_returns", symbol="2330")
    ret_1s = _compute_forward_returns(d2330, HORIZONS_NS["1s"])
    ret_5s = _compute_forward_returns(d2330, HORIZONS_NS["5s"])

    # Sweep
    results = []
    total = len(EMA_LEVELS) * len(EMA_OUTPUTS)
    log.info("starting_sweep", total=total)

    for i, ema_l in enumerate(EMA_LEVELS):
        for j, ema_o in enumerate(EMA_OUTPUTS):
            idx = i * len(EMA_OUTPUTS) + j + 1
            signal = _apply_ema_chain(raw_2330, ema_l, ema_o, WARMUP, CLIP)

            # IS metrics
            s_is = signal[is_mask_2330]
            r1_is = ret_1s[is_mask_2330]
            r5_is = ret_5s[is_mask_2330]
            ic1_is, t1_is = _compute_ic(s_is, r1_is)
            ic5_is, _ = _compute_ic(s_is, r5_is)

            # OOS metrics
            s_oos = signal[oos_mask_2330]
            r1_oos = ret_1s[oos_mask_2330]
            r5_oos = ret_5s[oos_mask_2330]
            ic1_oos, t1_oos = _compute_ic(s_oos, r1_oos)
            ic5_oos, _ = _compute_ic(s_oos, r5_oos)

            # Full sample
            ic1_full, t1_full = _compute_ic(signal, ret_1s)
            ic5_full, _ = _compute_ic(signal, ret_5s)

            # Autocorrelation
            valid = signal[WARMUP:]
            if len(valid) > 2:
                rho = float(np.corrcoef(valid[:-1], valid[1:])[0, 1])
            else:
                rho = 0.0

            results.append({
                "ema_level": ema_l,
                "ema_output": ema_o,
                "ic1_is": round(ic1_is, 5),
                "ic5_is": round(ic5_is, 5),
                "ic1_oos": round(ic1_oos, 5),
                "ic5_oos": round(ic5_oos, 5),
                "ic1_full": round(ic1_full, 5),
                "ic5_full": round(ic5_full, 5),
                "t_nw_1s_is": round(t1_is, 2),
                "t_nw_1s_oos": round(t1_oos, 2),
                "t_nw_1s_full": round(t1_full, 2),
                "autocorr": round(rho, 4),
            })

            if idx % 5 == 0:
                log.info("sweep_progress", done=idx, total=total,
                         last=f"L{ema_l}_O{ema_o}", ic1_is=round(ic1_is, 4))

    # Sort by IS IC_1s (most negative = best)
    results.sort(key=lambda r: r["ic1_is"])

    # Top 3 on 2317 cross-validation
    log.info("cross_validating_2317")
    raw_2317 = _replay_raw_gradient(d2317)
    ret_1s_2317 = _compute_forward_returns(d2317, HORIZONS_NS["1s"])

    for r in results[:3]:
        sig_2317 = _apply_ema_chain(raw_2317, r["ema_level"], r["ema_output"], WARMUP, CLIP)
        ic_2317, t_2317 = _compute_ic(sig_2317, ret_1s_2317)
        r["ic1_2317"] = round(ic_2317, 5)
        r["t_nw_2317"] = round(t_2317, 2)

    # Neighborhood robustness for #1
    best = results[0]
    log.info("neighborhood_check", best_ema_l=best["ema_level"], best_ema_o=best["ema_output"])
    neighbors = []
    for dl in [-1, 0, 1]:
        for do in [-1, 0, 1]:
            if dl == 0 and do == 0:
                continue
            li = EMA_LEVELS.index(best["ema_level"]) + dl
            oi = EMA_OUTPUTS.index(best["ema_output"]) + do
            if 0 <= li < len(EMA_LEVELS) and 0 <= oi < len(EMA_OUTPUTS):
                nl = EMA_LEVELS[li]
                no = EMA_OUTPUTS[oi]
                # Find in results
                for r in results:
                    if r["ema_level"] == nl and r["ema_output"] == no:
                        neighbors.append({
                            "ema_level": nl, "ema_output": no,
                            "ic1_is": r["ic1_is"],
                            "ratio_vs_best": round(r["ic1_is"] / best["ic1_is"], 3) if best["ic1_is"] != 0 else 0,
                        })
                        break

    # Write results
    out_dir = _PROJECT / "outputs" / "team_artifacts" / "alpha-research"
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(out_dir / "stage5_param_optimization_data.json", "w") as f:
        json.dump({"grid_results": results, "neighbors": neighbors}, f, indent=2)

    # Print summary
    print("\n" + "=" * 80)
    print("STAGE 5: PARAMETER OPTIMIZATION RESULTS")
    print("=" * 80)
    print(f"\nGrid: {len(results)} combinations (EMA_level × EMA_output)")
    print(f"IS: {is_days} days, OOS: {n_days - is_days} days\n")

    print("Top 10 by IS IC_1s:")
    print(f"{'Rank':>4} {'EMA_L':>5} {'EMA_O':>5} {'IC1_IS':>8} {'IC5_IS':>8} {'IC1_OOS':>8} {'IC5_OOS':>8} {'t_NW_IS':>8} {'rho':>6}")
    for i, r in enumerate(results[:10]):
        print(f"{i+1:4d} {r['ema_level']:5d} {r['ema_output']:5d} "
              f"{r['ic1_is']:8.4f} {r['ic5_is']:8.4f} {r['ic1_oos']:8.4f} {r['ic5_oos']:8.4f} "
              f"{r['t_nw_1s_is']:8.2f} {r['autocorr']:6.4f}")

    print(f"\nBest: EMA_level={best['ema_level']}, EMA_output={best['ema_output']}")
    print(f"  IS  IC_1s={best['ic1_is']:.4f}, IC_5s={best['ic5_is']:.4f}")
    print(f"  OOS IC_1s={best['ic1_oos']:.4f}, IC_5s={best['ic5_oos']:.4f}")
    if "ic1_2317" in best:
        print(f"  2317 IC_1s={best['ic1_2317']:.4f}, t_NW={best['t_nw_2317']:.2f}")

    print(f"\nNeighborhood robustness (IC_1s ratio vs best):")
    for nb in neighbors:
        print(f"  L{nb['ema_level']}_O{nb['ema_output']}: {nb['ic1_is']:.4f} ({nb['ratio_vs_best']:.1%})")

    # Current default comparison
    for r in results:
        if r["ema_level"] == 8 and r["ema_output"] == 8:
            print(f"\nCurrent default (L8_O8): IC1_IS={r['ic1_is']:.4f}, IC1_OOS={r['ic1_oos']:.4f}")
            break

    log.info("sweep_complete", total=len(results))


if __name__ == "__main__":
    main()
