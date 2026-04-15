#!/usr/bin/env python3
"""R47 Maker — TMFD6 Enhancement Sweep (CK-Direct).

Establishes baseline (max_pos=1 and max_pos=3) then tests 4 enhancement
candidates independently against baseline:
  E1: Volatility-Adaptive Spread Gate
  E2: Dynamic max_pos (Vol-Gated)
  E3: Time-of-Day Spread Multiplier
  E4: Spread Persistence Filter

Uses CK-direct backtest (queue-depletion fill model at half-queue).
Validated as ground-truth for R47 (+4,504 pts matches live +4,534).

Usage:
    uv run python research/alphas/r47_maker_pivot/tmfd6_enhancement_sweep.py
"""

from __future__ import annotations

import math
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

SCALE = 1_000_000  # Golden data scale

# TMFD6 economics
POINT_VALUE_NTD = 10
FEE_PER_SIDE_NTD = 20  # 13 comm + 7 tax
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 4.0 pts

BASE_SPREAD_THRESHOLD = 5
QUEUE_FRAC = 0.5  # Half-queue (validated match to live)


# ── CK Helpers ───────────────────────────────────────────────────────


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


# ── Data Structures ──────────────────────────────────────────────────


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
    spread_at_fill: float = 0.0


@dataclass
class EnhancementConfig:
    name: str
    max_pos: int = 1
    spread_threshold: int = BASE_SPREAD_THRESHOLD
    queue_frac: float = QUEUE_FRAC
    # E1: Volatility-Adaptive Spread Gate
    e1_enabled: bool = False
    e1_k: float = 2.0
    e1_vol_window: int = 100
    # E2: Dynamic max_pos (Vol-Gated)
    e2_enabled: bool = False
    e2_vol_window: int = 500
    # E3: Time-of-Day Spread Multiplier
    e3_enabled: bool = False
    # E4: Spread Persistence Filter
    e4_enabled: bool = False
    e4_n_consecutive: int = 3


# ── Configs ──────────────────────────────────────────────────────────

CONFIGS = [
    # Baselines
    EnhancementConfig(name="baseline_max1", max_pos=1),
    EnhancementConfig(name="baseline_max3", max_pos=3),
    # E1: Volatility-Adaptive Spread Gate
    EnhancementConfig(name="E1_k1.5", max_pos=1, e1_enabled=True, e1_k=1.5),
    EnhancementConfig(name="E1_k2.0", max_pos=1, e1_enabled=True, e1_k=2.0),
    EnhancementConfig(name="E1_k3.0", max_pos=1, e1_enabled=True, e1_k=3.0),
    # E2: Dynamic max_pos (Vol-Gated)
    EnhancementConfig(name="E2_dynamic", max_pos=3, e2_enabled=True),
    # E3: Time-of-Day Spread Multiplier
    EnhancementConfig(name="E3_ToD", max_pos=1, e3_enabled=True),
    # E4: Spread Persistence Filter
    EnhancementConfig(name="E4_N3", max_pos=1, e4_enabled=True, e4_n_consecutive=3),
    EnhancementConfig(name="E4_N5", max_pos=1, e4_enabled=True, e4_n_consecutive=5),
    EnhancementConfig(name="E4_N10", max_pos=1, e4_enabled=True, e4_n_consecutive=10),
]


# ── Session time helper ──────────────────────────────────────────────


