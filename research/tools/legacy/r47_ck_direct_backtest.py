#!/usr/bin/env python3
"""
R47 Independent Backtest — ClickHouse Direct (No hftbacktest dependency)

Cross-validates R47 maker strategy results using raw CK BidAsk + Tick data.
Fill model: queue-position based with tick-level trade detection.

PnL method: REALIZED round-trip PnL (buy then sell, or sell then buy).
Unrealized PnL marked-to-mid at end of day.

Usage:
    python research/tools/r47_ck_direct_backtest.py
"""

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

# ---------------------------------------------------------------------------
# ClickHouse connection
# ---------------------------------------------------------------------------
CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000  # CK prices are x1e6
POINT_VALUE_NTD = 200  # TXFD6: 1 point = 200 NTD


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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
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
      AND length(bids_price) >= 1
      AND length(asks_price) >= 1
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


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class OpenOrder:
    side: str          # 'buy' or 'sell'
    price: int         # scaled price (x1e6)
    placed_ts: int
    queue_pos: float   # contracts ahead of us


@dataclass
class Fill:
    side: str
    price_pts: float   # fill price in points
    ts: int


@dataclass
class BacktestResult:
    fills: list = field(default_factory=list)
    # Computed after
    realized_pnl_pts: float = 0.0
    unrealized_pnl_pts: float = 0.0
    total_pnl_pts: float = 0.0
    final_position: int = 0
    n_round_trips: int = 0
    win_rate: float = 0.0


