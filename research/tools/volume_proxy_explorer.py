"""volume_proxy_explorer.py — Alpha exploration using depth-derived volume proxies.

Since L1 data has volume=0 (BidAsk only), we construct volume proxies from
queue dynamics to approximate trade-dependent alphas like VPIN, Kyle's Lambda.

Key insight: |Δbid| + |Δask| ≈ proxy for trade activity (consumed + replenished depth).
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

logger = get_logger("volume_proxy_explorer")

_EMA_ALPHA_4 = 0.2212
_EMA_ALPHA_8 = 0.1175
_EMA_ALPHA_16 = 0.0606
_EMA_ALPHA_32 = 0.0308
_EMA_ALPHA_64 = 0.0155
_EPS = 1e-8


def _ema(x: np.ndarray, alpha: float) -> np.ndarray:
    if len(x) == 0:
        return x.copy()
    b = np.array([alpha], dtype=np.float64)
    a = np.array([1.0, -(1.0 - alpha)], dtype=np.float64)
    zi = np.array([x[0] * (1.0 - alpha)], dtype=np.float64)
    out, _ = lfilter(b, a, x, zi=zi)
    return np.asarray(out, dtype=np.float64)


# --- Volume proxy construction ---

def _depth_churn(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """Total depth churn = |Δbid| + |Δask|. Proxy for trading activity."""
    return np.abs(np.diff(bid_qty, prepend=bid_qty[0])) + np.abs(np.diff(ask_qty, prepend=ask_qty[0]))


def _net_consumption(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """Net consumption = max(-Δbid, 0) + max(-Δask, 0). Proxy for market order volume."""
    return np.maximum(-np.diff(bid_qty, prepend=bid_qty[0]), 0) + np.maximum(-np.diff(ask_qty, prepend=ask_qty[0]), 0)


def _signed_consumption(bid_qty: np.ndarray, ask_qty: np.ndarray) -> np.ndarray:
    """Signed consumption: bid depletion - ask depletion. Buy pressure proxy."""
    return np.maximum(-np.diff(bid_qty, prepend=bid_qty[0]), 0) - np.maximum(-np.diff(ask_qty, prepend=ask_qty[0]), 0)


# --- Alpha formulas ---

def alpha_vpin_depth_proxy(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """VPIN approximation using depth churn as volume proxy.
    VPIN = |buy_vol - sell_vol| / total_vol ≈ |signed_consumption| / net_consumption."""
    signed = _signed_consumption(bid_qty, ask_qty)
    total = _net_consumption(bid_qty, ask_qty) + _EPS
    # Rolling VPIN over EMA window
    buy_proxy = _ema(np.maximum(signed, 0), _EMA_ALPHA_32)
    sell_proxy = _ema(np.maximum(-signed, 0), _EMA_ALPHA_32)
    total_ema = _ema(total, _EMA_ALPHA_32) + _EPS
    return np.clip(np.abs(buy_proxy - sell_proxy) / total_ema, 0, 1)


def alpha_kyle_lambda_depth(bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any) -> np.ndarray:
    """Kyle's lambda via depth-proxy: λ = cov(Δmid, signed_consumption) / var(signed_consumption)."""
    d_mid = np.diff(mid, prepend=mid[0])
    signed = _signed_consumption(bid_qty, ask_qty)
    cov = _ema(d_mid * signed, _EMA_ALPHA_32)
    var = _ema(signed ** 2, _EMA_ALPHA_32) + _EPS
    lam = cov / var
    return np.clip(lam / (np.maximum(_ema(np.abs(lam), _EMA_ALPHA_64), _EPS)), -2, 2)


