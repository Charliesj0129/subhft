"""Batch alpha signal evaluation on real data.

Runs multiple alphas tick-by-tick on golden data, computes IC/Sharpe/stats,
and outputs a comparison table. This is a quick pre-Gate-C screen.

Usage:
    python -m research.tools.batch_alpha_eval \
        --data research/data/processed/queue_imbalance/queue_imbalance_goldendata_20260304_research.npz \
        --out outputs/alpha_eval_results.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Data enrichment: derive fields from raw golden data
# ---------------------------------------------------------------------------

def enrich_data(data: np.ndarray) -> dict[str, np.ndarray]:
    """Derive all fields needed by alpha candidates from raw golden data.

    Input fields: bid_qty, ask_qty, bid_px, ask_px, mid_price, spread_bps, volume, local_ts
    """
    n = len(data)
    bid_qty = data["bid_qty"].astype(np.float64)
    ask_qty = data["ask_qty"].astype(np.float64)
    bid_px = data["bid_px"].astype(np.float64)
    ask_px = data["ask_px"].astype(np.float64)
    mid_price = data["mid_price"].astype(np.float64)
    volume = data["volume"].astype(np.float64)

    total_qty = bid_qty + ask_qty
    safe_total = np.where(total_qty > 0, total_qty, 1.0)

    # microprice_x2: volume-weighted microprice × 2 (scaled int convention)
    microprice = np.where(
        total_qty > 0,
        (bid_px * ask_qty + ask_px * bid_qty) / safe_total,
        mid_price,
    )
    microprice_x2 = (microprice * 2.0).astype(np.float64)

    # spread_scaled: (ask - bid) × 10000
    spread_scaled = ((ask_px - bid_px) * 10000.0).astype(np.float64)

    # mid_price_x2
    mid_price_x2 = (mid_price * 2.0).astype(np.float64)

    # OFI L1 raw: tick-by-tick order flow imbalance
    ofi_l1_raw = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        # Bid flow
        if bid_px[i] > bid_px[i - 1]:
            b_flow = bid_qty[i]
        elif bid_px[i] == bid_px[i - 1]:
            b_flow = bid_qty[i] - bid_qty[i - 1]
        else:
            b_flow = -bid_qty[i - 1]
        # Ask flow
        if ask_px[i] > ask_px[i - 1]:
            a_flow = -ask_qty[i - 1]
        elif ask_px[i] == ask_px[i - 1]:
            a_flow = ask_qty[i] - ask_qty[i - 1]
        else:
            a_flow = ask_qty[i]
        ofi_l1_raw[i] = b_flow - a_flow

    # Imbalance PPM
    l1_imbalance_ppm = np.where(
        total_qty > 0,
        ((bid_qty - ask_qty) / safe_total * 1_000_000).astype(np.int64),
        0,
    ).astype(np.float64)

    return {
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "bid_px": bid_px,
        "ask_px": ask_px,
        "mid_price": mid_price,
        "volume": volume,
        "microprice_x2": microprice_x2,
        "spread_scaled": spread_scaled,
        "mid_price_x2": mid_price_x2,
        "l1_bid_qty": bid_qty,  # alias
        "l1_ask_qty": ask_qty,  # alias
        "ofi_l1_raw": ofi_l1_raw,
        "l1_imbalance_ppm": l1_imbalance_ppm,
        "trade_vol": volume,  # alias
        "current_mid": mid_price,  # alias
    }


# ---------------------------------------------------------------------------
# Signal quality metrics
# ---------------------------------------------------------------------------

def compute_forward_returns(mid_price: np.ndarray, horizons: tuple[int, ...] = (1, 5, 10, 20)) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons."""
    result = {}
    for h in horizons:
        fwd = np.zeros_like(mid_price)
        fwd[:-h] = mid_price[h:] - mid_price[:-h]
        fwd[-h:] = np.nan
        result[h] = fwd
    return result


def information_coefficient(signal: np.ndarray, fwd_ret: np.ndarray) -> float:
    """Rank IC: Spearman correlation between signal and forward returns."""
    mask = ~np.isnan(signal) & ~np.isnan(fwd_ret) & (signal != 0.0)
    if mask.sum() < 100:
        return 0.0
    from scipy.stats import spearmanr
    ic, _ = spearmanr(signal[mask], fwd_ret[mask])
    return float(ic) if not np.isnan(ic) else 0.0


def signal_sharpe(signal: np.ndarray, fwd_ret: np.ndarray, annualize: float = np.sqrt(252 * 5 * 3600 / 2)) -> float:
    """Sharpe of a simple sign(signal) × fwd_return strategy."""
    mask = ~np.isnan(signal) & ~np.isnan(fwd_ret) & (signal != 0.0)
    if mask.sum() < 100:
        return 0.0
    positions = np.sign(signal[mask])
    returns = positions * fwd_ret[mask]
    if returns.std() < 1e-12:
        return 0.0
    return float(returns.mean() / returns.std() * annualize)


