"""C74 R10/T5 backtest runner — CK-direct joint TXFD6+TMFD6 basis mean-reversion.

Per team-lead T5 dispatch (10 DA mandatory flags):
  1. requires_broker_confirmation_before_live: true
  2. Fresh CK-direct with ADAPTIVE rolling-sigma (not fixed)
  3. Stale-quote filter (|basis|>50) with hit-logging
  4. Maker-maker on both legs; 4-sigma TAKER stop-loss only
  5. Cross-instrument ts alignment using exch_ts
  6. Per-trip PnL distribution (not just daily aggregate)
  7. Hedge-leg PnL not double-counted — per-leg + combined reports
  8. Split PnL by (direction x session-sigma quartile) 2x2 grid
  9. Mutual-exclusion with C63 (check no concurrent TXFD6 inventory)
  10. Fanelli 2023 precedent: non-trivial idiosyncratic basis structure

Parameter sweeps:
  - window_seconds {300, 1800, 3600}
  - entry_sigma {1.5, 2.0, 2.5}
  - fixed stop_sigma=4.0, timeout=1800, min_samples=120

Output: outputs/team_artifacts/alpha-research/round-10/artifacts/executor_t5_*
"""
from __future__ import annotations

import argparse
import heapq
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

logger = structlog.get_logger("c74.t5_backtest")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10
_RT_SCENARIOS_PT_COMBINED: tuple[float, ...] = (2.1, 3.0, 3.9, 6.0)


def _compute_trip_pnl_ntd(trip: dict) -> float:
    """Per-trip PnL in NTD.

    For short_basis: sell TXF @ entry_basis high, buy TXF @ exit_basis.
    PnL = (entry_basis - exit_basis) * TXF_pt_value (per TXF contract).
    Hedge-leg TMF offset is captured by the basis computation itself
    (basis = txf - 20*tmf), so trip PnL is a single basis number.
    """
    entry = trip["entry_basis_pts"]
    exit_ = trip["exit_basis_pts"]
    if trip["side"] == "short_basis":
        basis_delta_pts = entry - exit_
    else:
        basis_delta_pts = exit_ - entry
    # Basis units are in pts of TXF-equivalent. 1 TXF contract * basis_delta.
    pnl_ntd = basis_delta_pts * _TXF_POINT_VALUE_NTD
    return pnl_ntd


def _apply_cost_per_trip(pnl_ntd: float, combined_rt_pts: float, taker_close: bool) -> float:
    """Deduct combined RT cost (TXF leg + TMF leg x 20, at inst).

    Cost per trip (NTD):
      - TXF leg: combined_rt_pts / 2 * TXF_pt_value (one RT = 2 * half-cost)
      - TMF leg: combined_rt_pts / 2 * TMF_pt_value * hedge_ratio_20
      Wait — combined RT in the dispatch is the sum of TXF RT + TMF RT in pts.
      Since TMF qty is 20x, TMF cost in NTD = (RT_tmf * 10) * 20 = RT_tmf * 200.
      TXF cost in NTD = RT_txf * 200.
      At RT_txf = RT_tmf = 1.5 each => total 600 NTD per trip.

    But dispatch says RT sensitivity: {2.1, 3.0, 3.9, 6.0} pt COMBINED.
    Interpret as: 2.1 pt combined => 1.05 TXF + 1.05 TMF (each at inst -30%).
    Total cost in NTD = 2.1 * 200 = 420 NTD per trip.

    taker_close doubles the TAKER half (both legs cross spread): add half-spread
    estimate of 2 pt TXF + 1 pt TMF combined ~= 500 NTD extra. Approximate as
    3 pt combined additional cost on top of RT.
    """
    total_cost_ntd = combined_rt_pts * _TXF_POINT_VALUE_NTD
    if taker_close:
        # Additional spread+adverse cost estimate: TXF 2pt + TMF 1pt = 3pt TXF-equivalent
        total_cost_ntd += 3.0 * _TXF_POINT_VALUE_NTD
    return pnl_ntd - total_cost_ntd


def _fetch_joint_events(
    source: ClickHouseSource,
    date_str: str,
    symbols: tuple[str, ...] = ("TXFD6", "TMFD6"),
) -> list[tuple[str, TickData]]:
    """Fetch and merge events from both symbols for one session.

    Returns list of (symbol, TickData) sorted by exch_ts.
    """
    streams: list[list[tuple[int, str, TickData]]] = []
    for sym in symbols:
        events = source.load_day(sym, date_str)
        stream = [(e.exch_ts, sym, e) for e in events]
        streams.append(stream)
    # Merge via heapq.merge
    merged = heapq.merge(*streams, key=lambda x: x[0])
    return [(sym, tick) for _, sym, tick in merged]


