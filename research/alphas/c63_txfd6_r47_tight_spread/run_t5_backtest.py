"""C63 R2/T5 backtest runner — CK-direct + bid/ask for TXFD6.

Mandates per team-lead T5 dispatch:
  1. Fresh CK-direct simulation (NOT linear ex-post re-price) — R1 lesson.
  2. Bid/ask execution (no mid-price).
  3. Sweep spread_threshold_pts in {3, 4, 5}; max_pos in {1, 2, 3}.
  4. Cost sensitivity: inst 1.5 + retail 3 + +/-30% band.
  5. Rebate decomp.
  6. C33 side-by-side at threshold=5 (same date range, isolated lever effect).
  7. Per-session-median-spread quartile split (Q1..Q4) for threshold=3
     to flag structural-break risk (regen rule 5).

Output JSON + scorecard mirror R1/T5 format.

Usage:
  CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
    uv run python -m research.alphas.c63_txfd6_r47_tight_spread.run_t5_backtest \\
    --days 20 --out outputs/team_artifacts/alpha-research/round-2/artifacts/
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
from research.alphas.c63_txfd6_r47_tight_spread.impl import (
    C63Params,
    TxfD6R47TightSpreadMaker,
    _TXF_INST_RT_COST_PTS,
    _TXF_POINT_VALUE_NTD,
)
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    ClickHouseSource,
    PostQuote,
    TickData,
)

logger = structlog.get_logger("c63.t5_backtest")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10
_COST_RT_SCENARIOS_PTS: tuple[float, ...] = (1.05, 1.50, 1.95, 3.00, 4.00)

# C33 PROMOTE OOS reference for side-by-side context
_C33_OOS_NTD_PER_DAY = 76920


def _simulate_day(
    strategy: object,
    events: list[TickData],
    queue_fraction: float,
) -> dict:
    """Run one day of CK-direct simulation with bid/ask fills.

    Mirrors R1/T5 pattern; tracks pos_before/pos_after/is_close/spread_pts.
    """
    buy_order: QueuePosition | None = None
    sell_order: QueuePosition | None = None
    position = 0
    fills: list[dict] = []
    position_path: list[tuple[int, int]] = []
    cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0
    session_spread_pts_samples: list[int] = []

    for event in events:
        if not event.is_trade:
            cur_bid = event.bid_price
            cur_ask = event.ask_price
            cur_bid_v = event.bid_qty
            cur_ask_v = event.ask_qty
            if cur_ask <= cur_bid:
                continue
            sp_pts = (cur_ask - cur_bid) // event.scale
            session_spread_pts_samples.append(int(sp_pts))

            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            actions = strategy.on_tick(event)
            for action in actions:
                if isinstance(action, PostQuote):
                    book_qty = cur_bid_v if action.side == "buy" else cur_ask_v
                    queue_ahead = max(1, int(book_qty * queue_fraction))
                    qp = QueuePosition(
                        side=action.side,
                        price=action.price,
                        queue_ahead=queue_ahead,
                    )
                    if action.side == "buy":
                        buy_order = qp
                    else:
                        sell_order = qp
                elif isinstance(action, CancelQuote):
                    if action.side == "buy":
                        buy_order = None
                    else:
                        sell_order = None
            position_path.append((event.exch_ts, abs(position)))
        else:
            mid = (cur_bid + cur_ask) / (2 * event.scale) if cur_bid > 0 else 0
            spread_pts = (
                (cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0
            )
            if buy_order is not None and event.trade_price <= buy_order.price:
                buy_order.queue_ahead -= event.trade_volume
                if buy_order.queue_ahead <= 0:
                    pos_before = position
                    position += 1
                    is_close = abs(position) < abs(pos_before)
                    fills.append({
                        "side": "buy",
                        "price": buy_order.price,
                        "mid": mid,
                        "spread_pts": int(spread_pts),
                        "pos_before": pos_before,
                        "pos_after": position,
                        "is_close": is_close,
                        "ts_ns": event.exch_ts,
                    })
                    strategy.on_fill("buy", buy_order.price, mid)
                    buy_order = None
            if sell_order is not None and event.trade_price >= sell_order.price:
                sell_order.queue_ahead -= event.trade_volume
                if sell_order.queue_ahead <= 0:
                    pos_before = position
                    position -= 1
                    is_close = abs(position) < abs(pos_before)
                    fills.append({
                        "side": "sell",
                        "price": sell_order.price,
                        "mid": mid,
                        "spread_pts": int(spread_pts),
                        "pos_before": pos_before,
                        "pos_after": position,
                        "is_close": is_close,
                        "ts_ns": event.exch_ts,
                    })
                    strategy.on_fill("sell", sell_order.price, mid)
                    sell_order = None

    # Session median spread (point-in-time samples)
    if session_spread_pts_samples:
        sorted_sp = sorted(session_spread_pts_samples)
        median_idx = len(sorted_sp) // 2
        session_median_sp = sorted_sp[median_idx]
    else:
        session_median_sp = 0

    return {
        "fills": fills,
        "position_path": position_path,
        "final_position": position,
        "session_median_spread_pts": int(session_median_sp),
    }


def _fifo_trips(fills: list[dict]) -> tuple[float, int, int]:
    """FIFO match fills -> (gross_pts, n_trips, n_wins)."""
    buy_q: list[dict] = []
    sell_q: list[dict] = []
    realized = 0.0
    trips = 0
    wins = 0
    for f in fills:
        price_pts = f["price"] / _SCALE
        if f["side"] == "buy":
            if sell_q:
                opening = sell_q.pop(0)
                pnl = opening["price"] / _SCALE - price_pts
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_q.append(f)
        else:
            if buy_q:
                opening = buy_q.pop(0)
                pnl = price_pts - opening["price"] / _SCALE
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_q.append(f)
    return realized, trips, wins


def _cost_sensitivity(
    gross_pts: float, n_fills: int
) -> dict:
    out = {}
    for rt in _COST_RT_SCENARIOS_PTS:
        fees_pts = n_fills * (rt / 2.0)
        net = gross_pts - fees_pts
        out[f"rt_{rt:.2f}_pt"] = {
            "rt_pts": rt,
            "fees_pts": round(fees_pts, 2),
            "net_pts": round(net, 2),
            "net_ntd": round(net * _TXF_POINT_VALUE_NTD, 0),
        }
    return out


def _rebate_decomp(gross_pts: float, n_fills: int) -> dict:
    rebate_ntd_per_rt = 2 * _MAKER_REBATE_NTD_PER_SIDE
    rebate_pts_per_rt = rebate_ntd_per_rt / _TXF_POINT_VALUE_NTD
    fees_pts = n_fills * (_TXF_INST_RT_COST_PTS / 2.0)
    rebate_pts_total = n_fills * (rebate_pts_per_rt / 2.0)
    return {
        "gross_pts": round(gross_pts, 2),
        "fees_inst_pts": round(fees_pts, 2),
        "rebate_pts_total": round(rebate_pts_total, 2),
        "net_pts_rebate_off": round(gross_pts - fees_pts, 2),
        "net_pts_rebate_on": round(gross_pts - fees_pts + rebate_pts_total, 2),
        "rebate_uplift_pts": round(rebate_pts_total, 2),
        "rebate_uplift_ntd": round(
            rebate_pts_total * _TXF_POINT_VALUE_NTD, 0
        ),
        "assumption_rebate_ntd_per_side": _MAKER_REBATE_NTD_PER_SIDE,
        "assumption_inst_rt_pts": _TXF_INST_RT_COST_PTS,
    }


def _run_c63_config(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    spread_threshold_pts: int,
    max_pos: int,
    queue_fraction: float = 1.0,
) -> dict:
    """Run C63 (TxfD6R47TightSpreadMaker) on given dates with given params."""
    params = C63Params(
        spread_threshold_pts=spread_threshold_pts,
        max_pos=max_pos,
    )
    strategy = TxfD6R47TightSpreadMaker(
        params=params, active_symbol=instrument
    )
    return _run_strategy(
        source, strategy, instrument, dates, queue_fraction,
        label=f"c63_sp{spread_threshold_pts}_mp{max_pos}",
    )


def _run_c33_config(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    max_pos: int = 3,
    queue_fraction: float = 1.0,
) -> dict:
    """Run C33 baseline (TxfD6SoloMaker, threshold=5) for side-by-side."""
    params = C33Params(spread_threshold_pts=5, max_pos=max_pos)
    strategy = TxfD6SoloMaker(params=params, active_symbol=instrument)
    return _run_strategy(
        source, strategy, instrument, dates, queue_fraction,
        label=f"c33_sp5_mp{max_pos}",
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

    for date in dates:
        events = source.load_day(instrument, date)
        if not events:
            logger.warning("no_events", date=date, label=label)
            continue
        day = _simulate_day(strategy, events, queue_fraction)
        day_gross, day_trips, day_wins = _fifo_trips(day["fills"])
        inst_fees_pts = len(day["fills"]) * (_TXF_INST_RT_COST_PTS / 2.0)
        inst_net_pts = day_gross - inst_fees_pts
        daily_results.append({
            "date": date,
            "n_events": len(events),
            "n_fills": len(day["fills"]),
            "n_trips": day_trips,
            "n_wins": day_wins,
            "gross_pts": round(day_gross, 2),
            "inst_net_pts": round(inst_net_pts, 2),
            "inst_net_ntd": round(inst_net_pts * _TXF_POINT_VALUE_NTD, 0),
            "session_median_sp_pts": day["session_median_spread_pts"],
            "final_position": day["final_position"],
        })
        all_fills.extend(day["fills"])

    gross_pts, total_trips, total_wins = _fifo_trips(all_fills)
    cost_sens = _cost_sensitivity(gross_pts, len(all_fills))
    rebate = _rebate_decomp(gross_pts, len(all_fills))
    close_rate = 100.0 if all_fills else 0.0  # engine only places limit orders

    n_days = len(daily_results)
    days_positive = sum(1 for d in daily_results if d["inst_net_pts"] > 0)

    return {
        "label": label,
        "fills_total": len(all_fills),
        "trips_total": total_trips,
        "wins_total": total_wins,
        "gross_pts_total": round(gross_pts, 2),
        "cost_sensitivity": cost_sens,
        "rebate_decomposition": rebate,
        "close_maker_rate_pct": close_rate,
        "n_days": n_days,
        "days_positive_inst": days_positive,
        "days_positive_inst_pct": (
            round(days_positive / n_days * 100, 1) if n_days else 0.0
        ),
        "daily": daily_results,
    }


def _session_spread_quartile_analysis(
    c63_run: dict,
) -> dict:
    """Split daily results by session-median-spread quartile, sum inst_net_ntd
    per quartile."""
    daily = c63_run.get("daily", [])
    if not daily:
        return {"n_quartiles": 0}
    # sort by session_median_sp_pts
    sorted_daily = sorted(daily, key=lambda d: d["session_median_sp_pts"])
    n = len(sorted_daily)
    q1_end = n // 4
    q2_end = n // 2
    q3_end = 3 * n // 4

    buckets = {
        "q1_lowest_spread": sorted_daily[:q1_end],
        "q2": sorted_daily[q1_end:q2_end],
        "q3": sorted_daily[q2_end:q3_end],
        "q4_highest_spread": sorted_daily[q3_end:],
    }
    result = {
        "n_days_total": n,
        "quartiles": {},
    }
    for qk, bucket in buckets.items():
        if not bucket:
            continue
        total_net = sum(d["inst_net_ntd"] for d in bucket)
        pos_days = sum(1 for d in bucket if d["inst_net_ntd"] > 0)
        median_sp = bucket[len(bucket) // 2]["session_median_sp_pts"]
        result["quartiles"][qk] = {
            "n_days": len(bucket),
            "median_sp_range": [
                bucket[0]["session_median_sp_pts"],
                bucket[-1]["session_median_sp_pts"],
            ],
            "bucket_median_sp_pts": median_sp,
            "total_net_ntd": total_net,
            "mean_net_ntd_per_day": round(total_net / len(bucket), 0),
            "days_positive": pos_days,
            "days_positive_pct": round(pos_days / len(bucket) * 100, 1),
            "dates": [d["date"] for d in bucket],
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="C63 T5 backtest")
    parser.add_argument("--instrument", default="TXFD6")
    parser.add_argument("--days", type=int, default=20)
    parser.add_argument(
        "--spread-sweep", default="3,4,5",
        help="Comma-separated spread thresholds to sweep (default: 3,4,5)",
    )
    parser.add_argument(
        "--max-pos-bracket", default="1,2,3",
        help="Comma-separated max_pos values (default: 1,2,3)",
    )
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-2/artifacts"),
    )
    parser.add_argument("--qf", type=float, default=1.0)
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error(
            "clickhouse_password_missing",
            hint="export CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2)",
        )
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    all_dates = source.available_dates(args.instrument)
    if len(all_dates) < args.days:
        logger.error(
            "insufficient_data",
            instrument=args.instrument,
            available_days=len(all_dates),
            requested=args.days,
        )
        return 3
    dates = all_dates[-args.days:]
    logger.info(
        "backtest_start",
        instrument=args.instrument,
        n_days=len(dates),
        date_range=(dates[0], dates[-1]),
        qf=args.qf,
    )

    spread_sweep = [int(x) for x in args.spread_sweep.split(",")]
    max_pos_bracket = [int(x) for x in args.max_pos_bracket.split(",")]

    # --- Main sweep: C63 across spread x max_pos grid ---
    c63_runs: dict[str, dict] = {}
    for sp in spread_sweep:
        for mp in max_pos_bracket:
            key = f"sp{sp}_mp{mp}"
            logger.info("run_start", label=f"c63_{key}")
            run = _run_c63_config(
                source, args.instrument, dates, sp, mp, args.qf
            )
            c63_runs[key] = run
            logger.info(
                "run_done",
                label=f"c63_{key}",
                fills=run["fills_total"],
                trips=run["trips_total"],
                gross_pts=run["gross_pts_total"],
                days_positive=run["days_positive_inst"],
                close_maker_rate=run["close_maker_rate_pct"],
            )

    # --- C33 side-by-side (threshold=5, mp=3) ---
    logger.info("c33_side_by_side_start")
    c33_sp5_mp3 = _run_c33_config(
        source, args.instrument, dates, max_pos=3, queue_fraction=args.qf
    )
    # Also run C33 at mp=1 (the current live config)
    c33_sp5_mp1 = _run_c33_config(
        source, args.instrument, dates, max_pos=1, queue_fraction=args.qf
    )
    logger.info(
        "c33_done",
        mp3_fills=c33_sp5_mp3["fills_total"],
        mp3_gross=c33_sp5_mp3["gross_pts_total"],
        mp1_fills=c33_sp5_mp1["fills_total"],
        mp1_gross=c33_sp5_mp1["gross_pts_total"],
    )

    # --- Per-session-median-spread quartile analysis (threshold=3, canonical mp=3) ---
    canonical_c63 = c63_runs.get("sp3_mp3", {})
    quartile = _session_spread_quartile_analysis(canonical_c63)

    # --- Isolate threshold effect: C63 sp=3 mp=3 vs C33 sp=5 mp=3 ---
    c63_canonical = c63_runs.get("sp3_mp3", {})
    threshold_effect = {
        "c63_sp3_mp3_ntd_per_day": (
            round(
                c63_canonical.get("cost_sensitivity", {})
                .get("rt_1.50_pt", {})
                .get("net_ntd", 0.0) / max(c63_canonical.get("n_days", 1), 1),
                0,
            )
        ),
        "c33_sp5_mp3_ntd_per_day": (
            round(
                c33_sp5_mp3["cost_sensitivity"]["rt_1.50_pt"]["net_ntd"]
                / max(c33_sp5_mp3["n_days"], 1),
                0,
            )
        ),
    }
    threshold_effect["delta_ntd_per_day"] = (
        threshold_effect["c63_sp3_mp3_ntd_per_day"]
        - threshold_effect["c33_sp5_mp3_ntd_per_day"]
    )
    threshold_effect["c63_over_c33_ratio"] = (
        round(
            threshold_effect["c63_sp3_mp3_ntd_per_day"]
            / max(threshold_effect["c33_sp5_mp3_ntd_per_day"], 1),
            3,
        )
    )

    summary = {
        "candidate": "C63",
        "instrument": args.instrument,
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "queue_fraction": args.qf,
        "c63_runs": c63_runs,
        "c33_side_by_side": {
            "sp5_mp3": c33_sp5_mp3,
            "sp5_mp1": c33_sp5_mp1,
            "c33_oos_reference_ntd_per_day": _C33_OOS_NTD_PER_DAY,
        },
        "threshold_effect_isolated": threshold_effect,
        "session_spread_quartile_analysis_canonical_c63": quartile,
        "cost_model_source": (
            "shared-context.yaml#cost_model.TXF (inst tier, ESTIMATED, "
            "confirmed=false)"
        ),
        "requires_broker_confirmation_before_live": True,
    }

    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
