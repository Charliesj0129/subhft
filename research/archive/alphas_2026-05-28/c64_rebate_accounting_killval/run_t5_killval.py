"""C64 R3/T5 kill-validation runner.

Validates Researcher T1 SELF-KILL arithmetic:
  - Runs C33 baseline (TxfD6SoloMaker, spread=5, R47-minimal) on 20 most-recent
    TXFD6 days at mp={1,2,3}.
  - Computes rebate-off baseline (R47-minimal economics) and rebate-on uplift
    at rebate rates {0, 5, 10, 15 NTD/side}.
  - Cost sensitivity at inst RT 1.5 pt +/-30% + retail 3 pt.
  - NOT a new strategy — C64 was dimensionally-dead per T1 self-kill.

Output: outputs/team_artifacts/alpha-research/round-3/artifacts/executor_t5_results.json

Usage:
  CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
    uv run python -m research.alphas.c64_rebate_accounting_killval.run_t5_killval \\
    --days 20
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import structlog

from research.alphas.c33_txfd6_solo_passive_maker.impl import (
    C33Params,
    TxfD6SoloMaker,
)
from research.alphas.c63_txfd6_r47_tight_spread.run_t5_backtest import (
    _fifo_trips,
    _simulate_day,
)
from research.backtest.maker_engine import ClickHouseSource

logger = structlog.get_logger("c64.t5_killval")

_SCALE = 1_000_000
_TXF_POINT_VALUE_NTD = 200
_TXF_INST_RT_COST_PTS = 1.5
_COST_RT_SCENARIOS_PTS: tuple[float, ...] = (1.05, 1.50, 1.95, 3.00, 4.00)
_REBATE_RATES_NTD_PER_SIDE: tuple[int, ...] = (0, 5, 10, 15)


def _cost_sensitivity(gross_pts: float, n_fills: int) -> dict:
    out = {}
    for rt in _COST_RT_SCENARIOS_PTS:
        fees = n_fills * (rt / 2.0)
        net = gross_pts - fees
        out[f"rt_{rt:.2f}_pt"] = {
            "rt_pts": rt,
            "fees_pts": round(fees, 2),
            "net_pts": round(net, 2),
            "net_ntd": round(net * _TXF_POINT_VALUE_NTD, 0),
        }
    return out


def _rebate_sweep(
    gross_pts: float, n_fills: int, inst_rt_pts: float = _TXF_INST_RT_COST_PTS
) -> dict:
    """For each rebate rate, compute net pts and NTD uplift vs rebate=0."""
    fees_pts = n_fills * (inst_rt_pts / 2.0)
    net_pts_rebate_off = gross_pts - fees_pts
    out = {}
    for rate in _REBATE_RATES_NTD_PER_SIDE:
        # rebate per fill (one side) = rate / point_value NTD-per-pt = pts per fill
        rebate_pts_per_fill = rate / _TXF_POINT_VALUE_NTD
        total_rebate_pts = n_fills * rebate_pts_per_fill
        net_pts = net_pts_rebate_off + total_rebate_pts
        out[f"rebate_{rate}_ntd_per_side"] = {
            "rate_ntd_per_side": rate,
            "rebate_pts_per_fill": round(rebate_pts_per_fill, 6),
            "total_rebate_pts": round(total_rebate_pts, 2),
            "total_rebate_ntd": round(total_rebate_pts * _TXF_POINT_VALUE_NTD, 0),
            "net_pts": round(net_pts, 2),
            "net_ntd": round(net_pts * _TXF_POINT_VALUE_NTD, 0),
            "uplift_vs_rebate_off_ntd": round(
                total_rebate_pts * _TXF_POINT_VALUE_NTD, 0
            ),
        }
    return {
        "net_pts_rebate_off": round(net_pts_rebate_off, 2),
        "net_ntd_rebate_off": round(net_pts_rebate_off * _TXF_POINT_VALUE_NTD, 0),
        "by_rate": out,
    }


def _run_c33(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    max_pos: int,
    queue_fraction: float = 1.0,
) -> dict:
    strategy = TxfD6SoloMaker(
        params=C33Params(spread_threshold_pts=5, max_pos=max_pos),
        active_symbol=instrument,
    )
    all_fills: list[dict] = []
    daily_results: list[dict] = []

    for date in dates:
        events = source.load_day(instrument, date)
        if not events:
            continue
        day = _simulate_day(strategy, events, queue_fraction)
        day_gross, day_trips, day_wins = _fifo_trips(day["fills"])
        inst_fees = len(day["fills"]) * (_TXF_INST_RT_COST_PTS / 2.0)
        inst_net = day_gross - inst_fees
        daily_results.append({
            "date": date,
            "n_fills": len(day["fills"]),
            "n_trips": day_trips,
            "n_wins": day_wins,
            "gross_pts": round(day_gross, 2),
            "inst_net_pts": round(inst_net, 2),
            "inst_net_ntd": round(inst_net * _TXF_POINT_VALUE_NTD, 0),
        })
        all_fills.extend(day["fills"])

    gross_pts, trips, wins = _fifo_trips(all_fills)
    cost_sens = _cost_sensitivity(gross_pts, len(all_fills))
    rebate_sweep = _rebate_sweep(gross_pts, len(all_fills))
    n_days = len(daily_results)
    days_positive = sum(1 for d in daily_results if d["inst_net_pts"] > 0)

    return {
        "mp": max_pos,
        "fills_total": len(all_fills),
        "trips_total": trips,
        "wins_total": wins,
        "gross_pts_total": round(gross_pts, 2),
        "cost_sensitivity": cost_sens,
        "rebate_sweep_at_inst_rt": rebate_sweep,
        "n_days": n_days,
        "days_positive_inst": days_positive,
        "daily": daily_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C64 T5 kill-validation runner")
    parser.add_argument("--instrument", default="TXFD6")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument("--max-pos-bracket", default="1,2,3")
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-3/artifacts"),
    )
    parser.add_argument("--qf", type=float, default=1.0)
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error("clickhouse_password_missing")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    all_dates = source.available_dates(args.instrument)
    if len(all_dates) < args.days:
        logger.error(
            "insufficient_data", available=len(all_dates), need=args.days
        )
        return 3
    dates = all_dates[-args.days:]
    logger.info(
        "killval_start",
        instrument=args.instrument,
        n_days=len(dates),
        range=(dates[0], dates[-1]),
    )

    bracket = [int(x) for x in args.max_pos_bracket.split(",")]
    runs: dict[str, dict] = {}
    for mp in bracket:
        logger.info("run_start", mp=mp)
        run = _run_c33(source, args.instrument, dates, mp, args.qf)
        runs[f"mp_{mp}"] = run
        logger.info(
            "run_done",
            mp=mp,
            fills=run["fills_total"],
            gross=run["gross_pts_total"],
            days_positive=run["days_positive_inst"],
        )

    # Dimensional summary per T1 self-kill claim
    t1_claim_validation = {
        "t1_claim_rebate_pt_per_side_on_txf": 10 / _TXF_POINT_VALUE_NTD,
        "t1_claim_rebate_pt_per_rt_on_txf": 2 * (10 / _TXF_POINT_VALUE_NTD),
        "t1_claim_tmf_comparison_pt_per_rt": 2 * (10 / 10),
        "magnitude_ratio_txf_to_tmf": (
            (2 * 10 / _TXF_POINT_VALUE_NTD) / (2 * 10 / 10)
        ),
        "adverse_selection_pt_assumed": 1.6,  # from hft-mm-design SKILL
        "rebate_over_adverse_ratio_txf": (2 * 10 / _TXF_POINT_VALUE_NTD) / 1.6,
        "interpretation": (
            "Rebate/adverse on TXFD6 = 0.0625. Rebate cannot tip any "
            "spread-choice gradient; no physical decision axis."
        ),
    }

    summary = {
        "candidate": "C64",
        "instrument": args.instrument,
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "queue_fraction": args.qf,
        "kill_status": "SELF_KILL_T1 (Researcher; see round-3/summary.md)",
        "purpose": (
            "Validate T1 arithmetic: (a) rebate uplift on C33 existing fills "
            "is +2-27K NTD/day accounting only; (b) separate C64 strategy is "
            "not warranted — layer is dimensionally-dead on TXFD6."
        ),
        "t1_dimensional_claims_validated": t1_claim_validation,
        "c33_rebate_accounting_sweep": runs,
        "cost_model_source": (
            "shared-context.yaml#cost_model.TXF (inst tier, ESTIMATED)"
        ),
        "requires_broker_confirmation_before_live": True,
    }

    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