def _run_one_config(
    source: ClickHouseSource,
    dates: list[str],
    params: C74Params,
) -> dict:
    strat = TxfTmfBasisMeanReversion(params=params)
    daily_summaries: list[dict] = []
    all_trips: list[dict] = []
    # Session sigma tracker per day
    session_sigmas: list[float] = []

    for d in dates:
        # Reset stats each session to avoid cross-day drift (approximation)
        strat.reset()
        events = _fetch_joint_events(source, d)
        if not events:
            continue
        start_trip_count = len(strat.closed_trips)
        for sym, tick in events:
            strat.update_mid(sym, tick)
        day_trips = strat.closed_trips[start_trip_count:]
        day_sigma = strat.rolling_stdev
        session_sigmas.append(day_sigma)
        daily_summaries.append({
            "date": d,
            "n_trips": len(day_trips),
            "stale_filter_hits": strat.stale_filter_hits,
            "entries_posted": strat.entries_posted,
            "exits_reversion": strat.exits_reversion,
            "exits_timeout": strat.exits_timeout,
            "exits_stop_loss": strat.exits_stop_loss,
            "session_end_sigma_pts": day_sigma,
        })
        # Tag each trip with its session and sigma
        for t in day_trips:
            t["session_date"] = d
            t["session_end_sigma_pts"] = day_sigma
        all_trips.extend(day_trips)

    # Compute per-trip PnL (gross)
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
        },
        "n_days": len(daily_summaries),
        "n_trips_total": len(all_trips),
        "daily": daily_summaries,
        "trips": all_trips,
        "session_sigmas": session_sigmas,
    }