def signal_stats(signal: np.ndarray) -> dict:
    """Basic signal statistics."""
    valid = signal[~np.isnan(signal)]
    if len(valid) == 0:
        return {"mean": 0, "std": 0, "nonzero_pct": 0, "autocorr_1": 0}
    nonzero = (valid != 0.0).sum() / len(valid) * 100
    # Lag-1 autocorrelation
    if len(valid) > 1 and valid.std() > 1e-12:
        ac1 = float(np.corrcoef(valid[:-1], valid[1:])[0, 1])
    else:
        ac1 = 0.0
    return {
        "mean": float(valid.mean()),
        "std": float(valid.std()),
        "nonzero_pct": float(nonzero),
        "autocorr_1": ac1 if not np.isnan(ac1) else 0.0,
    }


def oos_split_eval(signal: np.ndarray, fwd_ret: np.ndarray, oos_ratio: float = 0.3) -> dict:
    """Compute IS and OOS metrics."""
    n = len(signal)
    split = int(n * (1 - oos_ratio))
    return {
        "ic_is": information_coefficient(signal[:split], fwd_ret[:split]),
        "ic_oos": information_coefficient(signal[split:], fwd_ret[split:]),
        "sharpe_is": signal_sharpe(signal[:split], fwd_ret[:split]),
        "sharpe_oos": signal_sharpe(signal[split:], fwd_ret[split:]),
    }


# ---------------------------------------------------------------------------
# Alpha runners
# ---------------------------------------------------------------------------

def run_depth_momentum(fields: dict, n: int) -> np.ndarray:
    from research.alphas.depth_momentum.impl import DepthMomentumAlpha
    alpha = DepthMomentumAlpha()
    out = np.zeros(n, dtype=np.float64)
    bq, aq = fields["bid_qty"], fields["ask_qty"]
    for i in range(n):
        out[i] = alpha.update(bq[i], aq[i])
    return out


def run_ofi_mc(fields: dict, n: int) -> np.ndarray:
    from research.alphas.ofi_mc.impl import OFIMCAlpha
    alpha = OFIMCAlpha()
    out = np.zeros(n, dtype=np.float64)
    bp, bq = fields["bid_px"], fields["bid_qty"]
    ap, aq = fields["ask_px"], fields["ask_qty"]
    tv, cm = fields["trade_vol"], fields["current_mid"]
    for i in range(n):
        out[i] = alpha.update(bp[i], bq[i], ap[i], aq[i], tv[i], cm[i])
    return out


def run_microprice_momentum(fields: dict, n: int) -> np.ndarray:
    from research.alphas.microprice_momentum.impl import MicropriceMomentumAlpha
    alpha = MicropriceMomentumAlpha()
    out = np.zeros(n, dtype=np.float64)
    mx2, ss = fields["microprice_x2"], fields["spread_scaled"]
    for i in range(n):
        out[i] = alpha.update(mx2[i], ss[i])
    return out


def run_tick_pressure(fields: dict, n: int) -> np.ndarray:
    from research.alphas.tick_pressure.impl import TickPressureAlpha
    alpha = TickPressureAlpha()
    out = np.zeros(n, dtype=np.float64)
    mx2, bq, aq = fields["mid_price_x2"], fields["l1_bid_qty"], fields["l1_ask_qty"]
    for i in range(n):
        out[i] = alpha.update(mx2[i], bq[i], aq[i])
    return out


def run_flow_toxicity(fields: dict, n: int) -> np.ndarray:
    from research.alphas.flow_toxicity_ratio.impl import FlowToxicityRatioAlpha
    alpha = FlowToxicityRatioAlpha()
    out = np.zeros(n, dtype=np.float64)
    ofi, bq, aq = fields["ofi_l1_raw"], fields["l1_bid_qty"], fields["l1_ask_qty"]
    for i in range(n):
        out[i] = alpha.update(ofi[i], bq[i], aq[i])
    return out


def run_queue_imbalance(fields: dict, n: int) -> np.ndarray:
    """Re-run the already-validated queue_imbalance for baseline comparison."""
    from research.alphas.queue_imbalance.impl import QueueImbalanceAlpha
    alpha = QueueImbalanceAlpha()
    out = np.zeros(n, dtype=np.float64)
    bq, aq = fields["bid_qty"], fields["ask_qty"]
    for i in range(n):
        out[i] = alpha.update(bq[i], aq[i])
    return out


def run_ofi_regime(fields: dict, n: int) -> np.ndarray:
    """Re-run ofi_regime for comparison."""
    from research.alphas.ofi_regime.impl import OfiRegimeAlpha
    alpha = OfiRegimeAlpha()
    out = np.zeros(n, dtype=np.float64)
    bq, aq = fields["bid_qty"], fields["ask_qty"]
    for i in range(n):
        out[i] = alpha.update(bq[i], aq[i])
    return out


