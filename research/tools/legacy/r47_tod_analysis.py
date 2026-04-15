#!/usr/bin/env python3
"""
R47 Time-of-Day Analysis — per-30min bucket PnL breakdown.

Runs CK-direct backtest (spread>=4, max_pos=3, half-queue) on TXFD6,
then segments fills into 30-min windows to evaluate Direction B.

TAIFEX day session: 08:45-13:45 Taiwan time (UTC+8)
10 buckets of 30min each.
"""

import math
import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000
# TXFD6: 1 pt = 200 NTD
POINT_VALUE_NTD = 200
FEE_PER_SIDE_NTD = 48
FEE_PER_FILL_PTS = FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 0.24 pts per fill

SPREAD_THRESHOLD = 4  # pts
MAX_POS = 3
QUEUE_FRAC = 0.5

# TAIFEX session buckets (Taiwan local = UTC+8)
# Session: 08:45 - 13:45
UTC_OFFSET_NS = 8 * 3600 * 1_000_000_000
BUCKETS = [
    ("08:45-09:15", 8 * 60 + 45, 9 * 60 + 15),
    ("09:15-09:45", 9 * 60 + 15, 9 * 60 + 45),
    ("09:45-10:15", 9 * 60 + 45, 10 * 60 + 15),
    ("10:15-10:45", 10 * 60 + 15, 10 * 60 + 45),
    ("10:45-11:15", 10 * 60 + 45, 11 * 60 + 15),
    ("11:15-11:45", 11 * 60 + 15, 11 * 60 + 45),
    ("11:45-12:15", 11 * 60 + 45, 12 * 60 + 15),
    ("12:15-12:45", 12 * 60 + 15, 12 * 60 + 45),
    ("12:45-13:15", 12 * 60 + 45, 13 * 60 + 15),
    ("13:15-13:45", 13 * 60 + 15, 13 * 60 + 45),
]


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


