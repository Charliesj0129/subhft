#!/usr/bin/env python3
"""
R47 TMFD6 QI Skew Parameter Sweep.

Fine-grained QI threshold sweep with max_pos=2, testing actual quote widening
(not just suppression). Sweeps spread_threshold × qi_threshold × widen_ticks × queue_frac.

Usage:
    python research/tools/r47_qi_sweep.py
"""

import os
import sys
import time
from dataclasses import dataclass
from itertools import product
from typing import Optional

import numpy as np
import requests

# --- ClickHouse connection ---
CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

# --- TMFD6 Economics ---
SCALE = 1_000_000  # prices in CK are x1e6
POINT_VALUE_NTD = 10
FEE_PER_SIDE_NTD = 20
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 4.0 pts
TICK_SIZE_SCALED = 1 * SCALE  # 1 point = 1_000_000 in scaled


# --- CK helpers ---

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


# --- Feature computation ---

def compute_qi(bid1_v: np.ndarray, ask1_v: np.ndarray) -> np.ndarray:
    """Queue imbalance: (bid_v - ask_v) / (bid_v + ask_v + 1e-6)."""
    total = bid1_v.astype(np.float64) + ask1_v.astype(np.float64) + 1e-6
    return (bid1_v.astype(np.float64) - ask1_v.astype(np.float64)) / total


# --- Sweep config ---

@dataclass(slots=True)
class SweepConfig:
    spread_threshold: int
    qi_threshold: float  # 1.0 = disabled (baseline)
    widen_ticks: int
    queue_frac: float
    max_pos: int = 2

    @property
    def name(self) -> str:
        qi_str = "OFF" if self.qi_threshold >= 1.0 else f"{self.qi_threshold:.2f}"
        return f"sp{self.spread_threshold}_qi{qi_str}_w{self.widen_ticks}_q{self.queue_frac}"

    @property
    def is_baseline(self) -> bool:
        return self.qi_threshold >= 1.0


@dataclass(slots=True)
class OpenOrder:
    side: str
    price: int  # x1e6
    placed_ts: int
    queue_pos: float


@dataclass(slots=True)
class FillRecord:
    side: str
    price_pts: float
    ts: int
    ba_idx: int
    mid_at_fill: float


# --- Core simulation with QI widening ---

