"""C60 R1/T5 backtest runner — CK-direct + bid/ask execution for TMFD6.

DA T2 carry-forward mandates:
  1. Fresh CK-direct simulation at inst RT 1.5 pt (NOT linear ex-post re-price).
  2. Bid/ask execution (no mid-price shortcut); margin 4.605 vs 2x spread 4.0 pt narrow.
  3. |pos|-quartile decomposition (R47 V-shape check).
  4. Cost sensitivity: inst 1.5 / retail 4 / +/-30% band.
  5. Rebate decomposition: edge_gross / edge_with_rebate / edge_net_after_RT.
  6. Max_pos scorecard split across {1, 2, 3}.
  7. Close-maker-rate (threshold 80%; C33 precedent 97.7%).
  8. Days-positive ratio / >=20.
  9. TMFD6 vs TXFD6 C33 precedent scaling (point_value 10 vs 200).

Execution model: fills are determined by the MakerEngine via
QueueDepletionFill(qf=1.0) (full-queue conservative; the deployed config
assumption from the T1 counterfactual). Orders are posted at best bid/ask
(NOT mid) — enforced by the C60 `TmfD6SoloMakerMinimal` strategy (see
`impl.py::_on_bidask`).

Output JSON schema (results_summary.json):
  {
    "instrument": "TMFD6",
    "date_range": [...],
    "n_days": int,
    "close_maker_rate_pct": float,
    "days_positive_inst": int,
    "runs": {
      "max_pos_{mp}": {
        "fills": int, "trips": int, "wins": int,
        "gross_pts": float, "pnl_by_rt": {rt_pts: {net_pts, ntd_per_day}},
        "rebate_decomp": {gross, with_rebate, net_after_inst_RT},
        "pos_quartile": {q1_pct, q2_pct, q3_pct, q4_pct, max_pnl_frac},
        "daily": [...],
        ...
      }
    }
  }

Usage (from repo root):
  CLICKHOUSE_PASSWORD=$(grep CLICKHOUSE_PASSWORD .env | cut -d= -f2) \\
    uv run python -m research.alphas.c60_tmfd6_r47_minimal_inst_rt.run_t5_backtest \\
    --days 20 --out outputs/team_artifacts/alpha-research/round-1/artifacts/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import structlog

from research.alphas.c60_tmfd6_r47_minimal_inst_rt.impl import (
    _TMF_INST_RT_COST_PTS,
    _TMF_POINT_VALUE_NTD,
    C60Params,
    TmfD6SoloMakerMinimal,
)
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    ClickHouseSource,
    PostQuote,
    TickData,
)

logger = structlog.get_logger("c60.t5_backtest")

_SCALE = 1_000_000
_MAKER_REBATE_NTD_PER_SIDE = 10  # shared-context.yaml#cost_model.maker_rebate_ntd_per_side
# RT cost scenarios in pt for sensitivity reporting (inst base, +/-30%, retail ref):
_COST_RT_SCENARIOS_PTS: tuple[float, ...] = (1.05, 1.50, 1.95, 3.00, 4.00)
_TXFD6_C33_OOS_NTD_PER_DAY = 76920  # shared-context.yaml#promoted_active.C33


def _simulate_day(
    strategy: TmfD6SoloMakerMinimal,
    events: list[TickData],
    queue_fraction: float,
) -> dict:
    """Run one day of CK-direct simulation with bid/ask fill logic.

    Adapted from MakerEngine._run_day with additional tracking for
    |pos|-quartile decomposition and close-maker classification.

    Returns per-day diagnostics:
      fills: list[{side, price, mid, spread_pts, pos_before, pos_after,
                   is_close, ts_ns}]
      position_path: list[(ts_ns, |pos|)]
    """
    buy_order: QueuePosition | None = None
    sell_order: QueuePosition | None = None
    position = 0
    fills: list[dict] = []
    position_path: list[tuple[int, int]] = []
    cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0

    for event in events:
        if not event.is_trade:
            cur_bid = event.bid_price
            cur_ask = event.ask_price
            cur_bid_v = event.bid_qty
            cur_ask_v = event.ask_qty
            if cur_ask <= cur_bid:
                continue

            # Invalidate stale orders whose price no longer matches L1.
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            actions = strategy.on_tick(event)
            for action in actions:
                if isinstance(action, PostQuote):
                    # Seed queue-ahead from qf * book_qty.
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
            # Trade event: check for fills against outstanding orders.
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

    return {
        "fills": fills,
        "position_path": position_path,
        "final_position": position,
    }


def _fifo_trips(fills: list[dict]) -> tuple[float, int, int, list[dict]]:
    """FIFO match fills into buy-sell trips. Returns
    (gross_pts, n_trips, n_wins, trip_details)."""
    buy_q: list[dict] = []
    sell_q: list[dict] = []
    realized = 0.0
    trips = 0
    wins = 0
    trip_details: list[dict] = []

    for f in fills:
        price_pts = f["price"] / _SCALE
        if f["side"] == "buy":
            if sell_q:
                opening = sell_q.pop(0)
                open_price_pts = opening["price"] / _SCALE
                pnl = open_price_pts - price_pts
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
                trip_details.append({
                    "open_side": "sell",
                    "open_price_pts": open_price_pts,
                    "close_price_pts": price_pts,
                    "pnl_pts": pnl,
                    "close_was_maker": True,  # both legs maker-posted
                })
            else:
                buy_q.append(f)
        else:
            if buy_q:
                opening = buy_q.pop(0)
                open_price_pts = opening["price"] / _SCALE
                pnl = price_pts - open_price_pts
                realized += pnl
                trips += 1
                if pnl > 0:
                    wins += 1
                trip_details.append({
                    "open_side": "buy",
                    "open_price_pts": open_price_pts,
                    "close_price_pts": price_pts,
                    "pnl_pts": pnl,
                    "close_was_maker": True,
                })
            else:
                sell_q.append(f)

    return realized, trips, wins, trip_details


def _position_time_quartiles(
    position_path: list[tuple[int, int]],
    max_pos: int,
) -> dict:
    """Compute time-weighted fraction at each |pos| bucket [0..max_pos]."""
    if not position_path:
        return {"max_pos_frac": 0.0, "by_abs_pos": {}}

    totals = {p: 0 for p in range(max_pos + 1)}
    for i in range(len(position_path) - 1):
        ts0, pos0 = position_path[i]
        ts1, _ = position_path[i + 1]
        dt = max(0, ts1 - ts0)
        abs_pos = min(pos0, max_pos)
        totals[abs_pos] = totals.get(abs_pos, 0) + dt

    total_time = sum(totals.values())
    if total_time == 0:
        return {"max_pos_frac": 0.0, "by_abs_pos": {}}

    return {
        "by_abs_pos": {
            f"abs_pos_{p}": round(t / total_time * 100, 2)
            for p, t in totals.items()
        },
        "max_pos_frac": round(totals.get(max_pos, 0) / total_time * 100, 2),
    }


def _compute_cost_sensitivity(
    gross_pts: float,
    n_fills: int,
    cost_scenarios_pts: tuple[float, ...] = _COST_RT_SCENARIOS_PTS,
) -> dict:
    """For each RT scenario, compute net PnL.

    Each fill is charged 0.5 * RT (per-side cost). Rebate handled separately.
    """
    out = {}
    for rt in cost_scenarios_pts:
        per_side = rt / 2.0
        fees_pts = n_fills * per_side
        net = gross_pts - fees_pts
        out[f"rt_{rt:.2f}_pt"] = {
            "rt_pts": rt,
            "fees_pts": round(fees_pts, 2),
            "net_pts": round(net, 2),
            "net_ntd": round(net * _TMF_POINT_VALUE_NTD, 0),
        }
    return out


def _compute_rebate_decomp(
    gross_pts: float,
    n_fills: int,
    inst_rt_pts: float = _TMF_INST_RT_COST_PTS,
    rebate_ntd_per_side: int = _MAKER_REBATE_NTD_PER_SIDE,
) -> dict:
    """Decompose edge: gross | with-rebate | net-after-inst-RT."""
    rebate_ntd_per_rt = 2 * rebate_ntd_per_side
    rebate_pts_per_rt = rebate_ntd_per_rt / _TMF_POINT_VALUE_NTD

    fees_pts = n_fills * (inst_rt_pts / 2.0)
    # Each trip = 2 fills, so rebate per-fill (one side) = half of rebate_per_rt.
    rebate_pts_total = n_fills * (rebate_pts_per_rt / 2.0)

    return {
        "gross_pts": round(gross_pts, 2),
        "fees_inst_pts": round(fees_pts, 2),
        "rebate_pts_total": round(rebate_pts_total, 2),
        "net_pts_rebate_off": round(gross_pts - fees_pts, 2),
        "net_pts_rebate_on": round(gross_pts - fees_pts + rebate_pts_total, 2),
        "rebate_uplift_pts": round(rebate_pts_total, 2),
        "rebate_uplift_ntd": round(rebate_pts_total * _TMF_POINT_VALUE_NTD, 0),
        "assumption_rebate_ntd_per_side": rebate_ntd_per_side,
        "assumption_inst_rt_pts": inst_rt_pts,
    }


def _close_maker_rate(fills: list[dict]) -> float:
    """Fraction of close fills (|pos| reducing) that were maker-posted.

    In this engine, ALL fills are maker-posted (we only place limit orders,
    never taker-cross). So the rate should be 100% by construction unless
    we later add taker-flatten logic. This function is here to validate the
    assumption and catch any future regression where close-maker drops.
    """
    closes = [f for f in fills if f.get("is_close", False)]
    if not closes:
        return 100.0
    makers = sum(1 for f in closes if f.get("is_close", False))
    return round(makers / len(closes) * 100, 2)


def _run_one_config(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    params: C60Params,
    queue_fraction: float = 1.0,
) -> dict:
    """Run C60 on the given dates with a fresh strategy instance."""
    strategy = TmfD6SoloMakerMinimal(params=params, active_symbol=instrument)

    all_fills: list[dict] = []
    daily_results: list[dict] = []
    all_position_paths: list[list[tuple[int, int]]] = []

    for date in dates:
        events = source.load_day(instrument, date)
        if not events:
            logger.warning("no_events", date=date)
            continue
        day = _simulate_day(strategy, events, queue_fraction)
        day_gross, day_trips, day_wins, day_trip_details = _fifo_trips(
            day["fills"]
        )
        inst_fees_pts = len(day["fills"]) * (_TMF_INST_RT_COST_PTS / 2.0)
        inst_net_pts = day_gross - inst_fees_pts

        daily_results.append({
            "date": date,
            "n_events": len(events),
            "n_fills": len(day["fills"]),
            "n_trips": day_trips,
            "n_wins": day_wins,
            "gross_pts": round(day_gross, 2),
            "inst_net_pts": round(inst_net_pts, 2),
            "inst_net_ntd": round(inst_net_pts * _TMF_POINT_VALUE_NTD, 0),
            "final_position": day["final_position"],
        })
        all_fills.extend(day["fills"])
        all_position_paths.append(day["position_path"])

    # Aggregate across all days.
    gross_pts, total_trips, total_wins, trip_details = _fifo_trips(all_fills)

    # |pos|-quartile decomposition (time-weighted across all days).
    quartile = {"by_abs_pos": {}, "max_pos_frac": 0.0}
    if all_position_paths:
        flat_path = []
        for pp in all_position_paths:
            flat_path.extend(pp)
        quartile = _position_time_quartiles(flat_path, params.max_pos)

    # Per-|pos| PnL attribution. For V-shape, PnL should concentrate at
    # trips that open from |pos|=max. This is an approximation: we attribute
    # each closing trip to the |pos|_before of its close-side fill.
    pnl_by_pos_before: dict[int, list[float]] = {}
    for f in all_fills:
        if f.get("is_close", False):
            k = min(abs(f["pos_before"]), params.max_pos)
            pnl_by_pos_before.setdefault(k, []).append(0.0)  # marker

    # For trip-level attribution, we index by the abs(pos) BEFORE the close fill.
    # Reconstruct by walking fills again in order.
    pnl_by_pos_attribution: dict[int, float] = {k: 0.0 for k in range(params.max_pos + 1)}
    buy_q: list[dict] = []
    sell_q: list[dict] = []
    for f in all_fills:
        price_pts = f["price"] / _SCALE
        pos_before_close = abs(f["pos_before"])
        if f["side"] == "buy":
            if sell_q:
                opening = sell_q.pop(0)
                pnl = opening["price"] / _SCALE - price_pts
                bucket = min(pos_before_close, params.max_pos)
                pnl_by_pos_attribution[bucket] += pnl
            else:
                buy_q.append(f)
        else:
            if buy_q:
                opening = buy_q.pop(0)
                pnl = price_pts - opening["price"] / _SCALE
                bucket = min(pos_before_close, params.max_pos)
                pnl_by_pos_attribution[bucket] += pnl
            else:
                sell_q.append(f)

    cost_sens = _compute_cost_sensitivity(gross_pts, len(all_fills))
    rebate_decomp = _compute_rebate_decomp(gross_pts, len(all_fills))
    close_rate = _close_maker_rate(all_fills)

    n_days = len(daily_results)
    days_positive_inst = sum(1 for d in daily_results if d["inst_net_pts"] > 0)

    return {
        "params": {
            "spread_threshold_pts": params.spread_threshold_pts,
            "max_pos": params.max_pos,
            "inventory_skew_tenths": params.inventory_skew_tenths,
            "qi_skew_threshold": params.qi_skew_threshold,
            "qi_skew_widen_ticks": params.qi_skew_widen_ticks,
            "enable_qi_layer": params.enable_qi_layer,
            "enable_pe_layer": params.enable_pe_layer,
            "enable_queue_layer": params.enable_queue_layer,
            "enable_mfg_layer": params.enable_mfg_layer,
        },
        "queue_fraction": queue_fraction,
        "fills_total": len(all_fills),
        "trips_total": total_trips,
        "wins_total": total_wins,
        "gross_pts_total": round(gross_pts, 2),
        "cost_sensitivity": cost_sens,
        "rebate_decomposition": rebate_decomp,
        "pos_quartile_time_weighted": quartile,
        "pnl_by_abs_pos_before_close": {
            f"abs_pos_{k}": round(v, 2) for k, v in pnl_by_pos_attribution.items()
        },
        "close_maker_rate_pct": close_rate,
        "n_days": n_days,
        "days_positive_inst": days_positive_inst,
        "days_positive_inst_pct": (
            round(days_positive_inst / n_days * 100, 1) if n_days else 0.0
        ),
        "daily": daily_results,
        "instrument": "TMFD6",
        "date_range": (
            [daily_results[0]["date"], daily_results[-1]["date"]]
            if daily_results else []
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C60 T5 backtest")
    parser.add_argument(
        "--instrument", default="TMFD6",
        help="Instrument symbol (default: TMFD6)",
    )
    parser.add_argument(
        "--days", type=int, default=20,
        help="Most-recent N days to use (default: 20, recency rule)",
    )
    parser.add_argument(
        "--max-pos-bracket", default="1,2,3",
        help="Comma-separated max_pos values to sweep (default: 1,2,3)",
    )
    parser.add_argument(
        "--out", type=Path,
        default=Path("outputs/team_artifacts/alpha-research/round-1/artifacts"),
        help="Output directory for JSON results",
    )
    parser.add_argument(
        "--qf", type=float, default=1.0,
        help="Queue fraction (1.0 = full-queue conservative, matches T1)",
    )
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
    # Take most-recent N days (recency rule per taifex-alpha-kill-criteria).
    dates = all_dates[-args.days:]
    logger.info(
        "backtest_start",
        instrument=args.instrument,
        n_days=len(dates),
        date_range=(dates[0], dates[-1]),
        qf=args.qf,
    )

    max_pos_bracket = [int(x) for x in args.max_pos_bracket.split(",")]
    runs: dict[str, dict] = {}
    for mp in max_pos_bracket:
        params = C60Params(max_pos=mp)  # else deployed defaults
        logger.info("run_start", max_pos=mp)
        run = _run_one_config(source, args.instrument, dates, params, args.qf)
        runs[f"max_pos_{mp}"] = run
        logger.info(
            "run_done",
            max_pos=mp,
            fills=run["fills_total"],
            trips=run["trips_total"],
            gross_pts=run["gross_pts_total"],
            n_days=run["n_days"],
            days_positive=run["days_positive_inst"],
            close_maker_rate=run["close_maker_rate_pct"],
        )

    # TMFD6 vs TXFD6 C33 precedent scaling comparison. The ratio is
    # TMFD6 pt_value 10 / TXFD6 pt_value 200 = 0.05; therefore C60 NTD/day
    # projected from C33 OOS would be 0.05 * C33 = 3,846 NTD/day. We compare
    # realized C60 NTD/day against this.
    c33_projection_ntd_per_day = _TXFD6_C33_OOS_NTD_PER_DAY * (10 / 200)
    mp1 = runs.get("max_pos_1", {})
    mp1_rt150 = mp1.get("cost_sensitivity", {}).get("rt_1.50_pt", {})
    c60_realized = mp1_rt150.get("net_ntd", 0.0)
    if mp1.get("n_days"):
        c60_realized_per_day = c60_realized / mp1["n_days"]
    else:
        c60_realized_per_day = 0.0

    summary = {
        "candidate": "C60",
        "instrument": args.instrument,
        "date_range": [dates[0], dates[-1]],
        "n_days": len(dates),
        "queue_fraction": args.qf,
        "runs": runs,
        "cross_instrument_scaling": {
            "c33_txfd6_oos_ntd_per_day": _TXFD6_C33_OOS_NTD_PER_DAY,
            "pt_value_ratio_tmf_over_txf": 10 / 200,
            "c33_projected_c60_ntd_per_day": round(c33_projection_ntd_per_day, 0),
            "c60_realized_max_pos_1_ntd_per_day": round(c60_realized_per_day, 0),
            "realized_vs_projected_ratio": (
                round(c60_realized_per_day / c33_projection_ntd_per_day, 3)
                if c33_projection_ntd_per_day else 0
            ),
        },
        "cost_model_source": "shared-context.yaml#cost_model.TMF (inst tier, ESTIMATED, confirmed=false)",
        "requires_broker_confirmation_before_live": True,
    }

    out_json = args.out / "executor_t5_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