def alpha_churn_imbalance(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Churn imbalance: |Δbid| - |Δask| normalized. Higher bid churn = sell pressure."""
    bid_churn = np.abs(np.diff(bid_qty, prepend=bid_qty[0]))
    ask_churn = np.abs(np.diff(ask_qty, prepend=ask_qty[0]))
    total = _ema(bid_churn + ask_churn, _EMA_ALPHA_16) + _EPS
    raw = (bid_churn - ask_churn) / total
    return np.clip(_ema(raw, _EMA_ALPHA_8), -1, 1)


def alpha_consumption_momentum(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Momentum of signed consumption — persistent buying/selling pressure."""
    signed = _signed_consumption(bid_qty, ask_qty)
    fast = _ema(signed, _EMA_ALPHA_4)
    slow = _ema(signed, _EMA_ALPHA_32)
    return np.clip((fast - slow) / (np.maximum(_ema(np.abs(signed), _EMA_ALPHA_32), _EPS)), -2, 2)


def alpha_activity_regime(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Activity regime: fast/slow churn ratio. High = active trading, low = quiet."""
    churn = _depth_churn(bid_qty, ask_qty)
    fast = _ema(churn, _EMA_ALPHA_8)
    slow = _ema(churn, _EMA_ALPHA_64) + _EPS
    return np.clip(fast / slow, 0, 5)


def alpha_toxic_consumption(bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any) -> np.ndarray:
    """Toxic consumption: high consumption + spread widening = informed flow."""
    consumption = _net_consumption(bid_qty, ask_qty)
    spread_dev = spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1) - 1
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    raw = _ema(consumption, _EMA_ALPHA_8) * np.maximum(spread_dev, 0)
    return np.clip(raw * np.sign(qi), -2, 2)


def alpha_replenishment_speed(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Asymmetric replenishment speed: which side refills faster after consumption."""
    bid_add = np.maximum(np.diff(bid_qty, prepend=bid_qty[0]), 0)
    ask_add = np.maximum(np.diff(ask_qty, prepend=ask_qty[0]), 0)
    bid_speed = _ema(bid_add, _EMA_ALPHA_8)
    ask_speed = _ema(ask_add, _EMA_ALPHA_8)
    total = bid_speed + ask_speed + _EPS
    return np.clip((bid_speed - ask_speed) / total, -1, 1)


def alpha_depth_turnover_ratio(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Depth turnover: churn / standing depth. High = nervous market makers."""
    churn = _depth_churn(bid_qty, ask_qty)
    depth = bid_qty + ask_qty + _EPS
    turnover = _ema(churn / depth, _EMA_ALPHA_16)
    baseline = _ema(churn / depth, _EMA_ALPHA_64) + _EPS
    return np.clip(turnover / baseline, 0, 5)


def alpha_consumption_price_impact(bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any) -> np.ndarray:
    """Price impact per unit consumption: Δmid / consumption. High = low liquidity."""
    d_mid = np.diff(mid, prepend=mid[0])
    consumption = _net_consumption(bid_qty, ask_qty) + _EPS
    impact = d_mid / consumption
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return np.clip(_ema(np.abs(impact), _EMA_ALPHA_16) * np.sign(qi), -2, 2)


def alpha_sweep_detector(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Detect sweeps: large one-sided consumption in short time. Hawkes-like."""
    signed = _signed_consumption(bid_qty, ask_qty)
    magnitude = np.abs(signed)
    threshold = _ema(magnitude, _EMA_ALPHA_64) * 2.0
    is_sweep = (magnitude > threshold).astype(np.float64)
    intensity = _ema(is_sweep, _EMA_ALPHA_8)
    return np.clip(intensity * np.sign(_ema(signed, _EMA_ALPHA_4)), -1, 1)


def alpha_hidden_order_proxy(bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any) -> np.ndarray:
    """Hidden order detection: price moves without visible depth change.
    When |Δmid| > 0 but churn is low → hidden orders driving price."""
    d_mid = np.diff(mid, prepend=mid[0])
    churn = _depth_churn(bid_qty, ask_qty) + _EPS
    hidden_ratio = np.abs(d_mid) / churn
    hidden_ema = _ema(hidden_ratio, _EMA_ALPHA_16)
    baseline = _ema(hidden_ratio, _EMA_ALPHA_64) + _EPS
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return np.clip((hidden_ema / baseline - 1) * np.sign(qi), -2, 2)


def alpha_consumption_asymmetry_ratio(bid_qty: np.ndarray, ask_qty: np.ndarray, **_: Any) -> np.ndarray:
    """Ratio of bid-side vs ask-side consumption. Persistent buy/sell pressure."""
    bid_consumed = np.maximum(-np.diff(bid_qty, prepend=bid_qty[0]), 0)
    ask_consumed = np.maximum(-np.diff(ask_qty, prepend=ask_qty[0]), 0)
    bid_ema = _ema(bid_consumed, _EMA_ALPHA_16)
    ask_ema = _ema(ask_consumed, _EMA_ALPHA_16)
    return np.clip((bid_ema - ask_ema) / (bid_ema + ask_ema + _EPS), -1, 1)


def alpha_vol_consumption_divergence(bid_qty: np.ndarray, ask_qty: np.ndarray, mid: np.ndarray, **_: Any) -> np.ndarray:
    """Divergence between volatility and consumption: vol up + consumption down = informed."""
    d_mid = np.diff(mid, prepend=mid[0])
    vol = _ema(d_mid ** 2, _EMA_ALPHA_16)
    consumption = _ema(_net_consumption(bid_qty, ask_qty), _EMA_ALPHA_16) + _EPS
    vol_baseline = _ema(vol, _EMA_ALPHA_64) + _EPS
    cons_baseline = _ema(consumption, _EMA_ALPHA_64) + _EPS
    vol_ratio = vol / vol_baseline
    cons_ratio = consumption / cons_baseline
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    divergence = (vol_ratio - cons_ratio) / (vol_ratio + cons_ratio + _EPS)
    return np.clip(divergence * np.sign(qi), -2, 2)


def alpha_refill_toxicity(bid_qty: np.ndarray, ask_qty: np.ndarray, spread: np.ndarray, **_: Any) -> np.ndarray:
    """When depth is consumed but NOT refilled + spread widens = toxic.
    Combines consumption, replenishment failure, and spread signal."""
    consumption = _net_consumption(bid_qty, ask_qty)
    replenishment = np.maximum(np.diff(bid_qty, prepend=bid_qty[0]), 0) + np.maximum(np.diff(ask_qty, prepend=ask_qty[0]), 0)
    failure = _ema(consumption - replenishment, _EMA_ALPHA_8)
    spread_dev = np.maximum(spread / np.maximum(_ema(spread, _EMA_ALPHA_64), 1) - 1, 0)
    qi = (bid_qty - ask_qty) / (bid_qty + ask_qty + _EPS)
    return np.clip(_ema(failure * spread_dev, _EMA_ALPHA_8) * np.sign(qi), -2, 2)


ALPHA_REGISTRY: dict[str, tuple] = {
    "vpin_depth_proxy":            (alpha_vpin_depth_proxy, "VPIN via depth churn"),
    "kyle_lambda_depth":           (alpha_kyle_lambda_depth, "Kyle λ via consumption"),
    "churn_imbalance":             (alpha_churn_imbalance, "|Δbid|-|Δask| normalized"),
    "consumption_momentum":        (alpha_consumption_momentum, "signed consumption trend"),
    "activity_regime":             (alpha_activity_regime, "fast/slow churn ratio"),
    "toxic_consumption":           (alpha_toxic_consumption, "consumption × spread widen"),
    "replenishment_speed":         (alpha_replenishment_speed, "bid vs ask refill speed"),
    "depth_turnover_ratio":        (alpha_depth_turnover_ratio, "churn/depth, MM nervousness"),
    "consumption_price_impact":    (alpha_consumption_price_impact, "Δmid/consumption"),
    "sweep_detector":              (alpha_sweep_detector, "large one-sided consumption"),
    "hidden_order_proxy":          (alpha_hidden_order_proxy, "price move w/o visible churn"),
    "consumption_asymmetry_ratio": (alpha_consumption_asymmetry_ratio, "bid vs ask consumption"),
    "vol_consumption_divergence":  (alpha_vol_consumption_divergence, "vol up + consumption down"),
    "refill_toxicity":             (alpha_refill_toxicity, "consumption-replenish fail × spread"),
}


# --- Metrics (shared) ---
def _compute_forward_returns(mid, horizons):
    ret = {}
    for h in horizons:
        fwd = np.empty_like(mid)
        fwd[:-h] = (mid[h:] - mid[:-h]) / (mid[:-h] + _EPS)
        fwd[-h:] = 0.0
        ret[h] = fwd
    return ret

def _compute_ic(signal, fwd_ret, n_chunks=20):
    valid = np.isfinite(signal) & np.isfinite(fwd_ret) & (signal != 0.0)
    sig_v, ret_v = signal[valid], fwd_ret[valid]
    if len(sig_v) < 1000:
        return 0.0, 0.0
    chunk_size = len(sig_v) // n_chunks
    if chunk_size < 50:
        return 0.0, 0.0
    ics = []
    for i in range(n_chunks):
        s = sig_v[i*chunk_size:(i+1)*chunk_size]
        r = ret_v[i*chunk_size:(i+1)*chunk_size]
        rs = np.argsort(np.argsort(s)).astype(np.float64); rs -= rs.mean()
        rr = np.argsort(np.argsort(r)).astype(np.float64); rr -= rr.mean()
        d = np.sqrt((rs**2).sum() * (rr**2).sum())
        ics.append(float((rs*rr).sum()/d) if d > _EPS else 0.0)
    ic_arr = np.array(ics)
    return float(ic_arr.mean()), float(ic_arr.mean() / (ic_arr.std() + _EPS))

def _compute_autocorr(signal, lag=1):
    valid = np.isfinite(signal) & (signal != 0.0)
    s = signal[valid]
    if len(s) < lag + 100: return 0.0
    s = s - s.mean()
    c0 = np.dot(s, s)
    return float(np.dot(s[:-lag], s[lag:]) / c0) if c0 > _EPS else 0.0

def explore_symbol(data_path, horizons=None):
    if horizons is None: horizons = [50, 200, 1000, 5000]
    data = np.load(data_path)
    n = len(data)
    if n < 2000: return {}
    if data.dtype.names is None or "bid_qty" not in data.dtype.names: return {}
    if "spread_bps" not in data.dtype.names: return {}

    bq = data["bid_qty"].astype(np.float64)
    aq = data["ask_qty"].astype(np.float64)
    mid = data["mid_price"].astype(np.float64)
    spread = data["spread_bps"].astype(np.float64)
    fwd = _compute_forward_returns(mid, horizons)

    results = {}
    for aid, (fn, desc) in ALPHA_REGISTRY.items():
        try:
            sig = fn(bid_qty=bq, ask_qty=aq, spread=spread, mid=mid)
        except Exception as e:
            logger.warning("alpha_failed", alpha=aid, error=str(e))
            continue
        w = 500; sig_w = sig[w:]
        r = {"description": desc, "n_rows": n, "signal_mean": float(np.nanmean(sig_w)),
             "signal_std": float(np.nanstd(sig_w)), "acf_1": _compute_autocorr(sig_w), "horizons": {}}
        for h in horizons:
            ic_mean, ic_ir = _compute_ic(sig_w, fwd[h][w:])
            r["horizons"][str(h)] = {"ic_mean": ic_mean, "ic_ir": ic_ir}
        results[aid] = r
    return results

def run_exploration(data_dir, horizons=None, out_path=None):
    if horizons is None: horizons = [50, 200, 1000, 5000]
    base = Path(data_dir)
    all_files = []
    for sd in sorted(base.iterdir()):
        if not sd.is_dir(): continue
        sym = sd.name.upper()
        cf = sd / f"{sym}_all_l1.npy"
        if cf.exists(): all_files.append((sym, str(cf)))
        else:
            daily = sorted(sd.glob(f"{sym}_*_l1.npy"))
            daily = [f for f in daily if "all" not in f.name]
            if daily: all_files.append((sym, str(max(daily, key=lambda f: f.stat().st_size))))

    logger.info("found_symbols", count=len(all_files))
    per_symbol = {}
    t0 = time.monotonic()
    for sym, fp in all_files:
        t1 = time.monotonic()
        sr = explore_symbol(fp, horizons)
        if sr:
            per_symbol[sym] = sr
            first = next(iter(sr.values()), {})
            logger.info("explored", symbol=sym, rows=first.get("n_rows",0), alphas=len(sr), elapsed_s=f"{time.monotonic()-t1:.1f}")

    # Aggregate
    alpha_ids = list(ALPHA_REGISTRY.keys())
    lb = []
    for aid in alpha_ids:
        agg = {"alpha_id": aid, "description": ALPHA_REGISTRY[aid][1]}
        for h in horizons:
            ics = [per_symbol[s][aid]["horizons"][str(h)]["ic_mean"] for s in per_symbol if aid in per_symbol[s] and str(h) in per_symbol[s][aid].get("horizons",{})]
            if ics:
                ic_arr = np.array(ics)
                agg[f"h{h}_ic_mean"] = float(ic_arr.mean())
                agg[f"h{h}_ic_ir"] = float(ic_arr.mean()/(ic_arr.std()+_EPS))
                agg[f"h{h}_syms_positive"] = int((ic_arr > 0).sum())
                agg[f"h{h}_syms_total"] = len(ics)
        acfs = [per_symbol[s][aid]["acf_1"] for s in per_symbol if aid in per_symbol[s]]
        if acfs: agg["acf_1_mean"] = float(np.mean(acfs))
        lb.append(agg)
    lb.sort(key=lambda x: abs(x.get("h1000_ic_mean",0)), reverse=True)

    output = {"timestamp": time.strftime("%Y-%m-%d %H:%M:%S"), "total_symbols": len(per_symbol),
              "total_alphas": len(alpha_ids), "leaderboard": lb, "per_symbol": per_symbol,
              "elapsed_s": time.monotonic()-t0}
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(json.dumps(output, indent=2, default=str))
        logger.info("results_saved", path=out_path)

    # Print leaderboard
    print(f"\n{'='*100}")
    print(f"VOLUME-PROXY ALPHA LEADERBOARD — {len(per_symbol)} symbols, {len(alpha_ids)} alphas")
    print(f"{'='*100}")
    print(f"{'Alpha':<30} {'IC@1000':>10} {'IC_IR':>8} {'ACF-1':>7} {'Syms+':>7} {'IC@50':>10} {'IC@5000':>10}  Description")
    print("-" * 100)
    for e in lb:
        ic = e.get("h1000_ic_mean",0); ir = e.get("h1000_ic_ir",0)
        acf = e.get("acf_1_mean",0); pos = e.get("h1000_syms_positive",0); tot = e.get("h1000_syms_total",0)
        ic50 = e.get("h50_ic_mean",0); ic5k = e.get("h5000_ic_mean",0)
        s = "★" if abs(ic)>0.05 and abs(ir)>1.5 else " "
        print(f"{s}{e['alpha_id']:<29} {ic:>+10.5f} {ir:>8.2f} {acf:>7.3f} {pos:>3}/{tot:<3} {ic50:>+10.5f} {ic5k:>+10.5f}  {e['description']}")
    return output

def main():
    parser = argparse.ArgumentParser(description="Volume-proxy alpha exploration")
    parser.add_argument("--data-dir", default="research/data/raw")
    parser.add_argument("--out", default="research/results/volume_proxy_exploration.json")
    parser.add_argument("--horizons", default="50,200,1000,5000")
    args = parser.parse_args()
    run_exploration(args.data_dir, [int(h) for h in args.horizons.split(",")], args.out)

if __name__ == "__main__":
    raise SystemExit(main() or 0)