# ---------------------------------------------------------------------------
# Core backtest: Tick-merged queue depletion fill model
# ---------------------------------------------------------------------------
def run_backtest(
    ba: dict,
    ticks: dict,
    spread_threshold: int = 1,
    max_pos: int = 3,
    queue_frac: float = 1.0,  # 1.0 = back of queue, 0.5 = mid queue
) -> BacktestResult:
    """
    Maker backtest with tick-level fill detection.

    Strategy: when spread >= threshold, place bid at best_bid, ask at best_ask.
    Fill model: track queue position; fill when queue depleted by trades.
    PnL: realized from round-trips + unrealized mark-to-mid.
    """
    result = BacktestResult()

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
    fills: list[Fill] = []

    ba_i = 0
    ti = 0

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            # BidAsk update
            cur_bid = bid1_p[ba_i]
            cur_ask = ask1_p[ba_i]
            cur_bid_v = bid1_v[ba_i]
            cur_ask_v = ask1_v[ba_i]
            cur_ts = ba_time
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            # Cancel if price moved
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            # Place orders
            if spread_pts >= spread_threshold:
                if buy_order is None and position < max_pos:
                    qp = max(1, int(cur_bid_v * queue_frac))
                    buy_order = OpenOrder(
                        side="buy", price=cur_bid,
                        placed_ts=cur_ts, queue_pos=qp,
                    )
                if sell_order is None and position > -max_pos:
                    qp = max(1, int(cur_ask_v * queue_frac))
                    sell_order = OpenOrder(
                        side="sell", price=cur_ask,
                        placed_ts=cur_ts, queue_pos=qp,
                    )
        else:
            # Tick (trade) event
            trade_p = t_price[ti]
            trade_v = t_vol[ti]
            cur_ts = tk_time
            ti += 1

            # Buy fill: trade at or below our bid depletes queue
            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(Fill(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=cur_ts,
                    ))
                    position += 1
                    buy_order = None

            # Sell fill: trade at or above our ask depletes queue
            if sell_order is not None and trade_p >= sell_order.price:
                sell_order.queue_pos -= trade_v
                if sell_order.queue_pos <= 0:
                    fills.append(Fill(
                        side="sell",
                        price_pts=sell_order.price / SCALE,
                        ts=cur_ts,
                    ))
                    position -= 1
                    sell_order = None

    # --- Compute PnL from fills using FIFO matching ---
    result.fills = fills
    result.final_position = position

    # FIFO PnL: match buys with sells in order
    buy_queue: list[float] = []  # prices in points
    sell_queue: list[float] = []
    realized = 0.0
    n_trips = 0
    wins = 0

    for f in fills:
        if f.side == "buy":
            if sell_queue:
                # Close a short: realized = sell_price - buy_price
                sell_p = sell_queue.pop(0)
                pnl = sell_p - f.price_pts
                realized += pnl
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                buy_queue.append(f.price_pts)
        else:  # sell
            if buy_queue:
                # Close a long: realized = sell_price - buy_price
                buy_p = buy_queue.pop(0)
                pnl = f.price_pts - buy_p
                realized += pnl
                n_trips += 1
                if pnl > 0:
                    wins += 1
            else:
                sell_queue.append(f.price_pts)

    result.realized_pnl_pts = realized
    result.n_round_trips = n_trips
    result.win_rate = (wins / n_trips * 100) if n_trips > 0 else 0

    # Unrealized: mark open position to last mid
    last_mid = (cur_bid + cur_ask) / (2 * SCALE) if cur_bid > 0 else 0
    if buy_queue:
        # Long position remaining
        for bp in buy_queue:
            result.unrealized_pnl_pts += last_mid - bp
    if sell_queue:
        # Short position remaining
        for sp in sell_queue:
            result.unrealized_pnl_pts += sp - last_mid

    result.total_pnl_pts = result.realized_pnl_pts + result.unrealized_pnl_pts

    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def report(result: BacktestResult, label: str, spread_thr: int) -> None:
    fills = result.fills
    n_fills = len(fills)
    buy_fills = sum(1 for f in fills if f.side == "buy")
    sell_fills = sum(1 for f in fills if f.side == "sell")
    total_nwd = result.total_pnl_pts * POINT_VALUE_NTD

    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"  Spread threshold:  {spread_thr} pts")
    print(f"  Total fills:       {n_fills} (buy: {buy_fills}, sell: {sell_fills})")
    print(f"  Round trips:       {result.n_round_trips}")
    print(f"  Win rate:          {result.win_rate:.1f}%")
    print(f"  Realized PnL:      {result.realized_pnl_pts:+.1f} pts")
    print(f"  Unrealized PnL:    {result.unrealized_pnl_pts:+.1f} pts")
    print(f"  TOTAL PnL:         {result.total_pnl_pts:+.1f} pts  ({total_nwd:+,.0f} NTD)")
    print(f"  Final position:    {result.final_position}")

    if n_fills > 0:
        # Position trace
        pos = 0
        max_p = 0
        min_p = 0
        for f in fills:
            pos += 1 if f.side == "buy" else -1
            max_p = max(max_p, pos)
            min_p = min(min_p, pos)
        print(f"  Max position:      {max_p}")
        print(f"  Min position:      {min_p}")

        # Fill price stats
        buy_prices = [f.price_pts for f in fills if f.side == "buy"]
        sell_prices = [f.price_pts for f in fills if f.side == "sell"]
        if buy_prices and sell_prices:
            avg_buy = np.mean(buy_prices)
            avg_sell = np.mean(sell_prices)
            print(f"  Avg buy price:     {avg_buy:.1f}")
            print(f"  Avg sell price:    {avg_sell:.1f}")
            print(f"  Avg spread earned: {avg_sell - avg_buy:.2f} pts")
    print(f"{'='*65}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    symbol = "TXFD6"
    date = "2026-03-19"

    print(f"Loading BidAsk data for {symbol} on {date}...")
    t0 = time.time()
    ba = load_bidask(symbol, date)
    t1 = time.time()
    print(f"  Loaded {len(ba.get('exch_ts', []))} BidAsk snapshots in {t1-t0:.1f}s")

    print(f"Loading Tick data for {symbol} on {date}...")
    ticks = load_ticks(symbol, date)
    t2 = time.time()
    print(f"  Loaded {len(ticks.get('exch_ts', []))} Tick events in {t2-t1:.1f}s")

    # Spread distribution
    if "bid1_p" in ba and "ask1_p" in ba:
        spreads = (ba["ask1_p"] - ba["bid1_p"]) / SCALE
        print(f"\nSpread distribution (points):")
        print(f"  Min: {spreads.min():.0f}, Median: {np.median(spreads):.0f}, "
              f"Mean: {spreads.mean():.1f}, Max: {spreads.max():.0f}")
        for thr in [1, 2, 3, 4, 5]:
            pct = (spreads >= thr).sum() / len(spreads) * 100
            print(f"  Spread >= {thr}: {pct:.1f}% of snapshots")

    print("\n" + "#" * 65)
    print("#  FULL QUEUE (back-of-queue, conservative)")
    print("#" * 65)
    for threshold in [1, 3, 5]:
        r = run_backtest(ba, ticks, spread_threshold=threshold, max_pos=3, queue_frac=1.0)
        report(r, f"Full queue | spread>={threshold}", threshold)

    print("\n" + "#" * 65)
    print("#  HALF QUEUE (mid-queue, moderate)")
    print("#" * 65)
    for threshold in [1, 3, 5]:
        r = run_backtest(ba, ticks, spread_threshold=threshold, max_pos=3, queue_frac=0.5)
        report(r, f"Half queue | spread>={threshold}", threshold)

    print("\n" + "#" * 65)
    print("#  FRONT QUEUE (1/4 queue, optimistic)")
    print("#" * 65)
    for threshold in [1, 3, 5]:
        r = run_backtest(ba, ticks, spread_threshold=threshold, max_pos=3, queue_frac=0.25)
        report(r, f"Quarter queue | spread>={threshold}", threshold)

    # Summary table
    print("\n" + "=" * 65)
    print("  SUMMARY TABLE")
    print("=" * 65)
    print(f"{'Queue':>12} {'Spread':>8} {'Fills':>8} {'Trips':>8} {'WinRate':>8} {'Realized':>10} {'Total':>10}")
    print("-" * 65)
    for qf, ql in [(1.0, "Full"), (0.5, "Half"), (0.25, "Quarter")]:
        for thr in [1, 3, 5]:
            r = run_backtest(ba, ticks, spread_threshold=thr, max_pos=3, queue_frac=qf)
            print(f"{ql:>12} {'>=' + str(thr):>8} {len(r.fills):>8} {r.n_round_trips:>8} "
                  f"{r.win_rate:>7.1f}% {r.realized_pnl_pts:>+10.1f} {r.total_pnl_pts:>+10.1f}")
    print("=" * 65)
    print("  All PnL values in POINTS (1 pt = 200 NTD)")
    print("  Negative = LOSS for maker strategy")


if __name__ == "__main__":
    main()
