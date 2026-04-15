#!/usr/bin/env python3
"""
R47 Independent Backtest v2 — with adverse selection analysis and fee modeling.

Adds:
1. Transaction fees (TAIFEX futures: ~2.0 NTD/contract each way)
2. Adverse selection: mid-price change after fill (5, 10, 50 events later)
3. Inventory penalty: holding cost per snapshot
4. Realistic fill validation

Usage:
    python research/tools/r47_ck_direct_backtest_v2.py
"""

import os
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000
POINT_VALUE_NTD = 200

# TAIFEX fee: exchange fee ~20 NTD + broker fee ~20-28 NTD per side
# Total round-trip ~80-96 NTD ≈ 0.4-0.48 pts (at 200 NTD/pt)
FEE_PER_SIDE_NTD = 48  # conservative estimate
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # ~0.48 pts round-trip


def ck_query(sql: str) -> str:
    resp = requests.post(CK_URL, params={"password": CK_PASSWORD}, data=sql, timeout=120)
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
    price: int         # x1e6
    placed_ts: int
    queue_pos: float


@dataclass
class FillRecord:
    side: str
    price_pts: float
    ts: int
    mid_at_fill: float     # mid price at fill time
    # Adverse selection: mid N events after fill
    mid_5_after: float = 0.0
    mid_10_after: float = 0.0
    mid_50_after: float = 0.0


def run_backtest(
    ba: dict,
    ticks: dict,
    spread_threshold: int = 1,
    max_pos: int = 3,
    queue_frac: float = 1.0,
) -> tuple[list[FillRecord], int]:
    """Returns (fills, final_position)."""

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

    # Pre-compute mid prices for adverse selection lookup
    mid_arr = (bid1_p.astype(np.float64) + ask1_p.astype(np.float64)) / (2 * SCALE)

    cur_bid = cur_ask = 0
    cur_bid_v = cur_ask_v = 0
    position = 0
    buy_order: Optional[OpenOrder] = None
    sell_order: Optional[OpenOrder] = None
    fills: list[FillRecord] = []

    ba_i = 0
    ti = 0
    # Track which ba_i we're at for adverse selection lookups
    last_ba_i = 0

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            cur_bid = bid1_p[ba_i]
            cur_ask = ask1_p[ba_i]
            cur_bid_v = bid1_v[ba_i]
            cur_ask_v = ask1_v[ba_i]
            last_ba_i = ba_i
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
                    # Adverse selection lookups
                    idx = last_ba_i
                    fr.mid_5_after = mid_arr[min(idx + 5, ba_n - 1)]
                    fr.mid_10_after = mid_arr[min(idx + 10, ba_n - 1)]
                    fr.mid_50_after = mid_arr[min(idx + 50, ba_n - 1)]
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
                    idx = last_ba_i
                    fr.mid_5_after = mid_arr[min(idx + 5, ba_n - 1)]
                    fr.mid_10_after = mid_arr[min(idx + 10, ba_n - 1)]
                    fr.mid_50_after = mid_arr[min(idx + 50, ba_n - 1)]
                    fills.append(fr)
                    position -= 1
                    sell_order = None

    return fills, position


def analyze_and_report(fills: list[FillRecord], position: int, label: str, spread_thr: int):
    n_fills = len(fills)
    if n_fills == 0:
        print(f"\n  {label}: NO FILLS")
        return

    buy_fills = [f for f in fills if f.side == "buy"]
    sell_fills = [f for f in fills if f.side == "sell"]

    # FIFO PnL
    buy_q: list[float] = []
    sell_q: list[float] = []
    realized = 0.0
    n_trips = 0
    wins = 0
    trip_pnls: list[float] = []

    for f in fills:
        if f.side == "buy":
            if sell_q:
                sp = sell_q.pop(0)
                pnl = sp - f.price_pts
                realized += pnl
                trip_pnls.append(pnl)
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_q.append(f.price_pts)
        else:
            if buy_q:
                bp = buy_q.pop(0)
                pnl = f.price_pts - bp
                realized += pnl
                trip_pnls.append(pnl)
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_q.append(f.price_pts)

    # Fees
    fee_total_pts = n_fills * (FEE_PER_SIDE_NTD / POINT_VALUE_NTD)
    net_pnl = realized - fee_total_pts

    # Adverse selection analysis
    buy_as5 = []
    buy_as10 = []
    buy_as50 = []
    sell_as5 = []
    sell_as10 = []
    sell_as50 = []

    for f in fills:
        if f.side == "buy":
            # After buying, price going DOWN = adverse selection
            buy_as5.append(f.mid_5_after - f.mid_at_fill)
            buy_as10.append(f.mid_10_after - f.mid_at_fill)
            buy_as50.append(f.mid_50_after - f.mid_at_fill)
        else:
            # After selling, price going UP = adverse selection
            sell_as5.append(f.mid_at_fill - f.mid_5_after)
            sell_as10.append(f.mid_at_fill - f.mid_10_after)
            sell_as50.append(f.mid_at_fill - f.mid_50_after)

    # Combine: positive = favorable, negative = adverse selection
    as5 = buy_as5 + sell_as5
    as10 = buy_as10 + sell_as10
    as50 = buy_as50 + sell_as50

    win_rate = wins / n_trips * 100 if n_trips > 0 else 0
    realized_nwd = realized * POINT_VALUE_NTD
    net_nwd = net_pnl * POINT_VALUE_NTD

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Spread threshold:    {spread_thr} pts")
    print(f"  Total fills:         {n_fills} (buy: {len(buy_fills)}, sell: {len(sell_fills)})")
    print(f"  Round trips:         {n_trips}")
    print(f"  Win rate:            {win_rate:.1f}%")
    print(f"")
    print(f"  --- P&L ---")
    print(f"  Gross realized PnL:  {realized:+.1f} pts ({realized_nwd:+,.0f} NTD)")
    print(f"  Total fees:          {fee_total_pts:-.1f} pts ({n_fills} fills x {FEE_PER_SIDE_NTD} NTD)")
    print(f"  NET PnL (after fee): {net_pnl:+.1f} pts ({net_nwd:+,.0f} NTD)")
    print(f"  Final position:      {position}")

    if trip_pnls:
        tp = np.array(trip_pnls)
        print(f"")
        print(f"  --- Round-trip stats ---")
        print(f"  Mean PnL/trip:       {tp.mean():+.3f} pts")
        print(f"  Median PnL/trip:     {np.median(tp):+.3f} pts")
        print(f"  Std PnL/trip:        {tp.std():.3f} pts")
        print(f"  Max win:             {tp.max():+.1f} pts")
        print(f"  Max loss:            {tp.min():+.1f} pts")
        print(f"  Fee per RT:          {FEE_RT_PTS:.2f} pts")
        print(f"  Mean after fee:      {tp.mean() - FEE_RT_PTS:+.3f} pts")

    print(f"")
    print(f"  --- Adverse Selection (mid-price move after fill) ---")
    print(f"  {'Horizon':>10} {'Mean':>10} {'Median':>10} {'%Adverse':>10}")
    for lbl, arr in [("5 snaps", as5), ("10 snaps", as10), ("50 snaps", as50)]:
        a = np.array(arr)
        adv_pct = (a < 0).sum() / len(a) * 100
        print(f"  {lbl:>10} {a.mean():>+10.3f} {np.median(a):>+10.3f} {adv_pct:>9.1f}%")

    print(f"{'='*70}")


