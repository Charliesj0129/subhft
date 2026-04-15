#!/usr/bin/env python3
"""
R47 TMFD6 Parameter Sweep — Find optimal (spread_threshold, max_pos, queue_frac).

Sweeps all combinations across all available TMFD6 trading days in ClickHouse.
Signal gates (PE, Queue, MFG, Toxicity) cannot be tested in CK-direct backtest
(no feature data) — noted as limitation.

Usage:
    python research/tools/r47_tmfd6_sweep.py
"""

import os
import sys
import time
from dataclasses import dataclass
from itertools import product
from typing import Optional

import numpy as np
import requests

CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000

# TMFD6 economics
POINT_VALUE_NTD = 10
FEE_PER_SIDE_NTD = 20  # 13 comm + 7 tax
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 4.0 pts


def ck_query(sql: str) -> str:
    resp = requests.post(
        CK_URL,
        params={"user": "default", "password": CK_PASSWORD},
        data=sql,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.text.strip()


def ck_query_numpy(sql: str) -> dict:
    raw = ck_query(sql + " FORMAT TSVWithNames")
    lines = raw.split("\n")
    if len(lines) < 2:
        return {}
    headers = lines[0].split("\t")
    rows = [line.split("\t") for line in lines[1:] if line]
    cols = {}
    for i, h in enumerate(headers):
        col_vals = [r[i] for r in rows]
        try:
            cols[h] = np.array(col_vals, dtype=np.int64)
        except ValueError:
            try:
                cols[h] = np.array(col_vals, dtype=np.float64)
            except ValueError:
                cols[h] = np.array(col_vals)
    return cols


def get_trading_days() -> list[str]:
    sql = """
    SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) as d
    FROM hft.market_data
    WHERE symbol = 'TMFD6' AND type = 'BidAsk'
    ORDER BY d
    """
    raw = ck_query(sql + " FORMAT TSV")
    if not raw:
        return []
    return [line.strip() for line in raw.split("\n") if line.strip()]


def load_day(date: str) -> tuple[dict, dict]:
    ba_sql = f"""
    SELECT
        exch_ts,
        bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
        asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    tick_sql = f"""
    SELECT
        exch_ts,
        price_scaled AS price,
        volume
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND type = 'Tick'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
    ORDER BY exch_ts
    """
    return ck_query_numpy(ba_sql), ck_query_numpy(tick_sql)


@dataclass
class OpenOrder:
    side: str
    price: int
    placed_ts: int
    queue_pos: float


@dataclass
class FillRecord:
    side: str
    price_pts: float
    ts: int
    mid_at_fill: float


def run_backtest(
    ba: dict,
    ticks: dict,
    spread_threshold: int,
    max_pos: int,
    queue_frac: float,
) -> tuple[list[FillRecord], int]:
    ba_ts = ba["exch_ts"]
    ba_n = len(ba_ts)
    tick_ts = ticks.get("exch_ts", np.array([], dtype=np.int64))
    tick_n = len(tick_ts)

    bid1_p = ba["bid1_p"]
    bid1_v = ba["bid1_v"]
    ask1_p = ba["ask1_p"]
    ask1_v = ba["ask1_v"]
    t_price = ticks.get("price", np.array([], dtype=np.int64))
    t_vol = ticks.get("volume", np.array([], dtype=np.int64))

    cur_bid = cur_ask = 0
    cur_bid_v = cur_ask_v = 0
    position = 0
    buy_order: Optional[OpenOrder] = None
    sell_order: Optional[OpenOrder] = None
    fills: list[FillRecord] = []

    ba_i = 0
    ti = 0

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            cur_bid = bid1_p[ba_i]
            cur_ask = ask1_p[ba_i]
            cur_bid_v = bid1_v[ba_i]
            cur_ask_v = ask1_v[ba_i]
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            if spread_pts >= spread_threshold:
                if buy_order is None and position < max_pos:
                    qp = max(1, int(cur_bid_v * queue_frac))
                    buy_order = OpenOrder(
                        side="buy", price=cur_bid,
                        placed_ts=ba_time, queue_pos=qp,
                    )
                if sell_order is None and position > -max_pos:
                    qp = max(1, int(cur_ask_v * queue_frac))
                    sell_order = OpenOrder(
                        side="sell", price=cur_ask,
                        placed_ts=ba_time, queue_pos=qp,
                    )
        else:
            trade_p = t_price[ti]
            trade_v = t_vol[ti]
            ti += 1

            cur_mid = (cur_bid + cur_ask) / (2 * SCALE) if cur_bid > 0 else 0

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(FillRecord(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=tk_time,
                        mid_at_fill=cur_mid,
                    ))
                    position += 1
                    buy_order = None

            if sell_order is not None and trade_p >= sell_order.price:
                sell_order.queue_pos -= trade_v
                if sell_order.queue_pos <= 0:
                    fills.append(FillRecord(
                        side="sell",
                        price_pts=sell_order.price / SCALE,
                        ts=tk_time,
                        mid_at_fill=cur_mid,
                    ))
                    position -= 1
                    sell_order = None

    return fills, position


def compute_fifo_pnl(fills: list[FillRecord]) -> tuple[float, int, int, list[float]]:
    """Returns (realized_pnl_pts, n_round_trips, wins, trip_pnls)."""
    buy_q: list[float] = []
    sell_q: list[float] = []
    realized = 0.0
    n_trips = 0
    wins = 0
    trip_pnls: list[float] = []

    for f in fills:
        if f.side == "buy":
            if sell_q:
                pnl = sell_q.pop(0) - f.price_pts
                realized += pnl
                trip_pnls.append(pnl)
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_q.append(f.price_pts)
        else:
            if buy_q:
                pnl = f.price_pts - buy_q.pop(0)
                realized += pnl
                trip_pnls.append(pnl)
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_q.append(f.price_pts)

    return realized, n_trips, wins, trip_pnls


def compute_daily_equity_curve(daily_nets: list[float]) -> tuple[float, float]:
    """Returns (max_drawdown_pts, sharpe_ratio)."""
    if not daily_nets:
        return 0.0, 0.0
    arr = np.array(daily_nets)
    cumsum = np.cumsum(arr)
    peak = np.maximum.accumulate(cumsum)
    dd = peak - cumsum
    max_dd = float(dd.max()) if len(dd) > 0 else 0.0

    mean_d = arr.mean()
    std_d = arr.std(ddof=1) if len(arr) > 1 else 1e-9
    sharpe = float(mean_d / std_d * np.sqrt(252)) if std_d > 1e-9 else 0.0

    return max_dd, sharpe


def main():
    print("=" * 100)
    print("  R47 TMFD6 Parameter Sweep")
    print(f"  Economics: 1pt = {POINT_VALUE_NTD} NTD, fee = {FEE_PER_SIDE_NTD} NTD/side, RT cost = {FEE_RT_PTS:.1f} pts")
    print("=" * 100)

    # Parameter grid
    spread_thresholds = [5, 6, 7, 8]
    max_positions = [1, 2, 3, 4, 5]
    queue_fracs = [1.0, 0.75, 0.5, 0.25]

    dates = get_trading_days()
    print(f"\nAvailable trading days: {len(dates)}")
    for d in dates:
        print(f"  {d}")

    # Load all days upfront
    print("\nLoading data from ClickHouse...")
    day_data: dict[str, tuple[dict, dict]] = {}
    for date in dates:
        sys.stdout.write(f"  {date}...")
        sys.stdout.flush()
        t0 = time.time()
        ba, ticks = load_day(date)
        elapsed = time.time() - t0
        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            print(f" SKIP (no data)")
            continue
        n_ba = len(ba["exch_ts"])
        n_tk = len(ticks.get("exch_ts", []))
        print(f" {n_ba} BA, {n_tk} ticks ({elapsed:.1f}s)")
        day_data[date] = (ba, ticks)

    n_days = len(day_data)
    print(f"\nLoaded {n_days} days. Running sweep...")

    # Spread distribution per day
    print("\nSpread distribution per day:")
    print(f"  {'Date':>12} {'Min':>6} {'P25':>6} {'Med':>6} {'Mean':>7} {'P75':>6} {'P95':>7} {'>=5%':>7} {'>=6%':>7} {'>=7%':>7} {'>=8%':>7}")
    for date in sorted(day_data.keys()):
        ba, _ = day_data[date]
        spreads = (ba["ask1_p"] - ba["bid1_p"]) / SCALE
        valid = spreads[spreads > 0]
        n = len(valid)
        pct5 = (valid >= 5).sum() / n * 100 if n > 0 else 0
        pct6 = (valid >= 6).sum() / n * 100 if n > 0 else 0
        pct7 = (valid >= 7).sum() / n * 100 if n > 0 else 0
        pct8 = (valid >= 8).sum() / n * 100 if n > 0 else 0
        print(f"  {date:>12} {valid.min():>6.0f} {np.percentile(valid,25):>6.0f} {np.median(valid):>6.0f} "
              f"{valid.mean():>7.1f} {np.percentile(valid,75):>6.0f} {np.percentile(valid,95):>7.0f} "
              f"{pct5:>6.1f}% {pct6:>6.1f}% {pct7:>6.1f}% {pct8:>6.1f}%")

    # Run sweep
    configs = list(product(spread_thresholds, max_positions, queue_fracs))
    n_configs = len(configs)
    print(f"\nSweeping {n_configs} configs x {n_days} days = {n_configs * n_days} backtests...")

    # results[config_key] = {daily_nets, total_gross, total_fills, total_trips, total_wins, trip_pnls_all}
    results: dict[tuple, dict] = {}
    for cfg in configs:
        results[cfg] = {
            "daily_nets": [],
            "daily_gross": [],
            "total_fills": 0,
            "total_trips": 0,
            "total_wins": 0,
            "trip_pnls": [],
        }

    sweep_t0 = time.time()
    for di, date in enumerate(sorted(day_data.keys())):
        ba, ticks = day_data[date]
        sys.stdout.write(f"  [{di+1}/{n_days}] {date}...")
        sys.stdout.flush()

        for cfg in configs:
            sp_thr, mx_pos, qf = cfg
            fills, _ = run_backtest(ba, ticks, sp_thr, mx_pos, qf)
            gross, trips, wins, trip_pnls = compute_fifo_pnl(fills)
            fee_pts = len(fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
            net = gross - fee_pts

            r = results[cfg]
            r["daily_nets"].append(net)
            r["daily_gross"].append(gross)
            r["total_fills"] += len(fills)
            r["total_trips"] += trips
            r["total_wins"] += wins
            r["trip_pnls"].extend(trip_pnls)

        print(f" done")

    sweep_elapsed = time.time() - sweep_t0
    print(f"\nSweep completed in {sweep_elapsed:.1f}s")

    # Build ranked table
    rows = []
    for cfg, r in results.items():
        sp_thr, mx_pos, qf = cfg
        daily_nets = r["daily_nets"]
        total_fills = r["total_fills"]
        total_trips = r["total_trips"]
        total_wins = r["total_wins"]
        trip_pnls = r["trip_pnls"]

        total_net = sum(daily_nets)
        total_gross = sum(r["daily_gross"])
        pnl_per_day = total_net / n_days if n_days > 0 else 0
        pnl_per_day_ntd = pnl_per_day * POINT_VALUE_NTD
        wr = total_wins / total_trips * 100 if total_trips > 0 else 0
        mean_pnl_per_rt = (total_gross / total_trips - FEE_RT_PTS) if total_trips > 0 else 0
        max_dd, sharpe = compute_daily_equity_curve(daily_nets)

        # t-stat of daily net PnL
        arr = np.array(daily_nets)
        if len(arr) > 1 and arr.std(ddof=1) > 1e-9:
            t_stat = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr))))
        else:
            t_stat = 0.0

        n_winning_days = int((arr > 0).sum())

        rows.append({
            "sp_thr": sp_thr,
            "mx_pos": mx_pos,
            "qf": qf,
            "total_net": total_net,
            "total_gross": total_gross,
            "pnl_per_day": pnl_per_day,
            "pnl_per_day_ntd": pnl_per_day_ntd,
            "t_stat": t_stat,
            "wr": wr,
            "n_fills": total_fills,
            "n_rt": total_trips,
            "mean_pnl_per_rt": mean_pnl_per_rt,
            "max_dd": max_dd,
            "sharpe": sharpe,
            "n_days": n_days,
            "n_winning_days": n_winning_days,
            "daily_nets": daily_nets,
        })

    # Sort by total net PnL descending
    rows.sort(key=lambda x: x["total_net"], reverse=True)

    # Print ranked table
    print(f"\n{'='*150}")
    print(f"  RANKED CONFIG TABLE (sorted by total net PnL, {n_days} days)")
    print(f"{'='*150}")
    header = (f"{'Rank':>4} {'Spr':>4} {'MxP':>4} {'QFr':>5} "
              f"{'TotalNet':>10} {'PnL/day':>9} {'NTD/day':>9} "
              f"{'t-stat':>7} {'WR%':>6} {'WinD':>5} "
              f"{'Fills':>7} {'RTs':>7} {'Net/RT':>8} "
              f"{'MaxDD':>8} {'Sharpe':>7}")
    print(header)
    print("-" * 150)

    for i, row in enumerate(rows):
        mark = " ***" if i < 3 else ""
        print(f"{i+1:>4} {'>=' + str(row['sp_thr']):>4} {row['mx_pos']:>4} {row['qf']:>5.2f} "
              f"{row['total_net']:>+10.0f} {row['pnl_per_day']:>+9.1f} {row['pnl_per_day_ntd']:>+9.0f} "
              f"{row['t_stat']:>+7.2f} {row['wr']:>5.1f}% {row['n_winning_days']:>3}/{row['n_days']:<1} "
              f"{row['n_fills']:>7} {row['n_rt']:>7} {row['mean_pnl_per_rt']:>+8.3f} "
              f"{row['max_dd']:>8.0f} {row['sharpe']:>+7.2f}{mark}")

    print("-" * 150)

    # Top 3 detailed per-day breakdown
    print(f"\n\n{'='*120}")
    print(f"  TOP 3 CONFIGS — PER-DAY BREAKDOWN")
    print(f"{'='*120}")

    sorted_dates = sorted(day_data.keys())
    for rank, row in enumerate(rows[:3], 1):
        print(f"\n--- #{rank}: spread>={row['sp_thr']}, max_pos={row['mx_pos']}, queue_frac={row['qf']:.2f} ---")
        print(f"  Total: {row['total_net']:+.0f} pts | {row['total_net']*POINT_VALUE_NTD:+,.0f} NTD | "
              f"Sharpe={row['sharpe']:+.2f} | t={row['t_stat']:+.2f} | MaxDD={row['max_dd']:.0f} pts")
        print(f"  {'Date':>12} {'Net PnL':>10} {'NTD':>10} {'Fills':>7} {'Cumul':>10}")

        cumul = 0.0
        for di, date in enumerate(sorted_dates):
            net_d = row["daily_nets"][di]
            cumul += net_d
            ba_tmp, ticks_tmp = day_data[date]
            fills_tmp, _ = run_backtest(ba_tmp, ticks_tmp, row["sp_thr"], row["mx_pos"], row["qf"])
            n_fills_d = len(fills_tmp)
            print(f"  {date:>12} {net_d:>+10.0f} {net_d*POINT_VALUE_NTD:>+10,.0f} {n_fills_d:>7} {cumul:>+10.0f}")

    # Sensitivity analysis for #1
    best = rows[0]
    print(f"\n\n{'='*120}")
    print(f"  SENSITIVITY ANALYSIS — Best config: spread>={best['sp_thr']}, max_pos={best['mx_pos']}, qf={best['qf']:.2f}")
    print(f"{'='*120}")

    # Vary spread_threshold +/- 1
    print(f"\n  Varying spread_threshold (max_pos={best['mx_pos']}, qf={best['qf']:.2f}):")
    for sp in sorted(set([max(1, best["sp_thr"] - 2), best["sp_thr"] - 1, best["sp_thr"], best["sp_thr"] + 1, best["sp_thr"] + 2])):
        key = (sp, best["mx_pos"], best["qf"])
        if key in results:
            r = results[key]
            net = sum(r["daily_nets"])
            arr = np.array(r["daily_nets"])
            t = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if len(arr) > 1 and arr.std(ddof=1) > 1e-9 else 0
            marker = " <-- BEST" if sp == best["sp_thr"] else ""
            print(f"    spread>={sp}: total={net:+.0f} pts, t={t:+.2f}{marker}")

    # Vary max_pos +/- 1
    print(f"\n  Varying max_pos (spread>={best['sp_thr']}, qf={best['qf']:.2f}):")
    for mp in sorted(set([max(1, best["mx_pos"] - 2), max(1, best["mx_pos"] - 1), best["mx_pos"], best["mx_pos"] + 1, min(5, best["mx_pos"] + 2)])):
        key = (best["sp_thr"], mp, best["qf"])
        if key in results:
            r = results[key]
            net = sum(r["daily_nets"])
            arr = np.array(r["daily_nets"])
            t = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if len(arr) > 1 and arr.std(ddof=1) > 1e-9 else 0
            marker = " <-- BEST" if mp == best["mx_pos"] else ""
            print(f"    max_pos={mp}: total={net:+.0f} pts, t={t:+.2f}{marker}")

    # Vary queue_frac
    print(f"\n  Varying queue_frac (spread>={best['sp_thr']}, max_pos={best['mx_pos']}):")
    for qf in sorted(queue_fracs, reverse=True):
        key = (best["sp_thr"], best["mx_pos"], qf)
        if key in results:
            r = results[key]
            net = sum(r["daily_nets"])
            arr = np.array(r["daily_nets"])
            t = float(arr.mean() / (arr.std(ddof=1) / np.sqrt(len(arr)))) if len(arr) > 1 and arr.std(ddof=1) > 1e-9 else 0
            marker = " <-- BEST" if qf == best["qf"] else ""
            print(f"    qf={qf:.2f}: total={net:+.0f} pts, t={t:+.2f}{marker}")

    # Write report
    write_report(rows, sorted_dates, day_data, n_days)

    print(f"\n{'*'*100}")
    print(f"  RECOMMENDATION")
    print(f"{'*'*100}")
    if best["total_net"] > 0:
        print(f"  Best config: spread>={best['sp_thr']}, max_pos={best['mx_pos']}, queue_frac={best['qf']:.2f}")
        print(f"  Total PnL: {best['total_net']:+.0f} pts ({best['total_net']*POINT_VALUE_NTD:+,.0f} NTD) over {n_days} days")
        print(f"  PnL/day: {best['pnl_per_day']:+.1f} pts ({best['pnl_per_day_ntd']:+,.0f} NTD/day)")
        print(f"  Sharpe: {best['sharpe']:+.2f} | t-stat: {best['t_stat']:+.2f} | Win days: {best['n_winning_days']}/{n_days}")
        print(f"  Max drawdown: {best['max_dd']:.0f} pts")
        print(f"  Net PnL per RT: {best['mean_pnl_per_rt']:+.3f} pts (after {FEE_RT_PTS:.1f} pts RT cost)")
    else:
        print(f"  ALL CONFIGS NEGATIVE. Best is spread>={best['sp_thr']}, max_pos={best['mx_pos']}, qf={best['qf']:.2f}")
        print(f"  Total PnL: {best['total_net']:+.0f} pts ({best['total_net']*POINT_VALUE_NTD:+,.0f} NTD)")
        print(f"  TMFD6 maker strategy at spread>=5+ is NOT profitable with 4.0 pts RT cost.")
    print(f"{'*'*100}")


def write_report(rows: list[dict], sorted_dates: list[str], day_data: dict, n_days: int):
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "outputs", "team_artifacts", "alpha-research",
    )
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "r47_tmfd6_optimal_config.md")

    lines = []
    lines.append("# R47 TMFD6 Optimal Config — Parameter Sweep Results")
    lines.append("")
    lines.append(f"**Date**: 2026-04-10")
    lines.append(f"**Days**: {n_days}")
    lines.append(f"**Economics**: 1pt = {POINT_VALUE_NTD} NTD, fee = {FEE_PER_SIDE_NTD} NTD/side, RT cost = {FEE_RT_PTS:.1f} pts")
    lines.append("")

    # Ranked table
    lines.append("## Ranked Config Table")
    lines.append("")
    lines.append("| Rank | Spread | MaxPos | QueueFrac | TotalNet pts | PnL/day pts | NTD/day | t-stat | WR% | WinDays | Fills | RTs | Net/RT pts | MaxDD pts | Sharpe |")
    lines.append("|------|--------|--------|-----------|-------------|-------------|---------|--------|-----|---------|-------|-----|-----------|-----------|--------|")

    for i, row in enumerate(rows):
        lines.append(
            f"| {i+1} | >={row['sp_thr']} | {row['mx_pos']} | {row['qf']:.2f} | "
            f"{row['total_net']:+.0f} | {row['pnl_per_day']:+.1f} | {row['pnl_per_day_ntd']:+.0f} | "
            f"{row['t_stat']:+.2f} | {row['wr']:.1f}% | {row['n_winning_days']}/{row['n_days']} | "
            f"{row['n_fills']} | {row['n_rt']} | {row['mean_pnl_per_rt']:+.3f} | "
            f"{row['max_dd']:.0f} | {row['sharpe']:+.2f} |"
        )

    lines.append("")

    # Top 3 per-day
    lines.append("## Top 3 Configs — Per-Day Breakdown")
    lines.append("")

    for rank, row in enumerate(rows[:3], 1):
        lines.append(f"### #{rank}: spread>={row['sp_thr']}, max_pos={row['mx_pos']}, qf={row['qf']:.2f}")
        lines.append("")
        lines.append(f"Total: {row['total_net']:+.0f} pts ({row['total_net']*POINT_VALUE_NTD:+,.0f} NTD) | "
                      f"Sharpe={row['sharpe']:+.2f} | t={row['t_stat']:+.2f} | MaxDD={row['max_dd']:.0f}")
        lines.append("")
        lines.append("| Date | Net PnL pts | NTD | Cumulative |")
        lines.append("|------|------------|-----|------------|")

        cumul = 0.0
        for di, date in enumerate(sorted_dates):
            if date not in day_data:
                continue
            net_d = row["daily_nets"][di]
            cumul += net_d
            lines.append(f"| {date} | {net_d:+.0f} | {net_d*POINT_VALUE_NTD:+,.0f} | {cumul:+.0f} |")

        lines.append("")

    # Sensitivity
    best = rows[0]
    lines.append("## Sensitivity Analysis")
    lines.append("")
    lines.append(f"Best config: spread>={best['sp_thr']}, max_pos={best['mx_pos']}, qf={best['qf']:.2f}")
    lines.append("")
    lines.append("See console output for detailed sensitivity across each parameter axis.")
    lines.append("")

    # Limitations
    lines.append("## Limitations")
    lines.append("")
    lines.append("- **Signal gates (PE, Queue, MFG, Toxicity) NOT tested**: CK-direct backtest has no feature data.")
    lines.append("  These gates were killed on TXFD6 but TMFD6 dynamics may differ. Requires live feature engine data to test.")
    lines.append("- Queue position model is simplified (linear depletion at queue_frac of L1 depth).")
    lines.append("- No latency modeling — assumes instant order placement and fills.")
    lines.append("- End-of-day position not force-closed (FIFO PnL counts completed round-trips only).")
    lines.append("")

    # Recommendation
    lines.append("## Recommendation")
    lines.append("")
    if best["total_net"] > 0:
        lines.append(f"**Deploy**: spread>={best['sp_thr']}, max_pos={best['mx_pos']}, queue_frac={best['qf']:.2f}")
        lines.append(f"- Total: {best['total_net']:+.0f} pts ({best['total_net']*POINT_VALUE_NTD:+,.0f} NTD) / {n_days} days")
        lines.append(f"- Sharpe: {best['sharpe']:+.2f}, t-stat: {best['t_stat']:+.2f}")
        lines.append(f"- Win days: {best['n_winning_days']}/{n_days}, MaxDD: {best['max_dd']:.0f} pts")

        # Check #2 as alternative
        if len(rows) > 1:
            r2 = rows[1]
            lines.append(f"\n**Alternative**: spread>={r2['sp_thr']}, max_pos={r2['mx_pos']}, qf={r2['qf']:.2f}")
            lines.append(f"- Total: {r2['total_net']:+.0f} pts, Sharpe={r2['sharpe']:+.2f}, t={r2['t_stat']:+.2f}")
    else:
        lines.append("**DO NOT DEPLOY**. All configurations show negative PnL.")
        lines.append(f"Best loss: {best['total_net']:+.0f} pts. RT cost of {FEE_RT_PTS:.1f} pts is too high.")

    lines.append("")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport written to: {output_path}")


if __name__ == "__main__":
    main()
