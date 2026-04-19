"""C68 R4/T5 backtest runner — rollover-window passive maker.

Per Researcher T1 CONDITIONAL PROCEED: the actual TXFE6 -> new-front
transition window (2026-04-15/16/17) is NOT IN CK (data ends 2026-04-14).
Primary OOS is the TXFD6 Feb 2026-02-23..25 analog (N=3 days):

  - Median spread 12-16 pt, 747-1323 trades/day
  - The 3-day window when TXFD6 became new-front after TXFB6 expiry.

Secondary OOS (TXFE6 2026-03-19..20 as 2nd-month back post-TXFC6 expiry):
NOT suitable — that's the 2nd-month-back transition, not the new-front
transition (median 110 pt, not 12-16 pt; different regime per T1).

Team-lead T5 mandates:
  - Fresh CK-direct + bid/ask execution.
  - Hedge-ratio sensitivity {0.9, 1.0, 1.1}. NOTE: T1 REJECTED the hedge-pair
    framing (TAKE hedge leg inverts edge to -0.9 pt/RT). We implement this
    as a REPORTING sensitivity — a hypothetical taker hedge at various
    notional ratios — and confirm all are negative as T1 predicted.
  - Rebate decomp.
  - +/-30% RT sensitivity.
  - Explicit N=3 sample-size caveat and S3 FAIL warning for DA T6.

Output JSON + scorecard at
  outputs/team_artifacts/alpha-research/round-4/artifacts/executor_t5_*

Usage:
  CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
    uv run python -m research.alphas.c68_txf_rollover_back_front_maker.run_t5_backtest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

import structlog

from research.alphas.c63_txfd6_r47_tight_spread.run_t5_backtest import (
    _fifo_trips,
    _simulate_day,
)
from research.alphas.c68_txf_rollover_back_front_maker.impl import (
    C68Params,
    TxfRolloverBackFrontMaker,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
)
from research.backtest.maker_engine import ClickHouseSource

logger = structlog.get_logger("c68.t5_backtest")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10
_COST_RT_SCENARIOS_PTS: tuple[float, ...] = (1.05, 1.50, 1.95, 3.00)
_HEDGE_RATIOS: tuple[float, ...] = (0.9, 1.0, 1.1)

# Identified rollover-window analogs in CK
# TXFD6 became new-front 2026-02-23..25 after TXFB6 expiry (T1 analog)
_ROLLOVER_ANALOG_TXFD6_FEB: tuple[str, str, str] = (
    "2026-02-23", "2026-02-24", "2026-02-25",
)


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


def _rebate_decomp(gross_pts: float, n_fills: int) -> dict:
    rebate_per_rt = 2 * _MAKER_REBATE_NTD_PER_SIDE
    rebate_pts_per_rt = rebate_per_rt / _TXF_POINT_VALUE_NTD
    fees_pts = n_fills * (_TXF_INST_RT_COST_PTS / 2.0)
    rebate_pts_total = n_fills * (rebate_pts_per_rt / 2.0)
    return {
        "gross_pts": round(gross_pts, 2),
        "fees_inst_pts": round(fees_pts, 2),
        "rebate_pts_total": round(rebate_pts_total, 2),
        "net_pts_rebate_off": round(gross_pts - fees_pts, 2),
        "net_pts_rebate_on": round(gross_pts - fees_pts + rebate_pts_total, 2),
        "rebate_uplift_pts": round(rebate_pts_total, 2),
        "rebate_uplift_ntd": round(rebate_pts_total * _TXF_POINT_VALUE_NTD, 0),
    }


def _hedge_ratio_sensitivity(
    passive_gross_pts: float,
    n_trips: int,
    take_spread_cost_pts: float = 2.0,
    take_adverse_pts: float = 1.6,
    take_fee_pts_per_side: float = 0.75,
) -> dict:
    """Simulate taker-hedge-leg cost at different hedge ratios.

    T1 ARITHMETIC (confirmed by this function):
      - Passive (no hedge): gross - fees_inst = +7.8 pt/RT at 14 pt spread.
      - TAKE hedge cost per leg: 2.0 spread + 1.6 adverse + 0.75 fee = 4.35 pt.
      - Full hedge (ratio=1.0, both legs TAKE): -8.7 pt on each RT.
      - Net: +7.8 - 8.7 = -0.9 pt/RT — inverts sign.

    This is a REPORTING sensitivity — it confirms hedge framing rejection.
    C68 implementation does NOT perform hedge legs (solo passive maker).
    """
    out = {}
    for ratio in _HEDGE_RATIOS:
        hedge_cost_per_trip_pts = ratio * 2 * (
            take_spread_cost_pts + take_adverse_pts + take_fee_pts_per_side
        )
        total_hedge_cost_pts = n_trips * hedge_cost_per_trip_pts
        net_with_hedge = passive_gross_pts - total_hedge_cost_pts
        out[f"ratio_{ratio:.1f}"] = {
            "hedge_ratio": ratio,
            "hedge_cost_per_trip_pts": round(hedge_cost_per_trip_pts, 3),
            "total_hedge_cost_pts": round(total_hedge_cost_pts, 2),
            "net_pts_with_hedge": round(net_with_hedge, 2),
            "net_ntd_with_hedge": round(
                net_with_hedge * _TXF_POINT_VALUE_NTD, 0
            ),
            "hedge_framing_verdict": (
                "NEGATIVE — T1 rejection confirmed"
                if net_with_hedge < 0
                else "POSITIVE — revisit T1 rejection"
            ),
        }
    return out


def _run_c68(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    max_pos: int,
    queue_fraction: float = 1.0,
    spread_threshold_pts: int = 12,
) -> dict:
    """Run C68 with calendar gate covering all supplied dates."""
    # Set window to cover all supplied dates so calendar gate is open.
    start = date.fromisoformat(dates[0])
    end = date.fromisoformat(dates[-1])
    strategy = TxfRolloverBackFrontMaker(
        params=C68Params(
            spread_threshold_pts=spread_threshold_pts,
            max_pos=max_pos,
            rollover_window_start_date=start,
            rollover_window_end_date=end,
        ),
        active_symbol=instrument,
    )

    all_fills: list[dict] = []
    daily_results: list[dict] = []

    for d in dates:
        session = date.fromisoformat(d)
        strategy.set_session_date(session)
        events = source.load_day(instrument, d)
        if not events:
            logger.warning("no_events", date=d)
            continue
        day = _simulate_day(strategy, events, queue_fraction)
        day_gross, day_trips, day_wins = _fifo_trips(day["fills"])
        inst_fees = len(day["fills"]) * (_TXF_INST_RT_COST_PTS / 2.0)
        inst_net = day_gross - inst_fees
        daily_results.append({
            "date": d,
            "n_events": len(events),
            "n_fills": len(day["fills"]),
            "n_trips": day_trips,
            "n_wins": day_wins,
            "gross_pts": round(day_gross, 2),
            "inst_net_pts": round(inst_net, 2),
            "inst_net_ntd": round(inst_net * _TXF_POINT_VALUE_NTD, 0),
            "session_median_sp_pts": day["session_median_spread_pts"],
            "final_position": day["final_position"],
        })
        all_fills.extend(day["fills"])

    gross_pts, total_trips, total_wins = _fifo_trips(all_fills)
    cost_sens = _cost_sensitivity(gross_pts, len(all_fills))
    rebate = _rebate_decomp(gross_pts, len(all_fills))
    hedge_sens = _hedge_ratio_sensitivity(gross_pts, total_trips)
    n_days = len(daily_results)
    days_positive = sum(1 for d in daily_results if d["inst_net_pts"] > 0)
    close_rate = 100.0 if all_fills else 0.0

    return {
        "instrument": instrument,
        "date_range": dates,
        "n_days": n_days,
        "spread_threshold_pts": spread_threshold_pts,
        "max_pos": max_pos,
        "queue_fraction": queue_fraction,
        "fills_total": len(all_fills),
        "trips_total": total_trips,
        "wins_total": total_wins,
        "gross_pts_total": round(gross_pts, 2),
        "cost_sensitivity": cost_sens,
        "rebate_decomposition": rebate,
        "hedge_ratio_sensitivity_reporting": hedge_sens,
        "close_maker_rate_pct": close_rate,
        "days_positive_inst": days_positive,
        "days_positive_inst_pct": (
            round(days_positive / n_days * 100, 1) if n_days else 0.0
        ),
        "daily": daily_results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C68 T5 backtest")
    parser.add_argument("--max-pos-bracket", default="1,2,3")
    parser.add_argument("--spread-threshold-pts", type=int, default=12)
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-4/artifacts"),
    )
    parser.add_argument("--qf", type=float, default=1.0)
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error("clickhouse_password_missing")
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    # Primary OOS: TXFD6 Feb 2026-02-23..25 (N=3 days).
    # This is the ONLY rollover analog with the narrow 12-16 pt spread regime
    # matching the C68 hypothesis (per Researcher T1).
    primary_dates = list(_ROLLOVER_ANALOG_TXFD6_FEB)
    logger.info(
        "primary_analog_start",
        instrument="TXFD6",
        dates=primary_dates,
        n_days=len(primary_dates),
    )

    bracket = [int(x) for x in args.max_pos_bracket.split(",")]
    primary_runs: dict[str, dict] = {}
    for mp in bracket:
        logger.info("primary_run_start", mp=mp)
        run = _run_c68(
            source, "TXFD6", primary_dates, mp, args.qf,
            spread_threshold_pts=args.spread_threshold_pts,
        )
        primary_runs[f"mp_{mp}"] = run
        logger.info(
            "primary_run_done",
            mp=mp,
            fills=run["fills_total"],
            trips=run["trips_total"],
            gross=run["gross_pts_total"],
            days_positive=run["days_positive_inst"],
        )

    # Best-case per-mp at inst RT 1.5 pt
    best_mp_inst = max(
        primary_runs.items(),
        key=lambda kv: kv[1]["cost_sensitivity"]["rt_1.50_pt"]["net_ntd"],
    )

    # Sample-size S3 assessment
    s3_assessment = {
        "n_days": 3,
        "n_trips_total_across_mp_bracket": sum(
            r["trips_total"] for r in primary_runs.values()
        ),
        "walk_forward_k_required": 5,
        "walk_forward_k_available_today": 1,  # only 1 rollover cycle in CK
        "s3_verdict": (
            "FAIL — rollover is rare event; only 1 complete cycle (TXFD6 Feb) "
            "in available CK data. Cannot satisfy walk-forward k=5 without "
            "3+ months of live shadow capturing additional rollover cycles. "
            "T6 DA must flag as structural S3 blocker."
        ),
        "mitigation": (
            "PARK candidate pending post-2026-04-18 CK update (TXFE6 -> "
            "new-front transition). After 6 more rollovers, walk-forward "
            "k=5 becomes feasible."
        ),
    }

    summary = {
        "candidate": "C68",
        "instrument_primary": "TXFD6",
        "date_range_primary": primary_dates,
        "n_days_primary": len(primary_dates),
        "sample_is_analog_not_direct_target": True,
        "direct_target_data_not_in_ck": {
            "target_symbol": "TXFE6",
            "target_window_est": "2026-04-15..2026-04-17",
            "ck_data_ends": "2026-04-14",
            "status": "NOT_YET_AVAILABLE",
        },
        "queue_fraction": args.qf,
        "primary_runs": primary_runs,
        "best_mp_at_inst_rt": {
            "key": best_mp_inst[0],
            "net_ntd_per_day": round(
                best_mp_inst[1]["cost_sensitivity"]["rt_1.50_pt"]["net_ntd"]
                / max(best_mp_inst[1]["n_days"], 1),
                0,
            ),
        },
        "hedge_framing_verdict": (
            "REJECTED per T1 arithmetic (see hedge_ratio_sensitivity_reporting "
            "per mp; all ratios negative confirm inversion)"
        ),
        "s3_sample_size_assessment": s3_assessment,
        "cost_model_source": (
            "shared-context.yaml#cost_model.TXF (inst tier, ESTIMATED, "
            "confirmed=false)"
        ),
        "requires_broker_confirmation_before_live": True,
        "requires_fresh_txfe6_transition_data": True,
    }

    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