def run_backtest(
    ba: dict,
    ticks: dict,
    cfg: SweepConfig,
) -> tuple[list[FillRecord], int, int]:
    """Run R47 maker backtest with QI skew widening.

    Returns (fills, final_position, total_quote_opportunities).
    """
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

    # Pre-compute QI array
    qi_arr = compute_qi(bid1_v, ask1_v) if not cfg.is_baseline else None

    cur_bid = cur_ask = 0
    cur_bid_v = cur_ask_v = 0
    position = 0
    buy_order: Optional[OpenOrder] = None
    sell_order: Optional[OpenOrder] = None
    fills: list[FillRecord] = []
    total_quote_opps = 0

    ba_i = 0
    ti = 0
    last_ba_i = 0

    widen_amount = cfg.widen_ticks * TICK_SIZE_SCALED

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            cur_bid = int(bid1_p[ba_i])
            cur_ask = int(ask1_p[ba_i])
            cur_bid_v = int(bid1_v[ba_i])
            cur_ask_v = int(ask1_v[ba_i])
            last_ba_i = ba_i
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            # Cancel stale orders (price changed at TOB)
            if buy_order is not None and buy_order.price != cur_bid:
                # Check if widened order should also be cancelled
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            if spread_pts >= cfg.spread_threshold:
                total_quote_opps += 1

                # Compute widened prices
                my_bid = cur_bid
                my_ask = cur_ask

                if qi_arr is not None:
                    qi_val = qi_arr[last_ba_i]
                    if qi_val > cfg.qi_threshold:
                        # Buying pressure -> widen ask (move ask up)
                        my_ask = cur_ask + widen_amount
                    elif qi_val < -cfg.qi_threshold:
                        # Selling pressure -> widen bid (move bid down)
                        my_bid = cur_bid - widen_amount

                # Place/update orders
                if buy_order is None and position < cfg.max_pos:
                    qp = max(1, int(cur_bid_v * cfg.queue_frac))
                    # If bid was widened down, we join at worse price with
                    # queue priority = full queue (we're alone at that level)
                    if my_bid < cur_bid:
                        qp = 1  # no queue ahead at widened level
                    buy_order = OpenOrder(
                        side="buy", price=my_bid,
                        placed_ts=ba_time, queue_pos=qp,
                    )

                if sell_order is None and position > -cfg.max_pos:
                    qp = max(1, int(cur_ask_v * cfg.queue_frac))
                    if my_ask > cur_ask:
                        qp = 1  # no queue ahead at widened level
                    sell_order = OpenOrder(
                        side="sell", price=my_ask,
                        placed_ts=ba_time, queue_pos=qp,
                    )

                # Cancel existing if now at wrong (non-widened) price
                if buy_order is not None and buy_order.price != my_bid:
                    if position < cfg.max_pos:
                        qp = max(1, int(cur_bid_v * cfg.queue_frac))
                        if my_bid < cur_bid:
                            qp = 1
                        buy_order = OpenOrder(
                            side="buy", price=my_bid,
                            placed_ts=ba_time, queue_pos=qp,
                        )
                    else:
                        buy_order = None

                if sell_order is not None and sell_order.price != my_ask:
                    if position > -cfg.max_pos:
                        qp = max(1, int(cur_ask_v * cfg.queue_frac))
                        if my_ask > cur_ask:
                            qp = 1
                        sell_order = OpenOrder(
                            side="sell", price=my_ask,
                            placed_ts=ba_time, queue_pos=qp,
                        )
                    else:
                        sell_order = None

        else:
            # Process tick
            trade_p = int(t_price[ti])
            trade_v = int(t_vol[ti])
            ti += 1

            cur_mid = (cur_bid + cur_ask) / (2.0 * SCALE) if cur_bid > 0 else 0.0

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(FillRecord(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=tk_time,
                        ba_idx=last_ba_i,
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
                        ba_idx=last_ba_i,
                        mid_at_fill=cur_mid,
                    ))
                    position -= 1
                    sell_order = None

    return fills, position, total_quote_opps


# --- Results computation ---

def compute_day_results(
    fills: list[FillRecord],
    position: int,
    total_quote_opps: int,
    mid_arr: np.ndarray,
    ba_n: int,
) -> dict:
    """Compute PnL and AS metrics for a single day."""
    n_fills = len(fills)
    if n_fills == 0:
        return {
            "fills": 0, "trips": 0, "wins": 0, "gross_pts": 0.0,
            "net_pts": 0.0, "mean_rt": 0.0, "trip_pnls": [],
            "as10_mean": 0.0, "position": position,
        }

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

    # AS10: mid-price move 10 snapshots after fill
    as10_vals = []
    for f in fills:
        idx_after = min(f.ba_idx + 10, ba_n - 1)
        mid_after = mid_arr[idx_after]
        if f.side == "buy":
            as10_vals.append(mid_after - f.mid_at_fill)
        else:
            as10_vals.append(f.mid_at_fill - mid_after)
    as10_mean = float(np.mean(as10_vals)) if as10_vals else 0.0

    fee_pts = n_fills * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
    net_pts = realized - fee_pts
    mean_rt = (realized / n_trips - FEE_RT_PTS) if n_trips > 0 else 0.0

    return {
        "fills": n_fills,
        "trips": n_trips,
        "wins": wins,
        "gross_pts": realized,
        "net_pts": net_pts,
        "mean_rt": mean_rt,
        "trip_pnls": trip_pnls,
        "as10_mean": as10_mean,
        "position": position,
    }


# --- Multi-day aggregation ---

