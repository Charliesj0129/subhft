#!/usr/bin/env python3
"""
R47 TMFD6 Economics Backtest — Compare TXFD6 vs TMFD6 fee structures.

Runs the R47 maker backtest with configurable economics across all available
trading days for both TXFD6 and TMFD6 symbols.

Usage:
    python research/tools/r47_tmfd6_backtest.py
"""

import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import requests

CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000


# --- Economics configs ---

TXFD6_ECONOMICS = {
    "name": "TXFD6",
    "point_value_ntd": 200,
    "fee_per_side_ntd": 48,
}
TXFD6_ECONOMICS["fee_rt_pts"] = 2 * TXFD6_ECONOMICS["fee_per_side_ntd"] / TXFD6_ECONOMICS["point_value_ntd"]

TMFD6_ECONOMICS = {
    "name": "TMFD6",
    "point_value_ntd": 10,
    "fee_per_side_ntd": 20,  # 13 NTD comm + 7 NTD tax
}
TMFD6_ECONOMICS["fee_rt_pts"] = 2 * TMFD6_ECONOMICS["fee_per_side_ntd"] / TMFD6_ECONOMICS["point_value_ntd"]


def ck_query(sql: str) -> str:
    resp = requests.post(
        CK_URL, params={"user": "default", "password": CK_PASSWORD},
        data=sql, timeout=120,
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


def get_trading_days(symbol: str) -> list[str]:
    sql = f"""
    SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) as d
    FROM hft.market_data
    WHERE symbol = '{symbol}' AND type = 'BidAsk'
    ORDER BY d
    """
    raw = ck_query(sql + " FORMAT TSV")
    if not raw:
        return []
    return [line.strip() for line in raw.split("\n") if line.strip()]


def load_bidask(symbol: str, date: str) -> dict:
    sql = f"""
    SELECT
        exch_ts,
        bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
        asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    return ck_query_numpy(sql)


def load_ticks(symbol: str, date: str) -> dict:
    sql = f"""
    SELECT
        exch_ts,
        price_scaled AS price,
        volume
    FROM hft.market_data
    WHERE symbol = '{symbol}'
      AND type = 'Tick'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
    ORDER BY exch_ts
    """
    return ck_query_numpy(sql)


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
    spread_threshold: int = 1,
    max_pos: int = 3,
    queue_frac: float = 1.0,
) -> tuple[list[FillRecord], int]:
    ba_ts = ba["exch_ts"]
    ba_n = len(ba_ts)
    tick_ts = ticks["exch_ts"] if ticks else np.array([], dtype=np.int64)
    tick_n = len(tick_ts)

    bid1_p = ba["bid1_p"]
    bid1_v = ba["bid1_v"]
    ask1_p = ba["ask1_p"]
    ask1_v = ba["ask1_v"]
    t_price = ticks["price"] if ticks else np.array([], dtype=np.int64)
    t_vol = ticks["volume"] if ticks else np.array([], dtype=np.int64)

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
                    fr = FillRecord(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=tk_time,
                        mid_at_fill=cur_mid,
                    )
                    fills.append(fr)
                    position += 1
                    buy_order = None

            if sell_order is not None and trade_p >= sell_order.price:
                sell_order.queue_pos -= trade_v
                if sell_order.queue_pos <= 0:
                    fr = FillRecord(
                        side="sell",
                        price_pts=sell_order.price / SCALE,
                        ts=tk_time,
                        mid_at_fill=cur_mid,
                    )
                    fills.append(fr)
                    position -= 1
                    sell_order = None

    return fills, position


def compute_fifo_pnl(fills: list[FillRecord]) -> tuple[float, int, int]:
    """Returns (realized_pnl_pts, n_round_trips, wins)."""
    buy_q: list[float] = []
    sell_q: list[float] = []
    realized = 0.0
    n_trips = 0
    wins = 0

    for f in fills:
        if f.side == "buy":
            if sell_q:
                pnl = sell_q.pop(0) - f.price_pts
                realized += pnl
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_q.append(f.price_pts)
        else:
            if buy_q:
                pnl = f.price_pts - buy_q.pop(0)
                realized += pnl
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_q.append(f.price_pts)

    return realized, n_trips, wins


def compute_spread_distribution(ba: dict) -> dict:
    """Compute spread stats from bid/ask data."""
    spreads = (ba["ask1_p"] - ba["bid1_p"]) / SCALE
    valid = spreads[spreads > 0]
    if len(valid) == 0:
        return {"min": 0, "p25": 0, "median": 0, "mean": 0, "p75": 0, "p95": 0, "max": 0, "n": 0}
    return {
        "min": float(valid.min()),
        "p25": float(np.percentile(valid, 25)),
        "median": float(np.median(valid)),
        "mean": float(valid.mean()),
        "p75": float(np.percentile(valid, 75)),
        "p95": float(np.percentile(valid, 95)),
        "max": float(valid.max()),
        "n": len(valid),
    }


def run_multiday(
    symbol: str,
    econ: dict,
    dates: list[str],
    queue_fracs: list[float],
    spread_thresholds: list[int],
    max_pos: int = 3,
) -> dict:
    """Run backtest across all days and configs. Returns aggregated results."""
    point_value = econ["point_value_ntd"]
    fee_per_side = econ["fee_per_side_ntd"]
    fee_rt_pts = econ["fee_rt_pts"]

    # results[qf][thr] = {fills, trips, wins, gross_pts, ...}
    results: dict[float, dict[int, dict]] = {}
    for qf in queue_fracs:
        results[qf] = {}
        for thr in spread_thresholds:
            results[qf][thr] = {
                "fills": 0, "trips": 0, "wins": 0,
                "gross_pts": 0.0, "days_run": 0,
                "daily_pnls": [],
            }

    spread_stats_all: list[dict] = []
    days_loaded = 0

    for date in dates:
        sys.stdout.write(f"  {symbol} {date}...")
        sys.stdout.flush()
        t0 = time.time()
        ba = load_bidask(symbol, date)
        ticks = load_ticks(symbol, date)
        elapsed = time.time() - t0

        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            print(f" SKIP (no data)")
            continue

        n_ba = len(ba["exch_ts"])
        n_ticks = len(ticks.get("exch_ts", []))
        print(f" {n_ba} BA, {n_ticks} ticks ({elapsed:.1f}s)")

        ss = compute_spread_distribution(ba)
        ss["date"] = date
        spread_stats_all.append(ss)
        days_loaded += 1

        for qf in queue_fracs:
            for thr in spread_thresholds:
                fills, pos = run_backtest(
                    ba, ticks, spread_threshold=thr,
                    max_pos=max_pos, queue_frac=qf,
                )
                gross, trips, wins = compute_fifo_pnl(fills)
                fee_pts = len(fills) * fee_per_side / point_value
                net = gross - fee_pts

                r = results[qf][thr]
                r["fills"] += len(fills)
                r["trips"] += trips
                r["wins"] += wins
                r["gross_pts"] += gross
                r["days_run"] += 1
                r["daily_pnls"].append(net)

    return {
        "results": results,
        "spread_stats": spread_stats_all,
        "days_loaded": days_loaded,
        "econ": econ,
    }


def print_summary_table(run_data: dict, symbol: str, label: str):
    econ = run_data["econ"]
    results = run_data["results"]
    point_value = econ["point_value_ntd"]
    fee_per_side = econ["fee_per_side_ntd"]
    fee_rt_pts = econ["fee_rt_pts"]
    days = run_data["days_loaded"]

    print(f"\n{'='*85}")
    print(f"  {label}")
    print(f"  Symbol: {symbol} | Economics: {econ['name']} (1pt={point_value} NTD, fee={fee_per_side} NTD/side, RT={fee_rt_pts:.2f} pts)")
    print(f"  Days: {days}")
    print(f"{'='*85}")
    print(f"{'Queue':>8} {'Spread':>8} {'Fills':>8} {'Trips':>8} {'WR%':>7} "
          f"{'Gross':>10} {'Fees':>10} {'NET pts':>10} {'NET NTD':>12} {'Mean/RT':>9} {'$/day':>10}")
    print("-" * 85)

    for qf in sorted(results.keys(), reverse=True):
        qlabel = f"{qf:.1f}"
        for thr in sorted(results[qf].keys()):
            r = results[qf][thr]
            fills = r["fills"]
            trips = r["trips"]
            wins = r["wins"]
            gross = r["gross_pts"]
            fee_pts = fills * fee_per_side / point_value
            net_pts = gross - fee_pts
            net_ntd = net_pts * point_value
            wr = wins / trips * 100 if trips else 0
            mean_rt = (gross / trips - fee_rt_pts) if trips else 0
            daily_ntd = net_ntd / days if days else 0

            profitable = "  <--" if net_pts > 0 else ""
            print(f"{qlabel:>8} {'>=' + str(thr):>8} {fills:>8} {trips:>8} {wr:>6.1f}% "
                  f"{gross:>+10.0f} {-fee_pts:>10.0f} {net_pts:>+10.0f} {net_ntd:>+12,.0f} "
                  f"{mean_rt:>+9.3f} {daily_ntd:>+10,.0f}{profitable}")
    print("-" * 85)


def print_spread_comparison(txfd6_spreads: list[dict], tmfd6_spreads: list[dict]):
    print(f"\n{'='*85}")
    print(f"  SPREAD DISTRIBUTION COMPARISON")
    print(f"{'='*85}")
    print(f"{'Symbol':>8} {'Days':>6} {'Min':>6} {'P25':>6} {'Med':>6} {'Mean':>7} {'P75':>6} {'P95':>7} {'Max':>7}")
    print("-" * 85)

    for label, stats_list in [("TXFD6", txfd6_spreads), ("TMFD6", tmfd6_spreads)]:
        if not stats_list:
            print(f"{label:>8}   (no data)")
            continue
        # Aggregate across days
        all_mins = [s["min"] for s in stats_list]
        all_meds = [s["median"] for s in stats_list]
        all_means = [s["mean"] for s in stats_list]
        all_p25 = [s["p25"] for s in stats_list]
        all_p75 = [s["p75"] for s in stats_list]
        all_p95 = [s["p95"] for s in stats_list]
        all_maxs = [s["max"] for s in stats_list]

        print(f"{label:>8} {len(stats_list):>6} "
              f"{np.mean(all_mins):>6.1f} {np.mean(all_p25):>6.1f} {np.mean(all_meds):>6.1f} "
              f"{np.mean(all_means):>7.1f} {np.mean(all_p75):>6.1f} {np.mean(all_p95):>7.1f} "
              f"{np.mean(all_maxs):>7.1f}")

    # Per-day detail
    print(f"\n  Per-day spread medians:")
    for label, stats_list in [("TXFD6", txfd6_spreads), ("TMFD6", tmfd6_spreads)]:
        if not stats_list:
            continue
        print(f"  {label}:")
        for s in stats_list:
            print(f"    {s.get('date', '?'):>12}: med={s['median']:.0f}  mean={s['mean']:.1f}  p95={s['p95']:.0f}")


def write_report(
    txfd6_tmfd6_econ: dict,
    tmfd6_tmfd6_econ: dict,
    txfd6_txfd6_econ: dict,
    output_path: str,
):
    """Write markdown report."""
    lines = []
    lines.append("# R47 TMFD6 Economics Backtest Results")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append("R47 is deployed on TMFD6 (Mini-TAIEX futures) but was validated with TXFD6 economics.")
    lines.append("This backtest compares profitability under correct TMFD6 fee structure.")
    lines.append("")
    lines.append("| Parameter | TXFD6 | TMFD6 |")
    lines.append("|-----------|-------|-------|")
    lines.append("| Point value | 200 NTD/pt | 10 NTD/pt |")
    lines.append("| Fee/side | 48 NTD | 20 NTD |")
    lines.append("| RT cost (pts) | 0.48 pts | 4.0 pts |")
    lines.append("")

    # --- Section 1: TXFD6 data, TXFD6 economics (baseline) ---
    lines.append("## 1. Baseline: TXFD6 data + TXFD6 economics")
    lines.append("")
    _append_results_table(lines, txfd6_txfd6_econ)

    # --- Section 2: TXFD6 data, TMFD6 economics ---
    lines.append("## 2. TXFD6 data + TMFD6 economics (same dynamics, TMFD6 fees)")
    lines.append("")
    _append_results_table(lines, txfd6_tmfd6_econ)

    # --- Section 3: TMFD6 data, TMFD6 economics ---
    lines.append("## 3. TMFD6 data + TMFD6 economics (actual TMFD6)")
    lines.append("")
    _append_results_table(lines, tmfd6_tmfd6_econ)

    # --- Spread comparison ---
    lines.append("## 4. Spread Distribution Comparison")
    lines.append("")
    lines.append("| Symbol | Days | Median Spread | Mean Spread | P95 Spread |")
    lines.append("|--------|------|--------------|-------------|------------|")
    for label, rd in [("TXFD6", txfd6_txfd6_econ), ("TMFD6", tmfd6_tmfd6_econ)]:
        ss = rd["spread_stats"]
        if ss:
            med = np.mean([s["median"] for s in ss])
            mean = np.mean([s["mean"] for s in ss])
            p95 = np.mean([s["p95"] for s in ss])
            lines.append(f"| {label} | {len(ss)} | {med:.1f} | {mean:.1f} | {p95:.1f} |")

    # --- Conclusion ---
    lines.append("")
    lines.append("## 5. Conclusion")
    lines.append("")

    # Check if any TMFD6 config is profitable
    any_profitable = False
    for rd in [txfd6_tmfd6_econ, tmfd6_tmfd6_econ]:
        econ = rd["econ"]
        for qf_results in rd["results"].values():
            for thr_result in qf_results.values():
                gross = thr_result["gross_pts"]
                fee = thr_result["fills"] * econ["fee_per_side_ntd"] / econ["point_value_ntd"]
                if gross - fee > 0:
                    any_profitable = True

    if any_profitable:
        lines.append("Some TMFD6 configurations show positive PnL. See tables above for details.")
    else:
        lines.append("**NO TMFD6 configuration is profitable.** The 4.0 pts/RT fee cost")
        lines.append("consumes all spread capture. R47 maker strategy is only viable on TXFD6 (0.48 pts/RT).")

    lines.append("")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport written to: {output_path}")


def _append_results_table(lines: list[str], run_data: dict):
    econ = run_data["econ"]
    results = run_data["results"]
    point_value = econ["point_value_ntd"]
    fee_per_side = econ["fee_per_side_ntd"]
    fee_rt_pts = econ["fee_rt_pts"]
    days = run_data["days_loaded"]

    lines.append(f"Economics: 1pt={point_value} NTD, fee={fee_per_side} NTD/side, RT={fee_rt_pts:.2f} pts | {days} days")
    lines.append("")
    lines.append("| Queue | Spread | Fills | Trips | WR% | Gross pts | Fees pts | NET pts | NET NTD | Mean/RT pts | NTD/day |")
    lines.append("|-------|--------|-------|-------|-----|-----------|----------|---------|---------|-------------|---------|")

    for qf in sorted(results.keys(), reverse=True):
        for thr in sorted(results[qf].keys()):
            r = results[qf][thr]
            fills = r["fills"]
            trips = r["trips"]
            wins = r["wins"]
            gross = r["gross_pts"]
            fee_pts = fills * fee_per_side / point_value
            net_pts = gross - fee_pts
            net_ntd = net_pts * point_value
            wr = wins / trips * 100 if trips else 0
            mean_rt = (gross / trips - fee_rt_pts) if trips else 0
            daily_ntd = net_ntd / days if days else 0

            lines.append(
                f"| {qf:.1f} | >={thr} | {fills} | {trips} | {wr:.1f}% | "
                f"{gross:+.0f} | {-fee_pts:.0f} | {net_pts:+.0f} | {net_ntd:+,.0f} | "
                f"{mean_rt:+.3f} | {daily_ntd:+,.0f} |"
            )

    lines.append("")


def main():
    print("=" * 85)
    print("  R47 TMFD6 Economics Backtest")
    print("  Comparing TXFD6 vs TMFD6 fee structures across all available days")
    print("=" * 85)

    queue_fracs = [1.0, 0.5]
    spread_thresholds = [1, 3, 4, 5, 6]

    # Get available days
    print("\nFetching trading days...")
    txfd6_days = get_trading_days("TXFD6")
    tmfd6_days = get_trading_days("TMFD6")
    print(f"  TXFD6: {len(txfd6_days)} days")
    print(f"  TMFD6: {len(tmfd6_days)} days")

    # --- Run 1: TXFD6 data, TXFD6 economics (baseline) ---
    print(f"\n{'#'*85}")
    print(f"# RUN 1: TXFD6 data + TXFD6 economics (baseline)")
    print(f"{'#'*85}")
    txfd6_txfd6 = run_multiday(
        "TXFD6", TXFD6_ECONOMICS, txfd6_days,
        queue_fracs, spread_thresholds,
    )
    print_summary_table(txfd6_txfd6, "TXFD6", "TXFD6 data + TXFD6 economics (BASELINE)")

    # --- Run 2: TXFD6 data, TMFD6 economics ---
    print(f"\n{'#'*85}")
    print(f"# RUN 2: TXFD6 data + TMFD6 economics")
    print(f"{'#'*85}")
    txfd6_tmfd6 = run_multiday(
        "TXFD6", TMFD6_ECONOMICS, txfd6_days,
        queue_fracs, spread_thresholds,
    )
    print_summary_table(txfd6_tmfd6, "TXFD6", "TXFD6 data + TMFD6 economics")

    # --- Run 3: TMFD6 data, TMFD6 economics ---
    print(f"\n{'#'*85}")
    print(f"# RUN 3: TMFD6 data + TMFD6 economics")
    print(f"{'#'*85}")
    tmfd6_tmfd6 = run_multiday(
        "TMFD6", TMFD6_ECONOMICS, tmfd6_days,
        queue_fracs, spread_thresholds,
    )
    print_summary_table(tmfd6_tmfd6, "TMFD6", "TMFD6 data + TMFD6 economics")

    # --- Spread comparison ---
    print_spread_comparison(
        txfd6_txfd6["spread_stats"],
        tmfd6_tmfd6["spread_stats"],
    )

    # --- Write report ---
    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "outputs", "team_artifacts", "alpha-research", "r47_tmfd6_economics.md",
    )
    write_report(txfd6_tmfd6, tmfd6_tmfd6, txfd6_txfd6, output_path)

    # Final verdict
    print(f"\n{'*'*85}")
    print(f"*  FINAL VERDICT")
    print(f"{'*'*85}")
    print(f"  TXFD6 RT cost: {TXFD6_ECONOMICS['fee_rt_pts']:.2f} pts")
    print(f"  TMFD6 RT cost: {TMFD6_ECONOMICS['fee_rt_pts']:.2f} pts")
    print(f"  Cost ratio: {TMFD6_ECONOMICS['fee_rt_pts'] / TXFD6_ECONOMICS['fee_rt_pts']:.1f}x")
    print(f"{'*'*85}")


if __name__ == "__main__":
    main()
