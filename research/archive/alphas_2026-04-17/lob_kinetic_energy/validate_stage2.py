"""LOB Price-Keyed KE — Stage 2 validation on TXFD6 L5 real data.

Deliverables:
  DC-1: Per-level rank correlation of depth change at L1..L5 with 1s fwd return
  DC-2: Collinearity — Spearman of LOB_momentum with depth_imbalance, l1_imbalance, ofi_l1
  DC-3: Pooled Spearman IC for LOB_momentum and LOB_gravity_center at h=10/50/200
  Execution: Integer overflow test with real TXFD6 prices (x10000 scale),
             compute cost estimation

Usage:
    uv run python -m research.alphas.lob_kinetic_energy.validate_stage2
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
import structlog

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from research.alphas.lob_kinetic_energy.price_keyed import LobPriceKeyedKE

logger = structlog.get_logger(__name__)

DATA_DIR = Path("research/data/raw/txfd6")
OUTPUT_DIR = Path("research/experiments/validations/lob_kinetic_energy")
HORIZONS = (10, 50, 200)

# hftbacktest event type encoding (upper 4 bits of ev field)
_EV_BID_DEPTH = 14  # 0xE
_EV_ASK_DEPTH = 13  # 0xD
_EV_TRADE = 12      # 0xC


# ------------------------------------------------------------------ #
# Data loading: reconstruct L5 snapshots from hftbt events
# ------------------------------------------------------------------ #

def _load_l5_snapshots(npz_path: Path) -> dict[str, np.ndarray]:
    """Reconstruct L5 book snapshots from hftbacktest event data.

    Returns dict with:
      'bids': shape (N_snapshots, 5, 2)  — [price, qty] per level
      'asks': shape (N_snapshots, 5, 2)
      'mid':  shape (N_snapshots,)
      'ts':   shape (N_snapshots,)  — nanosecond timestamps
    """
    raw = np.load(npz_path)["data"]
    ev_types = raw["ev"] >> 28
    timestamps = raw["exch_ts"]

    # Find unique timestamps for depth events (exclude trades)
    depth_mask = (ev_types == _EV_BID_DEPTH) | (ev_types == _EV_ASK_DEPTH)
    depth_data = raw[depth_mask]
    depth_ev = ev_types[depth_mask]
    depth_ts = timestamps[depth_mask]

    unique_ts = np.unique(depth_ts)
    n_snaps = len(unique_ts)

    bids_out = np.zeros((n_snaps, 5, 2), dtype=np.float64)
    asks_out = np.zeros((n_snaps, 5, 2), dtype=np.float64)
    mid_out = np.zeros(n_snaps, dtype=np.float64)
    ts_out = unique_ts.copy()

    # Build index for fast grouping
    ts_indices = np.searchsorted(unique_ts, depth_ts)

    # Iterate and fill snapshots
    snap_bid_count = np.zeros(n_snaps, dtype=np.int32)
    snap_ask_count = np.zeros(n_snaps, dtype=np.int32)

    for idx in range(len(depth_data)):
        snap_i = ts_indices[idx]
        row = depth_data[idx]
        px = row["px"]
        qty = row["qty"]

        if depth_ev[idx] == _EV_BID_DEPTH:
            li = snap_bid_count[snap_i]
            if li < 5:
                bids_out[snap_i, li, 0] = px
                bids_out[snap_i, li, 1] = qty
                snap_bid_count[snap_i] = li + 1
        else:  # ASK_DEPTH
            li = snap_ask_count[snap_i]
            if li < 5:
                asks_out[snap_i, li, 0] = px
                asks_out[snap_i, li, 1] = qty
                snap_ask_count[snap_i] = li + 1

    # Compute mid prices
    valid = (snap_bid_count > 0) & (snap_ask_count > 0)
    mid_out[valid] = (bids_out[valid, 0, 0] + asks_out[valid, 0, 0]) * 0.5

    # Filter to valid snapshots only
    bids_out = bids_out[valid]
    asks_out = asks_out[valid]
    mid_out = mid_out[valid]
    ts_out = ts_out[valid]

    return {"bids": bids_out, "asks": asks_out, "mid": mid_out, "ts": ts_out}


def _load_all_l5_days() -> list[dict[str, np.ndarray]]:
    """Load all available L5 days from hftbt npz files."""
    files = sorted(DATA_DIR.glob("TXFD6_*_l2.hftbt.npz"))
    days = []
    for f in files:
        logger.info("loading_l5", file=f.name)
        d = _load_l5_snapshots(f)
        logger.info("loaded_l5", file=f.name, n_snapshots=len(d["mid"]))
        days.append(d)
    return days


# ------------------------------------------------------------------ #
# Feature computation
# ------------------------------------------------------------------ #

def _compute_ofi_l1(day: dict[str, np.ndarray]) -> np.ndarray:
    """L1 OFI from BBO changes."""
    bids = day["bids"]  # (N, 5, 2)
    asks = day["asks"]
    n = len(bids)
    ofi = np.zeros(n, dtype=np.float64)

    for i in range(1, n):
        bb = bids[i, 0, 0]
        bq = bids[i, 0, 1]
        ba = asks[i, 0, 0]
        aq = asks[i, 0, 1]
        pbb = bids[i - 1, 0, 0]
        pbq = bids[i - 1, 0, 1]
        pba = asks[i - 1, 0, 0]
        paq = asks[i - 1, 0, 1]

        if bb > pbb:
            b_flow = bq
        elif bb == pbb:
            b_flow = bq - pbq
        else:
            b_flow = -pbq

        if ba > pba:
            a_flow = -paq
        elif ba == pba:
            a_flow = aq - paq
        else:
            a_flow = aq

        ofi[i] = b_flow - a_flow
    return ofi


def _compute_depth_imbalance(day: dict[str, np.ndarray]) -> np.ndarray:
    """Total depth imbalance across all levels."""
    bids = day["bids"]  # (N, 5, 2)
    asks = day["asks"]
    bid_total = bids[:, :, 1].sum(axis=1)
    ask_total = asks[:, :, 1].sum(axis=1)
    total = bid_total + ask_total
    return np.where(total > 0, (bid_total - ask_total) / total, 0.0)


def _compute_l1_imbalance(day: dict[str, np.ndarray]) -> np.ndarray:
    """L1 queue imbalance."""
    bq = day["bids"][:, 0, 1]
    aq = day["asks"][:, 0, 1]
    total = bq + aq
    return np.where(total > 0, bq / total - 0.5, 0.0)  # centered at 0


def _compute_cumulative_ofi(ofi: np.ndarray) -> np.ndarray:
    """Cumulative OFI (running sum)."""
    return np.cumsum(ofi)


def _compute_forward_returns(mid: np.ndarray, h: int) -> np.ndarray:
    """Forward mid-price return at horizon h ticks."""
    n = len(mid)
    fwd = np.full(n, np.nan, dtype=np.float64)
    if h < n:
        fwd[:n - h] = mid[h:] - mid[:n - h]
    return fwd


def _spearman_ic(signal: np.ndarray, fwd_ret: np.ndarray) -> tuple[float, int]:
    """Pooled Spearman rank IC.

    Returns (ic, n_valid).
    """
    mask = np.isfinite(signal) & np.isfinite(fwd_ret)
    n = int(mask.sum())
    if n < 30:
        return 0.0, n
    s = signal[mask]
    r = fwd_ret[mask]
    # Rank transform
    s_rank = np.argsort(np.argsort(s)).astype(np.float64)
    r_rank = np.argsort(np.argsort(r)).astype(np.float64)
    # Pearson on ranks = Spearman
    s_z = s_rank - s_rank.mean()
    r_z = r_rank - r_rank.mean()
    denom = np.sqrt((s_z ** 2).sum() * (r_z ** 2).sum())
    if denom < 1e-30:
        return 0.0, n
    return float((s_z * r_z).sum() / denom), n


# ------------------------------------------------------------------ #
# DC-1: Per-level depth change correlation with forward return
# ------------------------------------------------------------------ #

def _dc1_per_level_correlation(day: dict[str, np.ndarray]) -> dict[str, object]:
    """Per-level rank correlation of depth change with ~1s forward return.

    Estimates how many ticks = 1 second from timestamp data.
    """
    mid = day["mid"]
    bids = day["bids"]  # (N, 5, 2)
    asks = day["asks"]
    ts = day["ts"]
    n = len(mid)

    # Estimate ticks per second
    if n > 100:
        dt_ns = np.diff(ts[:1000]).astype(np.float64)
        dt_ns = dt_ns[dt_ns > 0]
        if len(dt_ns) > 0:
            median_dt_ns = np.median(dt_ns)
            ticks_per_sec = max(1, int(1e9 / median_dt_ns))
        else:
            ticks_per_sec = 8  # fallback
    else:
        ticks_per_sec = 8

    h = ticks_per_sec  # ~1 second horizon
    fwd_ret = _compute_forward_returns(mid, h)

    results = {"ticks_per_sec_estimate": ticks_per_sec, "horizon_ticks": h}

    for level in range(5):
        # Depth change at this level (bid side)
        bid_qty = bids[:, level, 1]
        ask_qty = asks[:, level, 1]

        # Net depth change (bid growth - ask growth)
        bid_delta = np.zeros(n, dtype=np.float64)
        ask_delta = np.zeros(n, dtype=np.float64)
        bid_delta[1:] = np.diff(bid_qty)
        ask_delta[1:] = np.diff(ask_qty)
        net_delta = bid_delta - ask_delta

        ic, nv = _spearman_ic(net_delta, fwd_ret)
        results[f"L{level + 1}_depth_delta_ic"] = round(ic, 6)
        results[f"L{level + 1}_n_valid"] = nv

        # Also check individual sides
        ic_bid, _ = _spearman_ic(bid_delta, fwd_ret)
        ic_ask, _ = _spearman_ic(-ask_delta, fwd_ret)  # negate: ask growth = sell pressure
        results[f"L{level + 1}_bid_delta_ic"] = round(ic_bid, 6)
        results[f"L{level + 1}_ask_delta_ic"] = round(ic_ask, 6)

    return results


# ------------------------------------------------------------------ #
# Integer overflow / precision test
# ------------------------------------------------------------------ #

def _integer_overflow_test() -> dict[str, str]:
    """Test with real TXFD6 price scales.

    TXFD6 prices in hftbt: ~33000-34000 (index points * 10)
    Scaled x10000: ~330_000_000 - 340_000_000
    Max qty: ~50 contracts
    KE_bid = qty * dist^2 where dist ~ 0-50 pts
    Max KE per level: 50 * 50^2 = 125_000
    Total KE: 5 * 125_000 = 625_000
    Well within float64 range.
    """
    results = {}
    alpha = LobPriceKeyedKE()

    # Test 1: Real TXFD6 scale (raw prices from hftbt)
    bids = np.array([
        [33391, 4], [33390, 3], [33389, 2], [33388, 4], [33387, 11],
    ], dtype=np.float64)
    asks = np.array([
        [33396, 5], [33397, 5], [33398, 4], [33399, 3], [33400, 6],
    ], dtype=np.float64)
    m, gc = alpha.update(bids, asks)
    results["txfd6_raw_prices"] = "PASS" if np.isfinite(m) and np.isfinite(gc) else "FAIL"
    results["txfd6_raw_ke_bid"] = round(alpha.ke_bid, 2)
    results["txfd6_raw_ke_ask"] = round(alpha.ke_ask, 2)

    # Test 2: Scaled x10000 prices
    alpha2 = LobPriceKeyedKE()
    bids_s = bids.copy()
    asks_s = asks.copy()
    bids_s[:, 0] *= 10_000
    asks_s[:, 0] *= 10_000
    m2, gc2 = alpha2.update(bids_s, asks_s)
    results["txfd6_x10000_prices"] = "PASS" if np.isfinite(m2) and np.isfinite(gc2) else "FAIL"
    results["txfd6_x10000_ke_bid"] = f"{alpha2.ke_bid:.2e}"
    results["txfd6_x10000_ke_ask"] = f"{alpha2.ke_ask:.2e}"

    # Test 3: Extreme qty (stress)
    alpha3 = LobPriceKeyedKE()
    bids_e = np.array([
        [33391, 1e6], [33390, 1e6], [33389, 1e6], [33388, 1e6], [33387, 1e6],
    ], dtype=np.float64)
    asks_e = asks.copy()
    m3, gc3 = alpha3.update(bids_e, asks_e)
    results["extreme_qty_1e6"] = "PASS" if np.isfinite(m3) and np.isfinite(gc3) else "FAIL"

    # Test 4: Zero depth
    alpha4 = LobPriceKeyedKE()
    bids_z = np.array([[33391, 0], [33390, 0], [33389, 0], [33388, 0], [33387, 0]], dtype=np.float64)
    asks_z = np.array([[33396, 0], [33397, 0], [33398, 0], [33399, 0], [33400, 0]], dtype=np.float64)
    m4, gc4 = alpha4.update(bids_z, asks_z)
    results["zero_depth"] = "PASS" if np.isfinite(m4) and np.isfinite(gc4) else "FAIL"

    # Test 5: Single level
    alpha5 = LobPriceKeyedKE()
    bids_1 = np.array([[33391, 10]], dtype=np.float64)
    asks_1 = np.array([[33396, 5]], dtype=np.float64)
    m5, gc5 = alpha5.update(bids_1, asks_1)
    results["single_level"] = "PASS" if np.isfinite(m5) and np.isfinite(gc5) else "FAIL"

    # Document max values for TXFD6
    results["max_value_analysis"] = (
        "TXFD6 raw prices ~33000-34000, max qty ~50. "
        "Max dist from mid to L5 ~ 10 pts. "
        "Max KE per level = 50 * 10^2 = 5000. "
        "Max total KE = 5 * 5000 = 25000. "
        "Well within float64. "
        "With x10000 scaling: dist ~ 100_000, KE ~ 50 * 1e10 = 5e11. Still safe."
    )

    return results


# ------------------------------------------------------------------ #
# Compute cost estimation
# ------------------------------------------------------------------ #

def _benchmark_compute_cost(n_iters: int = 100_000) -> dict[str, float]:
    """Estimate microseconds per tick."""
    alpha = LobPriceKeyedKE()
    bids = np.array([
        [33391, 4], [33390, 3], [33389, 2], [33388, 4], [33387, 11],
    ], dtype=np.float64)
    asks = np.array([
        [33396, 5], [33397, 5], [33398, 4], [33399, 3], [33400, 6],
    ], dtype=np.float64)

    # Warmup
    for _ in range(100):
        alpha.update(bids, asks)

    # Benchmark
    t0 = time.perf_counter_ns()
    for _ in range(n_iters):
        alpha.update(bids, asks)
    t1 = time.perf_counter_ns()

    us_per_tick = (t1 - t0) / n_iters / 1000.0
    return {
        "n_iters": n_iters,
        "us_per_tick": round(us_per_tick, 2),
        "verdict": "OK" if us_per_tick < 100 else "SLOW",
    }


# ------------------------------------------------------------------ #
# Main validation
# ------------------------------------------------------------------ #

def main() -> None:
    logger.info("stage2_validation_start")

    # --- Integer overflow test ---
    overflow = _integer_overflow_test()
    logger.info("overflow_test", results=overflow)

    # --- Compute cost ---
    bench = _benchmark_compute_cost()
    logger.info("benchmark", **bench)

    # --- Load L5 data ---
    days = _load_all_l5_days()
    if not days:
        logger.error("no_l5_data_found")
        return
    logger.info("l5_data_loaded", n_days=len(days), total_snapshots=sum(len(d["mid"]) for d in days))

    # --- Run alpha on all days ---
    all_momentum: list[np.ndarray] = []
    all_gravity: list[np.ndarray] = []
    all_fwd: dict[int, list[np.ndarray]] = {h: [] for h in HORIZONS}
    all_ofi: list[np.ndarray] = []
    all_dimb: list[np.ndarray] = []
    all_l1imb: list[np.ndarray] = []
    all_cum_ofi: list[np.ndarray] = []
    per_day_ic: dict[int, list[dict[str, float]]] = {h: [] for h in HORIZONS}

    for day_idx, day in enumerate(days):
        mid = day["mid"]
        bids = day["bids"]
        asks = day["asks"]
        n = len(mid)

        alpha = LobPriceKeyedKE()
        momentum = np.zeros(n, dtype=np.float64)
        gravity = np.zeros(n, dtype=np.float64)

        for i in range(n):
            m, gc = alpha.update(bids[i], asks[i], mid_price=mid[i])
            momentum[i] = m
            gravity[i] = gc

        all_momentum.append(momentum)
        all_gravity.append(gravity)

        # Existing features
        ofi = _compute_ofi_l1(day)
        dimb = _compute_depth_imbalance(day)
        l1imb = _compute_l1_imbalance(day)
        cum_ofi = _compute_cumulative_ofi(ofi)

        all_ofi.append(ofi)
        all_dimb.append(dimb)
        all_l1imb.append(l1imb)
        all_cum_ofi.append(cum_ofi)

        # Forward returns + per-day IC
        for h in HORIZONS:
            fwd = _compute_forward_returns(mid, h)
            all_fwd[h].append(fwd)
            ic_m, nv_m = _spearman_ic(momentum, fwd)
            ic_g, nv_g = _spearman_ic(gravity, fwd)
            per_day_ic[h].append({"momentum_ic": ic_m, "gravity_ic": ic_g, "n": nv_m})
            logger.info(
                "day_ic", day=day_idx, h=h,
                momentum_ic=round(ic_m, 6), gravity_ic=round(ic_g, 6), n=nv_m,
            )

    # --- DC-3: Pooled IC ---
    pooled_momentum = np.concatenate(all_momentum)
    pooled_gravity = np.concatenate(all_gravity)
    pooled_ic = {}
    for h in HORIZONS:
        pooled_fwd = np.concatenate(all_fwd[h])
        ic_m, nv_m = _spearman_ic(pooled_momentum, pooled_fwd)
        ic_g, nv_g = _spearman_ic(pooled_gravity, pooled_fwd)

        day_ics_m = [d["momentum_ic"] for d in per_day_ic[h]]
        day_ics_g = [d["gravity_ic"] for d in per_day_ic[h]]

        pooled_ic[f"h={h}"] = {
            "momentum_pooled_ic": round(ic_m, 6),
            "gravity_pooled_ic": round(ic_g, 6),
            "n_valid": nv_m,
            "momentum_per_day_mean": round(float(np.mean(day_ics_m)), 6),
            "momentum_per_day_std": round(float(np.std(day_ics_m)), 6),
            "gravity_per_day_mean": round(float(np.mean(day_ics_g)), 6),
            "gravity_per_day_std": round(float(np.std(day_ics_g)), 6),
        }
        logger.info("pooled_ic", h=h, **pooled_ic[f"h={h}"])

    # --- DC-2: Collinearity ---
    pooled_ofi = np.concatenate(all_ofi)
    pooled_dimb = np.concatenate(all_dimb)
    pooled_l1imb = np.concatenate(all_l1imb)
    pooled_cum_ofi = np.concatenate(all_cum_ofi)

    def _sr(a: np.ndarray, b: np.ndarray) -> float:
        """Spearman rank correlation."""
        mask = np.isfinite(a) & np.isfinite(b)
        n = int(mask.sum())
        if n < 30:
            return 0.0
        a_r = np.argsort(np.argsort(a[mask])).astype(np.float64)
        b_r = np.argsort(np.argsort(b[mask])).astype(np.float64)
        a_z = a_r - a_r.mean()
        b_z = b_r - b_r.mean()
        d = np.sqrt((a_z ** 2).sum() * (b_z ** 2).sum())
        return float((a_z * b_z).sum() / d) if d > 1e-30 else 0.0

    collinearity = {
        "momentum_vs_ofi_l1_raw": round(_sr(pooled_momentum, pooled_ofi), 4),
        "momentum_vs_depth_imbalance": round(_sr(pooled_momentum, pooled_dimb), 4),
        "momentum_vs_l1_imbalance": round(_sr(pooled_momentum, pooled_l1imb), 4),
        "momentum_vs_cum_ofi": round(_sr(pooled_momentum, pooled_cum_ofi), 4),
        "gravity_vs_ofi_l1_raw": round(_sr(pooled_gravity, pooled_ofi), 4),
        "gravity_vs_depth_imbalance": round(_sr(pooled_gravity, pooled_dimb), 4),
        "gravity_vs_l1_imbalance": round(_sr(pooled_gravity, pooled_l1imb), 4),
        "n_samples": len(pooled_momentum),
        "dc2_threshold": 0.7,
    }
    collinearity["dc2_all_pass"] = all(
        abs(v) < 0.7 for k, v in collinearity.items()
        if k.startswith("momentum_vs_") or k.startswith("gravity_vs_")
    )
    logger.info("collinearity_dc2", **collinearity)

    # --- DC-1: Per-level correlation ---
    dc1_results = {}
    for day_idx, day in enumerate(days):
        dc1 = _dc1_per_level_correlation(day)
        dc1_results[f"day_{day_idx}"] = dc1

    # Aggregate DC-1 across days
    dc1_agg = {}
    for level in range(1, 6):
        key = f"L{level}_depth_delta_ic"
        vals = [dc1_results[f"day_{i}"][key] for i in range(len(days))]
        dc1_agg[f"L{level}_mean_ic"] = round(float(np.mean(vals)), 6)
        dc1_agg[f"L{level}_std_ic"] = round(float(np.std(vals)), 6)
        dc1_agg[f"L{level}_per_day"] = [round(v, 6) for v in vals]
    logger.info("dc1_per_level", **dc1_agg)

    # --- Signal statistics ---
    sig_stats = {
        "momentum": {
            "mean": round(float(pooled_momentum.mean()), 6),
            "std": round(float(pooled_momentum.std()), 6),
            "min": round(float(pooled_momentum.min()), 6),
            "max": round(float(pooled_momentum.max()), 6),
            "p5": round(float(np.percentile(pooled_momentum, 5)), 6),
            "p95": round(float(np.percentile(pooled_momentum, 95)), 6),
        },
        "gravity_center": {
            "mean": round(float(pooled_gravity.mean()), 6),
            "std": round(float(pooled_gravity.std()), 6),
            "min": round(float(pooled_gravity.min()), 6),
            "max": round(float(pooled_gravity.max()), 6),
            "p5": round(float(np.percentile(pooled_gravity, 5)), 6),
            "p95": round(float(np.percentile(pooled_gravity, 95)), 6),
        },
    }
    logger.info("signal_stats", **sig_stats)

    # --- Assemble report ---
    report = {
        "alpha_id": "lob_kinetic_energy_price_keyed",
        "formulation": {
            "KE_bid": "Σ bid_qty[i] × (mid - bid_price[i])²",
            "KE_ask": "Σ ask_qty[i] × (ask_price[i] - mid)²",
            "LOB_momentum": "(KE_bid - KE_ask) / (KE_bid + KE_ask + ε)",
            "LOB_gravity_center": "bid_gc - ask_gc, where gc = Σ qty*dist / Σ qty",
            "smoothing": "8-tick EMA",
            "clip": "[-1, 1] for momentum",
        },
        "data": {
            "source": "TXFD6 L5 hftbacktest .npz files",
            "n_days": len(days),
            "total_snapshots": sum(len(d["mid"]) for d in days),
            "snapshots_per_day": [len(d["mid"]) for d in days],
        },
        "dc3_pooled_ic": pooled_ic,
        "dc2_collinearity": collinearity,
        "dc1_per_level": dc1_agg,
        "dc1_per_day": dc1_results,
        "signal_stats": sig_stats,
        "overflow_tests": overflow,
        "compute_cost": bench,
    }

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "stage2_validation_results.json"
    with open(out_path, "w") as f:
        json.dump(report, f, indent=2, default=str)

    # --- Print summary ---
    print("\n" + "=" * 72)
    print("LOB Price-Keyed KE — Stage 2 Validation Summary")
    print("=" * 72)
    print(f"Data: {len(days)} days, {sum(len(d['mid']) for d in days):,} L5 snapshots")
    print(f"Compute cost: {bench['us_per_tick']:.2f} µs/tick ({bench['verdict']})")
    print()

    print("DC-3: Pooled Spearman IC (authoritative)")
    for h in HORIZONS:
        r = pooled_ic[f"h={h}"]
        print(f"  h={h:>3}: momentum={r['momentum_pooled_ic']:+.6f}  "
              f"gravity={r['gravity_pooled_ic']:+.6f}  (n={r['n_valid']:,})")
    print()

    print("DC-2: Collinearity (Spearman, threshold < 0.7)")
    for k, v in collinearity.items():
        if k.startswith("momentum_vs_") or k.startswith("gravity_vs_"):
            status = "PASS" if abs(v) < 0.7 else "FAIL"
            print(f"  {k:>35}: r={v:+.4f}  {status}")
    print(f"  DC-2 all pass: {collinearity['dc2_all_pass']}")
    print()

    print("DC-1: Per-level depth delta IC (Spearman, ~1s fwd return)")
    for level in range(1, 6):
        m = dc1_agg[f"L{level}_mean_ic"]
        s = dc1_agg[f"L{level}_std_ic"]
        per_day = dc1_agg[f"L{level}_per_day"]
        print(f"  L{level}: mean={m:+.6f} std={s:.6f}  per_day={per_day}")
    print()

    print("Signal stats:")
    for name, stats in sig_stats.items():
        print(f"  {name}: mean={stats['mean']:+.6f} std={stats['std']:.6f} "
              f"[{stats['p5']:+.6f}, {stats['p95']:+.6f}]")
    print()

    print("Overflow tests:")
    for k, v in overflow.items():
        if not k.startswith("max_value"):
            print(f"  {k:>25}: {v}")
    print()
    print(f"Report: {out_path}")
    print("=" * 72)


if __name__ == "__main__":
    main()
