#!/usr/bin/env python3
"""R53 Alpha Candidate Sweep — 5 candidates comparison (CK-Direct).

Baselines (from R52 sweep):
  baseline_max1: spread>=5, max_pos=1, all signals OFF
  baseline_max3: spread>=5, max_pos=3

Candidates:
  C1: Hawkes Spread Forecaster — exponential intensity of spread-widening events
  C2: LOB Active Depth Energy — L1-L5 kinetic energy for vol-burst detection
  C3: Adaptive Lookup Table — proxy for RL (vol/spread regime → params)
  C4: Cross-Contract Signal — TMFC6 basis divergence (overlapping days only)
  C5: Options-Implied Vol — SKIP (data blocker, only 6 days TXO+TXFD6 overlap)

Usage:
    uv run python research/alphas/r53_literature/r53_candidate_sweep.py
"""

from __future__ import annotations

import math
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import requests

CK_HOST = os.environ.get("CLICKHOUSE_HOST", "localhost")
CK_PORT = os.environ.get("CLICKHOUSE_PORT", "8123")
CK_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "changeme")
CK_URL = f"http://{CK_HOST}:{CK_PORT}/"

SCALE = 1_000_000
POINT_VALUE_NTD = 10
FEE_PER_SIDE_NTD = 20
FEE_RT_PTS = 2 * FEE_PER_SIDE_NTD / POINT_VALUE_NTD  # 4.0 pts
BASE_SPREAD_THRESHOLD = 5
QUEUE_FRAC = 0.5


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
    return [line.strip() for line in raw.split("\n") if line.strip()]


