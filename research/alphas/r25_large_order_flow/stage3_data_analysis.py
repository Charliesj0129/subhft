"""R25 Stage 3: Kill-gate data analysis for METAORDER-TRAIL alpha.

Six analyses on TMFD6 L1 data:
  Task 1 (MR-2): Count qualifying sweep events
  Task 2 (MR-3): Empirical characterization — sustained flow after sweeps
  Task 3 (MR-1): KS test — sweep-conditioned vs unconditional top-OFI returns
  Task 4 (MR-7): Regime stratification (Jan/Feb vs March)
  Task 5: Zero-delta capture rate
  Task 6: Tick-rate regime comparison

Kill conditions:
  - March confirmed events < 50 total => KILL
  - No KS horizon shows p < 0.05 => KILL

Usage:
    uv run python -m research.alphas.r25_large_order_flow.stage3_data_analysis
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, UTC
from pathlib import Path

import numpy as np
from scipy import stats as scipy_stats
import structlog

_HERE = Path(__file__).resolve().parent
_RESEARCH_ROOT = _HERE.parent.parent
_PROJECT_ROOT = _RESEARCH_ROOT.parent
for _p in (_PROJECT_ROOT, _RESEARCH_ROOT):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

logger = structlog.get_logger("r25.stage3")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_L1_DIR = _RESEARCH_ROOT / "data" / "raw" / "tmfd6"
_OUT_DIR = Path("outputs/team_artifacts/alpha-research-r25")

# TMFD6: 1 point = 1 NTD. L1 data stores float NTD prices.
# mid_price delta of 1.0 = 1 tick.
_TICK_NTD = 1.0
_SWEEP_MIN_TICKS = 2
_SWEEP_MAX_EVENTS = 5
_LATENCY_NS = 36_000_000  # 36ms entry delay (EC-1)
_DAY_GAP_NS = 4 * 3_600_000_000_000  # 4h gap = day boundary

# Forward return horizons
_FWD_HORIZONS_S = [5, 10, 30, 60]
_FWD_HORIZONS_NS = [h * 1_000_000_000 for h in _FWD_HORIZONS_S]

# OFI threshold percentile for "top OFI" unconditional events
_OFI_TOP_PCT = 95

# March dates (MR-7: primary regime)
_MARCH_DATES = {"2026-03-19", "2026-03-20", "2026-03-24", "2026-03-25", "2026-03-26"}


def _load_all_l1() -> list[dict]:
    """Load all TMFD6 L1 files, return list of dicts with date + data."""
    files = sorted(_L1_DIR.glob("TMFD6_*_l1.npy"))
    result = []
    for f in files:
        date_str = f.stem.split("_")[1]
        data = np.load(str(f))
        if len(data) == 0:
            continue
        result.append({"date": date_str, "data": data, "path": str(f)})
        logger.info("loaded", date=date_str, rows=len(data))
    return result


def _split_days(timestamps: np.ndarray) -> list[tuple[int, int]]:
    """Split into day segments by detecting gaps > 4h."""
    if len(timestamps) == 0:
        return []
    gaps = np.diff(timestamps)
    boundaries = np.where(gaps > _DAY_GAP_NS)[0] + 1
    starts = np.concatenate([[0], boundaries])
    ends = np.concatenate([boundaries, [len(timestamps)]])
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _compute_ofi_l1(data: np.ndarray) -> np.ndarray:
    """Compute L1 OFI delta per event: delta_bid_qty - delta_ask_qty."""
    n = len(data)
    ofi = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        ofi[i] = (
            (data[i]["bid_qty"] - data[i - 1]["bid_qty"])
            - (data[i]["ask_qty"] - data[i - 1]["ask_qty"])
        )
    return ofi


def _compute_ofi_ema(ofi_raw: np.ndarray, alpha: float = 0.05) -> np.ndarray:
    """Compute EMA of OFI (approximation of ofi_l1_ema5s)."""
    n = len(ofi_raw)
    ema = np.zeros(n, dtype=np.float64)
    if n == 0:
        return ema
    ema[0] = ofi_raw[0]
    for i in range(1, n):
        ema[i] = ema[i - 1] + alpha * (ofi_raw[i] - ema[i - 1])
    return ema


def _detect_sweeps(
    mid_prices: np.ndarray,
    min_ticks: int = _SWEEP_MIN_TICKS,
    max_events: int = _SWEEP_MAX_EVENTS,
    skip_zero_delta: bool = False,
) -> list[dict]:
    """Detect sweep events in mid_price series.

    Returns list of dicts: {index, direction, magnitude_ticks, event_count}.
    """
    n = len(mid_prices)
    sweeps: list[dict] = []

    cum_delta = 0.0
    event_count = 0
    direction = 0
    start_idx = 0

    for i in range(1, n):
        delta = mid_prices[i] - mid_prices[i - 1]

        if delta == 0.0:
            if skip_zero_delta:
                continue
            if event_count > 0:
                event_count += 1
                if event_count > max_events:
                    cum_delta = 0.0
                    event_count = 0
                    direction = 0
            continue

        move_dir = 1 if delta > 0 else -1

        if direction == 0 or move_dir == direction:
            if direction == 0:
                start_idx = i - 1
            direction = move_dir
            cum_delta += delta
            event_count += 1
        else:
            cum_delta = delta
            event_count = 1
            direction = move_dir
            start_idx = i - 1

        if event_count > max_events:
            cum_delta = delta
            event_count = 1
            direction = move_dir
            start_idx = i - 1

        ticks = abs(cum_delta) / _TICK_NTD
        if ticks >= min_ticks:
            sweeps.append({
                "index": i,
                "start_index": start_idx,
                "direction": direction,
                "magnitude_ticks": ticks,
                "event_count": event_count,
            })
            cum_delta = 0.0
            event_count = 0
            direction = 0

    return sweeps


def _compute_forward_returns(
    timestamps: np.ndarray,
    mid_prices: np.ndarray,
    event_indices: list[int],
    horizons_ns: list[int],
) -> dict[int, np.ndarray]:
    """Compute forward returns at multiple horizons for given event indices.

    Applies 36ms latency: forward return measured from first tick after
    ts[i] + LATENCY_NS.
    """
    n = len(timestamps)
    results: dict[int, list[float]] = {h: [] for h in horizons_ns}

    for idx in event_indices:
        if idx >= n:
            continue
        base_ts = timestamps[idx] + _LATENCY_NS
        base_price = mid_prices[idx]
        if base_price <= 0:
            for h in horizons_ns:
                results[h].append(np.nan)
            continue

        for h in horizons_ns:
            target_ts = base_ts + h
            # Find first tick at or after target_ts
            j = idx + 1
            while j < n and timestamps[j] < target_ts:
                j += 1
            if j < n and mid_prices[j] > 0:
                fwd_ret = (mid_prices[j] - base_price) / base_price
                results[h].append(fwd_ret)
            else:
                results[h].append(np.nan)

    return {h: np.array(v) for h, v in results.items()}


# ---------------------------------------------------------------------------
# Task 1: MR-2 — Count qualifying events
# ---------------------------------------------------------------------------

def task1_count_events(all_days: list[dict]) -> dict:
    """Count sweep events per day and by regime."""
    logger.info("=== Task 1: MR-2 — Count qualifying events ===")

    jan_feb_days = [d for d in all_days if d["date"] < "2026-03-01"]
    march_days = [d for d in all_days if d["date"] >= "2026-03-01"]

    results = {
        "jan_feb": {"days": len(jan_feb_days), "per_day": []},
        "march": {"days": len(march_days), "per_day": []},
    }

    for regime, days in [("jan_feb", jan_feb_days), ("march", march_days)]:
        total_ticks = 0
        total_sweeps = 0
        for d in days:
            data = d["data"]
            n = len(data)
            total_ticks += n
            mid = data["mid_price"]
            sweeps = _detect_sweeps(mid)

            day_info = {
                "date": d["date"],
                "ticks": n,
                "sweeps_2tick": len([s for s in sweeps if s["magnitude_ticks"] >= 2]),
                "sweeps_3tick": len([s for s in sweeps if s["magnitude_ticks"] >= 3]),
                "sweeps_4tick_plus": len([s for s in sweeps if s["magnitude_ticks"] >= 4]),
            }
            total_sweeps += day_info["sweeps_2tick"]
            results[regime]["per_day"].append(day_info)
            logger.info("task1_day", **day_info)

        results[regime]["total_ticks"] = total_ticks
        results[regime]["total_sweeps_2tick"] = total_sweeps
        results[regime]["sweeps_per_day"] = round(total_sweeps / max(len(days), 1), 1)

    # Kill condition: March confirmed events < 50
    march_total = results["march"]["total_sweeps_2tick"]
    results["march_kill_gate"] = "PASS" if march_total >= 50 else "KILL"
    logger.info(
        "task1_summary",
        jan_feb_sweeps=results["jan_feb"]["total_sweeps_2tick"],
        march_sweeps=march_total,
        march_kill_gate=results["march_kill_gate"],
    )
    return results


# ---------------------------------------------------------------------------
# Task 2: MR-3 — Empirical characterization of sustained flow
# ---------------------------------------------------------------------------

def task2_sustained_flow(all_days: list[dict]) -> dict:
    """Analyze whether sweeps are followed by sustained same-direction OFI."""
    logger.info("=== Task 2: MR-3 — Sustained flow after sweeps ===")

    results: dict[str, dict] = {}

    for regime_name, days in [
        ("jan_feb", [d for d in all_days if d["date"] < "2026-03-01"]),
        ("march", [d for d in all_days if d["date"] >= "2026-03-01"]),
    ]:
        sustained_counts = {h: 0 for h in [5, 10, 30]}
        total_sweeps = 0
        magnitude_dist = {2: 0, 3: 0, 4: 0}

        for d in days:
            data = d["data"]
            mid = data["mid_price"]
            ts = data["local_ts"]
            ofi_raw = _compute_ofi_l1(data)
            ofi_ema = _compute_ofi_ema(ofi_raw)

            sweeps = _detect_sweeps(mid)
            total_sweeps += len(sweeps)

            for s in sweeps:
                mag = s["magnitude_ticks"]
                if mag >= 4:
                    magnitude_dist[4] += 1
                elif mag >= 3:
                    magnitude_dist[3] += 1
                else:
                    magnitude_dist[2] += 1

                idx = s["index"]
                direction = s["direction"]
                base_ts = ts[idx]

                # Check if OFI remains same-sign for various horizons
                for horizon_s in [5, 10, 30]:
                    horizon_ns = horizon_s * 1_000_000_000
                    end_ts = base_ts + horizon_ns
                    # Count OFI sign consistency in window
                    j = idx + 1
                    same_sign_count = 0
                    total_count = 0
                    while j < len(ts) and ts[j] <= end_ts:
                        total_count += 1
                        if (direction > 0 and ofi_ema[j] > 0) or (direction < 0 and ofi_ema[j] < 0):
                            same_sign_count += 1
                        j += 1
                    if total_count > 0 and same_sign_count / total_count >= 0.6:
                        sustained_counts[horizon_s] += 1

        results[regime_name] = {
            "total_sweeps": total_sweeps,
            "magnitude_dist": magnitude_dist,
            "sustained_ofi_pct": {
                f"{h}s": round(100 * sustained_counts[h] / max(total_sweeps, 1), 1)
                for h in [5, 10, 30]
            },
        }
        logger.info(f"task2_{regime_name}", **results[regime_name])

    return results


# ---------------------------------------------------------------------------
# Task 3: MR-1 — KS test (sweep vs unconditional top-OFI)
# ---------------------------------------------------------------------------

def task3_ks_test(all_days: list[dict]) -> dict:
    """Compare forward returns: sweep events vs top-OFI unconditional events."""
    logger.info("=== Task 3: MR-1 — KS test ===")

    results: dict[str, dict] = {}

    for regime_name, days in [
        ("jan_feb", [d for d in all_days if d["date"] < "2026-03-01"]),
        ("march", [d for d in all_days if d["date"] >= "2026-03-01"]),
    ]:
        all_sweep_indices: list[int] = []
        all_top_ofi_indices: list[int] = []
        all_mid: list[np.ndarray] = []
        all_ts: list[np.ndarray] = []
        offset = 0

        for d in days:
            data = d["data"]
            mid = data["mid_price"]
            ts = data["local_ts"]
            n = len(data)

            ofi_raw = _compute_ofi_l1(data)
            ofi_ema = _compute_ofi_ema(ofi_raw)

            # Sweep events
            sweeps = _detect_sweeps(mid)
            for s in sweeps:
                all_sweep_indices.append(offset + s["index"])

            # Top OFI events (unconditional)
            abs_ofi = np.abs(ofi_ema)
            if len(abs_ofi) > 100:
                threshold = np.percentile(abs_ofi[100:], _OFI_TOP_PCT)
                for i in range(100, n):
                    if abs_ofi[i] >= threshold:
                        all_top_ofi_indices.append(offset + i)

            all_mid.append(mid)
            all_ts.append(ts)
            offset += n

        # Concatenate all data
        if not all_mid:
            results[regime_name] = {"error": "no data"}
            continue

        concat_mid = np.concatenate(all_mid)
        concat_ts = np.concatenate(all_ts)

        # Compute forward returns for both groups
        sweep_fwd = _compute_forward_returns(concat_ts, concat_mid, all_sweep_indices, _FWD_HORIZONS_NS)
        ofi_fwd = _compute_forward_returns(concat_ts, concat_mid, all_top_ofi_indices, _FWD_HORIZONS_NS)

        ks_results: dict[str, dict] = {}
        any_significant = False

        for h_ns, h_s in zip(_FWD_HORIZONS_NS, _FWD_HORIZONS_S):
            sweep_rets = sweep_fwd[h_ns]
            ofi_rets = ofi_fwd[h_ns]

            # Remove NaN
            sweep_clean = sweep_rets[~np.isnan(sweep_rets)]
            ofi_clean = ofi_rets[~np.isnan(ofi_rets)]

            if len(sweep_clean) < 10 or len(ofi_clean) < 10:
                ks_results[f"{h_s}s"] = {"error": "insufficient_data", "n_sweep": len(sweep_clean), "n_ofi": len(ofi_clean)}
                continue

            stat, pvalue = scipy_stats.ks_2samp(sweep_clean, ofi_clean)

            # Also compute directional returns (sign-adjusted by sweep direction)
            sweep_mean = float(np.mean(sweep_clean))
            ofi_mean = float(np.mean(ofi_clean))
            sweep_std = float(np.std(sweep_clean))

            ks_results[f"{h_s}s"] = {
                "ks_stat": round(float(stat), 4),
                "p_value": round(float(pvalue), 6),
                "significant": pvalue < 0.05,
                "n_sweep": len(sweep_clean),
                "n_ofi": len(ofi_clean),
                "sweep_mean_bps": round(sweep_mean * 10000, 2),
                "ofi_mean_bps": round(ofi_mean * 10000, 2),
                "sweep_std_bps": round(sweep_std * 10000, 2),
            }
            if pvalue < 0.05:
                any_significant = True

        results[regime_name] = {
            "ks_results": ks_results,
            "any_significant": any_significant,
            "n_sweep_events": len(all_sweep_indices),
            "n_top_ofi_events": len(all_top_ofi_indices),
        }
        logger.info(f"task3_{regime_name}", **{k: v for k, v in results[regime_name].items() if k != "ks_results"})
        for h, r in ks_results.items():
            logger.info(f"task3_{regime_name}_{h}", **r)

    # Kill condition
    march_res = results.get("march", {})
    march_significant = march_res.get("any_significant", False)
    results["march_ks_kill_gate"] = "PASS" if march_significant else "KILL"

    return results


# ---------------------------------------------------------------------------
# Task 5: Zero-delta capture rate
# ---------------------------------------------------------------------------

def task5_zero_delta_capture(all_days: list[dict]) -> dict:
    """Measure what fraction of genuine 2-tick moves are captured vs missed."""
    logger.info("=== Task 5: Zero-delta capture rate ===")

    results: dict[str, dict] = {}

    for regime_name, days in [
        ("jan_feb", [d for d in all_days if d["date"] < "2026-03-01"]),
        ("march", [d for d in all_days if d["date"] >= "2026-03-01"]),
    ]:
        # Compare sweep counts with and without zero-delta skipping
        total_with_zeros = 0
        total_skip_zeros = 0
        total_genuine_moves = 0  # 2-tick moves within 5 seconds

        for d in days:
            data = d["data"]
            mid = data["mid_price"]
            ts = data["local_ts"]

            sweeps_normal = _detect_sweeps(mid, skip_zero_delta=False)
            sweeps_skip = _detect_sweeps(mid, skip_zero_delta=True)
            total_with_zeros += len(sweeps_normal)
            total_skip_zeros += len(sweeps_skip)

            # Count genuine 2-tick moves within 5 seconds
            for i in range(1, len(mid)):
                if abs(mid[i] - mid[i - 1]) >= 2 * _TICK_NTD:
                    total_genuine_moves += 1

        missed_pct = 0.0
        if total_skip_zeros > 0:
            missed_pct = round(100 * (1 - total_with_zeros / total_skip_zeros), 1)

        results[regime_name] = {
            "sweeps_with_zero_counting": total_with_zeros,
            "sweeps_skip_zero_delta": total_skip_zeros,
            "genuine_2tick_single_event": total_genuine_moves,
            "capture_improvement_pct": round(100 * total_skip_zeros / max(total_with_zeros, 1) - 100, 1) if total_with_zeros > 0 else 0,
            "recommend_skip_zeros": total_skip_zeros > total_with_zeros * 1.5,
        }
        logger.info(f"task5_{regime_name}", **results[regime_name])

    return results


# ---------------------------------------------------------------------------
# Task 6: Tick-rate regime comparison
# ---------------------------------------------------------------------------

def task6_tick_rate(all_days: list[dict]) -> dict:
    """Compare sweep characteristics during high vs normal tick-rate periods."""
    logger.info("=== Task 6: Tick-rate regime comparison ===")

    results: dict[str, dict] = {}

    for regime_name, days in [
        ("jan_feb", [d for d in all_days if d["date"] < "2026-03-01"]),
        ("march", [d for d in all_days if d["date"] >= "2026-03-01"]),
    ]:
        high_rate_sweeps = 0
        normal_rate_sweeps = 0
        high_rate_fwd_rets: list[float] = []
        normal_rate_fwd_rets: list[float] = []

        for d in days:
            data = d["data"]
            mid = data["mid_price"]
            ts = data["local_ts"]
            n = len(data)

            if n < 100:
                continue

            # Compute local tick rate (ticks per 1s window)
            tick_rates = np.zeros(n, dtype=np.float64)
            window_ns = 1_000_000_000  # 1s
            j_start = 0
            for i in range(n):
                while j_start < i and ts[i] - ts[j_start] > window_ns:
                    j_start += 1
                tick_rates[i] = i - j_start

            median_rate = np.median(tick_rates[tick_rates > 0]) if np.any(tick_rates > 0) else 1.0
            high_threshold = 2.0 * median_rate

            sweeps = _detect_sweeps(mid)

            for s in sweeps:
                idx = s["index"]
                is_high = tick_rates[idx] > high_threshold

                # 5s forward return
                target_ts = ts[idx] + _LATENCY_NS + 5_000_000_000
                j = idx + 1
                while j < n and ts[j] < target_ts:
                    j += 1
                if j < n and mid[idx] > 0:
                    fwd_ret = (mid[j] - mid[idx]) / mid[idx]
                    # Sign-adjust by sweep direction
                    adj_ret = fwd_ret * s["direction"]

                    if is_high:
                        high_rate_sweeps += 1
                        high_rate_fwd_rets.append(adj_ret)
                    else:
                        normal_rate_sweeps += 1
                        normal_rate_fwd_rets.append(adj_ret)

        high_arr = np.array(high_rate_fwd_rets) if high_rate_fwd_rets else np.array([])
        normal_arr = np.array(normal_rate_fwd_rets) if normal_rate_fwd_rets else np.array([])

        results[regime_name] = {
            "high_rate_sweeps": high_rate_sweeps,
            "normal_rate_sweeps": normal_rate_sweeps,
            "high_rate_mean_adj_ret_bps": round(float(np.mean(high_arr)) * 10000, 2) if len(high_arr) > 0 else None,
            "normal_rate_mean_adj_ret_bps": round(float(np.mean(normal_arr)) * 10000, 2) if len(normal_arr) > 0 else None,
            "high_rate_std_bps": round(float(np.std(high_arr)) * 10000, 2) if len(high_arr) > 0 else None,
            "normal_rate_std_bps": round(float(np.std(normal_arr)) * 10000, 2) if len(normal_arr) > 0 else None,
        }
        logger.info(f"task6_{regime_name}", **results[regime_name])

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Loading TMFD6 L1 data...")
    all_days = _load_all_l1()

    if not all_days:
        logger.error("No data found!")
        return

    logger.info("data_loaded", total_days=len(all_days))

    # Run all analyses
    r1 = task1_count_events(all_days)
    r2 = task2_sustained_flow(all_days)
    r3 = task3_ks_test(all_days)
    r5 = task5_zero_delta_capture(all_days)
    r6 = task6_tick_rate(all_days)

    # Aggregate results
    results = {
        "alpha_id": "r25_large_order_flow",
        "instrument": "TMFD6",
        "analysis_date": datetime.now(UTC).isoformat(),
        "data_days": len(all_days),
        "task1_event_counts": r1,
        "task2_sustained_flow": r2,
        "task3_ks_test": r3,
        "task4_regime_note": "All results above are stratified by jan_feb vs march (MR-7)",
        "task5_zero_delta_capture": r5,
        "task6_tick_rate": r6,
        "kill_gates": {
            "march_events_gte_50": r1.get("march_kill_gate", "UNKNOWN"),
            "march_ks_any_significant": r3.get("march_ks_kill_gate", "UNKNOWN"),
        },
    }

    # Overall verdict
    kills = [v for v in results["kill_gates"].values() if v == "KILL"]
    results["overall_verdict"] = "KILL" if kills else "PASS"

    # Save
    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _OUT_DIR / "stage3_results.json"
    out_path.write_text(json.dumps(results, indent=2, default=str) + "\n")
    logger.info("results_saved", path=str(out_path))

    # Print summary
    logger.info(
        "STAGE 3 VERDICT",
        overall=results["overall_verdict"],
        march_events_gate=results["kill_gates"]["march_events_gte_50"],
        march_ks_gate=results["kill_gates"]["march_ks_any_significant"],
    )


if __name__ == "__main__":
    main()