def ts_to_bucket(ts_ns: int) -> int:
    """Convert nanosecond timestamp to bucket index (0-9), or -1 if outside session."""
    local_ns = ts_ns + UTC_OFFSET_NS
    # Seconds since midnight
    secs_in_day = (local_ns // 1_000_000_000) % 86400
    mins = secs_in_day // 60
    for i, (_, start_min, end_min) in enumerate(BUCKETS):
        if start_min <= mins < end_min:
            return i
    return -1


def ts_to_date_str(ts_ns: int) -> str:
    """Convert ns timestamp to date string for grouping."""
    local_ns = ts_ns + UTC_OFFSET_NS
    secs = local_ns // 1_000_000_000
    import datetime
    dt = datetime.datetime.fromtimestamp(secs, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d")


@dataclass
class Fill:
    side: str
    price_pts: float
    ts: int
    bucket: int
    spread_at_fill: float


def run_backtest_day(date: str) -> list[Fill]:
    """Run CK-direct backtest for one day, return fills with bucket info."""
    ba_sql = f"""
    SELECT exch_ts,
           bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
           asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
    FROM hft.market_data
    WHERE symbol = 'TXFD6' AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    tick_sql = f"""
    SELECT exch_ts, price_scaled AS price, volume
    FROM hft.market_data
    WHERE symbol = 'TXFD6' AND type = 'Tick'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
    ORDER BY exch_ts
    """
    ba = ck_query_numpy(ba_sql)
    ticks = ck_query_numpy(tick_sql)

    if not ba or "exch_ts" not in ba:
        return []

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
    buy_order_price = 0
    buy_order_qpos = 0.0
    sell_order_price = 0
    sell_order_qpos = 0.0
    has_buy = False
    has_sell = False
    fills: list[Fill] = []

    ba_i = 0
    ti = 0

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            cur_bid = int(bid1_p[ba_i])
            cur_ask = int(ask1_p[ba_i])
            cur_bid_v = int(bid1_v[ba_i])
            cur_ask_v = int(ask1_v[ba_i])
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            # Cancel orders if price moved
            if has_buy and buy_order_price != cur_bid:
                has_buy = False
            if has_sell and sell_order_price != cur_ask:
                has_sell = False

            # Place new orders if spread qualifies
            if spread_pts >= SPREAD_THRESHOLD:
                if not has_buy and position < MAX_POS:
                    buy_order_price = cur_bid
                    buy_order_qpos = max(1, int(cur_bid_v * QUEUE_FRAC))
                    has_buy = True
                if not has_sell and position > -MAX_POS:
                    sell_order_price = cur_ask
                    sell_order_qpos = max(1, int(cur_ask_v * QUEUE_FRAC))
                    has_sell = True
        else:
            trade_p = int(t_price[ti])
            trade_v = int(t_vol[ti])
            fill_ts = int(tick_ts[ti])
            ti += 1

            cur_spread = (cur_ask - cur_bid) / SCALE if cur_bid > 0 else 0

            if has_buy and trade_p <= buy_order_price:
                buy_order_qpos -= trade_v
                if buy_order_qpos <= 0:
                    bucket = ts_to_bucket(fill_ts)
                    fills.append(Fill(
                        side="buy",
                        price_pts=buy_order_price / SCALE,
                        ts=fill_ts,
                        bucket=bucket,
                        spread_at_fill=cur_spread,
                    ))
                    position += 1
                    has_buy = False

            if has_sell and trade_p >= sell_order_price:
                sell_order_qpos -= trade_v
                if sell_order_qpos <= 0:
                    bucket = ts_to_bucket(fill_ts)
                    fills.append(Fill(
                        side="sell",
                        price_pts=sell_order_price / SCALE,
                        ts=fill_ts,
                        bucket=bucket,
                        spread_at_fill=cur_spread,
                    ))
                    position -= 1
                    has_sell = False

    return fills


def compute_pnl_by_bucket(all_fills: dict[str, list[Fill]]) -> None:
    """Compute and print per-bucket PnL statistics."""
    n_days = len(all_fills)
    dates = sorted(all_fills.keys())

    # Per-bucket, per-day PnL
    # PnL attribution: each fill gets attributed to its bucket
    # For round-trips spanning buckets, we split: each fill gets half the RT PnL
    # Simpler approach: compute FIFO PnL for the whole day, then attribute
    # each RT's PnL to the bucket of the ENTRY fill (more actionable for ToD gating)

    bucket_daily_pnl: dict[int, list[float]] = {i: [] for i in range(10)}
    bucket_fills: dict[int, int] = {i: 0 for i in range(10)}
    bucket_spreads: dict[int, list[float]] = {i: [] for i in range(10)}
    bucket_winning_days: dict[int, int] = {i: 0 for i in range(10)}

    for date in dates:
        fills = all_fills[date]
        if not fills:
            for i in range(10):
                bucket_daily_pnl[i].append(0.0)
            continue

        # FIFO matching with bucket attribution
        # Attribute RT PnL to the bucket of the OPENING fill
        buy_q: list[tuple[float, int]] = []  # (price, bucket)
        sell_q: list[tuple[float, int]] = []

        day_bucket_pnl: dict[int, float] = {i: 0.0 for i in range(10)}
        day_bucket_fills: dict[int, int] = {i: 0 for i in range(10)}

        for f in fills:
            if f.bucket < 0:
                continue
            bucket_spreads[f.bucket].append(f.spread_at_fill)

            if f.side == "buy":
                if sell_q:
                    sp, sb = sell_q.pop(0)
                    pnl = sp - f.price_pts - 2 * FEE_PER_FILL_PTS
                    # Attribute to the OPENING fill's bucket
                    day_bucket_pnl[sb] += pnl
                    day_bucket_fills[sb] += 1
                    day_bucket_fills[f.bucket] += 1
                else:
                    buy_q.append((f.price_pts, f.bucket))
                    day_bucket_fills[f.bucket] += 1
            else:
                if buy_q:
                    bp, bb = buy_q.pop(0)
                    pnl = f.price_pts - bp - 2 * FEE_PER_FILL_PTS
                    day_bucket_pnl[bb] += pnl
                    day_bucket_fills[bb] += 1
                    day_bucket_fills[f.bucket] += 1
                else:
                    sell_q.append((f.price_pts, f.bucket))
                    day_bucket_fills[f.bucket] += 1

        for i in range(10):
            bucket_daily_pnl[i].append(day_bucket_pnl[i])
            bucket_fills[i] += day_bucket_fills[i]
            if day_bucket_pnl[i] > 0:
                bucket_winning_days[i] += 1

    # Print results
    print("=" * 100)
    print("R47 TXFD6 Time-of-Day Analysis — per-30min bucket")
    print(f"Config: spread>={SPREAD_THRESHOLD}, max_pos={MAX_POS}, queue_frac={QUEUE_FRAC}")
    print(f"Days: {n_days} ({dates[0]} to {dates[-1]})")
    print("=" * 100)
    print()
    print(f"{'Window':<14} {'Fills':>6} {'TotalPnL':>10} {'MeanPnL':>10} {'StdPnL':>10} "
          f"{'t-stat':>8} {'Win/Tot':>8} {'AvgSprd':>8}")
    print("-" * 85)

    for i in range(10):
        label = BUCKETS[i][0]
        pnls = bucket_daily_pnl[i]
        total_fills = bucket_fills[i]
        total_pnl = sum(pnls)
        mean_pnl = np.mean(pnls) if pnls else 0
        std_pnl = np.std(pnls, ddof=1) if len(pnls) > 1 else 0
        t_stat = mean_pnl / (std_pnl / math.sqrt(n_days)) if std_pnl > 0 else 0
        wins = bucket_winning_days[i]
        avg_spread = np.mean(bucket_spreads[i]) if bucket_spreads[i] else 0

        flag = ""
        if t_stat <= -2.0 and mean_pnl <= -250:
            flag = " *** LOSING WINDOW"
        elif t_stat >= 2.0 and mean_pnl >= 250:
            flag = " *** WINNING WINDOW"

        print(f"{label:<14} {total_fills:>6} {total_pnl:>10.1f} {mean_pnl:>10.1f} {std_pnl:>10.1f} "
              f"{t_stat:>8.2f} {wins:>3}/{n_days:<3} {avg_spread:>8.2f}{flag}")

    print()

    # Overall summary
    all_pnl = [sum(bucket_daily_pnl[i][d] for i in range(10)) for d in range(n_days)]
    total = sum(all_pnl)
    mean = np.mean(all_pnl)
    std = np.std(all_pnl, ddof=1)
    t = mean / (std / math.sqrt(n_days)) if std > 0 else 0
    total_fills = sum(bucket_fills.values())
    print(f"{'TOTAL':<14} {total_fills:>6} {total:>10.1f} {mean:>10.1f} {std:>10.1f} {t:>8.2f}")
    print()

    # Decision
    has_losing = False
    has_winning = False
    for i in range(10):
        pnls = bucket_daily_pnl[i]
        mean_pnl = np.mean(pnls) if pnls else 0
        std_pnl = np.std(pnls, ddof=1) if len(pnls) > 1 else 0
        t_stat = mean_pnl / (std_pnl / math.sqrt(n_days)) if std_pnl > 0 else 0
        if t_stat <= -2.0 and mean_pnl <= -250:
            has_losing = True
            print(f"SIGNAL: {BUCKETS[i][0]} is a statistically significant losing window "
                  f"(t={t_stat:.2f}, mean={mean_pnl:.1f} pts/day)")
        if t_stat >= 2.0 and mean_pnl >= 250:
            has_winning = True

    if not has_losing:
        print("RESULT: No window meets losing threshold (t<=-2.0 AND mean<=-250 pts)")
        print("CONCLUSION: Direction B has NO empirical support → KILL Direction B")
        print("R47 current config (spread>=4, max_pos=3, uniform quoting) is ALREADY OPTIMAL")
    else:
        print("RESULT: Losing window(s) found → Direction B has potential")
        print("Next: implement position-aware ToD filter excluding losing windows")


def main():
    dates_raw = ck_query("""
        SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) AS dt
        FROM hft.market_data
        WHERE symbol = 'TXFD6' AND type = 'BidAsk'
          AND toDate(fromUnixTimestamp64Nano(exch_ts)) >= '2026-03-19'
        ORDER BY dt
        FORMAT TSV
    """)
    dates = [d.strip() for d in dates_raw.split("\n") if d.strip()]
    print(f"Found {len(dates)} trading days: {dates[0]} to {dates[-1]}")

    all_fills: dict[str, list[Fill]] = {}
    for date in dates:
        print(f"  Processing {date}...", end=" ", flush=True)
        fills = run_backtest_day(date)
        all_fills[date] = fills
        n_buy = sum(1 for f in fills if f.side == "buy")
        n_sell = sum(1 for f in fills if f.side == "sell")
        print(f"{len(fills)} fills (buy={n_buy}, sell={n_sell})")

    compute_pnl_by_bucket(all_fills)


if __name__ == "__main__":
    main()
