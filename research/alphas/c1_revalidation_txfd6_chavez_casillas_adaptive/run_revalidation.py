"""C1 re-validation backtest runner — TXFD6 Chávez-Casillas adaptive maker.

Mandates:
  1. CK-direct simulation (queue-depletion fill model with bid/ask execution).
  2. Latency profile r47_maker_shioaji_p95_v2026-04-24_measured (place 395ms,
     cancel 59ms, asymmetric 6.7x).
  3. Sweep max_pos in {1, 3} (per T1 risk #4 fills_distinct_days_min concern).
  4. Gate C survivor criteria mandatory:
       max_day_concentration_pct <= 25
       winning_days_min >= 5
       jackknife (leave-one-out by day) no sign flip
       bootstrap CI on Sharpe excludes 0
       fills_distinct_days_min >= 5
  5. Profile-conditionality caveat in output JSON.

Usage:
  CLICKHOUSE_PASSWORD=changeme uv run python -m \\
    research.alphas.c1_revalidation_txfd6_chavez_casillas_adaptive.run_revalidation
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import structlog

from research.alphas.c1_revalidation_txfd6_chavez_casillas_adaptive.impl import (
    C1ChavezCasillasAdaptiveMaker,
    C1Params,
    _TXF_POINT_VALUE_NTD,
    _TXF_RT_COST_PTS,
)
from research.backtest.fill_models import QueuePosition
from research.backtest.maker_engine import (
    CancelQuote,
    ClickHouseSource,
    PostQuote,
    TickData,
)

logger = structlog.get_logger("c1.revalidation")

_SCALE = 1_000_000

# Latency profile (r47_maker_shioaji_p95_v2026-04-24_measured)
_LATENCY_PLACE_NS = 395_000_000   # P95 quote-activation 395 ms
_LATENCY_CANCEL_NS = 59_000_000   # P95 cancel ack 59 ms

# F2_maker per-fill wedge (per shared-context.yaml inherited_kill_walls F2)
_F2_MAKER_WEDGE_PTS = 7.3


def _simulate_day(
    strategy: C1ChavezCasillasAdaptiveMaker,
    events: list[TickData],
    queue_fraction: float,
    place_ns: int,
    cancel_ns: int,
) -> dict:
    """Run one day with latency-faithful pending-action queue.

    Tracks pos_before/pos_after/is_close/spread_pts for FIFO matching and
    Gate C calculations.
    """
    buy_order: QueuePosition | None = None
    sell_order: QueuePosition | None = None
    position = 0
    fills: list[dict] = []
    cur_bid = cur_ask = cur_bid_v = cur_ask_v = 0
    session_spread_pts: list[int] = []

    pending: list[tuple[int, str, QueuePosition | None]] = []

    def _drain_pending(now_ts: int) -> None:
        nonlocal buy_order, sell_order, pending
        keep: list[tuple[int, str, QueuePosition | None]] = []
        for ts, op, payload in pending:
            if ts <= now_ts:
                if op == "place_buy":
                    buy_order = payload
                elif op == "place_sell":
                    sell_order = payload
                elif op == "cancel_buy":
                    buy_order = None
                elif op == "cancel_sell":
                    sell_order = None
            else:
                keep.append((ts, op, payload))
        pending = keep

    for event in events:
        _drain_pending(event.exch_ts)

        if not event.is_trade:
            cur_bid = event.bid_price
            cur_ask = event.ask_price
            cur_bid_v = event.bid_qty
            cur_ask_v = event.ask_qty
            if cur_ask <= cur_bid:
                continue
            sp_pts = (cur_ask - cur_bid) // event.scale
            session_spread_pts.append(int(sp_pts))

            # Quote moved away from posted price -> assume order missed.
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            actions = strategy.on_tick(event)
            for action in actions:
                if isinstance(action, PostQuote):
                    book_qty = (
                        cur_bid_v if action.side == "buy" else cur_ask_v
                    )
                    queue_ahead = max(1, int(book_qty * queue_fraction))
                    qp = QueuePosition(
                        side=action.side,
                        price=action.price,
                        queue_ahead=queue_ahead,
                    )
                    op = (
                        "place_buy" if action.side == "buy" else "place_sell"
                    )
                    if place_ns == 0:
                        if action.side == "buy":
                            buy_order = qp
                        else:
                            sell_order = qp
                    else:
                        pending.append(
                            (event.exch_ts + place_ns, op, qp)
                        )
                elif isinstance(action, CancelQuote):
                    op = (
                        "cancel_buy"
                        if action.side == "buy"
                        else "cancel_sell"
                    )
                    if cancel_ns == 0:
                        if action.side == "buy":
                            buy_order = None
                        else:
                            sell_order = None
                    else:
                        pending.append(
                            (event.exch_ts + cancel_ns, op, None)
                        )
        else:
            mid = (
                (cur_bid + cur_ask) / (2 * event.scale) if cur_bid > 0 else 0
            )
            spread_pts = (
                (cur_ask - cur_bid) // event.scale if cur_bid > 0 else 0
            )

            if buy_order is not None and event.trade_price <= buy_order.price:
                buy_order.queue_ahead -= event.trade_volume
                if buy_order.queue_ahead <= 0:
                    pos_before = position
                    position += 1
                    fills.append({
                        "side": "buy",
                        "price": buy_order.price,
                        "mid": mid,
                        "spread_pts": int(spread_pts),
                        "pos_before": pos_before,
                        "pos_after": position,
                        "is_close": abs(position) < abs(pos_before),
                        "ts_ns": event.exch_ts,
                    })
                    strategy.on_fill("buy", buy_order.price, mid)
                    buy_order = None

            if sell_order is not None and event.trade_price >= sell_order.price:
                sell_order.queue_ahead -= event.trade_volume
                if sell_order.queue_ahead <= 0:
                    pos_before = position
                    position -= 1
                    fills.append({
                        "side": "sell",
                        "price": sell_order.price,
                        "mid": mid,
                        "spread_pts": int(spread_pts),
                        "pos_before": pos_before,
                        "pos_after": position,
                        "is_close": abs(position) < abs(pos_before),
                        "ts_ns": event.exch_ts,
                    })
                    strategy.on_fill("sell", sell_order.price, mid)
                    sell_order = None

    if session_spread_pts:
        sorted_sp = sorted(session_spread_pts)
        median_sp = sorted_sp[len(sorted_sp) // 2]
    else:
        median_sp = 0

    return {
        "fills": fills,
        "final_position": position,
        "session_median_spread_pts": int(median_sp),
        "n_quote_samples": len(session_spread_pts),
    }


def _fifo_trips(fills: list[dict]) -> tuple[float, int, int, list[float]]:
    """FIFO match -> (gross_pts, n_trips, n_wins, per_trip_pnl_pts)."""
    buy_q: list[dict] = []
    sell_q: list[dict] = []
    realized = 0.0
    trips = 0
    wins = 0
    per_trip: list[float] = []
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
                per_trip.append(pnl)
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
                per_trip.append(pnl)
            else:
                sell_q.append(f)
    return realized, trips, wins, per_trip


def _gate_c_calc(
    daily: list[dict],
    rt_pts: float,
    bootstrap_n: int = 1000,
    rng_seed: int = 42,
) -> dict:
    """Compute Gate C survivor criteria.

    daily entries: {date, n_fills, gross_pts, net_pts}
    """
    if not daily:
        return {"computable": False, "reason": "no daily data"}

    net_pts = np.array([d["net_pts"] for d in daily], dtype=float)
    n_fills = np.array([d["n_fills"] for d in daily], dtype=int)
    n_days = len(daily)
    total_net = float(net_pts.sum())

    # 1. max_day_concentration_pct
    if abs(total_net) > 1e-9:
        # absolute-contribution method (handles sign flips)
        abs_contrib = np.abs(net_pts)
        max_pct = float(abs_contrib.max() / abs_contrib.sum() * 100.0)
    else:
        max_pct = 0.0

    # 2. winning_days
    winning_days = int(np.sum(net_pts > 0))

    # 3. jackknife sign flip
    sign_total = np.sign(total_net) if abs(total_net) > 1e-9 else 0
    jackknife_sign_flip = False
    jackknife_signs = []
    for i in range(n_days):
        loo = np.delete(net_pts, i)
        loo_total = float(loo.sum())
        loo_sign = np.sign(loo_total) if abs(loo_total) > 1e-9 else 0
        jackknife_signs.append(int(loo_sign))
        if sign_total != 0 and loo_sign != 0 and loo_sign != sign_total:
            jackknife_sign_flip = True

    # 4. bootstrap CI on daily Sharpe
    if n_days >= 2 and net_pts.std() > 1e-9:
        rng = np.random.default_rng(rng_seed)
        boot_sharpes = []
        for _ in range(bootstrap_n):
            sample = rng.choice(net_pts, size=n_days, replace=True)
            if sample.std() > 1e-9:
                s = float(sample.mean() / sample.std() * np.sqrt(252))
                boot_sharpes.append(s)
        boot_arr = np.array(boot_sharpes)
        ci_low, ci_high = float(np.percentile(boot_arr, 2.5)), float(
            np.percentile(boot_arr, 97.5)
        )
        ci_excludes_zero = (ci_low > 0) or (ci_high < 0)
    else:
        ci_low = ci_high = 0.0
        ci_excludes_zero = False

    # 5. fills_distinct_days
    fills_distinct_days = int(np.sum(n_fills > 0))

    return {
        "computable": True,
        "max_day_concentration_pct": round(max_pct, 1),
        "max_day_concentration_pass": max_pct <= 25.0,
        "winning_days": winning_days,
        "winning_days_pass": winning_days >= 5,
        "jackknife_sign_flip": jackknife_sign_flip,
        "jackknife_pass": not jackknife_sign_flip,
        "jackknife_signs": jackknife_signs,
        "sharpe_bootstrap_ci_low": round(ci_low, 3),
        "sharpe_bootstrap_ci_high": round(ci_high, 3),
        "bootstrap_ci_excludes_zero": ci_excludes_zero,
        "fills_distinct_days": fills_distinct_days,
        "fills_distinct_days_pass": fills_distinct_days >= 5,
        "all_gate_c_pass": (
            max_pct <= 25.0
            and winning_days >= 5
            and not jackknife_sign_flip
            and ci_excludes_zero
            and fills_distinct_days >= 5
        ),
    }


def _drawdown_pts(equity: np.ndarray) -> tuple[float, int]:
    """Return (max_dd_pts, longest_dd_n_steps)."""
    if len(equity) == 0:
        return 0.0, 0
    peak = equity[0]
    max_dd = 0.0
    longest = 0
    cur_run = 0
    for v in equity:
        peak = max(peak, v)
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
        if v < peak:
            cur_run += 1
            if cur_run > longest:
                longest = cur_run
        else:
            cur_run = 0
    return float(max_dd), int(longest)


def _run_config(
    source: ClickHouseSource,
    instrument: str,
    dates: list[str],
    max_pos: int,
    queue_fraction: float,
    place_ns: int,
    cancel_ns: int,
) -> dict:
    params = C1Params(
        spread_threshold_pts=4,
        max_pos=max_pos,
    )
    strategy = C1ChavezCasillasAdaptiveMaker(
        params=params, active_symbol=instrument
    )
    daily: list[dict] = []
    all_fills: list[dict] = []
    fit_stats_final: dict = {}

    for date in dates:
        events = source.load_day(instrument, date)
        if not events:
            logger.warning("no_events", date=date)
            continue
        # P2 #11 fix: reset strategy state at each day boundary so that stale
        # `_position` / fit history from prior day does not gate today's
        # quotes while daily FIFO PnL is computed assuming flat-start.
        strategy.reset_for_day()
        day = _simulate_day(
            strategy, events, queue_fraction, place_ns, cancel_ns
        )
        gross, n_trips, n_wins, per_trip = _fifo_trips(day["fills"])
        n_fills = len(day["fills"])
        # Cost: per-side fee applied to each fill (FIFO model already pairs).
        cost_per_side = _TXF_RT_COST_PTS / 2.0
        fees = n_fills * cost_per_side
        net = gross - fees
        daily.append({
            "date": date,
            "n_events": len(events),
            "n_fills": n_fills,
            "n_trips": n_trips,
            "n_wins": n_wins,
            "gross_pts": round(gross, 4),
            "fees_pts": round(fees, 4),
            "net_pts": round(net, 4),
            "session_median_sp_pts": day["session_median_spread_pts"],
            "n_quote_samples": day["n_quote_samples"],
            "final_position": day["final_position"],
        })
        all_fills.extend(day["fills"])
        fit_stats_final = strategy.fit_stats

    total_fills = len(all_fills)
    gross_total, trips_total, wins_total, per_trip_all = _fifo_trips(all_fills)
    fees_total = total_fills * (_TXF_RT_COST_PTS / 2.0)
    net_total = gross_total - fees_total

    daily_net = np.array([d["net_pts"] for d in daily], dtype=float)
    n_days = len(daily)
    if n_days >= 2 and daily_net.std() > 1e-9:
        sharpe = float(daily_net.mean() / daily_net.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    equity = np.concatenate(([0.0], np.cumsum(daily_net)))
    max_dd_pts, longest_dd = _drawdown_pts(equity)

    win_rate_pct = (
        round(wins_total / trips_total * 100, 1) if trips_total > 0 else 0.0
    )
    avg_edge_per_trip = (
        round(sum(per_trip_all) / trips_total, 4) if trips_total > 0 else 0.0
    )
    pnl_per_fill = (
        round(net_total / total_fills, 4) if total_fills > 0 else 0.0
    )
    avg_spread_at_entry = (
        round(
            sum(f["spread_pts"] for f in all_fills) / total_fills, 2
        )
        if total_fills > 0
        else 0.0
    )

    gate_c = _gate_c_calc(daily, _TXF_RT_COST_PTS)

    return {
        "config": {
            "instrument": instrument,
            "max_pos": max_pos,
            "spread_threshold_pts": 4,
            "queue_fraction": queue_fraction,
            "place_ns": place_ns,
            "cancel_ns": cancel_ns,
            "rt_cost_pts": _TXF_RT_COST_PTS,
            "f2_maker_wedge_pts": _F2_MAKER_WEDGE_PTS,
        },
        "n_days": n_days,
        "date_range": [dates[0], dates[-1]] if dates else [None, None],
        "fills_total": total_fills,
        "trips_total": trips_total,
        "wins_total": wins_total,
        "win_rate_pct": win_rate_pct,
        "gross_pts_total": round(gross_total, 2),
        "fees_pts_total": round(fees_total, 2),
        "net_pts_total": round(net_total, 2),
        "net_ntd_total": round(net_total * _TXF_POINT_VALUE_NTD, 0),
        "pnl_per_fill_pts": pnl_per_fill,
        "avg_edge_per_trip_pts": avg_edge_per_trip,
        "avg_spread_at_entry_pts": avg_spread_at_entry,
        "sharpe_annualized": round(sharpe, 3),
        "max_drawdown_pts": round(max_dd_pts, 2),
        "longest_drawdown_n_days": longest_dd,
        "gate_c": gate_c,
        "daily": daily,
        "fit_stats_final": fit_stats_final,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="C1 re-validation backtest")
    parser.add_argument("--instrument", default="TXFD6")
    parser.add_argument(
        "--max-pos-bracket",
        default="1,3",
        help="Comma-separated max_pos values (default: 1,3)",
    )
    parser.add_argument(
        "--qf", type=float, default=0.5,
        help="Queue fraction (default 0.5; CK calibration baseline)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(
            "outputs/team_artifacts/alpha-research/re-validation-2026-04-27/C1"
        ),
    )
    args = parser.parse_args()

    if "CLICKHOUSE_PASSWORD" not in os.environ:
        logger.error(
            "clickhouse_password_missing",
            hint="export CLICKHOUSE_PASSWORD=changeme",
        )
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    source = ClickHouseSource(password=os.environ["CLICKHOUSE_PASSWORD"])
    source.health_check()

    all_dates = source.available_dates(args.instrument)
    if not all_dates:
        logger.error("no_data", instrument=args.instrument)
        return 3
    dates = all_dates  # full continuous-month inventory
    logger.info(
        "backtest_start",
        instrument=args.instrument,
        n_days=len(dates),
        date_range=(dates[0], dates[-1]),
        place_ns=_LATENCY_PLACE_NS,
        cancel_ns=_LATENCY_CANCEL_NS,
        rt_cost_pts=_TXF_RT_COST_PTS,
    )

    max_pos_bracket = [int(x) for x in args.max_pos_bracket.split(",")]
    runs: dict[str, dict] = {}
    for mp in max_pos_bracket:
        key = f"mp{mp}"
        logger.info("run_start", config=key)
        run = _run_config(
            source,
            args.instrument,
            dates,
            max_pos=mp,
            queue_fraction=args.qf,
            place_ns=_LATENCY_PLACE_NS,
            cancel_ns=_LATENCY_CANCEL_NS,
        )
        runs[key] = run
        logger.info(
            "run_done",
            config=key,
            fills=run["fills_total"],
            trips=run["trips_total"],
            net_pts=run["net_pts_total"],
            sharpe=run["sharpe_annualized"],
        )

    # Pick best max_pos by net_pts
    best_key = max(runs, key=lambda k: runs[k]["net_pts_total"])
    summary = {
        "candidate": "C1_revalidation",
        "instrument": args.instrument,
        "data_inventory": {
            "available_dates": all_dates,
            "n_dates": len(all_dates),
            "first_date": all_dates[0] if all_dates else None,
            "last_date": all_dates[-1] if all_dates else None,
        },
        "latency_profile": {
            "name": "r47_maker_shioaji_p95_v2026-04-24_measured",
            "place_ms": _LATENCY_PLACE_NS / 1e6,
            "cancel_ms": _LATENCY_CANCEL_NS / 1e6,
            "asymmetry_ratio": round(
                _LATENCY_PLACE_NS / max(_LATENCY_CANCEL_NS, 1), 1
            ),
            "f2_maker_wedge_pts": _F2_MAKER_WEDGE_PTS,
        },
        "cost_model": {
            "rt_cost_pts": _TXF_RT_COST_PTS,
            "point_value_ntd": _TXF_POINT_VALUE_NTD,
            "edge_floor_2x_pts": 2 * _TXF_RT_COST_PTS,
            "source": "memory/feedback_taifex_fee_structure.md",
        },
        "queue_fraction": args.qf,
        "runs": runs,
        "best_config_by_net_pts": best_key,
        "profile_conditionality_caveat": (
            "(latency_profile=v2026-04-24_measured, cost_tier=retail_shioaji_taifex)"
        ),
    }

    out_json = args.out / "executor_revalidation_results.json"
    out_json.write_text(json.dumps(summary, indent=2))
    logger.info("results_written", path=str(out_json))
    return 0


if __name__ == "__main__":
    sys.exit(main())
