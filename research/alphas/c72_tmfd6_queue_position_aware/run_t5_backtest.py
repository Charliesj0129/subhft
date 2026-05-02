"""C72 R5/T5 backtest runner — CK-direct + bid/ask for TMFD6 queue-depth overlay.

Mandates per team-lead T5 dispatch:
  - Fresh CK-direct + bid/ask execution
  - Queue-position threshold sweep {top1, top2, top3}
    -> CK-observable proxy per Researcher T1: use L1 depth thresholds
       {2, 5, 10} (approximating top1-/top2-/top3-tier density).
  - max_pos sweep {1, 2, 3}
  - +/-30% RT sensitivity
  - Dominance comparison vs C60 baseline (same date range, same max_pos,
    gate OFF) — per T1 dominance-risk carry-forward.

Output: outputs/team_artifacts/alpha-research/round-5/artifacts/executor_t5_*

Usage:
  CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
    uv run python -m research.alphas.c72_tmfd6_queue_position_aware.run_t5_backtest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import structlog

from research.alphas.c63_txfd6_r47_tight_spread.run_t5_backtest import (
    _fifo_trips,
    _simulate_day,
)
from research.alphas.c72_tmfd6_queue_position_aware.impl import (
    C72Params,
    TmfD6QueuePositionAwareMaker,
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
)
from research.backtest.maker_engine import ClickHouseSource

logger = structlog.get_logger("c72.t5_backtest")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10
_COST_RT_SCENARIOS_PTS: tuple[float, ...] = (1.05, 1.50, 1.95, 3.00, 4.00)
_DEPTH_THRESHOLDS: tuple[int, ...] = (2, 5, 10)  # top1/top2/top3 proxy


def _cost_sensitivity(gross_pts: float, n_fills: int) -> dict:
    out = {}
    for rt in _COST_RT_SCENARIOS_PTS:
        fees = n_fills * (rt / 2.0)
        net = gross_pts - fees
        out[f"rt_{rt:.2f}_pt"] = {
            "rt_pts": rt,
            "fees_pts": round(fees, 2),
            "net_pts": round(net, 2),
            "net_ntd": round(net * _TMF_POINT_VALUE_NTD, 0),
        }
    return out


def _rebate_decomp(gross_pts: float, n_fills: int) -> dict:
    rebate_per_rt = 2 * _MAKER_REBATE_NTD_PER_SIDE
    rebate_pts_per_rt = rebate_per_rt / _TMF_POINT_VALUE_NTD
    fees_pts = n_fills * (_TMF_INST_RT_COST_PTS / 2.0)
    rebate_pts_total = n_fills * (rebate_pts_per_rt / 2.0)
    return {
        "gross_pts": round(gross_pts, 2),
        "fees_inst_pts": round(fees_pts, 2),
        "rebate_pts_total": round(rebate_pts_total, 2),
        "net_pts_rebate_off": round(gross_pts - fees_pts, 2),
        "net_pts_rebate_on": round(gross_pts - fees_pts + rebate_pts_total, 2),
        "rebate_uplift_ntd": round(rebate_pts_total * _TMF_POINT_VALUE_NTD, 0),
    }


def _run_c72(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    depth_threshold: int,
    max_pos: int,
    queue_fraction: float = 1.0,
) -> dict:
    """Run C72 with queue-depth gate active at given threshold/max_pos."""
    strategy = TmfD6QueuePositionAwareMaker(
        params=C72Params(
            max_pos=max_pos,
            queue_depth_max_bid=depth_threshold,
            queue_depth_max_ask=depth_threshold,
            enable_queue_depth_gate=True,
        ),
        active_symbol=instrument,
    )
    return _run_strategy(
        source, strategy, instrument, dates, queue_fraction,
        label=f"c72_depth{depth_threshold}_mp{max_pos}",
    )


def _run_c72_gate_off_as_c60_baseline(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    max_pos: int,
    queue_fraction: float = 1.0,
) -> dict:
    """C72 with gate DISABLED == C60 baseline (per test_queue_depth_gate_disabled_behaves_like_c60).

    Running via C72 code path with gate=False guarantees apples-to-apples
    (identical spread gate, QI, skew, max_pos behaviour) vs gated C72 runs.
    """
    strategy = TmfD6QueuePositionAwareMaker(
        params=C72Params(
            max_pos=max_pos,
            enable_queue_depth_gate=False,
        ),
        active_symbol=instrument,
    )
    return _run_strategy(
        source, strategy, instrument, dates, queue_fraction,
        label=f"c60_baseline_mp{max_pos}",
    )


def _run_strategy(
    source: ClickHouseSource,
    strategy: object,
    instrument: str,
    dates: list[str],
    queue_fraction: float,
    label: str,
) -> dict:
    all_fills: list[dict] = []
    daily_results: list[dict] = []

    for d in dates:
        events = source.load_day(instrument, d)
        if not events:
            continue
        day = _simulate_day(strategy, events, queue_fraction)
        day_gross, day_trips, day_wins = _fifo_trips(day["fills"])
        inst_fees = len(day["fills"]) * (_TMF_INST_RT_COST_PTS / 2.0)
        inst_net = day_gross - inst_fees
        daily_results.append({
            "date": d,
            "n_fills": len(day["fills"]),
            "n_trips": day_trips,
            "n_wins": day_wins,
            "gross_pts": round(day_gross, 2),
            "inst_net_pts": round(inst_net, 2),
            "inst_net_ntd": round(inst_net * _TMF_POINT_VALUE_NTD, 0),
            "session_median_sp_pts": day["session_median_spread_pts"],
            "final_position": day["final_position"],
        })
        all_fills.extend(day["fills"])

    gross, trips, wins = _fifo_trips(all_fills)
    cost_sens = _cost_sensitivity(gross, len(all_fills))
    rebate = _rebate_decomp(gross, len(all_fills))
    n_days = len(daily_results)
    days_positive = sum(1 for d in daily_results if d["inst_net_pts"] > 0)
    close_rate = 100.0 if all_fills else 0.0

    return {
        "label": label,
        "fills_total": len(all_fills),
        "trips_total": trips,
        "wins_total": wins,
        "gross_pts_total": round(gross, 2),
        "cost_sensitivity": cost_sens,
        "rebate_decomposition": rebate,
        "close_maker_rate_pct": close_rate,
        "n_days": n_days,
        "days_positive_inst": days_positive,
        "daily": daily_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C72 T5 backtest")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--depth-sweep", default="2,5,10")
    parser.add_argument("--max-pos-bracket", default="1,2,3")
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-5/artifacts"),
    )
    parser.add_argument("--qf", type=float, default=1.0)
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error("clickhouse_password_missing")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()
    all_dates = source.available_dates("TMFD6")
    if len(all_dates) < args.days:
        logger.error(
            "insufficient_data", available=len(all_dates), need=args.days
        )
        return 3
    dates = all_dates[-args.days:]
    logger.info(
        "t5_start",
        instrument="TMFD6",
        n_days=len(dates),
        date_range=(dates[0], dates[-1]),
        qf=args.qf,
    )

    depths = [int(x) for x in args.depth_sweep.split(",")]
    bracket = [int(x) for x in args.max_pos_bracket.split(",")]

    # --- C72 main sweep ---
    c72_runs: dict[str, dict] = {}
    for depth in depths:
        for mp in bracket:
            key = f"depth{depth}_mp{mp}"
            logger.info("c72_run_start", key=key)
            run = _run_c72(source, "TMFD6", dates, depth, mp, args.qf)
            c72_runs[key] = run
            logger.info(
                "c72_run_done",
                key=key,
                fills=run["fills_total"],
                trips=run["trips_total"],
                gross=run["gross_pts_total"],
                days_positive=run["days_positive_inst"],
            )

    # --- C60 baseline (gate OFF) per-mp ---
    c60_baseline: dict[str, dict] = {}
    for mp in bracket:
        key = f"mp{mp}"
        logger.info("c60_baseline_start", key=key)
        run = _run_c72_gate_off_as_c60_baseline(
            source, "TMFD6", dates, mp, args.qf
        )
        c60_baseline[key] = run
        logger.info(
            "c60_baseline_done",
            key=key,
            fills=run["fills_total"],
            trips=run["trips_total"],
            gross=run["gross_pts_total"],
            days_positive=run["days_positive_inst"],
        )

    # --- Dominance analysis (per mp at inst RT 1.5) ---
    dominance = {}
    for mp in bracket:
        baseline = c60_baseline[f"mp{mp}"]
        baseline_ntd = baseline["cost_sensitivity"]["rt_1.50_pt"]["net_ntd"]
        baseline_per_day = baseline_ntd / max(baseline["n_days"], 1)
        mp_summary = {
            "c60_baseline_ntd_per_day": round(baseline_per_day, 0),
            "c72_by_depth": {},
        }
        for depth in depths:
            c72 = c72_runs[f"depth{depth}_mp{mp}"]
            c72_ntd = c72["cost_sensitivity"]["rt_1.50_pt"]["net_ntd"]
            c72_per_day = c72_ntd / max(c72["n_days"], 1)
            per_trip_c60 = (
                baseline["gross_pts_total"] / max(baseline["trips_total"], 1)
            )
            per_trip_c72 = (
                c72["gross_pts_total"] / max(c72["trips_total"], 1)
            )
            mp_summary["c72_by_depth"][f"depth{depth}"] = {
                "c72_ntd_per_day": round(c72_per_day, 0),
                "delta_vs_c60_ntd_per_day": round(
                    c72_per_day - baseline_per_day, 0
                ),
                "fill_retention_pct": (
                    round(
                        c72["fills_total"] / max(baseline["fills_total"], 1)
                        * 100, 1,
                    )
                ),
                "trip_retention_pct": (
                    round(
                        c72["trips_total"] / max(baseline["trips_total"], 1)
                        * 100, 1,
                    )
                ),
                "per_trip_edge_c60_pts": round(per_trip_c60, 3),
                "per_trip_edge_c72_pts": round(per_trip_c72, 3),
                "per_trip_uplift_pct": (
                    round(
                        (per_trip_c72 - per_trip_c60) / max(abs(per_trip_c60), 1e-9)
                        * 100, 1,
                    )
                ),
                "dominance_verdict": (
                    "C72 BEATS C60" if c72_per_day > baseline_per_day
                    else "C60 DOMINATES C72"
                ),
            }
        dominance[f"mp{mp}"] = mp_summary

    summary = {
        "candidate": "C72",
        "instrument": "TMFD6",
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "queue_fraction": args.qf,
        "c72_runs": c72_runs,
        "c60_baseline_gate_off": c60_baseline,
        "dominance_analysis": dominance,
        "cost_model_source": (
            "shared-context.yaml#cost_model.TMF (inst tier, ESTIMATED)"
        ),
        "requires_broker_confirmation_before_live": True,
        "t1_flags": {
            "dominance_risk_flagged": True,
            "observability_resolved_as_l1_depth_proxy": True,
            "lipton_pss_adverse_risk": (
                "Thin near-side queue may be adverse-selection signal; see "
                "per_trip_edge_c72_pts vs c60 for empirical check."
            ),
        },
    }

    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