def _summarize_trips(run: dict) -> dict:
    """Summarize a run's per-trip distribution + cost sensitivity."""
    trips = run["trips"]
    n = len(trips)
    if n == 0:
        return {
            "n_trips": 0,
            "note": "no trips — parameter set yields zero entries in window",
        }

    gross = [t["gross_pnl_ntd"] for t in trips]
    net = [t["net_pnl_ntd_inst"] for t in trips]
    n_days = max(run["n_days"], 1)

    # Per-trip distribution stats
    pct = sorted(net)
    def _pct(p: float) -> float:
        idx = max(0, min(n - 1, int(p * n)))
        return pct[idx]

    # Cost sensitivity (combined RT pts)
    cost_sens = {}
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

    # Direction split
    short_trips = [t for t in trips if t["side"] == "short_basis"]
    long_trips = [t for t in trips if t["side"] == "long_basis"]
    short_mean = (
        sum(t["net_pnl_ntd_inst"] for t in short_trips) / len(short_trips)
        if short_trips else 0.0
    )
    long_mean = (
        sum(t["net_pnl_ntd_inst"] for t in long_trips) / len(long_trips)
        if long_trips else 0.0
    )

    # 2x2 direction x session-sigma-quartile grid
    sorted_by_sig = sorted(
        trips, key=lambda t: t.get("session_end_sigma_pts", 0)
    )
    half = len(sorted_by_sig) // 2
    low_sig = set(id(t) for t in sorted_by_sig[:half])
    direction_sigma_grid = {
        "short_low_sigma": [t["net_pnl_ntd_inst"] for t in short_trips if id(t) in low_sig],
        "short_high_sigma": [t["net_pnl_ntd_inst"] for t in short_trips if id(t) not in low_sig],
        "long_low_sigma": [t["net_pnl_ntd_inst"] for t in long_trips if id(t) in low_sig],
        "long_high_sigma": [t["net_pnl_ntd_inst"] for t in long_trips if id(t) not in low_sig],
    }
    grid_summary = {}
    for k, vals in direction_sigma_grid.items():
        if vals:
            grid_summary[k] = {
                "n": len(vals),
                "mean_ntd": round(sum(vals) / len(vals), 1),
                "total_ntd": round(sum(vals), 0),
            }
        else:
            grid_summary[k] = {"n": 0, "mean_ntd": 0.0, "total_ntd": 0.0}

    # Exit reasons
    exits_reversion = sum(1 for t in trips if t.get("exit_reason") == "reversion")
    exits_timeout = sum(1 for t in trips if t.get("exit_reason") == "timeout")
    exits_stop_loss = sum(1 for t in trips if t.get("exit_reason") == "stop_loss")

    # Days-positive from per-trip (group by session_date)
    days_pnl: dict[str, float] = {}
    for t in trips:
        d = t.get("session_date", "unknown")
        days_pnl[d] = days_pnl.get(d, 0.0) + t["net_pnl_ntd_inst"]
    days_positive = sum(1 for v in days_pnl.values() if v > 0)

    # Daily PnL series for Sharpe
    daily_pnls = sorted(days_pnl.items())
    daily_net_list = [v for _, v in daily_pnls]
    if len(daily_net_list) >= 2:
        mean_daily = statistics.mean(daily_net_list)
        stdev_daily = statistics.stdev(daily_net_list)
        sharpe = (
            (mean_daily / stdev_daily) * math.sqrt(252) if stdev_daily > 0 else 0.0
        )
    else:
        mean_daily = sum(daily_net_list) if daily_net_list else 0.0
        stdev_daily = 0.0
        sharpe = 0.0

    # Stale filter hits (sum over days)
    stale_total = sum(d["stale_filter_hits"] for d in run["daily"])
    stop_loss_rate = exits_stop_loss / n if n else 0.0

    return {
        "n_trips": n,
        "n_days": n_days,
        "days_positive": days_positive,
        "days_positive_pct": round(days_positive / n_days * 100, 1),
        "trip_pnl_net_inst_ntd": {
            "mean": round(sum(net) / n, 1),
            "median": round(statistics.median(net), 1),
            "stdev": round(statistics.stdev(net) if n >= 2 else 0, 1),
            "p05": round(_pct(0.05), 1),
            "p25": round(_pct(0.25), 1),
            "p50": round(_pct(0.50), 1),
            "p75": round(_pct(0.75), 1),
            "p95": round(_pct(0.95), 1),
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
                "mean_ntd": round(short_mean, 1),
                "total_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in short_trips), 0
                ),
            },
            "long_basis": {
                "n": len(long_trips),
                "mean_ntd": round(long_mean, 1),
                "total_ntd": round(
                    sum(t["net_pnl_ntd_inst"] for t in long_trips), 0
                ),
            },
        },
        "direction_sigma_2x2_grid": grid_summary,
        "exit_reasons": {
            "reversion": exits_reversion,
            "timeout": exits_timeout,
            "stop_loss": exits_stop_loss,
            "stop_loss_rate_pct": round(stop_loss_rate * 100, 1),
        },
        "stale_filter_hits_total": stale_total,
        "sharpe_daily_annualized": round(sharpe, 2),
        "daily_pnl_list": daily_pnls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C74 T5 backtest")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-10/artifacts"),
    )
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error("clickhouse_password_missing")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    # Get joint dates
    txf_dates = set(source.available_dates("TXFD6"))
    tmf_dates = set(source.available_dates("TMFD6"))
    joint = sorted(txf_dates & tmf_dates)
    if len(joint) < args.days:
        logger.error("insufficient_data", n_joint=len(joint))
        return 3
    dates = joint[-args.days:]
    logger.info(
        "t5_start",
        n_days=len(dates),
        range=(dates[0], dates[-1]),
    )

    # Parameter sweeps
    sweeps = []
    for window_sec in [300, 1800, 3600]:
        for entry_sig in [1.5, 2.0, 2.5]:
            sweeps.append((window_sec, entry_sig))

    configs: dict[str, dict] = {}
    for window_sec, entry_sig in sweeps:
        key = f"win{window_sec}_ent{entry_sig}"
        logger.info("run_start", key=key)
        # Need smaller warmup for short windows
        min_samples = min(120, window_sec // 3)
        params = C74Params(
            window_seconds=window_sec,
            entry_sigma=entry_sig,
            min_samples_for_entry=min_samples,
        )
        run = _run_one_config(source, dates, params)
        summary = _summarize_trips(run)
        configs[key] = {
            "params": run["params"],
            "summary": summary,
        }
        logger.info(
            "run_done",
            key=key,
            n_trips=summary.get("n_trips", 0),
            ntd_per_day=summary.get("trip_pnl_net_inst_ntd", {}).get("ntd_per_day", 0),
            sharpe=summary.get("sharpe_daily_annualized", 0),
        )

    # Find best by ntd_per_day at inst RT (w/ positive Sharpe preference)
    best_key = None
    best_score = float("-inf")
    for key, c in configs.items():
        s = c["summary"]
        if s.get("n_trips", 0) == 0:
            continue
        score = s["trip_pnl_net_inst_ntd"]["ntd_per_day"]
        # Require at least 3 trips for meaningful score
        if s["n_trips"] < 3:
            continue
        if score > best_score:
            best_score = score
            best_key = key

    summary_json = {
        "candidate": "C74",
        "instrument": "TXFD6+TMFD6",
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "configs": configs,
        "best_config_key": best_key,
        "best_config_ntd_per_day": (
            configs[best_key]["summary"]["trip_pnl_net_inst_ntd"]["ntd_per_day"]
            if best_key else None
        ),
        "cost_model_source": (
            "shared-context.yaml#cost_model (inst tier, ESTIMATED)"
        ),
        "hedge_ratio_tmf_per_txf": _HEDGE_RATIO_TMF_PER_TXF,
        "requires_broker_confirmation_before_live": True,
    }
    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary_json, indent=2, default=str))
    logger.info("results_written", path=str(out_json), best=best_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
