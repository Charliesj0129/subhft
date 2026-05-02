"""C74 R10/T5 per-minute resampled backtest (Option B per team-lead).

Rationale for per-minute resample (vs tick-sync gate):
  - Researcher OU validation used per-minute resampled basis
  - DA T2 stale-filter 50pt was calibrated to per-minute distribution
  - Matches the entire approved analysis pipeline
  - Tick-sync 100ms gate would be novel mechanism outside DA T2 spec

Mechanism:
  1. Resample TXFD6 + TMFD6 L1 mids to 1-minute buckets (LAST-observed-mid
     within each minute). Align bucket boundaries on both instruments.
  2. Compute basis_t = mid_txf - 20 * mid_tmf at minute granularity.
  3. Feed minute basis samples to existing TxfTmfBasisMeanReversion via
     synthetic ticks at minute boundaries. Rolling-60min sigma now operates
     on 60 minute-samples (not 60 minutes of raw ticks).
  4. Entry/exit fire on minute boundaries.
  5. Cross-instrument exch_ts alignment via minute bucketing.

Parameter sweeps: window_minutes x entry_sigma (matching dispatch spec
translated from seconds to minutes: {5, 30, 60} min; {1.5, 2.0, 2.5}).

Output: outputs/team_artifacts/alpha-research/round-10/artifacts/
        executor_t5_resampled_results.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
from pathlib import Path

import structlog

from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import (
    _HEDGE_RATIO_TMF_PER_TXF,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    C74Params,
    TxfTmfBasisMeanReversion,
)
from research.backtest.maker_engine import ClickHouseSource, TickData

logger = structlog.get_logger("c74.t5_resampled")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10
_RT_SCENARIOS_PT_COMBINED: tuple[float, ...] = (2.1, 3.0, 3.9, 6.0)
_MINUTE_NS = 60 * 1_000_000_000


def _resample_to_minutes(
    source: ClickHouseSource,
    date_str: str,
) -> list[tuple[int, str, TickData]]:
    """Build per-minute ticks by taking LAST L1 mid within each minute bucket.

    Returns list of (exch_ts_ns_at_minute, symbol, TickData) pairs sorted
    by minute timestamp, with both instruments represented per minute
    (when available).
    """
    txf_events = source.load_day("TXFD6", date_str)
    tmf_events = source.load_day("TMFD6", date_str)

    def _bucket_last(
        events: list[TickData], scale: int
    ) -> dict[int, TickData]:
        """Map minute_bucket_ns -> LAST TickData in that bucket."""
        by_minute: dict[int, TickData] = {}
        for e in events:
            if e.is_trade:
                continue
            if e.bid_price <= 0 or e.ask_price <= 0:
                continue
            if e.ask_price <= e.bid_price:
                continue
            bucket = (e.exch_ts // _MINUTE_NS) * _MINUTE_NS
            by_minute[bucket] = e
        return by_minute

    txf_by_min = _bucket_last(txf_events, _SCALE)
    tmf_by_min = _bucket_last(tmf_events, _SCALE)

    # Only emit minutes where BOTH sides have data (clean pair).
    joint_minutes = sorted(set(txf_by_min.keys()) & set(tmf_by_min.keys()))
    merged: list[tuple[int, str, TickData]] = []
    for m in joint_minutes:
        # Emit TXF first, then TMF; both at same minute exch_ts for alignment
        # (use the bucket boundary ts, which is deterministic).
        txf_tick = txf_by_min[m]
        tmf_tick = tmf_by_min[m]
        # Reconstruct TickData with bucket-aligned exch_ts
        aligned_txf = TickData(
            exch_ts=m,
            bid_price=txf_tick.bid_price,
            ask_price=txf_tick.ask_price,
            bid_qty=txf_tick.bid_qty,
            ask_qty=txf_tick.ask_qty,
            trade_price=0,
            trade_volume=0,
            is_trade=False,
            scale=txf_tick.scale,
        )
        aligned_tmf = TickData(
            exch_ts=m + 1,  # +1 ns so update order deterministic
            bid_price=tmf_tick.bid_price,
            ask_price=tmf_tick.ask_price,
            bid_qty=tmf_tick.bid_qty,
            ask_qty=tmf_tick.ask_qty,
            trade_price=0,
            trade_volume=0,
            is_trade=False,
            scale=tmf_tick.scale,
        )
        merged.append((m, "TXFD6", aligned_txf))
        merged.append((m + 1, "TMFD6", aligned_tmf))
    return merged


def _compute_trip_pnl_ntd(trip: dict) -> float:
    entry = trip["entry_basis_pts"]
    exit_ = trip["exit_basis_pts"]
    if trip["side"] == "short_basis":
        basis_delta_pts = entry - exit_
    else:
        basis_delta_pts = exit_ - entry
    return basis_delta_pts * _TXF_POINT_VALUE_NTD


def _apply_cost_per_trip(
    pnl_ntd: float, combined_rt_pts: float, taker_close: bool
) -> float:
    total_cost_ntd = combined_rt_pts * _TXF_POINT_VALUE_NTD
    if taker_close:
        total_cost_ntd += 3.0 * _TXF_POINT_VALUE_NTD
    return pnl_ntd - total_cost_ntd


def _run_one_config_resampled(
    source: ClickHouseSource,
    dates: list[str],
    params: C74Params,
) -> dict:
    strat = TxfTmfBasisMeanReversion(params=params)
    daily_summaries: list[dict] = []
    all_trips: list[dict] = []

    for d in dates:
        strat.reset()
        events = _resample_to_minutes(source, d)
        if not events:
            continue
        start_trip_count = len(strat.closed_trips)
        for _minute_ns, sym, tick in events:
            strat.update_mid(sym, tick)
        day_trips = strat.closed_trips[start_trip_count:]
        day_sigma = strat.rolling_stdev
        for t in day_trips:
            t["session_date"] = d
            t["session_end_sigma_pts"] = day_sigma
        daily_summaries.append({
            "date": d,
            "n_minutes_joint": len(events) // 2,
            "n_trips": len(day_trips),
            "stale_filter_hits": strat.stale_filter_hits,
            "entries_posted": strat.entries_posted,
            "exits_reversion": strat.exits_reversion,
            "exits_timeout": strat.exits_timeout,
            "exits_stop_loss": strat.exits_stop_loss,
            "session_end_sigma_pts": day_sigma,
        })
        all_trips.extend(day_trips)

    # Per-trip PnL
    for t in all_trips:
        t["gross_pnl_ntd"] = _compute_trip_pnl_ntd(t)
        t["net_pnl_ntd_inst"] = _apply_cost_per_trip(
            t["gross_pnl_ntd"],
            combined_rt_pts=(_TXF_INST_RT_COST_PTS + _TMF_INST_RT_COST_PTS),
            taker_close=t.get("taker_close", False),
        )
    return {
        "params": {
            "window_seconds": params.window_seconds,
            "entry_sigma": params.entry_sigma,
            "stop_sigma": params.stop_sigma,
            "timeout_seconds": params.timeout_seconds,
            "min_samples_for_entry": params.min_samples_for_entry,
        },
        "n_days": len(daily_summaries),
        "n_trips_total": len(all_trips),
        "daily": daily_summaries,
        "trips": all_trips,
    }


def _summarize(run: dict) -> dict:
    trips = run["trips"]
    n = len(trips)
    n_days = max(run["n_days"], 1)
    stale_total = sum(d["stale_filter_hits"] for d in run["daily"])
    if n == 0:
        return {
            "n_trips": 0,
            "n_days": n_days,
            "stale_filter_hits_total": stale_total,
            "daily": run["daily"],
        }
    gross = [t["gross_pnl_ntd"] for t in trips]
    net = [t["net_pnl_ntd_inst"] for t in trips]
    pct = sorted(net)

    def _p(p: float) -> float:
        idx = max(0, min(n - 1, int(p * n)))
        return pct[idx]

    cost_sens: dict[str, dict] = {}
    for rt in _RT_SCENARIOS_PT_COMBINED:
        scenario_nets = [
            _apply_cost_per_trip(
                t["gross_pnl_ntd"], rt, t.get("taker_close", False)
            )
            for t in trips
        ]
        total = sum(scenario_nets)
        cost_sens[f"rt_{rt:.1f}pt"] = {
            "total_ntd": round(total, 0),
            "ntd_per_day": round(total / n_days, 0),
            "mean_per_trip": round(total / n, 1),
        }

    short_trips = [t for t in trips if t["side"] == "short_basis"]
    long_trips = [t for t in trips if t["side"] == "long_basis"]

    # 2x2 direction x sigma-quartile grid
    sorted_by_sig = sorted(trips, key=lambda t: t.get("session_end_sigma_pts", 0))
    half = len(sorted_by_sig) // 2
    low_sig_ids = {id(t) for t in sorted_by_sig[:half]}
    grid: dict[str, dict] = {}
    for key, subset in (
        ("short_low_sigma", [t for t in short_trips if id(t) in low_sig_ids]),
        ("short_high_sigma", [t for t in short_trips if id(t) not in low_sig_ids]),
        ("long_low_sigma", [t for t in long_trips if id(t) in low_sig_ids]),
        ("long_high_sigma", [t for t in long_trips if id(t) not in low_sig_ids]),
    ):
        if subset:
            grid[key] = {
                "n": len(subset),
                "mean_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in subset) / len(subset), 1
                ),
                "total_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in subset), 0
                ),
            }
        else:
            grid[key] = {"n": 0, "mean_ntd": 0.0, "total_ntd": 0.0}

    exits_reversion = sum(1 for t in trips if t.get("exit_reason") == "reversion")
    exits_timeout = sum(1 for t in trips if t.get("exit_reason") == "timeout")
    exits_stop_loss = sum(1 for t in trips if t.get("exit_reason") == "stop_loss")

    days_pnl: dict[str, float] = {}
    for t in trips:
        d = t.get("session_date", "unknown")
        days_pnl[d] = days_pnl.get(d, 0.0) + t["net_pnl_ntd_inst"]
    days_positive = sum(1 for v in days_pnl.values() if v > 0)
    daily_list = [v for _, v in sorted(days_pnl.items())]
    if len(daily_list) >= 2:
        mean_daily = statistics.mean(daily_list)
        stdev_daily = statistics.stdev(daily_list)
        sharpe = (
            (mean_daily / stdev_daily) * math.sqrt(252)
            if stdev_daily > 0 else 0.0
        )
    else:
        mean_daily = sum(daily_list) if daily_list else 0.0
        stdev_daily = 0.0
        sharpe = 0.0

    return {
        "n_trips": n,
        "n_days": n_days,
        "days_positive": days_positive,
        "days_positive_pct": round(days_positive / n_days * 100, 1),
        "stale_filter_hits_total": stale_total,
        "trip_pnl_net_inst_ntd": {
            "mean": round(sum(net) / n, 1),
            "median": round(statistics.median(net), 1),
            "stdev": round(statistics.stdev(net) if n >= 2 else 0, 1),
            "p05": round(_p(0.05), 1),
            "p25": round(_p(0.25), 1),
            "p50": round(_p(0.50), 1),
            "p75": round(_p(0.75), 1),
            "p95": round(_p(0.95), 1),
            "min": round(min(net), 1),
            "max": round(max(net), 1),
            "total_ntd": round(sum(net), 0),
            "ntd_per_day": round(sum(net) / n_days, 0),
        },
        "trip_pnl_gross_ntd": {
            "mean": round(sum(gross) / n, 1),
            "total_ntd": round(sum(gross), 0),
        },
        "cost_sensitivity": cost_sens,
        "direction_split": {
            "short_basis": {
                "n": len(short_trips),
                "mean_ntd": (
                    round(
                        sum(t["net_pnl_ntd_inst"] for t in short_trips)
                        / len(short_trips), 1,
                    )
                    if short_trips else 0.0
                ),
                "total_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in short_trips), 0
                ),
            },
            "long_basis": {
                "n": len(long_trips),
                "mean_ntd": (
                    round(
                        sum(t["net_pnl_ntd_inst"] for t in long_trips)
                        / len(long_trips), 1,
                    )
                    if long_trips else 0.0
                ),
                "total_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in long_trips), 0
                ),
            },
        },
        "direction_sigma_2x2_grid": grid,
        "exit_reasons": {
            "reversion": exits_reversion,
            "timeout": exits_timeout,
            "stop_loss": exits_stop_loss,
            "stop_loss_rate_pct": round(exits_stop_loss / n * 100, 1),
        },
        "sharpe_daily_annualized": round(sharpe, 2),
        "mean_daily_ntd": round(mean_daily, 0),
        "stdev_daily_ntd": round(stdev_daily, 0),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C74 T5 per-minute resampled")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-10/artifacts"),
    )
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        print("ERROR: CLICKHOUSE_PASSWORD not set", file=sys.stderr)
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()
    txf = set(source.available_dates("TXFD6"))
    tmf = set(source.available_dates("TMFD6"))
    joint = sorted(txf & tmf)
    if len(joint) < args.days:
        print(f"ERROR: only {len(joint)} joint days; need {args.days}",
              file=sys.stderr)
        return 3
    dates = joint[-args.days:]
    print(f"C74 T5 resampled: {len(dates)} days {dates[0]}..{dates[-1]}",
          flush=True)

    # Parameter sweeps (minute-scale)
    # window_minutes {5, 30, 60}, entry_sigma {1.5, 2.0, 2.5}
    sweeps = [
        (window_min, entry_sig)
        for window_min in (5, 30, 60)
        for entry_sig in (1.5, 2.0, 2.5)
    ]
    configs: dict[str, dict] = {}
    for window_min, entry_sig in sweeps:
        key = f"win{window_min}m_ent{entry_sig}"
        # min_samples scaled to window (but capped reasonable)
        min_samples = min(window_min, 30)
        params = C74Params(
            window_seconds=window_min * 60,
            entry_sigma=entry_sig,
            stop_sigma=4.0,
            timeout_seconds=1800,   # 30 min
            min_samples_for_entry=min_samples,
        )
        print(f"  config {key}...", flush=True)
        run = _run_one_config_resampled(source, dates, params)
        summary = _summarize(run)
        configs[key] = {"params": run["params"], "summary": summary}
        print(f"    n_trips={summary.get('n_trips', 0)} "
              f"ntd/day={summary.get('trip_pnl_net_inst_ntd', {}).get('ntd_per_day', 0)} "
              f"sharpe={summary.get('sharpe_daily_annualized', 0)}",
              flush=True)

    # Best config at inst RT 1.5 (combined 3.0 pt scenario)
    best_key = None
    best_score = float("-inf")
    for key, c in configs.items():
        s = c["summary"]
        n_trips = s.get("n_trips", 0)
        if n_trips < 3:
            continue
        score = s["trip_pnl_net_inst_ntd"]["ntd_per_day"]
        if score > best_score:
            best_score = score
            best_key = key

    summary_json = {
        "candidate": "C74",
        "instrument": "TXFD6+TMFD6",
        "mode": "per_minute_resampled",
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "hedge_ratio_tmf_per_txf": _HEDGE_RATIO_TMF_PER_TXF,
        "configs": configs,
        "best_config_key": best_key,
        "best_config_ntd_per_day": (
            configs[best_key]["summary"]["trip_pnl_net_inst_ntd"]["ntd_per_day"]
            if best_key else None
        ),
        "cost_model_source": (
            "shared-context.yaml#cost_model (inst tier, ESTIMATED)"
        ),
        "requires_broker_confirmation_before_live": True,
    }
    out_json = args.out / "executor_t5_resampled_results.json"
    out_json.write_text(json.dumps(summary_json, indent=2, default=str))
    print(f"DONE best={best_key} ntd/day={best_score if best_key else 'N/A'}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
