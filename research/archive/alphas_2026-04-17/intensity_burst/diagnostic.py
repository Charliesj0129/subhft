"""Intensity Burst — Gate Zero diagnostic.

Runs BurstDetector over March 2026 TXFD6 and TMFD6 tick data.

Reports:
  1. Number of bursts per day (kill gate: <5/day = too rare)
  2. Average burst duration (consecutive burst-active ticks)
  3. Burst rate distribution (P25/P50/P75/P99 of tick rate during burst)
  4. IC: burst state vs future realized volatility at 5s/30s/60s horizons

Usage:
    python -m research.alphas.intensity_burst.diagnostic
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
import structlog

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from hft_platform.feature.burst_detector import BurstDetector

logger = structlog.get_logger(__name__)

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

TXFD6_DATA_DIR = Path(__file__).resolve().parents[3] / "research" / "data" / "raw" / "txfd6"
TMFD6_DATA_DIR = Path(__file__).resolve().parents[3] / "research" / "data" / "raw" / "tmfd6"
OUTPUT_DIR = Path(__file__).resolve().parents[3] / "research" / "experiments" / "validations" / "intensity_burst"

WINDOW_NS: int = 30_000_000_000  # 30s
MULTIPLIER: float = 3.0
COOLDOWN_NS: int = 5_000_000_000  # 5s

# Forward volatility horizons in nanoseconds
VOL_HORIZONS_NS: dict[str, int] = {
    "5s": 5_000_000_000,
    "30s": 30_000_000_000,
    "60s": 60_000_000_000,
}

# IC kill gate
MIN_BURSTS_PER_DAY: int = 5

SEC_NS: int = 1_000_000_000


# --------------------------------------------------------------------------- #
# Data loading
# --------------------------------------------------------------------------- #


def _load_l1_files(data_dir: Path, symbol_prefix: str) -> list[tuple[str, np.ndarray]]:
    """Load per-day L1 npy files.

    Returns list of (filename, array) tuples, sorted by name.
    """
    files = sorted(data_dir.glob(f"{symbol_prefix}_*_l1.npy"))
    files = [f for f in files if "all" not in f.name]
    if not files:
        logger.warning("no_l1_files", data_dir=str(data_dir), prefix=symbol_prefix)
        return []
    days: list[tuple[str, np.ndarray]] = []
    for f in files:
        d = np.load(f, allow_pickle=True)
        if len(d) > 100:
            days.append((f.stem, d))
    return days


def _extract_timestamps_ns(data: np.ndarray) -> np.ndarray:
    """Extract tick timestamps in nanoseconds from L1 data.

    Handles structured arrays with 'ts' or 'exch_ts' fields,
    and plain arrays where column 0 is timestamp.
    """
    if hasattr(data.dtype, "names") and data.dtype.names is not None:
        for field in ("ts", "exch_ts", "timestamp"):
            if field in data.dtype.names:
                ts = data[field].astype(np.int64)
                # If timestamps look like seconds (< 1e12), convert to ns
                if ts.max() < 1_000_000_000_000:
                    ts = ts * SEC_NS
                return ts
    # Fallback: first column
    ts = data[:, 0].astype(np.int64) if data.ndim == 2 else data.astype(np.int64)
    if ts.max() < 1_000_000_000_000:
        ts = ts * SEC_NS
    return ts


def _extract_mid_price_x2(data: np.ndarray) -> np.ndarray | None:
    """Extract mid_price_x2 (int) if available, else compute from bid/ask."""
    if hasattr(data.dtype, "names") and data.dtype.names is not None:
        if "mid_price_x2" in data.dtype.names:
            return data["mid_price_x2"].astype(np.int64)
        # Try computing from best_bid + best_ask
        for bid_f, ask_f in [("best_bid", "best_ask"), ("bid", "ask"), ("bid_price", "ask_price")]:
            if bid_f in data.dtype.names and ask_f in data.dtype.names:
                bid = data[bid_f].astype(np.int64)
                ask = data[ask_f].astype(np.int64)
                return bid + ask  # mid_price_x2 = bid + ask (no division)
    return None


# --------------------------------------------------------------------------- #
# Realized volatility
# --------------------------------------------------------------------------- #


def _compute_forward_rv(
    timestamps_ns: np.ndarray,
    mid_price_x2: np.ndarray,
    horizon_ns: int,
) -> np.ndarray:
    """Compute forward realized volatility (squared returns) at given horizon.

    For each tick i, find tick j where ts[j] >= ts[i] + horizon_ns.
    RV = (mid_x2[j] - mid_x2[i])^2 as integer.
    Returns NaN where horizon extends beyond data.
    """
    n = len(timestamps_ns)
    rv = np.full(n, np.nan, dtype=np.float64)
    j = 0
    for i in range(n):
        target = timestamps_ns[i] + horizon_ns
        while j < n and timestamps_ns[j] < target:
            j += 1
        if j < n:
            diff = int(mid_price_x2[j]) - int(mid_price_x2[i])
            rv[i] = float(diff * diff)
        # Reset j only moves forward (timestamps are sorted)
        if j > 0:
            j = j  # noqa: PLW0127 — intentional: j only advances
    return rv


# --------------------------------------------------------------------------- #
# IC computation
# --------------------------------------------------------------------------- #


def _pearson_ic(signal: np.ndarray, forward: np.ndarray) -> float:
    """Pearson IC between signal and forward values, ignoring NaN."""
    mask = ~np.isnan(signal) & ~np.isnan(forward)
    s = signal[mask]
    f = forward[mask]
    if len(s) < 30:
        return float("nan")
    s_mean = np.mean(s)
    f_mean = np.mean(f)
    s_std = np.std(s)
    f_std = np.std(f)
    if s_std < 1e-12 or f_std < 1e-12:
        return 0.0
    return float(np.mean((s - s_mean) * (f - f_mean)) / (s_std * f_std))


# --------------------------------------------------------------------------- #
# Per-day analysis
# --------------------------------------------------------------------------- #


def _analyze_day(
    day_name: str,
    timestamps_ns: np.ndarray,
    mid_price_x2: np.ndarray | None,
) -> dict:
    """Run burst detection on one day and compute diagnostics."""
    det = BurstDetector(
        window_ns=WINDOW_NS,
        multiplier=MULTIPLIER,
        cooldown_ns=COOLDOWN_NS,
        capacity=512,
    )

    n = len(timestamps_ns)
    burst_flags = np.zeros(n, dtype=np.int8)
    burst_events: list[int] = []  # timestamps of burst rising edges
    tick_rates: list[int] = []

    for i in range(n):
        triggered = det.on_tick(int(timestamps_ns[i]))
        if det.is_burst:
            burst_flags[i] = 1
            tick_rates.append(det.tick_rate)
        if triggered:
            burst_events.append(int(timestamps_ns[i]))

    # Burst count
    burst_count = len(burst_events)

    # Burst duration: runs of consecutive burst=1 ticks
    burst_durations_ns: list[int] = []
    in_burst = False
    burst_start_ts = 0
    for i in range(n):
        if burst_flags[i] == 1 and not in_burst:
            in_burst = True
            burst_start_ts = int(timestamps_ns[i])
        elif burst_flags[i] == 0 and in_burst:
            in_burst = False
            burst_durations_ns.append(int(timestamps_ns[i]) - burst_start_ts)
    if in_burst:
        burst_durations_ns.append(int(timestamps_ns[-1]) - burst_start_ts)

    # Rate distribution during bursts
    rate_stats: dict = {}
    if tick_rates:
        rates_arr = np.array(tick_rates, dtype=np.int64)
        rate_stats = {
            "p25": int(np.percentile(rates_arr, 25)),
            "p50": int(np.percentile(rates_arr, 50)),
            "p75": int(np.percentile(rates_arr, 75)),
            "p99": int(np.percentile(rates_arr, 99)),
        }

    # Duration stats
    duration_stats: dict = {}
    if burst_durations_ns:
        dur_arr = np.array(burst_durations_ns, dtype=np.int64)
        duration_stats = {
            "mean_ms": float(np.mean(dur_arr)) / 1e6,
            "median_ms": float(np.median(dur_arr)) / 1e6,
            "max_ms": float(np.max(dur_arr)) / 1e6,
            "count": len(burst_durations_ns),
        }

    # IC vs forward volatility
    ic_results: dict = {}
    if mid_price_x2 is not None:
        signal = burst_flags.astype(np.float64)
        for hz_name, hz_ns in VOL_HORIZONS_NS.items():
            rv = _compute_forward_rv(timestamps_ns, mid_price_x2, hz_ns)
            ic = _pearson_ic(signal, rv)
            ic_results[hz_name] = round(ic, 6)

    result = {
        "day": day_name,
        "total_ticks": n,
        "burst_count": burst_count,
        "burst_ticks": int(np.sum(burst_flags)),
        "burst_pct": round(100.0 * float(np.sum(burst_flags)) / n, 3) if n > 0 else 0.0,
        "rate_during_burst": rate_stats,
        "duration": duration_stats,
        "ic_vs_rv": ic_results,
    }
    return result


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #


def run_diagnostic(symbol: str, data_dir: Path) -> list[dict]:
    """Run Gate Zero diagnostic for a single symbol."""
    prefix = symbol.upper()
    days = _load_l1_files(data_dir, prefix)
    if not days:
        logger.error("no_data", symbol=symbol, data_dir=str(data_dir))
        return []

    results: list[dict] = []
    for day_name, data in days:
        ts = _extract_timestamps_ns(data)
        mid = _extract_mid_price_x2(data)
        day_result = _analyze_day(day_name, ts, mid)
        results.append(day_result)
        logger.info(
            "day_complete",
            day=day_name,
            bursts=day_result["burst_count"],
            burst_pct=day_result["burst_pct"],
            ic_5s=day_result["ic_vs_rv"].get("5s", "N/A"),
        )
    return results


def _summarize(results: list[dict], symbol: str) -> dict:
    """Compute aggregate summary across days."""
    if not results:
        return {"symbol": symbol, "status": "NO_DATA"}

    burst_counts = [r["burst_count"] for r in results]
    burst_pcts = [r["burst_pct"] for r in results]

    # Aggregate IC (mean across days)
    ic_agg: dict[str, float] = {}
    for hz_name in VOL_HORIZONS_NS:
        ics = [r["ic_vs_rv"][hz_name] for r in results if hz_name in r.get("ic_vs_rv", {})]
        ics = [ic for ic in ics if not (isinstance(ic, float) and np.isnan(ic))]
        if ics:
            ic_agg[hz_name] = round(float(np.mean(ics)), 6)

    mean_bursts = float(np.mean(burst_counts))
    kill_gate_pass = mean_bursts >= MIN_BURSTS_PER_DAY

    summary = {
        "symbol": symbol,
        "days_analyzed": len(results),
        "bursts_per_day_mean": round(mean_bursts, 1),
        "bursts_per_day_median": round(float(np.median(burst_counts)), 1),
        "bursts_per_day_min": int(np.min(burst_counts)),
        "bursts_per_day_max": int(np.max(burst_counts)),
        "burst_pct_mean": round(float(np.mean(burst_pcts)), 3),
        "ic_vs_rv_mean": ic_agg,
        "kill_gate_min_5_per_day": kill_gate_pass,
        "verdict": "PASS" if kill_gate_pass else "KILL — too few bursts",
    }
    return summary


def main() -> None:
    """Run diagnostic on TXFD6 and TMFD6."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    all_results: dict = {}

    for symbol, data_dir in [("TXFD6", TXFD6_DATA_DIR), ("TMFD6", TMFD6_DATA_DIR)]:
        logger.info("start_diagnostic", symbol=symbol, data_dir=str(data_dir))
        day_results = run_diagnostic(symbol, data_dir)
        summary = _summarize(day_results, symbol)
        all_results[symbol] = {
            "summary": summary,
            "per_day": day_results,
        }
        logger.info(
            "diagnostic_complete",
            symbol=symbol,
            verdict=summary.get("verdict", "N/A"),
            bursts_per_day_mean=summary.get("bursts_per_day_mean", 0),
            ic_5s=summary.get("ic_vs_rv_mean", {}).get("5s", "N/A"),
            ic_30s=summary.get("ic_vs_rv_mean", {}).get("30s", "N/A"),
            ic_60s=summary.get("ic_vs_rv_mean", {}).get("60s", "N/A"),
        )

    # Write results
    output_path = OUTPUT_DIR / "gate_zero_results.json"
    with open(output_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("results_written", path=str(output_path))

    # Print summary table
    print("\n" + "=" * 70)
    print("INTENSITY BURST — Gate Zero Diagnostic Results")
    print("=" * 70)
    for symbol, data in all_results.items():
        s = data["summary"]
        print(f"\n{symbol}:")
        print(f"  Days analyzed:       {s.get('days_analyzed', 0)}")
        print(f"  Bursts/day (mean):   {s.get('bursts_per_day_mean', 0)}")
        print(f"  Bursts/day (median): {s.get('bursts_per_day_median', 0)}")
        print(f"  Bursts/day (range):  [{s.get('bursts_per_day_min', 0)}, {s.get('bursts_per_day_max', 0)}]")
        print(f"  Burst % of ticks:    {s.get('burst_pct_mean', 0):.3f}%")
        ic = s.get("ic_vs_rv_mean", {})
        if ic:
            print(f"  IC vs RV (5s):       {ic.get('5s', 'N/A')}")
            print(f"  IC vs RV (30s):      {ic.get('30s', 'N/A')}")
            print(f"  IC vs RV (60s):      {ic.get('60s', 'N/A')}")
        print(f"  Kill gate (>=5/day): {s.get('kill_gate_min_5_per_day', False)}")
        print(f"  VERDICT:             {s.get('verdict', 'N/A')}")
    print("\n" + "=" * 70)


if __name__ == "__main__":
    main()