ALPHA_RUNNERS = {
    "queue_imbalance": run_queue_imbalance,   # baseline (GATE_D/E)
    "ofi_regime": run_ofi_regime,             # Gate C pass
    "depth_momentum": run_depth_momentum,     # DRAFT
    "ofi_mc": run_ofi_mc,                     # GATE_B
    "microprice_momentum": run_microprice_momentum,  # DRAFT
    "tick_pressure": run_tick_pressure,        # DRAFT
    "flow_toxicity": run_flow_toxicity,        # DRAFT
}


# ---------------------------------------------------------------------------
# Main evaluation
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Batch alpha signal evaluation")
    parser.add_argument("--data", required=True, help="NPZ data path")
    parser.add_argument("--out", default=None, help="JSON output path")
    parser.add_argument("--horizons", default="1,5,10,20", help="Forward return horizons (ticks)")
    parser.add_argument("--oos-ratio", default=0.3, type=float, help="OOS split ratio")
    args = parser.parse_args()

    # Load data
    print(f"Loading {args.data}...")
    raw = np.load(args.data)
    data = raw["data"] if "data" in raw else raw[list(raw.keys())[0]]
    n = len(data)
    print(f"  {n:,} ticks, fields: {list(data.dtype.names)}")

    # Enrich
    print("Enriching data with derived fields...")
    fields = enrich_data(data)

    # Forward returns
    horizons = tuple(int(x) for x in args.horizons.split(","))
    print(f"Computing forward returns at horizons {horizons}...")
    fwd_rets = compute_forward_returns(fields["mid_price"], horizons)

    # Run all alphas
    results = {}
    for alpha_id, runner in ALPHA_RUNNERS.items():
        print(f"\n{'='*60}")
        print(f"Running: {alpha_id}")
        try:
            signal = runner(fields, n)
            stats = signal_stats(signal)
            print(f"  Signal stats: mean={stats['mean']:.6f}, std={stats['std']:.6f}, "
                  f"nonzero={stats['nonzero_pct']:.1f}%, AC1={stats['autocorr_1']:.3f}")

            # IC and Sharpe at each horizon
            horizon_results = {}
            for h in horizons:
                ic = information_coefficient(signal, fwd_rets[h])
                sharpe = signal_sharpe(signal, fwd_rets[h])
                horizon_results[f"h{h}"] = {"ic": round(ic, 4), "sharpe": round(sharpe, 2)}
                print(f"  h={h:3d}: IC={ic:+.4f}  Sharpe={sharpe:+.1f}")

            # OOS split at h=5
            oos = oos_split_eval(signal, fwd_rets[5], args.oos_ratio)
            print(f"  OOS split (h=5): IC_IS={oos['ic_is']:+.4f} IC_OOS={oos['ic_oos']:+.4f} "
                  f"Sharpe_IS={oos['sharpe_is']:+.1f} Sharpe_OOS={oos['sharpe_oos']:+.1f}")

            results[alpha_id] = {
                "signal_stats": stats,
                "horizons": horizon_results,
                "oos_split_h5": oos,
                "status": "ok",
            }
        except Exception as e:
            print(f"  ERROR: {e}")
            results[alpha_id] = {"status": "error", "error": str(e)}

    # Summary table
    print(f"\n{'='*80}")
    print(f"{'ALPHA COMPARISON SUMMARY':^80}")
    print(f"{'='*80}")
    print(f"{'Alpha':<25} {'IC_OOS(h5)':>10} {'Sharpe_OOS(h5)':>14} {'IC(h1)':>8} {'IC(h10)':>8} {'AC1':>6}")
    print("-" * 80)
    ranked = []
    for alpha_id, r in results.items():
        if r["status"] != "ok":
            print(f"{alpha_id:<25} {'ERROR':>10}")
            continue
        ic_oos = r["oos_split_h5"]["ic_oos"]
        sh_oos = r["oos_split_h5"]["sharpe_oos"]
        ic_h1 = r["horizons"]["h1"]["ic"]
        ic_h10 = r["horizons"]["h10"]["ic"]
        ac1 = r["signal_stats"]["autocorr_1"]
        print(f"{alpha_id:<25} {ic_oos:>+10.4f} {sh_oos:>+14.1f} {ic_h1:>+8.4f} {ic_h10:>+8.4f} {ac1:>6.3f}")
        ranked.append((alpha_id, ic_oos, sh_oos))

    ranked.sort(key=lambda x: x[1], reverse=True)
    print(f"\n--- Ranked by IC_OOS (h=5 ticks) ---")
    for i, (alpha_id, ic, sh) in enumerate(ranked, 1):
        marker = " ★" if ic > 0.01 else ""
        print(f"  {i}. {alpha_id}: IC_OOS={ic:+.4f}, Sharpe_OOS={sh:+.1f}{marker}")

    # Save
    output = {"data_path": args.data, "ticks": n, "oos_ratio": args.oos_ratio, "results": results}
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"\nSaved: {args.out}")

    return output


if __name__ == "__main__":
    main()
