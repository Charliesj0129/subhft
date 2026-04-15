#!/usr/bin/env python3
"""
R47 TMFD6 Adverse Selection Reduction Backtest.

Tests 4 AS reduction mechanisms on TMFD6 spread>=5 maker strategy:
1. Toxicity Gate (R23) — suppress quoting when toxicity > threshold
2. TOB Survival Gate (R24 RegimeClassifier) — suppress when TOB < threshold
3. QI Skew (R6 composite simplified) — widen quote on pressured side
4. OFI Suppression (R10) — suppress depleted side

Usage:
    python research/tools/r47_tmfd6_as_backtest.py
"""

import os
import sys
import time
from dataclasses import dataclass, field
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


# --- Feature computation (pre-computed arrays) ---

def compute_toxicity(tick_prices: np.ndarray, alpha: float = 0.04) -> np.ndarray:
    """Compute toxicity_x1000 from tick prices using uptick/downtick EMA.

    Returns array of len(tick_prices) with toxicity_x1000 values.
    toxicity_x1000 = int(abs(ema_direction) * 1000)
    """
    n = len(tick_prices)
    toxicity = np.zeros(n, dtype=np.int64)
    if n == 0:
        return toxicity
    ema = 0.0
    prev_p = tick_prices[0]
    for i in range(n):
        p = tick_prices[i]
        if p > prev_p:
            direction = 1.0
        elif p < prev_p:
            direction = -1.0
        else:
            direction = 0.0  # no change, use 0
        ema = alpha * direction + (1.0 - alpha) * ema
        toxicity[i] = int(abs(ema) * 1000)
        prev_p = p
    return toxicity


def compute_tob_survival_ns(ba_ts: np.ndarray, bid1_p: np.ndarray,
                             ask1_p: np.ndarray) -> np.ndarray:
    """Compute time since last TOB price change in nanoseconds.

    Returns array of len(ba_ts).
    """
    n = len(ba_ts)
    survival = np.zeros(n, dtype=np.int64)
    if n == 0:
        return survival
    last_change_ts = ba_ts[0]
    prev_bid = bid1_p[0]
    prev_ask = ask1_p[0]
    for i in range(n):
        if bid1_p[i] != prev_bid or ask1_p[i] != prev_ask:
            last_change_ts = ba_ts[i]
            prev_bid = bid1_p[i]
            prev_ask = ask1_p[i]
        survival[i] = ba_ts[i] - last_change_ts
    return survival


def compute_qi(bid1_v: np.ndarray, ask1_v: np.ndarray) -> np.ndarray:
    """Compute queue imbalance: (bid_v - ask_v) / (bid_v + ask_v).

    Returns float64 array.
    """
    total = bid1_v.astype(np.float64) + ask1_v.astype(np.float64)
    # Avoid division by zero
    total = np.where(total == 0, 1.0, total)
    return (bid1_v.astype(np.float64) - ask1_v.astype(np.float64)) / total


def compute_ofi_ema(bid1_v: np.ndarray, ask1_v: np.ndarray,
                     alpha: float = 0.05) -> np.ndarray:
    """Compute OFI EMA from L1 volume changes.

    ofi = delta(bid1_v) - delta(ask1_v)
    ofi_ema = alpha * ofi + (1-alpha) * ofi_ema

    Returns float64 array of len(bid1_v).
    """
    n = len(bid1_v)
    ofi_ema = np.zeros(n, dtype=np.float64)
    if n == 0:
        return ofi_ema
    ema = 0.0
    prev_bid_v = float(bid1_v[0])
    prev_ask_v = float(ask1_v[0])
    for i in range(n):
        d_bid = float(bid1_v[i]) - prev_bid_v
        d_ask = float(ask1_v[i]) - prev_ask_v
        ofi = d_bid - d_ask
        ema = alpha * ofi + (1.0 - alpha) * ema
        ofi_ema[i] = ema
        prev_bid_v = float(bid1_v[i])
        prev_ask_v = float(ask1_v[i])
    return ofi_ema