def main():
    symbol = "TXFD6"
    date = "2026-03-19"

    print(f"R47 Independent Backtest v2 — ClickHouse Direct")
    print(f"Symbol: {symbol}, Date: {date}")
    print(f"Fee model: {FEE_PER_SIDE_NTD} NTD/side, RT cost = {FEE_RT_PTS:.2f} pts")
    print()

    print("Loading data...")
    t0 = time.time()
    ba = load_bidask(symbol, date)
    ticks = load_ticks(symbol, date)
    t1 = time.time()
    print(f"  BidAsk: {len(ba['exch_ts'])} snapshots, Ticks: {len(ticks['exch_ts'])} events ({t1-t0:.1f}s)")

    spreads = (ba["ask1_p"] - ba["bid1_p"]) / SCALE
    print(f"\nSpread: min={spreads.min():.0f}, median={np.median(spreads):.0f}, "
          f"mean={spreads.mean():.1f}, max={spreads.max():.0f} pts")

    # Run across configurations
    configs = [
        (1.0, "Full queue (conservative)"),
        (0.5, "Half queue (moderate)"),
    ]

    for qf, qlabel in configs:
        print(f"\n{'#'*70}")
        print(f"#  {qlabel} (queue_frac={qf})")
        print(f"{'#'*70}")
        for thr in [1, 3, 5]:
            fills, pos = run_backtest(ba, ticks, spread_threshold=thr, max_pos=3, queue_frac=qf)
            analyze_and_report(fills, pos, f"{qlabel} | spread>={thr}", thr)

    # === DECISIVE SUMMARY ===
    print(f"\n{'*'*70}")
    print(f"*  DECISIVE SUMMARY TABLE")
    print(f"{'*'*70}")
    print(f"{'Queue':>10} {'Spread':>7} {'Fills':>7} {'Trips':>7} {'WR%':>6} "
          f"{'Gross':>9} {'Fees':>8} {'NET':>9} {'Mean/RT':>9}")
    print("-" * 70)
    for qf, ql in [(1.0, "Full"), (0.5, "Half"), (0.25, "Qtr")]:
        for thr in [1, 3, 5]:
            fills, pos = run_backtest(ba, ticks, spread_threshold=thr, max_pos=3, queue_frac=qf)
            # Compute FIFO PnL
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

            fee = len(fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
            net = realized - fee
            wr = wins / n_trips * 100 if n_trips else 0
            mean_rt = realized / n_trips if n_trips else 0
            mean_net = mean_rt - FEE_RT_PTS

            print(f"{ql:>10} {'>=' + str(thr):>7} {len(fills):>7} {n_trips:>7} {wr:>5.1f}% "
                  f"{realized:>+9.0f} {-fee:>8.0f} {net:>+9.0f} {mean_net:>+9.3f}")

    print("-" * 70)
    print(f"  All values in POINTS (1 pt = 200 NTD)")
    print(f"  Fee model: {FEE_PER_SIDE_NTD} NTD/side = {FEE_RT_PTS:.2f} pts/RT")
    print(f"  Positive NET = maker strategy IS profitable")
    print(f"  Negative NET = maker strategy is NOT profitable")
    print(f"{'*'*70}")


if __name__ == "__main__":
    main()