def load_day_l5(date: str) -> tuple[dict, dict]:
    """Load TMFD6 BA (L1-L5) + Tick data for one day."""
    ba_sql = f"""
    SELECT
        exch_ts,
        bids_price[1] AS b1p, bids_vol[1] AS b1v,
        asks_price[1] AS a1p, asks_vol[1] AS a1v,
        bids_price[2] AS b2p, bids_vol[2] AS b2v,
        asks_price[2] AS a2p, asks_vol[2] AS a2v,
        bids_price[3] AS b3p, bids_vol[3] AS b3v,
        asks_price[3] AS a3p, asks_vol[3] AS a3v,
        bids_price[4] AS b4p, bids_vol[4] AS b4v,
        asks_price[4] AS a4p, asks_vol[4] AS a4v,
        bids_price[5] AS b5p, bids_vol[5] AS b5v,
        asks_price[5] AS a5p, asks_vol[5] AS a5v
    FROM hft.market_data
    WHERE symbol = 'TMFD6'
      AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    tick_sql = f"""
    SELECT exch_ts, price_scaled AS price, volume
    FROM hft.market_data
    WHERE symbol = 'TMFD6' AND type = 'Tick'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
    ORDER BY exch_ts
    """
    return ck_query_numpy(ba_sql), ck_query_numpy(tick_sql)


def load_tmfc6_day(date: str) -> dict:
    """Load TMFC6 BA L1 for one day (cross-contract signal)."""
    sql = f"""
    SELECT exch_ts, bids_price[1] AS bid1_p, asks_price[1] AS ask1_p
    FROM hft.market_data
    WHERE symbol = 'TMFC6' AND type = 'BidAsk'
      AND toDate(fromUnixTimestamp64Nano(exch_ts)) = '{date}'
      AND length(bids_price) >= 1 AND length(asks_price) >= 1
    ORDER BY exch_ts
    """
    return ck_query_numpy(sql)


def get_tmfc6_dates() -> set[str]:
    sql = """
    SELECT DISTINCT toDate(fromUnixTimestamp64Nano(exch_ts)) as d
    FROM hft.market_data WHERE symbol = 'TMFC6' AND type = 'BidAsk'
    """
    raw = ck_query(sql + " FORMAT TSV")
    return {line.strip() for line in raw.split("\n") if line.strip()}


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


@dataclass
class CandidateConfig:
    name: str
    max_pos: int = 1
    spread_threshold: int = BASE_SPREAD_THRESHOLD
    # C1: Hawkes Spread Forecaster
    c1_enabled: bool = False
    c1_alpha: float = 1.0
    c1_beta: float = 0.5
    c1_threshold: float = 0.3
    # C2: LOB Active Depth Energy
    c2_enabled: bool = False
    c2_energy_pctile: float = 75.0
    c2_window: int = 200
    # C3: Adaptive Lookup Table
    c3_enabled: bool = False
    # C4: Cross-Contract
    c4_enabled: bool = False
    c4_basis_window: int = 500
    c4_z_threshold: float = 1.5


CONFIGS = [
    CandidateConfig(name="baseline_max1", max_pos=1),
    CandidateConfig(name="baseline_max3", max_pos=3),
    # C1: Hawkes Spread Forecaster — pre-position when spread likely to widen
    CandidateConfig(name="C1_hawkes_a1.0", max_pos=1, c1_enabled=True, c1_alpha=1.0, c1_beta=0.5, c1_threshold=0.3),
    CandidateConfig(name="C1_hawkes_a2.0", max_pos=1, c1_enabled=True, c1_alpha=2.0, c1_beta=1.0, c1_threshold=0.5),
    # C2: LOB Active Depth Energy
    CandidateConfig(name="C2_energy_p75", max_pos=1, c2_enabled=True, c2_energy_pctile=75.0),
    CandidateConfig(name="C2_energy_p90", max_pos=1, c2_enabled=True, c2_energy_pctile=90.0),
    # C3: Adaptive Lookup Table (proxy for RL)
    CandidateConfig(name="C3_adaptive", max_pos=3, c3_enabled=True),
    # C4: Cross-Contract Signal (TMFC6 basis)
    CandidateConfig(name="C4_crosscontract", max_pos=1, c4_enabled=True),
]


# ── Core Backtest Engine ─────────────────────────────────────────────


def run_backtest(
    ba: dict,
    ticks: dict,
    cfg: CandidateConfig,
    tmfc6_data: Optional[dict] = None,
) -> tuple[list[FillRecord], int, dict]:
    ba_ts = ba["exch_ts"]
    ba_n = len(ba_ts)
    tick_ts = ticks.get("exch_ts", np.array([], dtype=np.int64))
    tick_n = len(tick_ts)

    b1p = ba["b1p"]
    b1v = ba["b1v"]
    a1p = ba["a1p"]
    a1v = ba["a1v"]
    t_price = ticks.get("price", np.array([], dtype=np.int64))
    t_vol = ticks.get("volume", np.array([], dtype=np.int64))

    # L2-L5 for C2
    has_l5 = "b2p" in ba
    if has_l5:
        bp = [ba.get(f"b{i}p", np.zeros(ba_n, dtype=np.int64)) for i in range(1, 6)]
        bv = [ba.get(f"b{i}v", np.zeros(ba_n, dtype=np.int64)) for i in range(1, 6)]
        ap = [ba.get(f"a{i}p", np.zeros(ba_n, dtype=np.int64)) for i in range(1, 6)]
        av = [ba.get(f"a{i}v", np.zeros(ba_n, dtype=np.int64)) for i in range(1, 6)]

    # TMFC6 for C4
    c4_tmfc6_ts = None
    c4_tmfc6_mid = None
    c4_tmfc6_idx = 0
    if cfg.c4_enabled and tmfc6_data and "exch_ts" in tmfc6_data:
        c4_tmfc6_ts = tmfc6_data["exch_ts"]
        c4_tmfc6_mid = (tmfc6_data["bid1_p"] + tmfc6_data["ask1_p"]) / (2.0 * SCALE)
        c4_tmfc6_idx = 0

    position = 0
    buy_order: Optional[OpenOrder] = None
    sell_order: Optional[OpenOrder] = None
    fills: list[FillRecord] = []

    # C1 state: Hawkes intensity
    c1_events: list[float] = []  # timestamps of spread>=5 events (in seconds)

    # C2 state: energy history
    c2_energy_history: list[float] = []
    c2_energy_threshold = 0.0

    # C3 state: rolling vol
    c3_mid_history: list[float] = []

    # C4 state: basis history
    c4_basis_history: list[float] = []
    c4_cur_tmfc6_mid = 0.0
    c4_bias = 0  # -1 = favor buy, +1 = favor sell, 0 = neutral

    spread_sum = 0.0
    spread_count = 0
    quote_opportunities = 0

    ba_i = 0
    ti = 0

    while ba_i < ba_n or ti < tick_n:
        ba_time = ba_ts[ba_i] if ba_i < ba_n else np.iinfo(np.int64).max
        tk_time = tick_ts[ti] if ti < tick_n else np.iinfo(np.int64).max

        if ba_time <= tk_time:
            cur_bid = b1p[ba_i]
            cur_ask = a1p[ba_i]
            cur_bid_v = b1v[ba_i]
            cur_ask_v = a1v[ba_i]
            cur_ts = ba_time
            ba_i += 1

            spread_pts = (cur_ask - cur_bid) / SCALE
            if spread_pts <= 0:
                continue

            spread_sum += spread_pts
            spread_count += 1
            mid = (cur_bid + cur_ask) / (2.0 * SCALE)
            cur_time_s = cur_ts / 1e9

            effective_threshold = cfg.spread_threshold
            effective_max_pos = cfg.max_pos
            extra_allow = True

            # ── C1: Hawkes Spread Forecaster ──
            if cfg.c1_enabled:
                if spread_pts >= BASE_SPREAD_THRESHOLD:
                    c1_events.append(cur_time_s)
                # Compute intensity
                intensity = 0.0
                cutoff = cur_time_s - 10.0  # 10s lookback
                new_events = []
                for evt_t in c1_events:
                    if evt_t >= cutoff:
                        new_events.append(evt_t)
                        dt = cur_time_s - evt_t
                        if dt > 0:
                            intensity += cfg.c1_alpha * math.exp(-cfg.c1_beta * dt)
                c1_events = new_events
                # When intensity high, lower threshold to enter early
                if intensity >= cfg.c1_threshold:
                    effective_threshold = max(4, BASE_SPREAD_THRESHOLD - 1)

            # ── C2: LOB Active Depth Energy ──
            if cfg.c2_enabled and has_l5:
                energy = 0.0
                mid_scaled = (cur_bid + cur_ask) / 2.0
                for lvl in range(5):
                    bp_val = bp[lvl][ba_i - 1]
                    bv_val = bv[lvl][ba_i - 1]
                    ap_val = ap[lvl][ba_i - 1]
                    av_val = av[lvl][ba_i - 1]
                    if bp_val > 0 and bv_val > 0:
                        energy += bv_val * abs(bp_val - mid_scaled) / SCALE
                    if ap_val > 0 and av_val > 0:
                        energy += av_val * abs(ap_val - mid_scaled) / SCALE

                c2_energy_history.append(energy)
                if len(c2_energy_history) > cfg.c2_window:
                    c2_energy_history.pop(0)
                if len(c2_energy_history) >= 50:
                    c2_energy_threshold = float(
                        np.percentile(c2_energy_history, cfg.c2_energy_pctile)
                    )
                # When energy is HIGH, expect spread burst → allow lower threshold
                if energy >= c2_energy_threshold and c2_energy_threshold > 0:
                    effective_threshold = max(4, BASE_SPREAD_THRESHOLD - 1)

            # ── C3: Adaptive Lookup Table ──
            if cfg.c3_enabled:
                c3_mid_history.append(mid)
                if len(c3_mid_history) > 500:
                    c3_mid_history.pop(0)
                if len(c3_mid_history) >= 50:
                    arr = np.array(c3_mid_history)
                    rets = np.diff(arr) / arr[:-1]
                    vol = float(np.std(rets))
                    vol_arr_local = np.array(c3_mid_history[-200:]) if len(c3_mid_history) >= 200 else arr
                    rets_local = np.diff(vol_arr_local) / vol_arr_local[:-1]
                    vol_local = float(np.std(rets_local))
                    # Adaptive: low vol → aggressive, high vol → conservative
                    if vol_local < vol * 0.5:
                        effective_threshold = 4
                        effective_max_pos = 3
                    elif vol_local < vol * 1.5:
                        effective_threshold = 5
                        effective_max_pos = 2
                    else:
                        effective_threshold = 7
                        effective_max_pos = 1

            # ── C4: Cross-Contract Signal ──
            if cfg.c4_enabled and c4_tmfc6_ts is not None:
                # Advance TMFC6 index to current timestamp
                while (c4_tmfc6_idx < len(c4_tmfc6_ts) - 1
                       and c4_tmfc6_ts[c4_tmfc6_idx + 1] <= cur_ts):
                    c4_tmfc6_idx += 1
                if c4_tmfc6_idx < len(c4_tmfc6_ts):
                    c4_cur_tmfc6_mid = float(c4_tmfc6_mid[c4_tmfc6_idx])

                if c4_cur_tmfc6_mid > 0:
                    basis = mid - c4_cur_tmfc6_mid
                    c4_basis_history.append(basis)
                    if len(c4_basis_history) > cfg.c4_basis_window:
                        c4_basis_history.pop(0)
                    if len(c4_basis_history) >= 100:
                        b_arr = np.array(c4_basis_history)
                        b_mean = float(b_arr.mean())
                        b_std = float(b_arr.std())
                        if b_std > 1e-9:
                            z = (basis - b_mean) / b_std
                            if z < -cfg.c4_z_threshold:
                                c4_bias = -1  # TMFD6 cheap → favor buy
                            elif z > cfg.c4_z_threshold:
                                c4_bias = 1  # TMFD6 expensive → favor sell
                            else:
                                c4_bias = 0

            # Cancel if price moved
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            # Quote decision
            if spread_pts >= effective_threshold and extra_allow:
                quote_opportunities += 1
                # C4 bias: restrict quoting to one side
                allow_buy = True
                allow_sell = True
                if cfg.c4_enabled and c4_bias != 0:
                    if c4_bias == 1:
                        allow_buy = False  # only sell when TMFD6 expensive
                    else:
                        allow_sell = False  # only buy when TMFD6 cheap

                if allow_buy and buy_order is None and position < effective_max_pos:
                    qp = max(1, int(cur_bid_v * QUEUE_FRAC))
                    buy_order = OpenOrder("buy", cur_bid, cur_ts, qp)
                if allow_sell and sell_order is None and position > -effective_max_pos:
                    qp = max(1, int(cur_ask_v * QUEUE_FRAC))
                    sell_order = OpenOrder("sell", cur_ask, cur_ts, qp)
        else:
            trade_p = t_price[ti]
            trade_v = t_vol[ti]
            ti += 1
            cur_mid = (b1p[min(ba_i - 1, ba_n - 1)] + a1p[min(ba_i - 1, ba_n - 1)]) / (2 * SCALE)

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fills.append(FillRecord("buy", buy_order.price / SCALE, tk_time, cur_mid))
                    position += 1
                    buy_order = None

            if sell_order is not None and trade_p >= sell_order.price:
                sell_order.queue_pos -= trade_v
                if sell_order.queue_pos <= 0:
                    fills.append(FillRecord("sell", sell_order.price / SCALE, tk_time, cur_mid))
                    position -= 1
                    sell_order = None

    stats = {
        "avg_spread": spread_sum / spread_count if spread_count > 0 else 0,
        "quote_opportunities": quote_opportunities,
    }
    return fills, position, stats


def compute_fifo_pnl(fills: list[FillRecord]) -> tuple[float, int, int]:
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


def compute_equity_curve(daily_nets: list[float]) -> tuple[float, float]:
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
    sys.stdout.write(
        f"{'=' * 130}\n"
        f"  R53 Alpha Candidate Sweep (CK-Direct)\n"
        f"  Economics: 1pt={POINT_VALUE_NTD} NTD, RT cost={FEE_RT_PTS:.1f} pts, queue_frac={QUEUE_FRAC}\n"
        f"{'=' * 130}\n"
    )

    dates = get_trading_days()
    sys.stdout.write(f"\nTMFD6 trading days: {len(dates)}\n")

    tmfc6_dates = get_tmfc6_dates()
    sys.stdout.write(f"TMFC6 dates available: {len(tmfc6_dates)}\n")
    overlap_dates = sorted(set(dates) & tmfc6_dates)
    sys.stdout.write(f"Overlap dates (for C4): {len(overlap_dates)}\n")

    # Load all TMFD6 days
    sys.stdout.write("\nLoading TMFD6 L5 data...\n")
    day_data: dict[str, tuple[dict, dict]] = {}
    for date in dates:
        sys.stdout.write(f"  {date}...")
        sys.stdout.flush()
        t0 = time.time()
        ba, ticks = load_day_l5(date)
        elapsed = time.time() - t0
        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            sys.stdout.write(" SKIP\n")
            continue
        sys.stdout.write(f" {len(ba['exch_ts'])} BA, {len(ticks.get('exch_ts', []))} ticks ({elapsed:.1f}s)\n")
        day_data[date] = (ba, ticks)

    # Load TMFC6 for overlap days
    tmfc6_data_by_date: dict[str, dict] = {}
    sys.stdout.write("\nLoading TMFC6 for overlap days...\n")
    for date in overlap_dates:
        if date not in day_data:
            continue
        sys.stdout.write(f"  {date}...")
        sys.stdout.flush()
        t0 = time.time()
        tmfc6 = load_tmfc6_day(date)
        elapsed = time.time() - t0
        if tmfc6 and "exch_ts" in tmfc6 and len(tmfc6["exch_ts"]) > 0:
            tmfc6_data_by_date[date] = tmfc6
            sys.stdout.write(f" {len(tmfc6['exch_ts'])} rows ({elapsed:.1f}s)\n")
        else:
            sys.stdout.write(" SKIP\n")

    n_days = len(day_data)
    sys.stdout.write(f"\nLoaded {n_days} TMFD6 days, {len(tmfc6_data_by_date)} TMFC6 overlap days.\n")

    # Run sweep
    sys.stdout.write(f"\nRunning {len(CONFIGS)} configs x {n_days} days = {len(CONFIGS) * n_days} backtests...\n")

    results: dict[str, dict] = {}
    for cfg in CONFIGS:
        results[cfg.name] = {
            "daily_nets": [], "daily_gross": [], "daily_fills": [],
            "total_fills": 0, "total_trips": 0, "total_wins": 0,
            "total_quotes": 0, "active_days": 0,
        }

    sweep_t0 = time.time()
    sorted_dates = sorted(day_data.keys())

    for di, date in enumerate(sorted_dates):
        ba, ticks = day_data[date]
        tmfc6 = tmfc6_data_by_date.get(date)
        sys.stdout.write(f"  [{di + 1}/{n_days}] {date}: ")
        sys.stdout.flush()

        for cfg in CONFIGS:
            # Skip C4 on days without TMFC6 data
            if cfg.c4_enabled and tmfc6 is None:
                r = results[cfg.name]
                r["daily_nets"].append(0.0)
                r["daily_gross"].append(0.0)
                r["daily_fills"].append(0)
                continue

            fills, final_pos, stats = run_backtest(ba, ticks, cfg, tmfc6)
            gross, trips, wins = compute_fifo_pnl(fills)
            fee_pts = len(fills) * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
            net = gross - fee_pts

            r = results[cfg.name]
            r["daily_nets"].append(net)
            r["daily_gross"].append(gross)
            r["daily_fills"].append(len(fills))
            r["total_fills"] += len(fills)
            r["total_trips"] += trips
            r["total_wins"] += wins
            r["total_quotes"] += stats["quote_opportunities"]
            if len(fills) > 0:
                r["active_days"] += 1

        b1 = results["baseline_max1"]["daily_nets"][-1]
        b1f = results["baseline_max1"]["daily_fills"][-1]
        sys.stdout.write(f"base1={b1:>+8.0f} fills={b1f:>5}\n")

    sweep_elapsed = time.time() - sweep_t0
    sys.stdout.write(f"\nSweep completed in {sweep_elapsed:.1f}s\n")

    # ── Results Table ────────────────────────────────────────────────
    sys.stdout.write(
        f"\n{'=' * 140}\n"
        f"  RESULTS TABLE ({n_days} days)\n"
        f"{'=' * 140}\n"
    )
    header = (
        f"{'Config':<22} {'TotalNet':>10} {'PnL/day':>9} {'NTD/day':>9} "
        f"{'t-stat':>7} {'WR%':>6} {'WinD':>7} "
        f"{'Fills':>7} {'RTs':>7} {'Net/RT':>8} "
        f"{'MaxDD':>8} {'Sharpe':>7}"
    )
    sys.stdout.write(header + "\n")
    sys.stdout.write("-" * 140 + "\n")

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
        t_stat = mean_daily / (std_daily / math.sqrt(len(arr))) if std_daily > 1e-9 else 0
        wr = total_wins / total_trips * 100 if total_trips > 0 else 0
        mean_pnl_per_rt = (total_gross / total_trips - FEE_RT_PTS) if total_trips > 0 else 0
        max_dd, sharpe = compute_equity_curve(daily_nets)
        n_winning = int((arr > 0).sum())

        summary_rows.append({
            "name": cfg.name, "total_net": total_net, "pnl_per_day": mean_daily,
            "pnl_per_day_ntd": mean_daily * POINT_VALUE_NTD, "t_stat": t_stat,
            "wr": wr, "n_fills": total_fills, "n_rt": total_trips,
            "mean_pnl_per_rt": mean_pnl_per_rt, "max_dd": max_dd,
            "sharpe": sharpe, "n_winning": n_winning, "n_days": n_days,
            "active_days": r["active_days"],
        })

        sys.stdout.write(
            f"{cfg.name:<22} {total_net:>+10.0f} {mean_daily:>+9.1f} "
            f"{mean_daily * POINT_VALUE_NTD:>+9.0f} {t_stat:>+7.2f} "
            f"{wr:>5.1f}% {n_winning:>3}/{n_days:<3} "
            f"{total_fills:>7} {total_trips:>7} {mean_pnl_per_rt:>+8.3f} "
            f"{max_dd:>8.0f} {sharpe:>+7.2f}\n"
        )

    sys.stdout.write("-" * 140 + "\n")

    # ── Enhancement Delta ────────────────────────────────────────────
    baseline = next((r for r in summary_rows if r["name"] == "baseline_max1"), None)
    if baseline:
        sys.stdout.write(
            f"\n{'=' * 100}\n"
            f"CANDIDATE DELTA vs BASELINE (max_pos=1, spread>={BASE_SPREAD_THRESHOLD})\n"
            f"{'=' * 100}\n"
        )
        sys.stdout.write(
            f"{'Config':<22} {'Delta PnL':>10} {'Delta/Day':>10} "
            f"{'Delta WR':>9} {'Delta DD':>9} {'Verdict':>10}\n"
        )
        sys.stdout.write("-" * 80 + "\n")
        for row in summary_rows:
            if row["name"].startswith("baseline"):
                continue
            dp = row["total_net"] - baseline["total_net"]
            dd = row["pnl_per_day"] - baseline["pnl_per_day"]
            dw = row["wr"] - baseline["wr"]
            ddd = row["max_dd"] - baseline["max_dd"]
            if dp > 0 and ddd <= 0:
                verdict = "BETTER"
            elif dp > 0:
                verdict = "MIXED"
            else:
                verdict = "WORSE"
            sys.stdout.write(
                f"{row['name']:<22} {dp:>+10.0f} {dd:>+10.1f} "
                f"{dw:>+8.1f}% {ddd:>+9.0f} {verdict:>10}\n"
            )
        sys.stdout.write("-" * 80 + "\n")

    # ── C4 restricted to overlap days only ──
    c4_row = next((r for r in summary_rows if r["name"] == "C4_crosscontract"), None)
    if c4_row:
        sys.stdout.write(
            f"\nC4 note: active on {c4_row['active_days']} overlap days only "
            f"(0 PnL on {n_days - c4_row['active_days']} non-overlap days).\n"
        )
        # Compute C4 vs baseline on overlap days only
        c4_daily = results["C4_crosscontract"]["daily_nets"]
        b1_daily = results["baseline_max1"]["daily_nets"]
        c4_overlap_sum = 0.0
        b1_overlap_sum = 0.0
        for i, date in enumerate(sorted_dates):
            if date in tmfc6_data_by_date:
                c4_overlap_sum += c4_daily[i]
                b1_overlap_sum += b1_daily[i]
        sys.stdout.write(
            f"  C4 on overlap days: {c4_overlap_sum:>+.0f} vs baseline: {b1_overlap_sum:>+.0f} "
            f"(delta: {c4_overlap_sum - b1_overlap_sum:>+.0f})\n"
        )

    sys.stdout.write(f"\nC5 (Options-Implied Vol): SKIPPED — only 6 days TXO+TXFD6 intraday overlap.\n")
    sys.stdout.write(f"\nTotal elapsed: {sweep_elapsed:.1f}s\n")


if __name__ == "__main__":
    main()