# --- Gate configuration ---

@dataclass
class ASConfig:
    """Adverse selection filter configuration."""
    name: str = "baseline"
    # Toxicity gate
    toxicity_enabled: bool = False
    toxicity_threshold: int = 9999  # x1000 scale, suppress when > threshold
    # TOB survival gate
    tob_enabled: bool = False
    tob_adverse_ms: float = 100.0  # suppress when survival < this (ms)
    # QI skew
    qi_enabled: bool = False
    qi_threshold: float = 0.3  # skew when |qi| > threshold
    # OFI suppression
    ofi_enabled: bool = False
    ofi_quantile: float = 0.2  # suppress side when |ofi_ema| > quantile


@dataclass
class OpenOrder:
    side: str
    price: int  # x1e6
    placed_ts: int
    queue_pos: float


@dataclass
class FillRecord:
    side: str
    price_pts: float
    ts: int
    ba_idx: int  # index into BidAsk arrays at fill time
    mid_at_fill: float


# --- Core simulation ---

def run_backtest_with_as(
    ba: dict,
    ticks: dict,
    cfg: ASConfig,
    spread_threshold: int = 5,
    max_pos: int = 3,
    queue_frac: float = 0.5,
) -> tuple[list[FillRecord], int, int, int]:
    """Run R47 maker backtest with AS filters.

    Returns (fills, final_position, total_quote_opportunities, suppressed_count).
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

    # Pre-compute mid prices for AS analysis
    mid_arr = (bid1_p.astype(np.float64) + ask1_p.astype(np.float64)) / (2 * SCALE)

    # Pre-compute features
    # Toxicity: keyed by tick index, need to map tick events to current toxicity
    toxicity_arr = np.array([], dtype=np.int64)
    if cfg.toxicity_enabled and tick_n > 0:
        toxicity_arr = compute_toxicity(t_price)

    # TOB survival: keyed by BA index
    tob_survival_ns = np.array([], dtype=np.int64)
    if cfg.tob_enabled and ba_n > 0:
        tob_survival_ns = compute_tob_survival_ns(ba_ts, bid1_p, ask1_p)

    # QI: keyed by BA index
    qi_arr = np.array([], dtype=np.float64)
    if cfg.qi_enabled and ba_n > 0:
        qi_arr = compute_qi(bid1_v, ask1_v)

    # OFI EMA: keyed by BA index
    ofi_ema_arr = np.array([], dtype=np.float64)
    ofi_threshold = 0.0
    if cfg.ofi_enabled and ba_n > 0:
        ofi_ema_arr = compute_ofi_ema(bid1_v, ask1_v)
        # Compute quantile threshold from absolute values
        abs_ofi = np.abs(ofi_ema_arr)
        ofi_threshold = float(np.percentile(abs_ofi[abs_ofi > 0], (1.0 - cfg.ofi_quantile) * 100))

    cur_bid = cur_ask = 0
    cur_bid_v = cur_ask_v = 0
    position = 0
    buy_order: Optional[OpenOrder] = None
    sell_order: Optional[OpenOrder] = None
    fills: list[FillRecord] = []
    total_quote_opps = 0
    suppressed = 0
    cur_toxicity = 0  # latest toxicity from tick stream

    ba_i = 0
    ti = 0
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

            # Cancel stale orders
            if buy_order is not None and buy_order.price != cur_bid:
                buy_order = None
            if sell_order is not None and sell_order.price != cur_ask:
                sell_order = None

            if spread_pts >= spread_threshold:
                total_quote_opps += 1

                # --- Apply AS filters ---
                suppress_buy = False
                suppress_sell = False

                # 1. Toxicity gate: suppress both sides
                if cfg.toxicity_enabled and cur_toxicity > cfg.toxicity_threshold:
                    suppress_buy = True
                    suppress_sell = True

                # 2. TOB survival gate: suppress both sides if TOB too young
                if cfg.tob_enabled and len(tob_survival_ns) > 0:
                    surv_ms = tob_survival_ns[last_ba_i] / 1_000_000.0
                    if surv_ms < cfg.tob_adverse_ms:
                        suppress_buy = True
                        suppress_sell = True

                # 3. QI skew: widen the pressured side by canceling that order
                if cfg.qi_enabled and len(qi_arr) > 0:
                    qi_val = qi_arr[last_ba_i]
                    if qi_val > cfg.qi_threshold:
                        # Buying pressure -> suppress ask (would get picked off)
                        suppress_ask_qi = True
                    elif qi_val < -cfg.qi_threshold:
                        # Selling pressure -> suppress bid
                        suppress_bid_qi = True
                    else:
                        suppress_ask_qi = False
                        suppress_bid_qi = False
                    # Note: QI skew conceptually widens one side. In this sim
                    # "widen ask" = don't place ask at best, effectively skipping.
                    if qi_val > cfg.qi_threshold:
                        suppress_sell = True
                    elif qi_val < -cfg.qi_threshold:
                        suppress_buy = True

                # 4. OFI suppression: suppress the depleted side
                if cfg.ofi_enabled and len(ofi_ema_arr) > 0:
                    ofi_val = ofi_ema_arr[last_ba_i]
                    if ofi_val < -ofi_threshold:
                        # Ask side depleting -> suppress ask
                        suppress_sell = True
                    elif ofi_val > ofi_threshold:
                        # Bid side depleting -> suppress bid
                        suppress_buy = True

                if suppress_buy and suppress_sell:
                    suppressed += 1

                # Place orders (respecting suppression)
                if buy_order is None and position < max_pos and not suppress_buy:
                    qp = max(1, int(cur_bid_v * queue_frac))
                    buy_order = OpenOrder(
                        side="buy", price=cur_bid,
                        placed_ts=ba_time, queue_pos=qp,
                    )
                if sell_order is None and position > -max_pos and not suppress_sell:
                    qp = max(1, int(cur_ask_v * queue_frac))
                    sell_order = OpenOrder(
                        side="sell", price=cur_ask,
                        placed_ts=ba_time, queue_pos=qp,
                    )

                # Also cancel existing orders if now suppressed
                if suppress_buy and buy_order is not None:
                    buy_order = None
                if suppress_sell and sell_order is not None:
                    sell_order = None

        else:
            # Process tick
            trade_p = t_price[ti]
            trade_v = t_vol[ti]

            # Update toxicity from tick stream
            if cfg.toxicity_enabled and len(toxicity_arr) > 0 and ti < len(toxicity_arr):
                cur_toxicity = toxicity_arr[ti]

            ti += 1

            cur_mid = (cur_bid + cur_ask) / (2 * SCALE) if cur_bid > 0 else 0.0

            if buy_order is not None and trade_p <= buy_order.price:
                buy_order.queue_pos -= trade_v
                if buy_order.queue_pos <= 0:
                    fr = FillRecord(
                        side="buy",
                        price_pts=buy_order.price / SCALE,
                        ts=tk_time,
                        ba_idx=last_ba_i,
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
                        ba_idx=last_ba_i,
                        mid_at_fill=cur_mid,
                    )
                    fills.append(fr)
                    position -= 1
                    sell_order = None

    return fills, position, total_quote_opps, suppressed


def compute_results(
    fills: list[FillRecord],
    position: int,
    total_quote_opps: int,
    suppressed: int,
    mid_arr: np.ndarray,
    ba_n: int,
) -> dict:
    """Compute PnL and AS metrics from fills."""
    n_fills = len(fills)
    if n_fills == 0:
        return {
            "fills": 0, "trips": 0, "wins": 0, "gross_pts": 0.0,
            "wr": 0.0, "mean_rt": 0.0, "trip_pnls": [],
            "as10_mean": 0.0, "suppressed_pct": 0.0,
            "position": position,
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

    # Adverse selection: mid-price move 10 snapshots after fill
    as10_vals = []
    for f in fills:
        idx_after = min(f.ba_idx + 10, ba_n - 1)
        mid_after = mid_arr[idx_after]
        if f.side == "buy":
            as10_vals.append(mid_after - f.mid_at_fill)  # positive = favorable for buyer
        else:
            as10_vals.append(f.mid_at_fill - mid_after)  # positive = favorable for seller
    as10_mean = float(np.mean(as10_vals)) if as10_vals else 0.0

    fee_pts = n_fills * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
    net_pts = realized - fee_pts
    wr = wins / n_trips * 100 if n_trips > 0 else 0.0
    mean_rt = (realized / n_trips - FEE_RT_PTS) if n_trips > 0 else 0.0
    suppressed_pct = suppressed / total_quote_opps * 100 if total_quote_opps > 0 else 0.0

    return {
        "fills": n_fills,
        "trips": n_trips,
        "wins": wins,
        "gross_pts": realized,
        "net_pts": net_pts,
        "wr": wr,
        "mean_rt": mean_rt,
        "trip_pnls": trip_pnls,
        "as10_mean": as10_mean,
        "suppressed_pct": suppressed_pct,
        "position": position,
    }


# --- Test matrix ---

def build_test_configs() -> list[ASConfig]:
    """Build the full test matrix."""
    configs = [
        # A. Baseline
        ASConfig(name="A.Baseline"),
        # B. Toxicity gate only
        ASConfig(name="B.Tox300", toxicity_enabled=True, toxicity_threshold=300),
        ASConfig(name="B.Tox500", toxicity_enabled=True, toxicity_threshold=500),
        ASConfig(name="B.Tox700", toxicity_enabled=True, toxicity_threshold=700),
        # C. TOB survival gate only
        ASConfig(name="C.TOB50", tob_enabled=True, tob_adverse_ms=50),
        ASConfig(name="C.TOB100", tob_enabled=True, tob_adverse_ms=100),
        ASConfig(name="C.TOB200", tob_enabled=True, tob_adverse_ms=200),
        # D. QI skew only
        ASConfig(name="D.QI0.2", qi_enabled=True, qi_threshold=0.2),
        ASConfig(name="D.QI0.3", qi_enabled=True, qi_threshold=0.3),
        ASConfig(name="D.QI0.4", qi_enabled=True, qi_threshold=0.4),
        # E. OFI suppression only
        ASConfig(name="E.OFI10%", ofi_enabled=True, ofi_quantile=0.10),
        ASConfig(name="E.OFI20%", ofi_enabled=True, ofi_quantile=0.20),
        ASConfig(name="E.OFI30%", ofi_enabled=True, ofi_quantile=0.30),
    ]
    return configs


def build_combo_configs(best_tox: int, best_tob: float, best_qi: float, best_ofi: float) -> list[ASConfig]:
    """Build combination configs from best individual thresholds."""
    return [
        # F. Best toxicity + best TOB
        ASConfig(
            name=f"F.Tox{best_tox}+TOB{int(best_tob)}",
            toxicity_enabled=True, toxicity_threshold=best_tox,
            tob_enabled=True, tob_adverse_ms=best_tob,
        ),
        # G. Best toxicity + best QI
        ASConfig(
            name=f"G.Tox{best_tox}+QI{best_qi}",
            toxicity_enabled=True, toxicity_threshold=best_tox,
            qi_enabled=True, qi_threshold=best_qi,
        ),
        # H. All 4 combined
        ASConfig(
            name=f"H.All4",
            toxicity_enabled=True, toxicity_threshold=best_tox,
            tob_enabled=True, tob_adverse_ms=best_tob,
            qi_enabled=True, qi_threshold=best_qi,
            ofi_enabled=True, ofi_quantile=best_ofi,
        ),
    ]


def run_all_configs(
    symbol: str,
    dates: list[str],
    configs: list[ASConfig],
    queue_fracs: list[float],
    spread_threshold: int = 5,
    max_pos: int = 3,
) -> dict:
    """Run all configs across all days.

    Returns: {(cfg_name, qf): aggregated_results}
    """
    # Initialize accumulators
    accum: dict[tuple[str, float], dict] = {}
    for cfg in configs:
        for qf in queue_fracs:
            accum[(cfg.name, qf)] = {
                "fills": 0, "trips": 0, "wins": 0, "gross_pts": 0.0,
                "net_pts": 0.0, "days": 0, "daily_net_pts": [],
                "as10_sum": 0.0, "as10_count": 0,
                "suppressed_pct_sum": 0.0,
                "trip_pnls_all": [],
            }

    for date in dates:
        sys.stdout.write(f"  {symbol} {date} ... ")
        sys.stdout.flush()
        t0 = time.time()
        ba = load_bidask(symbol, date)
        ticks = load_ticks(symbol, date)
        elapsed = time.time() - t0

        if not ba or "exch_ts" not in ba or len(ba["exch_ts"]) == 0:
            print("SKIP (no data)")
            continue

        n_ba = len(ba["exch_ts"])
        n_ticks = len(ticks.get("exch_ts", []))

        # Pre-compute mid array for AS analysis
        mid_arr = (ba["bid1_p"].astype(np.float64) + ba["ask1_p"].astype(np.float64)) / (2 * SCALE)

        n_configs_run = 0
        for cfg in configs:
            for qf in queue_fracs:
                fills, pos, opps, supp = run_backtest_with_as(
                    ba, ticks, cfg,
                    spread_threshold=spread_threshold,
                    max_pos=max_pos,
                    queue_frac=qf,
                )
                res = compute_results(fills, pos, opps, supp, mid_arr, n_ba)

                key = (cfg.name, qf)
                a = accum[key]
                a["fills"] += res["fills"]
                a["trips"] += res["trips"]
                a["wins"] += res["wins"]
                a["gross_pts"] += res["gross_pts"]
                a["net_pts"] += res["net_pts"]
                a["days"] += 1
                a["daily_net_pts"].append(res["net_pts"])
                a["as10_sum"] += res["as10_mean"] * res["fills"]
                a["as10_count"] += res["fills"]
                a["suppressed_pct_sum"] += res["suppressed_pct"]
                a["trip_pnls_all"].extend(res["trip_pnls"])
                n_configs_run += 1

        elapsed_total = time.time() - t0
        print(f"{n_ba} BA, {n_ticks} ticks, {n_configs_run} cfgs ({elapsed_total:.1f}s)")

    return accum


def print_results_table(accum: dict, queue_frac: float, baseline_fills: int):
    """Print formatted results table for one queue_frac."""
    print(f"\n{'='*120}")
    print(f"  Queue fraction = {queue_frac}")
    print(f"  TMFD6 spread>=5 | max_pos=3 | 1pt=10NTD | fee=20NTD/side | RT cost=4.0pts")
    print(f"{'='*120}")
    hdr = (
        f"{'Config':<22} {'Fills':>7} {'Trips':>7} {'WR%':>6} "
        f"{'Gross':>9} {'Fees':>8} {'NET pts':>9} {'NTD/day':>10} "
        f"{'Mean/RT':>8} {'AS10':>7} {'Supp%':>6} {'FillPct':>8} {'t-stat':>7}"
    )
    print(hdr)
    print("-" * 120)

    # Sort by config name
    keys = sorted([k for k in accum if k[1] == queue_frac], key=lambda x: x[0])

    for key in keys:
        a = accum[key]
        cfg_name = key[0]
        fills = a["fills"]
        trips = a["trips"]
        wins = a["wins"]
        gross = a["gross_pts"]
        fee_pts = fills * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
        net = gross - fee_pts
        days = a["days"]
        wr = wins / trips * 100 if trips > 0 else 0.0
        mean_rt = (gross / trips - FEE_RT_PTS) if trips > 0 else 0.0
        ntd_day = (net * POINT_VALUE_NTD / days) if days > 0 else 0.0
        as10 = a["as10_sum"] / a["as10_count"] if a["as10_count"] > 0 else 0.0
        supp_pct = a["suppressed_pct_sum"] / days if days > 0 else 0.0
        fill_pct = fills / baseline_fills * 100 if baseline_fills > 0 else 0.0

        # t-statistic on daily net PnL
        daily = np.array(a["daily_net_pts"])
        if len(daily) > 1 and daily.std() > 0:
            t_stat = daily.mean() / (daily.std() / np.sqrt(len(daily)))
        else:
            t_stat = 0.0

        marker = " <--" if net > 0 and cfg_name != "A.Baseline" else ""
        print(
            f"{cfg_name:<22} {fills:>7} {trips:>7} {wr:>5.1f}% "
            f"{gross:>+9.0f} {-fee_pts:>8.0f} {net:>+9.0f} {ntd_day:>+10,.0f} "
            f"{mean_rt:>+8.3f} {as10:>+7.3f} {supp_pct:>5.1f}% {fill_pct:>7.1f}% "
            f"{t_stat:>+7.2f}{marker}"
        )

    print("-" * 120)


def find_best_individual(accum: dict, queue_frac: float) -> tuple[int, float, float, float]:
    """Find best threshold for each individual mechanism by Mean/RT."""
    baseline_key = ("A.Baseline", queue_frac)
    baseline_mean_rt = _get_mean_rt(accum, baseline_key)

    best_tox = 500
    best_tox_score = -999.0
    best_tob = 100.0
    best_tob_score = -999.0
    best_qi = 0.3
    best_qi_score = -999.0
    best_ofi = 0.2
    best_ofi_score = -999.0

    for key, a in accum.items():
        if key[1] != queue_frac:
            continue
        name = key[0]
        score = _get_mean_rt(accum, key)
        fills = a["fills"]
        baseline_fills = accum.get(baseline_key, {}).get("fills", 1)
        fill_pct = fills / baseline_fills if baseline_fills > 0 else 0

        # Must keep >70% of fills to be viable
        if fill_pct < 0.70:
            continue

        if name.startswith("B.Tox") and score > best_tox_score:
            best_tox_score = score
            best_tox = int(name.replace("B.Tox", ""))
        elif name.startswith("C.TOB") and score > best_tob_score:
            best_tob_score = score
            best_tob = float(name.replace("C.TOB", ""))
        elif name.startswith("D.QI") and score > best_qi_score:
            best_qi_score = score
            best_qi = float(name.replace("D.QI", ""))
        elif name.startswith("E.OFI") and score > best_ofi_score:
            best_ofi_score = score
            best_ofi = float(name.replace("E.OFI", "").replace("%", "")) / 100.0

    return best_tox, best_tob, best_qi, best_ofi


def _get_mean_rt(accum: dict, key: tuple) -> float:
    a = accum.get(key)
    if not a or a["trips"] == 0:
        return -999.0
    return a["gross_pts"] / a["trips"] - FEE_RT_PTS


def write_report(accum: dict, queue_fracs: list[float], output_path: str):
    """Write markdown results to file."""
    lines = []
    lines.append("# R47 TMFD6 Adverse Selection Integration Results")
    lines.append("")
    lines.append("## Context")
    lines.append("")
    lines.append("R47 on TMFD6 with spread>=5 has ~1 pt/RT margin after 4.0 pts/RT fee.")
    lines.append("Testing 4 AS reduction mechanisms to improve Mean PnL/RT.")
    lines.append("")
    lines.append("## Economics")
    lines.append("")
    lines.append("| Parameter | Value |")
    lines.append("|-----------|-------|")
    lines.append(f"| Scale | {SCALE} (prices x1e6) |")
    lines.append(f"| Point value | {POINT_VALUE_NTD} NTD/pt |")
    lines.append(f"| Fee/side | {FEE_PER_SIDE_NTD} NTD |")
    lines.append(f"| RT cost | {FEE_RT_PTS:.1f} pts |")
    lines.append("")

    for qf in queue_fracs:
        baseline_key = ("A.Baseline", qf)
        baseline_fills = accum.get(baseline_key, {}).get("fills", 0)

        lines.append(f"## Queue fraction = {qf}")
        lines.append("")
        lines.append(
            f"| Config | Fills | Trips | WR% | Gross | Fees | NET pts | NTD/day | "
            f"Mean/RT | AS10 | Supp% | Fill% | t-stat |"
        )
        lines.append(
            "|--------|-------|-------|-----|-------|------|---------|---------|"
            "---------|------|-------|-------|--------|"
        )

        keys = sorted([k for k in accum if k[1] == qf], key=lambda x: x[0])
        for key in keys:
            a = accum[key]
            cfg_name = key[0]
            fills = a["fills"]
            trips = a["trips"]
            wins = a["wins"]
            gross = a["gross_pts"]
            fee_pts = fills * FEE_PER_SIDE_NTD / POINT_VALUE_NTD
            net = gross - fee_pts
            days = a["days"]
            wr = wins / trips * 100 if trips > 0 else 0.0
            mean_rt = (gross / trips - FEE_RT_PTS) if trips > 0 else 0.0
            ntd_day = (net * POINT_VALUE_NTD / days) if days > 0 else 0.0
            as10 = a["as10_sum"] / a["as10_count"] if a["as10_count"] > 0 else 0.0
            supp_pct = a["suppressed_pct_sum"] / days if days > 0 else 0.0
            fill_pct = fills / baseline_fills * 100 if baseline_fills > 0 else 0.0

            daily = np.array(a["daily_net_pts"])
            if len(daily) > 1 and daily.std() > 0:
                t_stat = daily.mean() / (daily.std() / np.sqrt(len(daily)))
            else:
                t_stat = 0.0

            lines.append(
                f"| {cfg_name} | {fills} | {trips} | {wr:.1f}% | "
                f"{gross:+.0f} | {-fee_pts:.0f} | {net:+.0f} | {ntd_day:+,.0f} | "
                f"{mean_rt:+.3f} | {as10:+.3f} | {supp_pct:.1f}% | {fill_pct:.1f}% | "
                f"{t_stat:+.2f} |"
            )

        lines.append("")

    lines.append("## Key Metrics")
    lines.append("")
    lines.append("- **Mean/RT**: Mean gross PnL per round trip minus RT fee cost (4.0 pts). Higher = better.")
    lines.append("- **AS10**: Mean mid-price move 10 snapshots after fill (positive = favorable).")
    lines.append("- **Supp%**: % of quote opportunities where BOTH sides were suppressed.")
    lines.append("- **Fill%**: Fills as % of baseline (higher = less aggressive filtering).")
    lines.append("- **t-stat**: t-statistic of daily net PnL (>2.0 = significant).")
    lines.append("")

    with open(output_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\nReport saved: {output_path}")


def main():
    symbol = "TMFD6"
    spread_threshold = 5
    max_pos = 3
    queue_fracs = [0.5, 1.0]

    print("=" * 120)
    print("  R47 TMFD6 Adverse Selection Reduction Backtest")
    print(f"  Symbol: {symbol} | spread>={spread_threshold} | max_pos={max_pos}")
    print(f"  Economics: 1pt={POINT_VALUE_NTD} NTD, fee={FEE_PER_SIDE_NTD} NTD/side, RT={FEE_RT_PTS:.1f} pts")
    print("=" * 120)

    # Get available days
    print("\nFetching trading days...")
    dates = get_trading_days(symbol)
    print(f"  Found {len(dates)} days: {dates}")

    if not dates:
        print("ERROR: No TMFD6 data found in ClickHouse.")
        sys.exit(1)

    # Phase 1: Run individual configs
    print(f"\n{'#'*120}")
    print(f"# Phase 1: Individual mechanism tests ({len(dates)} days)")
    print(f"{'#'*120}")

    individual_configs = build_test_configs()
    accum = run_all_configs(
        symbol, dates, individual_configs,
        queue_fracs=queue_fracs,
        spread_threshold=spread_threshold,
        max_pos=max_pos,
    )

    # Print Phase 1 results
    for qf in queue_fracs:
        baseline_fills = accum.get(("A.Baseline", qf), {}).get("fills", 0)
        print_results_table(accum, qf, baseline_fills)

    # Phase 2: Find best individual thresholds and run combos
    print(f"\n{'#'*120}")
    print(f"# Phase 2: Combination tests")
    print(f"{'#'*120}")

    # Use half-queue results to pick best
    best_tox, best_tob, best_qi, best_ofi = find_best_individual(accum, 0.5)
    print(f"  Best individual (qf=0.5, fill%>=70%):")
    print(f"    Toxicity threshold: {best_tox}")
    print(f"    TOB adverse ms: {best_tob}")
    print(f"    QI threshold: {best_qi}")
    print(f"    OFI quantile: {best_ofi}")

    combo_configs = build_combo_configs(best_tox, best_tob, best_qi, best_ofi)
    combo_accum = run_all_configs(
        symbol, dates, combo_configs,
        queue_fracs=queue_fracs,
        spread_threshold=spread_threshold,
        max_pos=max_pos,
    )

    # Merge combo results into main accumulator
    accum.update(combo_accum)

    # Print final combined table
    print(f"\n{'*'*120}")
    print(f"*  FINAL COMBINED RESULTS")
    print(f"{'*'*120}")
    for qf in queue_fracs:
        baseline_fills = accum.get(("A.Baseline", qf), {}).get("fills", 0)
        print_results_table(accum, qf, baseline_fills)

    # Write report
    output_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "outputs", "team_artifacts", "alpha-research",
    )
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "r47_as_integration_results.md")
    write_report(accum, queue_fracs, output_path)

    # Print recommendation
    print(f"\n{'*'*120}")
    print(f"*  RECOMMENDATION")
    print(f"{'*'*120}")

    # Find best combo at qf=0.5
    best_key = None
    best_mean_rt = -999.0
    baseline_fills_05 = accum.get(("A.Baseline", 0.5), {}).get("fills", 1)
    for key, a in accum.items():
        if key[1] != 0.5 or key[0] == "A.Baseline":
            continue
        fills = a["fills"]
        fill_pct = fills / baseline_fills_05 if baseline_fills_05 > 0 else 0
        if fill_pct < 0.70:
            continue
        mrt = _get_mean_rt(accum, key)
        if mrt > best_mean_rt:
            best_mean_rt = mrt
            best_key = key

    baseline_mrt = _get_mean_rt(accum, ("A.Baseline", 0.5))
    if best_key:
        improvement = best_mean_rt - baseline_mrt
        a = accum[best_key]
        fill_pct = a["fills"] / baseline_fills_05 * 100
        print(f"  Best config (qf=0.5, fill%>=70%): {best_key[0]}")
        print(f"  Baseline Mean/RT: {baseline_mrt:+.3f} pts")
        print(f"  Best Mean/RT:     {best_mean_rt:+.3f} pts")
        print(f"  Improvement:      {improvement:+.3f} pts/RT")
        print(f"  Fill retention:   {fill_pct:.1f}%")
    else:
        print(f"  No config improved Mean/RT while keeping fill% >= 70%")
        print(f"  Baseline Mean/RT: {baseline_mrt:+.3f} pts")
    print(f"{'*'*120}")


if __name__ == "__main__":
    main()
