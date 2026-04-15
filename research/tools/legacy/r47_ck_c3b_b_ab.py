#!/usr/bin/env python3
"""R47 C3b-B Stale Quote Suppression — CK Direct A/B Test.

Compares two modes across all 12 backtest days:
  Baseline: Cancel and resubmit orders EVERY LOB update (reset queue position)
  C3b-B:    Keep order when price unchanged (preserve queue position)

Uses the CK direct backtest method (no hftbacktest dependency).
"""

import json
import os
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
POINT_VALUE_NTD = 200
FEE_PER_SIDE_NTD = 48
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD


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
    SELECT exch_ts, bids_price[1] AS bid1_p, bids_vol[1] AS bid1_v,
           asks_price[1] AS ask1_p, asks_vol[1] AS ask1_v
    FROM hft.market_data
    WHERE symbol = '{symbol}' AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    return ck_query_numpy(sql)


def load_ticks(symbol: str, date: str) -> dict:
    sql = f"""
    SELECT exch_ts, price_scaled AS price, volume
    FROM hft.market_data
    WHERE symbol = '{symbol}' AND type = 'Tick'
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


def run_backtest(
    ba: dict,
    ticks: dict,
    spread_threshold: int = 3,
    max_pos: int = 3,
    queue_frac: float = 0.5,
    stale_suppression: bool = False,
) -> tuple[list[FillRecord], int, int]:
    """Run backtest. Returns (fills, final_position, stale_count)."""

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
    stale_count = 0

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
                buy_order = None
                sell_order = None
                continue

            if stale_suppression:
                # C3b-B: Only cancel if price CHANGED
                if buy_order is not None and buy_order.price != cur_bid:
                    buy_order = None
                if sell_order is not None and sell_order.price != cur_ask:
                    sell_order = None
            else:
                # Baseline: Cancel and resubmit EVERY update (reset queue pos)
                buy_order = None
                sell_order = None

            if spread_pts >= spread_threshold:
                if buy_order is None and position < max_pos:
                    qp = max(1, int(cur_bid_v * queue_frac))
                    buy_order = OpenOrder(
                        side="buy", price=cur_bid,
                        placed_ts=ba_time, queue_pos=qp,
                    )
                elif buy_order is not None and position < max_pos:
                    stale_count += 1  # order kept, queue advancing
                if sell_order is None and position > -max_pos:
                    qp = max(1, int(cur_ask_v * queue_frac))
                    sell_order = OpenOrder(
                        side="sell", price=cur_ask,
                        placed_ts=ba_time, queue_pos=qp,
                    )
                elif sell_order is not None and position > -max_pos:
                    stale_count += 1
        else:
            trade_p = t_price[ti]
            trade_v = t_vol[ti]
            ti += 1

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(FillRecord(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=tk_time,
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
                    ))
                    position -= 1
                    sell_order = None

    return fills, position, stale_count


def compute_pnl(fills: list[FillRecord]) -> tuple[float, int, int]:
    """FIFO PnL. Returns (realized_pts, n_trips, wins)."""
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


def main():
    symbol = "TXFD6"
    dates = [
        "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24",
        "2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31",
        "2026-04-01", "2026-04-02", "2026-04-07", "2026-04-08",
    ]

    # Use half-queue spread>=3 (closest to validated +4,504 baseline)
    queue_frac = 0.5
    spread_threshold = 3

    print(f"R47 C3b-B A/B Test — CK Direct Backtest")
    print(f"Symbol: {symbol}, Queue: {queue_frac}, Spread>={spread_threshold}")
    print(f"Fee: {FEE_PER_SIDE_NTD} NTD/side = {FEE_RT_PTS:.2f} pts/RT")
    print()

    results = []
    totals = {
        "base_pnl": 0, "base_fills": 0, "base_trips": 0, "base_wins": 0,
        "treat_pnl": 0, "treat_fills": 0, "treat_trips": 0, "treat_wins": 0,
        "treat_stale": 0,
    }

    print(f"{'Date':>12} | {'Base PnL':>10} {'Base F':>7} {'Base PPF':>9} | "
          f"{'Treat PnL':>10} {'Treat F':>7} {'Treat PPF':>9} {'Stale':>7} | "
          f"{'dPnL':>8} {'dPPF':>8}")
    print("-" * 110)

    for date in dates:
        t0 = time.time()
        ba = load_bidask(symbol, date)
        ticks = load_ticks(symbol, date)
        if not ba or len(ba.get("exch_ts", [])) == 0:
            print(f"{date:>12} | SKIP (no data)")
            continue

        # Baseline: no stale suppression (cancel+resubmit every update)
        b_fills, b_pos, _ = run_backtest(
            ba, ticks, spread_threshold=spread_threshold,
            max_pos=3, queue_frac=queue_frac, stale_suppression=False,
        )
        b_realized, b_trips, b_wins = compute_pnl(b_fills)
        b_fee = len(b_fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
        b_net = b_realized - b_fee

        # Treatment: C3b-B stale suppression (keep order when price unchanged)
        t_fills, t_pos, t_stale = run_backtest(
            ba, ticks, spread_threshold=spread_threshold,
            max_pos=3, queue_frac=queue_frac, stale_suppression=True,
        )
        t_realized, t_trips, t_wins = compute_pnl(t_fills)
        t_fee = len(t_fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
        t_net = t_realized - t_fee

        b_ppf = b_net / len(b_fills) if b_fills else 0
        t_ppf = t_net / len(t_fills) if t_fills else 0
        d_pnl = t_net - b_net
        d_ppf = t_ppf - b_ppf

        elapsed = time.time() - t0
        print(f"{date:>12} | {b_net:>+10.1f} {len(b_fills):>7} {b_ppf:>+9.3f} | "
              f"{t_net:>+10.1f} {len(t_fills):>7} {t_ppf:>+9.3f} {t_stale:>7} | "
              f"{d_pnl:>+8.1f} {d_ppf:>+8.3f}  ({elapsed:.1f}s)")

        totals["base_pnl"] += b_net
        totals["base_fills"] += len(b_fills)
        totals["base_trips"] += b_trips
        totals["base_wins"] += b_wins
        totals["treat_pnl"] += t_net
        totals["treat_fills"] += len(t_fills)
        totals["treat_trips"] += t_trips
        totals["treat_wins"] += t_wins
        totals["treat_stale"] += t_stale

        results.append({
            "date": date,
            "baseline": {"net_pnl": b_net, "fills": len(b_fills), "trips": b_trips, "ppf": round(b_ppf, 4)},
            "treatment": {"net_pnl": t_net, "fills": len(t_fills), "trips": t_trips, "ppf": round(t_ppf, 4), "stale": t_stale},
            "delta": {"pnl": round(d_pnl, 1), "ppf": round(d_ppf, 4)},
        })

    print("-" * 110)

    b_total_ppf = totals["base_pnl"] / totals["base_fills"] if totals["base_fills"] else 0
    t_total_ppf = totals["treat_pnl"] / totals["treat_fills"] if totals["treat_fills"] else 0

    print(f"{'TOTAL':>12} | {totals['base_pnl']:>+10.1f} {totals['base_fills']:>7} {b_total_ppf:>+9.3f} | "
          f"{totals['treat_pnl']:>+10.1f} {totals['treat_fills']:>7} {t_total_ppf:>+9.3f} {totals['treat_stale']:>7} | "
          f"{totals['treat_pnl'] - totals['base_pnl']:>+8.1f} {t_total_ppf - b_total_ppf:>+8.3f}")
    print()
    print(f"Baseline PnL/fill:  {b_total_ppf:+.4f}")
    print(f"Treatment PnL/fill: {t_total_ppf:+.4f}")
    print(f"Delta PnL/fill:     {t_total_ppf - b_total_ppf:+.4f}")
    print(f"Treatment wins {sum(1 for r in results if r['delta']['pnl'] > 0)}/{len(results)} days")

    # Save results
    out = {
        "config": {
            "method": "CK_direct_backtest",
            "queue_frac": queue_frac,
            "spread_threshold": spread_threshold,
            "fee_per_side_ntd": FEE_PER_SIDE_NTD,
        },
        "summary": {
            "baseline": {"total_pnl": round(totals["base_pnl"], 1), "total_fills": totals["base_fills"], "ppf": round(b_total_ppf, 4)},
            "treatment": {"total_pnl": round(totals["treat_pnl"], 1), "total_fills": totals["treat_fills"], "ppf": round(t_total_ppf, 4), "stale": totals["treat_stale"]},
            "delta": {"pnl": round(totals["treat_pnl"] - totals["base_pnl"], 1), "ppf": round(t_total_ppf - b_total_ppf, 4)},
        },
        "per_day": results,
    }
    out_path = "outputs/team_artifacts/alpha-research/R51_optimal_execution/c3b_b_ck_direct_ab.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nResults saved to {out_path}")


if __name__ == "__main__":
    main()
