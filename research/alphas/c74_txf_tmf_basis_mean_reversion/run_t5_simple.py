"""Simpler C74 T5 runner — SINGLE config, fewer days, direct file output.

Avoids the 9-config sweep that kept crashing under memory load. Runs ONE
config (window=3600, entry_sigma=2.0, canonical T2-approved) on 10 days to
produce a core scorecard. Sweep can be run separately as follow-up.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path

import structlog

from research.alphas.c74_txf_tmf_basis_mean_reversion.run_t5_backtest import (
    _apply_cost_per_trip,
    _compute_trip_pnl_ntd,
    _RT_SCENARIOS_PT_COMBINED,
    _run_one_config,
)
from research.alphas.c74_txf_tmf_basis_mean_reversion.impl import (
    _HEDGE_RATIO_TMF_PER_TXF,
    _TMF_INST_RT_COST_PTS,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
    C74Params,
)
from research.backtest.maker_engine import ClickHouseSource

logger = structlog.get_logger("c74.t5_simple")


def main() -> int:
    if "CLICKHOUSE_PASSWORD" not in os.environ:
        print("ERROR: CLICKHOUSE_PASSWORD not set", file=sys.stderr)
        return 2

    out_dir = Path(
        "outputs/team_artifacts/alpha-research/round-10/artifacts"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    # Joint dates
    txf = set(source.available_dates("TXFD6"))
    tmf = set(source.available_dates("TMFD6"))
    joint = sorted(txf & tmf)
    # Take 10 most-recent days (manageable load)
    dates = joint[-10:]
    print(f"Running C74 T5 on {len(dates)} days: {dates[0]}..{dates[-1]}",
          flush=True)

    params = C74Params(
        window_seconds=3600,
        entry_sigma=2.0,
        stop_sigma=4.0,
        timeout_seconds=1800,
        min_samples_for_entry=120,
    )
    run = _run_one_config(source, dates, params)
    trips = run["trips"]
    n_trips = len(trips)
    n_days = run["n_days"]

    # Per-trip PnL at inst RT
    for t in trips:
        t["gross_pnl_ntd"] = _compute_trip_pnl_ntd(t)
        t["net_pnl_ntd_inst"] = _apply_cost_per_trip(
            t["gross_pnl_ntd"],
            combined_rt_pts=(_TXF_INST_RT_COST_PTS + _TMF_INST_RT_COST_PTS),
            taker_close=t.get("taker_close", False),
        )

    print(f"Trips: {n_trips} over {n_days} days", flush=True)

    if n_trips == 0:
        summary_json = {
            "candidate": "C74",
            "n_days": n_days,
            "n_trips": 0,
            "note": "No trips — 60-min window never produced 2-sigma deviation entries on 10 days.",
            "stale_filter_hits_total": sum(
                d["stale_filter_hits"] for d in run["daily"]
            ),
            "daily": run["daily"],
        }
        (out_dir / "executor_t5_results.json").write_text(
            json.dumps(summary_json, indent=2, default=str)
        )
        print("Zero trips — wrote results_json", flush=True)
        return 0

    gross = [t["gross_pnl_ntd"] for t in trips]
    net = [t["net_pnl_ntd_inst"] for t in trips]
    pct = sorted(net)

    def _p(p: float) -> float:
        idx = max(0, min(n_trips - 1, int(p * n_trips)))
        return pct[idx]

    # Cost sensitivity
    cost_sens = {}
    for rt in _RT_SCENARIOS_PT_COMBINED:
        nets = [
            _apply_cost_per_trip(
                t["gross_pnl_ntd"], rt, t.get("taker_close", False)
            )
            for t in trips
        ]
        cost_sens[f"rt_{rt:.1f}pt"] = {
            "total_ntd": round(sum(nets), 0),
            "ntd_per_day": round(sum(nets) / n_days, 0),
            "mean_per_trip": round(sum(nets) / n_trips, 1),
        }

    # Direction split
    short_trips = [t for t in trips if t["side"] == "short_basis"]
    long_trips = [t for t in trips if t["side"] == "long_basis"]

    # Exit reason counts
    exits_reversion = sum(1 for t in trips if t.get("exit_reason") == "reversion")
    exits_timeout = sum(1 for t in trips if t.get("exit_reason") == "timeout")
    exits_stop_loss = sum(1 for t in trips if t.get("exit_reason") == "stop_loss")

    # Daily PnL for Sharpe
    days_pnl: dict[str, float] = {}
    for t in trips:
        d = t.get("session_date", "unknown")
        days_pnl[d] = days_pnl.get(d, 0.0) + t["net_pnl_ntd_inst"]
    days_positive = sum(1 for v in days_pnl.values() if v > 0)
    daily_net = list(days_pnl.values())
    if len(daily_net) >= 2:
        mean_daily = statistics.mean(daily_net)
        stdev_daily = statistics.stdev(daily_net)
        sharpe = (mean_daily / stdev_daily) * (252 ** 0.5) if stdev_daily > 0 else 0.0
    else:
        mean_daily = sum(daily_net) if daily_net else 0.0
        stdev_daily = 0.0
        sharpe = 0.0

    summary_json = {
        "candidate": "C74",
        "instrument": "TXFD6+TMFD6",
        "n_days": n_days,
        "date_range": [dates[0], dates[-1]],
        "params": run["params"],
        "n_trips": n_trips,
        "days_positive": days_positive,
        "days_positive_pct": round(days_positive / n_days * 100, 1),
        "hedge_ratio_tmf_per_txf": _HEDGE_RATIO_TMF_PER_TXF,
        "point_value_txf_ntd": _TXF_POINT_VALUE_NTD,
        "stale_filter_hits_total": sum(
            d["stale_filter_hits"] for d in run["daily"]
        ),
        "trip_pnl_net_inst_ntd": {
            "mean": round(sum(net) / n_trips, 1),
            "median": round(statistics.median(net), 1),
            "stdev": round(statistics.stdev(net), 1) if n_trips >= 2 else 0,
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
            "mean": round(sum(gross) / n_trips, 1),
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
        "exit_reasons": {
            "reversion": exits_reversion,
            "timeout": exits_timeout,
            "stop_loss": exits_stop_loss,
            "stop_loss_rate_pct": round(exits_stop_loss / n_trips * 100, 1),
        },
        "daily": run["daily"],
        "sharpe_daily_annualized": round(sharpe, 2),
        "mean_daily_ntd": round(mean_daily, 0),
        "stdev_daily_ntd": round(stdev_daily, 0),
        "cost_model_source": (
            "shared-context.yaml#cost_model (inst tier, ESTIMATED)"
        ),
        "requires_broker_confirmation_before_live": True,
    }

    (out_dir / "executor_t5_results.json").write_text(
        json.dumps(summary_json, indent=2, default=str)
    )
    print(f"DONE trips={n_trips} days+={days_positive}/{n_days} "
          f"ntd/day_inst={summary_json['trip_pnl_net_inst_ntd']['ntd_per_day']:.0f}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