def _get_session_phase(ts_ns: int) -> str:
    """Classify timestamp into session phase.

    TAIFEX day session: 08:45 - 13:45 (Taiwan time, UTC+8).
    First 30 min = 08:45 - 09:15, Last 30 min = 13:15 - 13:45.
    """
    ts_s = ts_ns / 1e9
    local_s = ts_s + 8 * 3600  # UTC+8
    day_s = local_s % 86400
    hour = int(day_s // 3600)
    minute = int((day_s % 3600) // 60)
    hhmm = hour * 100 + minute

    if 845 <= hhmm < 915:
        return "first30"
    elif 1315 <= hhmm < 1345:
        return "last30"
    return "mid"


# ── Core Backtest Engine ─────────────────────────────────────────────


def run_backtest(
    ba: dict,
    ticks: dict,
    cfg: EnhancementConfig,
) -> tuple[list[FillRecord], int, dict]:
    """Run single-day backtest with enhancement config.

    Returns: (fills, final_position, stats_dict)
    """
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

    # Enhancement state
    mid_history: list[float] = []  # For E1/E2 volatility
    vol_history: list[float] = []  # For E2 percentile tracking
    consecutive_above = 0  # For E4

    # Stats
    spread_sum = 0.0
    spread_count = 0
    spread_above_base = 0
    quote_opportunities = 0

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
            cur_ts = ba_time
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            spread_sum += spread_pts
            spread_count += 1
            mid = (cur_bid + cur_ask) / (2.0 * SCALE)

            # ── Compute effective spread threshold ──
            effective_threshold = cfg.spread_threshold

            # E1: Volatility-Adaptive Spread Gate
            if cfg.e1_enabled:
                mid_history.append(mid)
                if len(mid_history) > cfg.e1_vol_window + 1:
                    mid_history.pop(0)
                if len(mid_history) >= 20:
                    arr = np.array(mid_history)
                    returns = np.diff(arr) / arr[:-1]
                    rolling_std = float(np.std(returns))
                    # k * vol * mid gives threshold in absolute price points
                    adaptive_pts = cfg.e1_k * rolling_std * mid
                    effective_threshold = max(
                        BASE_SPREAD_THRESHOLD, int(round(adaptive_pts))
                    )

            # E3: Time-of-Day Spread Multiplier
            if cfg.e3_enabled:
                phase = _get_session_phase(cur_ts)
                if phase == "first30":
                    effective_threshold = max(
                        1, int(round(effective_threshold * 1.5))
                    )
                elif phase == "last30":
                    effective_threshold = max(
                        1, int(round(effective_threshold * 1.3))
                    )

            # E2: Dynamic max_pos (Vol-Gated)
            effective_max_pos = cfg.max_pos
            if cfg.e2_enabled:
                if not cfg.e1_enabled:
                    mid_history.append(mid)
                    if len(mid_history) > cfg.e2_vol_window + 1:
                        mid_history.pop(0)
                if len(mid_history) >= 50:
                    arr = np.array(mid_history)
                    returns = np.diff(arr) / arr[:-1]
                    vol = float(np.std(returns))
                    vol_history.append(vol)
                    if len(vol_history) >= 100:
                        vol_arr = np.array(vol_history[-2000:])
                        p25 = float(np.percentile(vol_arr, 25))
                        p75 = float(np.percentile(vol_arr, 75))
                        if vol < p25:
                            effective_max_pos = 3
                        elif vol < p75:
                            effective_max_pos = 2
                        else:
                            effective_max_pos = 1

            # E4: Spread Persistence Filter
            e4_allow = True
            if cfg.e4_enabled:
                if spread_pts >= effective_threshold:
                    consecutive_above += 1
                else:
                    consecutive_above = 0
                if consecutive_above < cfg.e4_n_consecutive:
                    e4_allow = False

            if spread_pts >= BASE_SPREAD_THRESHOLD:
                spread_above_base += 1

            # Cancel if price moved
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            # Quote only if spread >= effective_threshold AND e4 allows
            if spread_pts >= effective_threshold and e4_allow:
                quote_opportunities += 1
                if buy_order is None and position < effective_max_pos:
                    qp = max(1, int(cur_bid_v * cfg.queue_frac))
                    buy_order = OpenOrder(
                        side="buy",
                        price=cur_bid,
                        placed_ts=cur_ts,
                        queue_pos=qp,
                    )
                if sell_order is None and position > -effective_max_pos:
                    qp = max(1, int(cur_ask_v * cfg.queue_frac))
                    sell_order = OpenOrder(
                        side="sell",
                        price=cur_ask,
                        placed_ts=cur_ts,
                        queue_pos=qp,
                    )
        else:
            trade_p = t_price[ti]
            trade_v = t_vol[ti]
            ti += 1

            cur_mid = (
                (cur_bid + cur_ask) / (2 * SCALE) if cur_bid > 0 else 0
            )
            cur_spr = (cur_ask - cur_bid) / SCALE if cur_bid > 0 else 0

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(
                        FillRecord(
                            side="buy",
                            price_pts=buy_order.price / SCALE,
                            ts=tk_time,
                            mid_at_fill=cur_mid,
                            spread_at_fill=cur_spr,
                        )
                    )
                    position += 1
                    buy_order = None

            if sell_order is not None and trade_p >= sell_order.price:
                sell_order.queue_pos -= trade_v
                if sell_order.queue_pos <= 0:
                    fills.append(
                        FillRecord(
                            side="sell",
                            price_pts=sell_order.price / SCALE,
                            ts=tk_time,
                            mid_at_fill=cur_mid,
                            spread_at_fill=cur_spr,
                        )
                    )
                    position -= 1
                    sell_order = None

    stats = {
        "avg_spread": spread_sum / spread_count if spread_count > 0 else 0,
        "pct_above_base": (
            spread_above_base / spread_count * 100 if spread_count > 0 else 0
        ),
        "quote_opportunities": quote_opportunities,
        "n_ba_events": spread_count,
    }
    return fills, position, stats


# ── PnL Computation ──────────────────────────────────────────────────


def compute_fifo_pnl(
    fills: list[FillRecord],
) -> tuple[float, int, int, list[float]]:
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


def compute_equity_curve(
    daily_nets: list[float],
) -> tuple[float, float]:
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
    sharpe = (
        float(mean_d / std_d * np.sqrt(252)) if std_d > 1e-9 else 0.0
    )

    return max_dd, sharpe


# ── Main ─────────────────────────────────────────────────────────────


def main():
    sys.stdout.write(
        f"{'=' * 120}\n"
        f"  R47 TMFD6 Enhancement Sweep (CK-Direct)\n"
        f"  Economics: 1pt = {POINT_VALUE_NTD} NTD, fee = {FEE_PER_SIDE_NTD} NTD/side, "
        f"RT cost = {FEE_RT_PTS:.1f} pts\n"
        f"  Queue frac = {QUEUE_FRAC} (half-queue, validated)\n"
        f"{'=' * 120}\n"
    )

    # Get trading days
    dates = get_trading_days()
    sys.stdout.write(f"\nAvailable trading days: {len(dates)}\n")

    # Load all days upfront
    sys.stdout.write("\nLoading data from ClickHouse...\n")
    day_data: dict[str, tuple[dict, dict]] = {}
    for date in dates:
        sys.stdout.write(f"  {date}...")
        sys.stdout.flush()
        t0 = time.time()
        ba, ticks = load_day(date)
        elapsed = time.time() - t0
        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            sys.stdout.write(f" SKIP (no data)\n")
            continue
        n_ba = len(ba["exch_ts"])
        n_tk = len(ticks.get("exch_ts", []))
        sys.stdout.write(f" {n_ba} BA, {n_tk} ticks ({elapsed:.1f}s)\n")
        day_data[date] = (ba, ticks)

    n_days = len(day_data)
    sys.stdout.write(f"\nLoaded {n_days} days.\n")

    # Spread distribution per day
    sys.stdout.write(
        f"\nSpread distribution per day:\n"
        f"  {'Date':>12} {'Min':>6} {'P25':>6} {'Med':>6} {'Mean':>7} {'P75':>6} "
        f"{'P95':>7} {'>=5%':>7} {'>=6%':>7}\n"
    )
    for date in sorted(day_data.keys()):
        ba, _ = day_data[date]
        spreads = (ba["ask1_p"] - ba["bid1_p"]) / SCALE
        valid = spreads[spreads > 0]
        n = len(valid)
        pct5 = (valid >= 5).sum() / n * 100 if n > 0 else 0
        pct6 = (valid >= 6).sum() / n * 100 if n > 0 else 0
        sys.stdout.write(
            f"  {date:>12} {valid.min():>6.0f} {np.percentile(valid, 25):>6.0f} "
            f"{np.median(valid):>6.0f} {valid.mean():>7.1f} "
            f"{np.percentile(valid, 75):>6.0f} {np.percentile(valid, 95):>7.0f} "
            f"{pct5:>6.1f}% {pct6:>6.1f}%\n"
        )

    # Run all configs
    sys.stdout.write(
        f"\nRunning {len(CONFIGS)} configs x {n_days} days "
        f"= {len(CONFIGS) * n_days} backtests...\n"
    )

    # Results: config_name -> {daily_nets, daily_gross, fills, trips, wins, ...}
    results: dict[str, dict] = {}
    for cfg in CONFIGS:
        results[cfg.name] = {
            "daily_nets": [],
            "daily_gross": [],
            "daily_fills": [],
            "daily_spreads": [],
            "total_fills": 0,
            "total_trips": 0,
            "total_wins": 0,
            "total_quotes": 0,
            "trip_pnls": [],
        }

    sweep_t0 = time.time()
    sorted_dates = sorted(day_data.keys())

    for di, date in enumerate(sorted_dates):
        ba, ticks = day_data[date]
        sys.stdout.write(f"  [{di + 1}/{n_days}] {date}: ")
        sys.stdout.flush()

        for cfg in CONFIGS:
            fills, final_pos, stats = run_backtest(ba, ticks, cfg)
            gross, trips, wins, trip_pnls = compute_fifo_pnl(fills)
            fee_pts = len(fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
            net = gross - fee_pts

            r = results[cfg.name]
            r["daily_nets"].append(net)
            r["daily_gross"].append(gross)
            r["daily_fills"].append(len(fills))
            r["daily_spreads"].append(stats["avg_spread"])
            r["total_fills"] += len(fills)
            r["total_trips"] += trips
            r["total_wins"] += wins
            r["total_quotes"] += stats["quote_opportunities"]
            r["trip_pnls"].extend(trip_pnls)

        # Print baseline_max1 result for this day
        b1_net = results["baseline_max1"]["daily_nets"][-1]
        b1_fills = results["baseline_max1"]["daily_fills"][-1]
        sys.stdout.write(
            f"base1={b1_net:>+7.0f} fills={b1_fills:>4}  "
        )
        b3_net = results["baseline_max3"]["daily_nets"][-1]
        b3_fills = results["baseline_max3"]["daily_fills"][-1]
        sys.stdout.write(f"base3={b3_net:>+7.0f} fills={b3_fills:>4}\n")

    sweep_elapsed = time.time() - sweep_t0
    sys.stdout.write(f"\nSweep completed in {sweep_elapsed:.1f}s\n")

    # ── Build summary table ──────────────────────────────────────────
    summary_rows = []
    for cfg in CONFIGS:
        r = results[cfg.name]
        daily_nets = r["daily_nets"]
        total_fills = r["total_fills"]
        total_trips = r["total_trips"]
        total_wins = r["total_wins"]

        total_net = sum(daily_nets)
        total_gross = sum(r["daily_gross"])
        arr = np.array(daily_nets)
        mean_daily = float(arr.mean()) if len(arr) > 0 else 0
        std_daily = float(arr.std(ddof=1)) if len(arr) > 1 else 0
        t_stat = (
            mean_daily / (std_daily / math.sqrt(len(arr)))
            if std_daily > 1e-9
            else 0
        )

        wr = total_wins / total_trips * 100 if total_trips > 0 else 0
        mean_pnl_per_rt = (
            (total_gross / total_trips - FEE_RT_PTS) if total_trips > 0 else 0
        )
        max_dd, sharpe = compute_equity_curve(daily_nets)
        n_winning = int((arr > 0).sum())
        avg_spread = (
            sum(r["daily_spreads"]) / len(r["daily_spreads"])
            if r["daily_spreads"]
            else 0
        )

        summary_rows.append(
            {
                "name": cfg.name,
                "total_net": total_net,
                "total_gross": total_gross,
                "pnl_per_day": mean_daily,
                "std_daily": std_daily,
                "pnl_per_day_ntd": mean_daily * POINT_VALUE_NTD,
                "t_stat": t_stat,
                "wr": wr,
                "n_fills": total_fills,
                "n_rt": total_trips,
                "n_winning": n_winning,
                "mean_pnl_per_rt": mean_pnl_per_rt,
                "max_dd": max_dd,
                "sharpe": sharpe,
                "n_days": n_days,
                "avg_spread": avg_spread,
                "daily_nets": daily_nets,
            }
        )

    # ── Print ranked table ───────────────────────────────────────────
    sys.stdout.write(
        f"\n{'=' * 140}\n"
        f"  RESULTS TABLE ({n_days} days, queue_frac={QUEUE_FRAC})\n"
        f"{'=' * 140}\n"
    )
    header = (
        f"{'Config':<18} {'TotalNet':>10} {'PnL/day':>9} {'NTD/day':>9} "
        f"{'t-stat':>7} {'WR%':>6} {'WinD':>5} "
        f"{'Fills':>7} {'RTs':>7} {'Net/RT':>8} "
        f"{'MaxDD':>8} {'Sharpe':>7} {'AvgSpr':>7}"
    )
    sys.stdout.write(header + "\n")
    sys.stdout.write("-" * 140 + "\n")

    for row in summary_rows:
        sys.stdout.write(
            f"{row['name']:<18} {row['total_net']:>+10.0f} "
            f"{row['pnl_per_day']:>+9.1f} {row['pnl_per_day_ntd']:>+9.0f} "
            f"{row['t_stat']:>+7.2f} {row['wr']:>5.1f}% "
            f"{row['n_winning']:>3}/{row['n_days']:<1} "
            f"{row['n_fills']:>7} {row['n_rt']:>7} "
            f"{row['mean_pnl_per_rt']:>+8.3f} "
            f"{row['max_dd']:>8.0f} {row['sharpe']:>+7.2f} "
            f"{row['avg_spread']:>7.1f}\n"
        )

    sys.stdout.write("-" * 140 + "\n")

    # ── Markdown table ───────────────────────────────────────────────
    sys.stdout.write(f"\n\nMARKDOWN TABLE:\n\n")
    sys.stdout.write(
        "| Config | Total PnL | PnL/Day | MaxDD | Trades | WinRate | PnL/Trade | t-stat |\n"
    )
    sys.stdout.write(
        "|--------|-----------|---------|-------|--------|---------|-----------|--------|\n"
    )
    for row in summary_rows:
        pnl_per_trade = (
            row["total_net"] / row["n_fills"] if row["n_fills"] > 0 else 0
        )
        wr_pct = row["wr"] / 100
        sys.stdout.write(
            f"| {row['name']} | {row['total_net']:+.0f} | "
            f"{row['pnl_per_day']:+.1f} | {row['max_dd']:.0f} | "
            f"{row['n_fills']} | {wr_pct:.1%} | "
            f"{pnl_per_trade:+.3f} | {row['t_stat']:+.2f} |\n"
        )

    # ── Enhancement delta vs baseline ────────────────────────────────
    baseline_max1 = next(
        (r for r in summary_rows if r["name"] == "baseline_max1"), None
    )
    baseline_max3 = next(
        (r for r in summary_rows if r["name"] == "baseline_max3"), None
    )

    if baseline_max1:
        sys.stdout.write(
            f"\n{'=' * 100}\n"
            f"ENHANCEMENT DELTA vs BASELINE (max_pos=1, spread>={BASE_SPREAD_THRESHOLD})\n"
            f"{'=' * 100}\n"
        )
        b = baseline_max1
        sys.stdout.write(
            f"{'Config':<18} {'Delta PnL':>10} {'Delta/Day':>10} "
            f"{'Delta WR':>9} {'Delta DD':>9} {'Verdict':>10}\n"
        )
        sys.stdout.write("-" * 80 + "\n")
        for row in summary_rows:
            if row["name"].startswith("baseline"):
                continue
            dp = row["total_net"] - b["total_net"]
            dd = row["pnl_per_day"] - b["pnl_per_day"]
            dw = row["wr"] - b["wr"]
            ddd = row["max_dd"] - b["max_dd"]
            if dp > 0 and ddd <= 0:
                verdict = "BETTER"
            elif dp > 0:
                verdict = "MIXED"
            else:
                verdict = "WORSE"
            sys.stdout.write(
                f"{row['name']:<18} {dp:>+10.0f} {dd:>+10.1f} "
                f"{dw:>+8.1f}% {ddd:>+9.0f} {verdict:>10}\n"
            )
        sys.stdout.write("-" * 80 + "\n")

    if baseline_max3:
        sys.stdout.write(
            f"\nE2 delta vs baseline_max3:\n"
        )
        e2 = next(
            (r for r in summary_rows if r["name"] == "E2_dynamic"), None
        )
        if e2:
            b3 = baseline_max3
            dp = e2["total_net"] - b3["total_net"]
            dd = e2["pnl_per_day"] - b3["pnl_per_day"]
            ddd = e2["max_dd"] - b3["max_dd"]
            sys.stdout.write(
                f"  E2_dynamic vs baseline_max3: PnL delta={dp:>+.0f}, "
                f"DD delta={ddd:>+.0f}\n"
            )

    # ── Per-day breakdown for top configs ─────────────────────────────
    sys.stdout.write(
        f"\n{'=' * 120}\n"
        f"PER-DAY BREAKDOWN (baseline_max1 vs baseline_max3)\n"
        f"{'=' * 120}\n"
    )
    sys.stdout.write(
        f"  {'Date':>12} {'base1_net':>10} {'base1_fill':>10} "
        f"{'base3_net':>10} {'base3_fill':>10} {'cumul1':>10} {'cumul3':>10}\n"
    )
    cumul1 = cumul3 = 0.0
    b1r = results["baseline_max1"]
    b3r = results["baseline_max3"]
    for di, date in enumerate(sorted_dates):
        n1 = b1r["daily_nets"][di]
        f1 = b1r["daily_fills"][di]
        n3 = b3r["daily_nets"][di]
        f3 = b3r["daily_fills"][di]
        cumul1 += n1
        cumul3 += n3
        sys.stdout.write(
            f"  {date:>12} {n1:>+10.0f} {f1:>10} "
            f"{n3:>+10.0f} {f3:>10} {cumul1:>+10.0f} {cumul3:>+10.0f}\n"
        )

    sys.stdout.write(f"\nTotal elapsed: {sweep_elapsed:.1f}s\n")


if __name__ == "__main__":
    main()
