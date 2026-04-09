"""Drift-Burst Detector — Real TXFD6 data evaluation.

Loads real L1 BidAsk data exported from ClickHouse, converts to scaled int,
runs DriftBurstDetector, and evaluates burst frequency, toxicity classification,
forward returns, and StormGuard escalation frequency.

Usage:
    uv run python research/tools/evaluate_drift_burst_real.py \
        --data research/data/raw/txfd6/TXFD6_all_l1.npy

Outputs: outputs/team_artifacts/alpha-research/stage4_drift_burst_real_data.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import structlog

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from hft_platform.risk.drift_burst_detector import DriftBurstDetector  # noqa: E402

# Suppress per-burst log spam
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(40))
logger = structlog.get_logger("eval.drift_burst.real")

_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = _REPO_ROOT / "outputs" / "team_artifacts" / "alpha-research" / "stage4_drift_burst_real_data.json"

# StormGuard thresholds
_STORMGUARD_WARM = 0.3
_STORMGUARD_STORM = 0.5
_STORMGUARD_HALT = 0.7

# Forward return windows (in ticks)
_FWD_WINDOWS = [10, 50, 100, 500]


# ---------------------------------------------------------------------------
# Single-pass collection of T-statistics + toxicity scores + burst events
# ---------------------------------------------------------------------------


def _run_single_pass(
    data: np.ndarray,
    window_size: int = 100,
    burst_threshold: float = 3.0,
    cooldown_ticks: int = 50,
    cooldown_ns: int = 5_000_000_000,
    min_bpv: float = 1e-10,
    skip_zero_returns: bool = True,
) -> tuple[list[dict[str, Any]], np.ndarray, np.ndarray]:
    """Run detector and return bursts, t_statistics array, toxicity_scores array."""
    n = len(data)

    detector = DriftBurstDetector(
        window_size=window_size,
        burst_threshold=burst_threshold,
        cooldown_ticks=cooldown_ticks,
        cooldown_ns=cooldown_ns,
        min_bpv=min_bpv,
        skip_zero_returns=skip_zero_returns,
    )

    bursts: list[dict[str, Any]] = []
    # Pre-allocate arrays for t-statistics and toxicity scores
    t_stats = np.zeros(n, dtype=np.float64)
    tox_scores = np.zeros(n, dtype=np.float64)

    t0 = time.monotonic()
    for i in range(n):
        row = data[i]
        mid_x2 = int(row["bid_px"] * 10000) + int(row["ask_px"] * 10000)
        spread = int(row["ask_px"] * 10000) - int(row["bid_px"] * 10000)
        bq = float(row["bid_qty"])
        aq = float(row["ask_qty"])
        tot = bq + aq
        imb = (bq - aq) / tot if tot > 0 else 0.0
        ts = int(row["local_ts"])

        result = detector.evaluate(
            mid_price_x2=mid_x2,
            spread_scaled=spread,
            imbalance=imb,
            ts=ts,
        )

        t_stats[i] = detector.t_statistic
        tox_scores[i] = result.toxicity_score

        if result.burst_detected and result.burst_event is not None:
            evt = result.burst_event
            bursts.append({
                "tick_idx": i,
                "ts_ns": ts,
                "direction": evt.direction,
                "magnitude": evt.magnitude,
                "t_statistic": evt.t_statistic,
                "toxicity_type": evt.toxicity_type,
                "toxicity_score": result.toxicity_score,
                "mid_price": float(row["mid_price"]),
                "spread_bps": float(row["spread_bps"]),
                "imbalance": round(imb, 4),
            })

        # Progress report every 500k ticks
        if i > 0 and i % 500_000 == 0:
            elapsed = time.monotonic() - t0
            rate = i / elapsed
            remaining = (n - i) / rate
            print(f"    {i:,}/{n:,} ticks ({i/n*100:.0f}%), "
                  f"{rate:.0f} ticks/s, ~{remaining:.0f}s remaining, "
                  f"bursts so far: {len(bursts)}")

    elapsed = time.monotonic() - t0
    print(f"    Pass complete: {elapsed:.1f}s, {n/elapsed:.0f} ticks/s, {len(bursts)} bursts")
    return bursts, t_stats, tox_scores


def _analyze(
    data: np.ndarray,
    bursts: list[dict[str, Any]],
    t_stats: np.ndarray,
    tox_scores: np.ndarray,
) -> dict[str, Any]:
    """Compute all analysis metrics from single-pass results."""
    n = len(data)
    mid_prices = data["mid_price"]
    ts_arr = data["local_ts"]

    # Log returns and cumulative
    log_returns = np.zeros(n, dtype=np.float64)
    log_returns[1:] = np.log(mid_prices[1:] / np.maximum(mid_prices[:-1], 1e-12))
    cum_log_ret = np.cumsum(log_returns)

    # --- Per-day/hour stats ---
    dates: set[str] = set()
    for i in range(0, n, max(1, n // 1000)):
        dates.add(datetime.fromtimestamp(int(ts_arr[i]) / 1e9).strftime("%Y-%m-%d"))
    n_days = max(1, len(dates))

    burst_dates: dict[str, int] = defaultdict(int)
    burst_hours: dict[int, int] = defaultdict(int)
    for b in bursts:
        dt = datetime.fromtimestamp(b["ts_ns"] / 1e9)
        burst_dates[dt.strftime("%Y-%m-%d")] += 1
        burst_hours[dt.hour] += 1

    # --- Forward returns ---
    for b in bursts:
        idx = b["tick_idx"]
        for fw in _FWD_WINDOWS:
            if idx + fw < n:
                fwd_ret = float(cum_log_ret[idx + fw] - cum_log_ret[idx])
                b[f"fwd_ret_{fw}"] = fwd_ret
                b[f"dir_fwd_ret_{fw}"] = b["direction"] * fwd_ret
            else:
                b[f"fwd_ret_{fw}"] = None
                b[f"dir_fwd_ret_{fw}"] = None

    # --- Toxicity type breakdown ---
    informed = [b for b in bursts if b["toxicity_type"] == "informed"]
    liquidity = [b for b in bursts if b["toxicity_type"] == "liquidity"]

    # --- Forward return analysis ---
    fwd_analysis: dict[str, dict[str, Any]] = {}
    for label, subset in [("informed", informed), ("liquidity", liquidity), ("all", bursts)]:
        info: dict[str, Any] = {"count": len(subset)}
        for fw in _FWD_WINDOWS:
            dir_rets = [b[f"dir_fwd_ret_{fw}"] for b in subset if b.get(f"dir_fwd_ret_{fw}") is not None]
            if dir_rets:
                arr = np.array(dir_rets, dtype=np.float64)
                info[f"fwd_{fw}_dir_mean"] = round(float(np.mean(arr)), 8)
                info[f"fwd_{fw}_dir_pos_rate"] = round(float(np.mean(arr > 0)), 4)
            else:
                info[f"fwd_{fw}_dir_mean"] = None
                info[f"fwd_{fw}_dir_pos_rate"] = None
        fwd_analysis[label] = info

    # --- StormGuard state analysis (vectorized from tox_scores) ---
    sg_calm = int(np.sum(tox_scores < _STORMGUARD_WARM))
    sg_warm = int(np.sum((tox_scores >= _STORMGUARD_WARM) & (tox_scores < _STORMGUARD_STORM)))
    sg_storm = int(np.sum((tox_scores >= _STORMGUARD_STORM) & (tox_scores < _STORMGUARD_HALT)))
    sg_halt = int(np.sum(tox_scores >= _STORMGUARD_HALT))
    sg_counts = {"CALM": sg_calm, "WARM": sg_warm, "STORM": sg_storm, "HALT": sg_halt}
    sg_pct = {k: round(v / n * 100, 4) for k, v in sg_counts.items()}

    # Escalation counting (vectorized)
    states = np.zeros(n, dtype=np.int8)
    states[tox_scores >= _STORMGUARD_WARM] = 1
    states[tox_scores >= _STORMGUARD_STORM] = 2
    states[tox_scores >= _STORMGUARD_HALT] = 3
    transitions = np.diff(states)
    # to_WARM: transition from 0->1
    to_warm = int(np.sum((states[:-1] == 0) & (states[1:] == 1)))
    to_storm = int(np.sum((states[:-1] < 2) & (states[1:] == 2)))
    to_halt = int(np.sum((states[:-1] < 3) & (states[1:] == 3)))
    escalations = {"to_WARM": to_warm, "to_STORM": to_storm, "to_HALT": to_halt}
    escalations_per_day = {k: round(v / n_days, 2) for k, v in escalations.items()}

    # --- Magnitude stats ---
    if bursts:
        mags = np.array([b["magnitude"] for b in bursts], dtype=np.float64)
        mag_stats = {
            "mean": round(float(np.mean(mags)), 3),
            "std": round(float(np.std(mags)), 3),
            "p50": round(float(np.median(mags)), 3),
            "p95": round(float(np.percentile(mags, 95)), 3),
            "max": round(float(np.max(mags)), 3),
        }
    else:
        mag_stats = {}

    # --- T-statistic distribution ---
    abs_t = np.abs(t_stats)
    t_stats_nz = abs_t[abs_t > 0]
    t_dist = {}
    if len(t_stats_nz) > 0:
        t_dist = {
            "mean": round(float(np.mean(t_stats_nz)), 3),
            "p50": round(float(np.median(t_stats_nz)), 3),
            "p95": round(float(np.percentile(t_stats_nz, 95)), 3),
            "p99": round(float(np.percentile(t_stats_nz, 99)), 3),
            "max": round(float(np.max(t_stats_nz)), 3),
        }

    # --- Threshold sweep from T-statistics (no re-run needed) ---
    sweep_results: list[dict[str, Any]] = []
    for thresh in [2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 10.0, 50.0, 100.0]:
        # Count how many times |T| > threshold (approximation of burst count
        # without cooldown; actual burst count with cooldown is lower)
        exceedances = int(np.sum(abs_t > thresh))
        sweep_results.append({
            "threshold": thresh,
            "ticks_exceeding": exceedances,
            "pct_exceeding": round(exceedances / n * 100, 4),
        })

    return {
        "total_bursts": len(bursts),
        "n_days": n_days,
        "bursts_per_day": round(len(bursts) / n_days, 2),
        "bursts_by_date": dict(sorted(burst_dates.items())),
        "bursts_by_hour": dict(sorted(burst_hours.items())),
        "toxicity_type_counts": {"informed": len(informed), "liquidity": len(liquidity)},
        "magnitude_stats": mag_stats,
        "t_statistic_distribution": t_dist,
        "toxicity_score_distribution": {
            "mean": round(float(np.mean(tox_scores)), 4),
            "p50": round(float(np.median(tox_scores)), 4),
            "p95": round(float(np.percentile(tox_scores, 95)), 4),
            "p99": round(float(np.percentile(tox_scores, 99)), 4),
            "max": round(float(np.max(tox_scores)), 4),
        },
        "forward_returns": fwd_analysis,
        "stormguard_state_counts": sg_counts,
        "stormguard_state_pct": sg_pct,
        "stormguard_escalations_total": escalations,
        "stormguard_escalations_per_day": escalations_per_day,
        "threshold_exceedance_sweep": sweep_results,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Drift-burst real-data evaluation")
    parser.add_argument("--data", type=str, default=str(_DEFAULT_DATA), help="Path to .npy L1 data")
    args = parser.parse_args()

    data_path = Path(args.data)

    if not data_path.exists():
        print(f"ERROR: data not found at {data_path}")
        return 1

    data = np.load(data_path, allow_pickle=False)
    n = len(data)
    mid_prices = data["mid_price"]

    print(f"\n{'='*70}")
    print(f"  Drift-Burst Detector — Real Data Evaluation (TXFD6)")
    print(f"{'='*70}")
    print(f"  Data: {data_path.name}")
    print(f"  Rows: {n:,}")
    print(f"  Price range: {mid_prices.min():.1f} - {mid_prices.max():.1f}")
    print(f"{'='*70}\n")

    # --- Single pass with production defaults (skip_zero_returns + min_bpv + 5s cooldown) ---
    print("[1/1] Running detector (threshold=3.0, window=100, skip_zero_returns=True, min_bpv=1e-10, cooldown_ns=5s)...")
    bursts, t_stats, tox_scores = _run_single_pass(
        data,
        burst_threshold=3.0,
        cooldown_ns=5_000_000_000,
        min_bpv=1e-10,
        skip_zero_returns=True,
    )

    # --- Analyze ---
    analysis = _analyze(data, bursts, t_stats, tox_scores)

    # --- Top bursts ---
    top_bursts = sorted(bursts, key=lambda b: b["magnitude"], reverse=True)[:20]
    top_bursts_clean = [
        {
            "tick": b["tick_idx"],
            "direction": b["direction"],
            "magnitude": round(b["magnitude"], 4),
            "toxicity_type": b["toxicity_type"],
            "mid_price": b["mid_price"],
            "date": datetime.fromtimestamp(b["ts_ns"] / 1e9).strftime("%Y-%m-%d %H:%M:%S"),
        }
        for b in top_bursts
    ]

    # --- Key findings ---
    fwd_informed = analysis["forward_returns"].get("informed", {})
    fwd_liquidity = analysis["forward_returns"].get("liquidity", {})
    ic_rate = fwd_informed.get("fwd_100_dir_pos_rate")
    lr_rate = fwd_liquidity.get("fwd_100_dir_pos_rate")

    key_findings = {
        "bursts_per_day": analysis["bursts_per_day"],
        "informed_continuation": {
            "fwd_100_dir_pos_rate": ic_rate,
            "supports_continuation": ic_rate > 0.5 if ic_rate is not None else None,
        },
        "liquidity_reversal": {
            "fwd_100_dir_pos_rate": lr_rate,
            "supports_reversal": lr_rate < 0.5 if lr_rate is not None else None,
        },
        "stormguard_escalations_per_day": analysis["stormguard_escalations_per_day"],
        "note_on_magnitude": (
            "T-statistics are extremely large (hundreds to thousands) because the BPV "
            "denominator becomes very small with high-frequency L1 updates where many "
            "consecutive ticks have identical mid-price (zero returns). The detector "
            "is oversensitive on raw L1 BidAsk data; consider downsampling or using "
            "a minimum BPV floor."
        ),
    }

    # --- Build output ---
    output: dict[str, Any] = {
        "stage": "4_drift_burst_real_data",
        "component": "DriftBurstDetector",
        "data_source": str(data_path),
        "data_rows": n,
        "analysis": analysis,
        "top_bursts_by_magnitude": top_bursts_clean,
        "key_findings": key_findings,
    }

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    # --- Print results ---
    print(f"\n{'='*70}")
    print(f"  RESULTS")
    print(f"{'='*70}")
    print(f"\n  Burst Frequency:")
    print(f"    Total: {analysis['total_bursts']}")
    print(f"    Days: {analysis['n_days']}")
    print(f"    Per day: {analysis['bursts_per_day']:.1f}")
    print(f"    By date: {analysis['bursts_by_date']}")
    print(f"    By hour: {analysis['bursts_by_hour']}")

    print(f"\n  Toxicity Type Breakdown:")
    print(f"    Informed: {analysis['toxicity_type_counts']['informed']}")
    print(f"    Liquidity: {analysis['toxicity_type_counts']['liquidity']}")

    if analysis["magnitude_stats"]:
        ms = analysis["magnitude_stats"]
        print(f"\n  Burst Magnitude (|T|):")
        print(f"    mean={ms['mean']:.1f}, p50={ms['p50']:.1f}, p95={ms['p95']:.1f}, max={ms['max']:.1f}")

    if analysis["t_statistic_distribution"]:
        td = analysis["t_statistic_distribution"]
        print(f"\n  T-statistic Distribution (|T|, all ticks):")
        print(f"    mean={td['mean']:.1f}, p50={td['p50']:.1f}, p95={td['p95']:.1f}, p99={td['p99']:.1f}, max={td['max']:.1f}")

    ts_d = analysis["toxicity_score_distribution"]
    print(f"\n  Toxicity Score Distribution:")
    print(f"    mean={ts_d['mean']:.4f}, p50={ts_d['p50']:.4f}, p95={ts_d['p95']:.4f}, max={ts_d['max']:.4f}")

    print(f"\n  Forward Returns (directional, aligned with burst direction):")
    for ttype in ["informed", "liquidity", "all"]:
        fwd = analysis["forward_returns"].get(ttype, {})
        print(f"    [{ttype}] (n={fwd.get('count', 0)}):")
        for fw in _FWD_WINDOWS:
            dm = fwd.get(f"fwd_{fw}_dir_mean")
            dp = fwd.get(f"fwd_{fw}_dir_pos_rate")
            if dm is not None:
                print(f"      fwd_{fw}: dir_mean={dm:.8f}, pos_rate={dp:.4f}")

    print(f"\n  StormGuard State Distribution:")
    print(f"    {analysis['stormguard_state_pct']}")
    print(f"  StormGuard Escalations per day:")
    print(f"    {analysis['stormguard_escalations_per_day']}")

    print(f"\n  Threshold Exceedance (|T| > threshold, no cooldown):")
    for s in analysis["threshold_exceedance_sweep"]:
        print(f"    threshold={s['threshold']:.1f}: {s['ticks_exceeding']:,} ticks ({s['pct_exceeding']:.2f}%)")

    print(f"\n  Key Findings:")
    ic = key_findings["informed_continuation"]
    lr = key_findings["liquidity_reversal"]
    print(f"    Informed continuation (fwd_100 pos_rate > 0.5)? "
          f"{ic['supports_continuation']} (rate={ic['fwd_100_dir_pos_rate']})")
    print(f"    Liquidity reversal (fwd_100 pos_rate < 0.5)? "
          f"{lr['supports_reversal']} (rate={lr['fwd_100_dir_pos_rate']})")

    print(f"\n  Output: {_OUT_PATH}")
    print(f"{'='*70}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