def aggregate_results(day_results: list[dict]) -> dict:
    """Aggregate per-day results into summary."""
    total_fills = sum(d["fills"] for d in day_results)
    total_trips = sum(d["trips"] for d in day_results)
    total_wins = sum(d["wins"] for d in day_results)
    total_gross = sum(d["gross_pts"] for d in day_results)
    total_net = sum(d["net_pts"] for d in day_results)
    days = len(day_results)

    daily_net = np.array([d["net_pts"] for d in day_results])
    win_days = int(np.sum(daily_net > 0))

    # t-statistic
    if len(daily_net) > 1 and daily_net.std() > 0:
        t_stat = float(daily_net.mean() / (daily_net.std() / np.sqrt(len(daily_net))))
    else:
        t_stat = 0.0

    # Sharpe (annualized, 252 trading days)
    if len(daily_net) > 1 and daily_net.std() > 0:
        sharpe = float(daily_net.mean() / daily_net.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    # Max drawdown (cumulative daily PnL)
    cum = np.cumsum(daily_net)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    max_dd = float(dd.max()) if len(dd) > 0 else 0.0

    # AS10 weighted average
    as10_sum = sum(d["as10_mean"] * d["fills"] for d in day_results)
    as10_count = total_fills
    as10_mean = as10_sum / as10_count if as10_count > 0 else 0.0

    mean_rt = (total_gross / total_trips - FEE_RT_PTS) if total_trips > 0 else 0.0
    wr = total_wins / total_trips * 100 if total_trips > 0 else 0.0
    ntd_day = (total_net * POINT_VALUE_NTD / days) if days > 0 else 0.0
    pnl_day = total_net / days if days > 0 else 0.0

    return {
        "fills": total_fills,
        "trips": total_trips,
        "wins": total_wins,
        "gross_pts": total_gross,
        "net_pts": total_net,
        "days": days,
        "daily_net": daily_net,
        "win_days": win_days,
        "wr": wr,
        "mean_rt": mean_rt,
        "ntd_day": ntd_day,
        "pnl_day": pnl_day,
        "t_stat": t_stat,
        "sharpe": sharpe,
        "max_dd": max_dd,
        "as10_mean": as10_mean,
    }


# --- Main sweep ---

def build_sweep_configs() -> list[SweepConfig]:
    """Build the full parameter grid."""
    spread_thresholds = [5]
    qi_thresholds = [0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80, 1.0]
    widen_ticks_list = [1, 2, 3]
    queue_fracs = [0.5, 1.0]

    configs = []
    for sp, qi, wt, qf in product(spread_thresholds, qi_thresholds, widen_ticks_list, queue_fracs):
        # For baseline (qi=1.0), widen_ticks doesn't matter — only run widen=1
        if qi >= 1.0 and wt > 1:
            continue
        configs.append(SweepConfig(
            spread_threshold=sp,
            qi_threshold=qi,
            widen_ticks=wt,
            queue_frac=qf,
            max_pos=2,
        ))
    return configs


def run_sweep(symbol: str, dates: list[str], configs: list[SweepConfig]) -> dict[str, dict]:
    """Run all configs across all days.

    Returns: {cfg.name: aggregated_results}
    """
    # Pre-group configs by (spread, queue_frac) to reuse data loads
    day_results: dict[str, list[dict]] = {c.name: [] for c in configs}

    for di, date in enumerate(dates):
        sys.stdout.write(f"  [{di+1}/{len(dates)}] {symbol} {date} ... ")
        sys.stdout.flush()
        t0 = time.time()

        ba = load_bidask(symbol, date)
        ticks = load_ticks(symbol, date)
        load_time = time.time() - t0

        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            print("SKIP (no data)")
            continue

        ba_n = len(ba["exch_ts"])
        n_ticks = len(ticks.get("exch_ts", []))

        # Pre-compute mid array
        mid_arr = (ba["bid1_p"].astype(np.float64) + ba["ask1_p"].astype(np.float64)) / (2 * SCALE)

        n_run = 0
        for cfg in configs:
            fills, pos, opps = run_backtest(ba, ticks, cfg)
            res = compute_day_results(fills, pos, opps, mid_arr, ba_n)
            day_results[cfg.name].append(res)
            n_run += 1

        elapsed = time.time() - t0
        print(f"{ba_n} BA, {n_ticks} ticks, {n_run} cfgs ({elapsed:.1f}s, load={load_time:.1f}s)")

    # Aggregate
    results = {}
    for cfg in configs:
        results[cfg.name] = aggregate_results(day_results[cfg.name])
    return results


def print_ranked_table(results: dict[str, dict], configs: list[SweepConfig],
                       queue_frac: float, top_n: int = 20):
    """Print top N configs ranked by Mean/RT for a given queue_frac."""
    # Filter to this queue_frac
    filtered = []
    for cfg in configs:
        if cfg.queue_frac != queue_frac:
            continue
        r = results.get(cfg.name)
        if r is None or r["trips"] == 0:
            continue
        filtered.append((cfg, r))

    # Sort by mean_rt descending
    filtered.sort(key=lambda x: x[1]["mean_rt"], reverse=True)

    # Find baseline fills for fill% computation
    baseline_fills = {}
    for cfg, r in filtered:
        if cfg.is_baseline:
            baseline_fills[cfg.spread_threshold] = r["fills"]

    print(f"\n{'='*150}")
    print(f"  TOP {top_n} BY MEAN/RT | queue_frac={queue_frac} | max_pos=2")
    print(f"  TMFD6 | 1pt=10NTD | fee=20NTD/side | RT cost=4.0pts")
    print(f"{'='*150}")
    hdr = (
        f"{'Rank':>4} {'Config':<32} {'Sprd':>4} {'QI':>5} {'W':>2} "
        f"{'Fills':>7} {'Trips':>6} {'WR%':>6} {'Gross':>9} {'NET pts':>9} "
        f"{'NTD/d':>9} {'Mean/RT':>8} {'AS10':>7} {'MaxDD':>8} {'Sharpe':>7} "
        f"{'t-stat':>7} {'WinD':>5} {'Fill%':>7}"
    )
    print(hdr)
    print("-" * 150)

    for rank, (cfg, r) in enumerate(filtered[:top_n], 1):
        bf = baseline_fills.get(cfg.spread_threshold, r["fills"])
        fill_pct = r["fills"] / bf * 100 if bf > 0 else 100.0
        qi_str = "OFF" if cfg.is_baseline else f"{cfg.qi_threshold:.2f}"
        win_day_str = f"{r['win_days']}/{r['days']}"
        print(
            f"{rank:>4} {cfg.name:<32} {cfg.spread_threshold:>4} {qi_str:>5} {cfg.widen_ticks:>2} "
            f"{r['fills']:>7} {r['trips']:>6} {r['wr']:>5.1f}% {r['gross_pts']:>+9.0f} "
            f"{r['net_pts']:>+9.0f} {r['ntd_day']:>+9,.0f} {r['mean_rt']:>+8.3f} "
            f"{r['as10_mean']:>+7.3f} {r['max_dd']:>8.1f} {r['sharpe']:>+7.2f} "
            f"{r['t_stat']:>+7.2f} {win_day_str:>5} {fill_pct:>6.1f}%"
        )
    print("-" * 150)


def print_heatmap(results: dict[str, dict], configs: list[SweepConfig],
                  best_spread: int, queue_frac: float):
    """Print Mean/RT heatmap for best_spread: qi_threshold × widen_ticks."""
    qi_vals = sorted(set(c.qi_threshold for c in configs if not c.is_baseline))
    widen_vals = sorted(set(c.widen_ticks for c in configs))

    print(f"\n{'='*80}")
    print(f"  MEAN/RT HEATMAP | spread>={best_spread} | queue_frac={queue_frac} | max_pos=2")
    print(f"{'='*80}")

    # Get baseline
    bl_name = SweepConfig(best_spread, 1.0, 1, queue_frac).name
    bl_r = results.get(bl_name, {})
    bl_mrt = bl_r.get("mean_rt", 0.0)
    print(f"  Baseline Mean/RT: {bl_mrt:+.3f} pts")
    print()

    # Header
    header = f"{'QI Thresh':>10}"
    for wt in widen_vals:
        header += f" {'w='+str(wt)+' MRT':>10} {'w='+str(wt)+' Fill%':>10}"
    print(header)
    print("-" * (10 + len(widen_vals) * 22))

    for qi in qi_vals:
        row = f"{qi:>10.2f}"
        for wt in widen_vals:
            cfg_name = SweepConfig(best_spread, qi, wt, queue_frac).name
            r = results.get(cfg_name)
            if r and r["trips"] > 0:
                bl_fills = bl_r.get("fills", 1)
                fill_pct = r["fills"] / bl_fills * 100 if bl_fills > 0 else 100.0
                row += f" {r['mean_rt']:>+10.3f} {fill_pct:>9.1f}%"
            else:
                row += f" {'N/A':>10} {'N/A':>10}"
        print(row)
    print()


def print_robustness(results: dict[str, dict], configs: list[SweepConfig],
                     queue_frac: float, top_n: int = 3):
    """Print per-day PnL breakdown for top N configs."""
    # Get top N by mean_rt
    filtered = []
    for cfg in configs:
        if cfg.queue_frac != queue_frac:
            continue
        r = results.get(cfg.name)
        if r is None or r["trips"] == 0:
            continue
        filtered.append((cfg, r))
    filtered.sort(key=lambda x: x[1]["mean_rt"], reverse=True)

    print(f"\n{'='*100}")
    print(f"  ROBUSTNESS: Per-day NET PnL (pts) for top {top_n} configs | queue_frac={queue_frac}")
    print(f"{'='*100}")

    for rank, (cfg, r) in enumerate(filtered[:top_n], 1):
        daily = r["daily_net"]
        print(f"\n  #{rank} {cfg.name} | Mean/RT={r['mean_rt']:+.3f} | Sharpe={r['sharpe']:+.2f}")
        print(f"  Days: {r['days']} | Win: {r['win_days']} | Loss: {r['days'] - r['win_days']}")
        print(f"  Daily PnL: {' | '.join(f'{d:+.1f}' for d in daily)}")
        print(f"  Mean: {daily.mean():+.1f} | Std: {daily.std():.1f} | Min: {daily.min():+.1f} | Max: {daily.max():+.1f}")


def print_full_queue_validation(results_hq: dict[str, dict], results_fq: dict[str, dict],
                                configs: list[SweepConfig], top_n: int = 5):
    """Compare top configs at half-queue vs full-queue."""
    # Get top N at half-queue
    filtered_hq = []
    for cfg in configs:
        if cfg.queue_frac != 0.5:
            continue
        r = results_hq.get(cfg.name)
        if r is None or r["trips"] == 0:
            continue
        filtered_hq.append((cfg, r))
    filtered_hq.sort(key=lambda x: x[1]["mean_rt"], reverse=True)

    print(f"\n{'='*130}")
    print(f"  FULL-QUEUE VALIDATION | Top {top_n} at q=0.5 vs q=1.0")
    print(f"{'='*130}")
    hdr = (
        f"{'Config':<32} "
        f"{'q=0.5 MRT':>10} {'q=0.5 NET':>10} {'q=0.5 Sharpe':>12} "
        f"{'q=1.0 MRT':>10} {'q=1.0 NET':>10} {'q=1.0 Sharpe':>12} {'Rank Hold':>10}"
    )
    print(hdr)
    print("-" * 130)

    # Build full-queue lookup (same params but queue_frac=1.0)
    for rank, (cfg_hq, r_hq) in enumerate(filtered_hq[:top_n], 1):
        cfg_fq_name = SweepConfig(
            cfg_hq.spread_threshold, cfg_hq.qi_threshold,
            cfg_hq.widen_ticks, 1.0,
        ).name
        r_fq = results_fq.get(cfg_fq_name, {})

        fq_mrt = r_fq.get("mean_rt", 0.0) if r_fq.get("trips", 0) > 0 else 0.0
        fq_net = r_fq.get("net_pts", 0.0)
        fq_sharpe = r_fq.get("sharpe", 0.0)

        # Check if still profitable
        rank_hold = "YES" if fq_net > 0 else "NO"

        print(
            f"{cfg_hq.name:<32} "
            f"{r_hq['mean_rt']:>+10.3f} {r_hq['net_pts']:>+10.0f} {r_hq['sharpe']:>+12.2f} "
            f"{fq_mrt:>+10.3f} {fq_net:>+10.0f} {fq_sharpe:>+12.2f} {rank_hold:>10}"
        )
    print("-" * 130)


def write_report(results: dict[str, dict], configs: list[SweepConfig], output_path: str):
    """Write full markdown report."""
    lines = []
    lines.append("# R47 TMFD6 QI Skew Parameter Optimization Results")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append("Fine-grained QI (queue imbalance) skew parameter sweep for R47 on TMFD6.")
    lines.append("max_pos fixed at 2. QI skew widens the adverse side quote by widen_ticks.")
    lines.append("")
    lines.append("## Economics")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("|-----------|-------|")
    lines.append(f"| Scale | {SCALE} (prices x1e6) |")
    lines.append(f"| Point value | {POINT_VALUE_NTD} NTD/pt |")
    lines.append(f"| Fee/side | {FEE_PER_SIDE_NTD} NTD |")
    lines.append(f"| RT cost | {FEE_RT_PTS:.1f} pts |")
    lines.append(f"| max_pos | 2 |")
    lines.append("")

    lines.append("## Parameter Grid")
    lines.append("")
    lines.append("- spread_threshold: 5")
    lines.append("- qi_threshold: 0.05, 0.10, 0.15, ..., 0.80, 1.0 (disabled)")
    lines.append("- widen_ticks: 1, 2, 3")
    lines.append("- queue_frac: 0.5, 1.0")
    lines.append("")

    # Top 20 at half-queue
    for qf in [0.5, 1.0]:
        filtered = []
        for cfg in configs:
            if cfg.queue_frac != qf:
                continue
            r = results.get(cfg.name)
            if r is None or r["trips"] == 0:
                continue
            filtered.append((cfg, r))
        filtered.sort(key=lambda x: x[1]["mean_rt"], reverse=True)

        # Find baselines
        baseline_fills = {}
        for cfg, r in filtered:
            if cfg.is_baseline:
                baseline_fills[cfg.spread_threshold] = r["fills"]

        lines.append(f"## Top 20 by Mean/RT (queue_frac={qf})")
        lines.append("")
        lines.append(
            "| Rank | Config | Spread | QI | Widen | Fills | Trips | WR% | "
            "Gross | NET | NTD/d | Mean/RT | AS10 | MaxDD | Sharpe | t-stat | WinD | Fill% |"
        )
        lines.append(
            "|------|--------|--------|-----|-------|-------|-------|-----|"
            "-------|-----|-------|---------|------|-------|--------|--------|------|-------|"
        )

        for rank, (cfg, r) in enumerate(filtered[:20], 1):
            bf = baseline_fills.get(cfg.spread_threshold, r["fills"])
            fill_pct = r["fills"] / bf * 100 if bf > 0 else 100.0
            qi_str = "OFF" if cfg.is_baseline else f"{cfg.qi_threshold:.2f}"
            win_d = f"{r['win_days']}/{r['days']}"
            lines.append(
                f"| {rank} | {cfg.name} | {cfg.spread_threshold} | {qi_str} | "
                f"{cfg.widen_ticks} | {r['fills']} | {r['trips']} | {r['wr']:.1f}% | "
                f"{r['gross_pts']:+.0f} | {r['net_pts']:+.0f} | {r['ntd_day']:+,.0f} | "
                f"{r['mean_rt']:+.3f} | {r['as10_mean']:+.3f} | {r['max_dd']:.1f} | "
                f"{r['sharpe']:+.2f} | {r['t_stat']:+.2f} | {win_d} | {fill_pct:.1f}% |"
            )
        lines.append("")

    # Heatmap section for spread=5
    for sp in [5]:
        qi_vals = sorted(set(c.qi_threshold for c in configs if not c.is_baseline))
        widen_vals = sorted(set(c.widen_ticks for c in configs))

        bl_name = SweepConfig(sp, 1.0, 1, 0.5).name
        bl_r = results.get(bl_name, {})
        bl_mrt = bl_r.get("mean_rt", 0.0)

        lines.append(f"## Heatmap: spread>={sp} (q=0.5) | Baseline Mean/RT={bl_mrt:+.3f}")
        lines.append("")
        header = "| QI Threshold |"
        for wt in widen_vals:
            header += f" w={wt} Mean/RT | w={wt} Fill% |"
        lines.append(header)
        sep = "|--------------|"
        for _ in widen_vals:
            sep += "-------------|---------|"
        lines.append(sep)

        for qi in qi_vals:
            row = f"| {qi:.2f} |"
            for wt in widen_vals:
                cfg_name = SweepConfig(sp, qi, wt, 0.5).name
                r = results.get(cfg_name)
                if r and r["trips"] > 0:
                    bf = bl_r.get("fills", 1)
                    fp = r["fills"] / bf * 100 if bf > 0 else 100.0
                    row += f" {r['mean_rt']:+.3f} | {fp:.1f}% |"
                else:
                    row += " N/A | N/A |"
            lines.append(row)
        lines.append("")

    lines.append("## Key Metrics")
    lines.append("")
    lines.append("- **Mean/RT**: Mean gross PnL per round trip minus RT fee cost (4.0 pts). Higher = better.")
    lines.append("- **AS10**: Mean mid-price move 10 snapshots after fill (positive = favorable).")
    lines.append("- **Fill%**: Fills as % of baseline (qi=OFF, same spread). Higher = less aggressive filtering.")
    lines.append("- **MaxDD**: Maximum drawdown in pts (cumulative daily PnL).")
    lines.append("- **Sharpe**: Annualized Sharpe ratio (daily PnL).")
    lines.append("- **t-stat**: t-statistic of daily net PnL (>2.0 = significant).")
    lines.append("- **WinD**: Profitable days / total days.")
    lines.append("")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport saved: {output_path}")


def main():
    symbol = "TMFD6"

    print("=" * 120)
    print("  R47 TMFD6 QI Skew Parameter Sweep")
    print(f"  Symbol: {symbol} | max_pos=2 | spread>=5")
    print(f"  Economics: 1pt={POINT_VALUE_NTD} NTD, fee={FEE_PER_SIDE_NTD} NTD/side, RT={FEE_RT_PTS:.1f} pts")
    print("=" * 120)

    # Get available days
    print("\nFetching trading days...")
    dates = get_trading_days(symbol)
    print(f"  Found {len(dates)} days: {dates}")

    if not dates:
        print("ERROR: No TMFD6 data found in ClickHouse.")
        sys.exit(1)

    # Build configs
    configs = build_sweep_configs()
    n_total = len(configs) * len(dates)
    print(f"\n  {len(configs)} configs x {len(dates)} days = {n_total} backtests")

    # Run sweep
    print(f"\n{'#'*120}")
    print(f"# Running sweep...")
    print(f"{'#'*120}")
    t_start = time.time()
    results = run_sweep(symbol, dates, configs)
    elapsed = time.time() - t_start
    print(f"\nTotal sweep time: {elapsed:.1f}s ({elapsed/60:.1f}m)")

    # --- Output 1: Ranked table (top 20 at q=0.5) ---
    print_ranked_table(results, configs, queue_frac=0.5, top_n=20)

    # --- Output 2: Sensitivity heatmap ---
    # Find best spread by highest baseline-adjusted Mean/RT among top configs
    best_spread = 6  # default
    best_mrt = -999.0
    for cfg in configs:
        if cfg.queue_frac != 0.5:
            continue
        r = results.get(cfg.name)
        if r and r["trips"] > 0 and r["mean_rt"] > best_mrt:
            best_mrt = r["mean_rt"]
            best_spread = cfg.spread_threshold

    print_heatmap(results, configs, 5, queue_frac=0.5)

    # --- Output 3: Robustness check ---
    print_robustness(results, configs, queue_frac=0.5, top_n=3)

    # --- Output 4: Full-queue validation ---
    # results already contains both q=0.5 and q=1.0
    print_full_queue_validation(results, results, configs, top_n=5)

    # --- Output 5: Recommendation ---
    print(f"\n{'*'*120}")
    print(f"*  RECOMMENDATION")
    print(f"{'*'*120}")

    # Find best overall at q=0.5 by Mean/RT, requiring fill% >= 50%
    best_cfg = None
    best_mean_rt = -999.0
    for cfg in configs:
        if cfg.queue_frac != 0.5:
            continue
        r = results.get(cfg.name)
        if r is None or r["trips"] == 0:
            continue
        # Get baseline fills for this spread
        bl_name = SweepConfig(cfg.spread_threshold, 1.0, 1, 0.5).name
        bl_r = results.get(bl_name, {})
        bl_fills = bl_r.get("fills", 1)
        fill_pct = r["fills"] / bl_fills if bl_fills > 0 else 1.0
        if fill_pct < 0.50:
            continue
        if r["mean_rt"] > best_mean_rt:
            best_mean_rt = r["mean_rt"]
            best_cfg = cfg

    if best_cfg:
        r = results[best_cfg.name]
        bl_name = SweepConfig(best_cfg.spread_threshold, 1.0, 1, 0.5).name
        bl_r = results.get(bl_name, {})
        bl_mrt = bl_r.get("mean_rt", 0.0) if bl_r.get("trips", 0) > 0 else 0.0
        bl_fills = bl_r.get("fills", 1)
        fill_pct = r["fills"] / bl_fills * 100 if bl_fills > 0 else 100.0
        improvement = best_mean_rt - bl_mrt

        print(f"  Best config (q=0.5, fill%>=50%): {best_cfg.name}")
        print(f"  Parameters: spread>={best_cfg.spread_threshold}, qi_threshold={best_cfg.qi_threshold}, "
              f"widen_ticks={best_cfg.widen_ticks}, max_pos=2")
        print(f"  Baseline Mean/RT (same spread, qi=OFF): {bl_mrt:+.3f} pts")
        print(f"  Best Mean/RT:     {best_mean_rt:+.3f} pts")
        print(f"  Improvement:      {improvement:+.3f} pts/RT")
        print(f"  Fill retention:   {fill_pct:.1f}%")
        print(f"  NET PnL:          {r['net_pts']:+.0f} pts ({r['ntd_day']:+,.0f} NTD/day)")
        print(f"  Sharpe:           {r['sharpe']:+.2f}")
        print(f"  Max DD:           {r['max_dd']:.1f} pts")
        print(f"  t-stat:           {r['t_stat']:+.2f}")
        print(f"  Win days:         {r['win_days']}/{r['days']}")

        # Full-queue check
        fq_name = SweepConfig(best_cfg.spread_threshold, best_cfg.qi_threshold,
                              best_cfg.widen_ticks, 1.0).name
        fq_r = results.get(fq_name, {})
        if fq_r.get("trips", 0) > 0:
            print(f"\n  Full-queue validation (q=1.0):")
            print(f"    Mean/RT: {fq_r['mean_rt']:+.3f} | NET: {fq_r['net_pts']:+.0f} | "
                  f"Sharpe: {fq_r['sharpe']:+.2f} | Profitable: {'YES' if fq_r['net_pts'] > 0 else 'NO'}")
    else:
        print(f"  No config improved Mean/RT while keeping fill% >= 50%")

    print(f"{'*'*120}")

    # Write report
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "outputs", "team_artifacts", "alpha-research",
    )
    output_path = os.path.join(output_dir, "r47_qi_optimization.md")
    write_report(results, configs, output_path)


if __name__ == "__main__":
    main()
