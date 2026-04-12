"""R30 Stage 3 Backtest — Omori DOWN-SHOCK Continuation on TAIFEX Mini-TAIEX Futures.

Signal: 150-pt downward move within 30-min rolling window during regular session.
Direction: SHORT continuation only.
Entry: Limit at mid-price, 60s delay after detection, 10s fill timeout.

Three configs tested:
  A: SL=30, TP=40, max_hold=10min
  B: SL=40, TP=40, max_hold=10min
  C: No TP/SL, flat close at 5min
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np

import clickhouse_connect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PRICE_SCALE = 1_000_000  # price_scaled / PRICE_SCALE = index points
TW_TZ = timezone(timedelta(hours=8))
SESSION_START_H, SESSION_START_M = 8, 45
SESSION_END_H, SESSION_END_M = 13, 30
ENTRY_GUARD_MINUTES = 10  # no entry after 13:20
MAINSHOCK_THRESHOLD_PTS = 150
MAINSHOCK_WINDOW_MIN = 30
ENTRY_DELAY_S = 60
FILL_TIMEOUT_S = 10
MAX_SPREAD_PTS = 5
MIN_GAP_S = 30 * 60  # 30 min between events
MAX_EVENTS_DAY = 10
DAILY_LOSS_LIMIT_PTS = 200
AFTERSHOCK_THRESHOLD_PTS = 10  # |1-min return| threshold for Omori fit
SPREAD_COST_PTS = 4.7  # round-trip: 3.7 pts spread avg + 1.0 pt exchange/broker fees

# Configs
CONFIGS = {
    "A": {"sl": 30, "tp": 40, "max_hold_s": 600},
    "B": {"sl": 40, "tp": 40, "max_hold_s": 600},
    "C": {"sl": None, "tp": None, "max_hold_s": 300},
}

# Also test 30s entry delay as sensitivity
ENTRY_DELAYS = [60, 30]

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TickRow:
    ts_ns: int
    price_pts: float
    volume: int


@dataclass(frozen=True, slots=True)
class BidAskRow:
    ts_ns: int
    best_bid_pts: float
    best_ask_pts: float
    spread_pts: float
    mid_pts: float


@dataclass(slots=True)
class Trade:
    config: str
    entry_delay_s: int
    event_id: int
    entry_time: str
    exit_time: str
    entry_price: float
    exit_price: float
    pnl_pts: float
    exit_reason: str
    mainshock_size: float
    mainshock_direction: str
    symbol: str
    day: str
    week: str
    fill_attempted: bool = True
    filled: bool = True


@dataclass(slots=True)
class MainshockEvent:
    ts_ns: int
    price_pts: float
    change_pts: float  # signed
    direction: str  # "DOWN" only in this backtest
    idx: int  # index in tick array


# ---------------------------------------------------------------------------
# ClickHouse data loading
# ---------------------------------------------------------------------------

def get_client() -> Any:
    return clickhouse_connect.get_client(
        host=os.environ.get("HFT_CLICKHOUSE_HOST", "localhost"),
        port=int(os.environ.get("HFT_CLICKHOUSE_PORT", "8123")),
        username=os.environ.get("HFT_CLICKHOUSE_USER", "default"),
        password=os.environ.get("CLICKHOUSE_PASSWORD", ""),
    )


def load_tick_data(client: Any, symbol: str, date_str: str) -> list[TickRow]:
    """Load tick data for a symbol on a given date, sorted by exch_ts."""
    q = (
        "SELECT exch_ts, price_scaled, volume "
        "FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'Tick' "
        f"AND toDate(toDateTime(exch_ts / 1000000000)) = '{date_str}' "
        "ORDER BY exch_ts"
    )
    result = client.query(q)
    rows = []
    for r in result.result_rows:
        rows.append(TickRow(ts_ns=r[0], price_pts=r[1] / PRICE_SCALE, volume=r[2]))
    return rows


def load_bidask_data(client: Any, symbol: str, date_str: str) -> list[BidAskRow]:
    """Load BidAsk data for spread/mid computation."""
    q = (
        "SELECT exch_ts, bids_price, asks_price "
        "FROM hft.market_data "
        f"WHERE symbol = '{symbol}' AND type = 'BidAsk' "
        f"AND toDate(toDateTime(exch_ts / 1000000000)) = '{date_str}' "
        "AND length(bids_price) > 0 AND length(asks_price) > 0 "
        "ORDER BY exch_ts"
    )
    result = client.query(q)
    rows = []
    for r in result.result_rows:
        bids = r[1]
        asks = r[2]
        if not bids or not asks:
            continue
        best_bid = bids[0] / PRICE_SCALE
        best_ask = asks[0] / PRICE_SCALE
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        rows.append(BidAskRow(
            ts_ns=r[0], best_bid_pts=best_bid, best_ask_pts=best_ask,
            spread_pts=spread, mid_pts=mid,
        ))
    return rows


def get_tmf_symbols_and_dates(client: Any) -> list[tuple[str, str]]:
    """Get all (symbol, date) pairs for TMF contracts."""
    q = (
        "SELECT symbol, toDate(toDateTime(exch_ts / 1000000000)) as dt, count() as cnt "
        "FROM hft.market_data "
        "WHERE symbol LIKE '%TMF%' AND type = 'Tick' "
        "GROUP BY symbol, dt "
        "HAVING cnt > 1000 "  # skip days with minimal data
        "ORDER BY dt, symbol"
    )
    result = client.query(q)
    return [(r[0], str(r[1])) for r in result.result_rows]


# ---------------------------------------------------------------------------
# Session filter helpers
# ---------------------------------------------------------------------------

def ts_to_tw_datetime(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1_000_000_000, tz=TW_TZ)


def is_in_session(dt: datetime) -> bool:
    t = dt.hour * 60 + dt.minute
    return (SESSION_START_H * 60 + SESSION_START_M) <= t <= (SESSION_END_H * 60 + SESSION_END_M)


def is_entry_allowed(dt: datetime, max_hold_s: int) -> bool:
    """No entry if session_end - current_time < max_hold."""
    session_end = dt.replace(hour=SESSION_END_H, minute=SESSION_END_M, second=0, microsecond=0)
    return (session_end - dt).total_seconds() >= max_hold_s / 60 * 60  # conservative


# ---------------------------------------------------------------------------
# Spread lookup
# ---------------------------------------------------------------------------

def build_spread_lookup(bidask: list[BidAskRow]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build arrays for fast spread/mid lookup by timestamp."""
    if not bidask:
        return np.array([]), np.array([]), np.array([])
    ts = np.array([b.ts_ns for b in bidask], dtype=np.int64)
    spreads = np.array([b.spread_pts for b in bidask], dtype=np.float64)
    mids = np.array([b.mid_pts for b in bidask], dtype=np.float64)
    return ts, spreads, mids


def get_spread_at(ba_ts: np.ndarray, ba_spreads: np.ndarray, ba_mids: np.ndarray, ts_ns: int) -> tuple[float, float]:
    """Get spread and mid at a given timestamp (latest before ts_ns)."""
    if len(ba_ts) == 0:
        return 999.0, 0.0
    idx = np.searchsorted(ba_ts, ts_ns, side="right") - 1
    if idx < 0:
        return 999.0, 0.0
    return float(ba_spreads[idx]), float(ba_mids[idx])


# ---------------------------------------------------------------------------
# Mainshock detection
# ---------------------------------------------------------------------------

def detect_mainshocks(
    ticks: list[TickRow],
    threshold_pts: float = MAINSHOCK_THRESHOLD_PTS,
    window_ns: int = MAINSHOCK_WINDOW_MIN * 60 * 1_000_000_000,
    min_gap_ns: int = MIN_GAP_S * 1_000_000_000,
) -> list[MainshockEvent]:
    """Detect down-shock mainshocks in tick data using rolling window."""
    events = []
    last_event_ts = 0
    # Use a pointer approach for the rolling window
    left = 0
    for right in range(len(ticks)):
        ts = ticks[right].ts_ns
        price = ticks[right].price_pts
        dt = ts_to_tw_datetime(ts)

        # Only detect during regular session
        if not is_in_session(dt):
            continue

        # Advance left pointer to maintain window
        while left < right and (ts - ticks[left].ts_ns) > window_ns:
            left += 1

        if left >= right:
            continue

        # Check change from oldest in window to current
        change = price - ticks[left].price_pts

        # Only DOWN-SHOCK
        if change <= -threshold_pts:
            # Gap check
            if ts - last_event_ts >= min_gap_ns:
                events.append(MainshockEvent(
                    ts_ns=ts, price_pts=price, change_pts=change,
                    direction="DOWN", idx=right,
                ))
                last_event_ts = ts

    return events


# ---------------------------------------------------------------------------
# Fill simulation
# ---------------------------------------------------------------------------

def check_fill(
    ticks: list[TickRow],
    start_idx: int,
    target_price_pts: float,
    timeout_ns: int,
    direction: str,
) -> tuple[bool, float, int]:
    """Check if a limit order at target_price gets filled within timeout.

    For SHORT entry (SELL): filled if any tick trades at or above target_price.
    Returns (filled, fill_price, fill_idx).
    """
    start_ts = ticks[start_idx].ts_ns
    for i in range(start_idx, len(ticks)):
        if ticks[i].ts_ns - start_ts > timeout_ns:
            break
        # For a SHORT limit sell at mid: filled if price >= mid (someone lifts our offer)
        if direction == "SELL" and ticks[i].price_pts >= target_price_pts:
            return True, ticks[i].price_pts, i
    return False, 0.0, start_idx


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

def simulate_trade(
    ticks: list[TickRow],
    entry_idx: int,
    entry_price: float,
    sl: float | None,
    tp: float | None,
    max_hold_ns: int,
    direction: str,
) -> tuple[float, float, str, int]:
    """Simulate a trade from entry_idx. Returns (exit_price, pnl, reason, exit_idx)."""
    entry_ts = ticks[entry_idx].ts_ns

    for i in range(entry_idx + 1, len(ticks)):
        elapsed = ticks[i].ts_ns - entry_ts
        price = ticks[i].price_pts

        if direction == "SELL":
            pnl = entry_price - price
            # Stop loss: price went UP by sl points
            if sl is not None and price >= entry_price + sl:
                return price, -sl, "stop_loss", i
            # Take profit: price went DOWN by tp points
            if tp is not None and price <= entry_price - tp:
                return price, tp, "take_profit", i
        else:
            pnl = price - entry_price
            if sl is not None and price <= entry_price - sl:
                return price, -sl, "stop_loss", i
            if tp is not None and price >= entry_price + tp:
                return price, tp, "take_profit", i

        # Max hold
        if elapsed >= max_hold_ns:
            if direction == "SELL":
                pnl = entry_price - price
            else:
                pnl = price - entry_price
            return price, pnl, "max_hold", i

    # End of data — force close
    last = ticks[-1]
    if direction == "SELL":
        pnl = entry_price - last.price_pts
    else:
        pnl = last.price_pts - entry_price
    return last.price_pts, pnl, "eod_close", len(ticks) - 1


# ---------------------------------------------------------------------------
# Omori aftershock fitting
# ---------------------------------------------------------------------------

def collect_aftershocks(
    ticks: list[TickRow],
    mainshock_idx: int,
    tracking_minutes: int = 120,
    aftershock_threshold_pts: float = AFTERSHOCK_THRESHOLD_PTS,
) -> list[float]:
    """Collect aftershock times (seconds since mainshock).

    An aftershock = |1-min return| > threshold.
    We compute 1-min returns at each tick using a lookback.
    """
    ms_ts = ticks[mainshock_idx].ts_ns
    max_ns = tracking_minutes * 60 * 1_000_000_000
    one_min_ns = 60 * 1_000_000_000
    aftershock_times = []

    lookback_idx = mainshock_idx
    for i in range(mainshock_idx + 1, len(ticks)):
        elapsed = ticks[i].ts_ns - ms_ts
        if elapsed > max_ns:
            break

        # Advance lookback to ~1 min before current
        while lookback_idx < i and (ticks[i].ts_ns - ticks[lookback_idx].ts_ns) > one_min_ns:
            lookback_idx += 1

        if lookback_idx >= i:
            continue

        ret = abs(ticks[i].price_pts - ticks[lookback_idx].price_pts)
        if ret >= aftershock_threshold_pts:
            aftershock_times.append(elapsed / 1_000_000_000)

    return aftershock_times


def fit_omori(aftershock_times: list[float]) -> tuple[float, float, float] | None:
    """Fit Omori K, c, p from aftershock times. Returns (K, c, p) or None."""
    if len(aftershock_times) < 5:
        return None

    times = np.array(sorted(aftershock_times))
    t_max = times[-1]
    if t_max <= 1.0:
        return None

    bin_edges = np.logspace(0, np.log10(t_max), num=10)
    counts, _ = np.histogram(times, bins=bin_edges)
    valid = counts > 0
    if valid.sum() < 3:
        return None

    bin_mids = 0.5 * (bin_edges[:-1] + bin_edges[1:])
    bin_widths = np.diff(bin_edges)
    rates = counts[valid] / bin_widths[valid]
    mids = bin_mids[valid]

    c = 1.0
    log_t = np.log(mids + c)
    log_r = np.log(rates)
    coeffs = np.polyfit(log_t, log_r, 1)
    p = -coeffs[0]
    K = np.exp(coeffs[1])
    return K, c, p


# ---------------------------------------------------------------------------
# Detrended IC
# ---------------------------------------------------------------------------

def compute_rank_ic(signal: list[float], ret: list[float]) -> float:
    """Spearman rank IC between signal and return."""
    if len(signal) < 3:
        return 0.0
    from scipy.stats import spearmanr
    ic, _ = spearmanr(signal, ret)
    return ic if not np.isnan(ic) else 0.0


# ---------------------------------------------------------------------------
# Main backtest
# ---------------------------------------------------------------------------

def run_backtest() -> dict[str, Any]:
    print("Connecting to ClickHouse...")
    client = get_client()

    print("Loading TMF symbol/date pairs...")
    sym_dates = get_tmf_symbols_and_dates(client)
    print(f"  Found {len(sym_dates)} symbol-date pairs")

    # Results containers
    all_trades: dict[str, list[Trade]] = {f"{c}_{d}": [] for c in CONFIGS for d in ENTRY_DELAYS}
    all_omori_params: list[dict[str, Any]] = []
    all_mainshock_signals: list[dict[str, float]] = []  # for IC computation
    fill_attempts = 0
    fill_successes = 0
    event_counter = 0

    for sym, date_str in sym_dates:
        print(f"  Processing {sym} / {date_str}...")
        ticks = load_tick_data(client, sym, date_str)
        bidask = load_bidask_data(client, sym, date_str)

        if len(ticks) < 100:
            print(f"    Skipping — only {len(ticks)} ticks")
            continue

        ba_ts, ba_spreads, ba_mids = build_spread_lookup(bidask)

        # Detect mainshocks
        mainshocks = detect_mainshocks(ticks)
        print(f"    {len(mainshocks)} mainshock(s) detected")

        for ms in mainshocks:
            event_counter += 1
            ms_dt = ts_to_tw_datetime(ms.ts_ns)
            day_str = ms_dt.strftime("%Y-%m-%d")
            week_str = ms_dt.strftime("%Y-W%W")

            # Collect aftershocks for Omori fitting
            as_times = collect_aftershocks(ticks, ms.idx)
            omori_fit = fit_omori(as_times)
            omori_entry = {
                "event_id": event_counter,
                "symbol": sym,
                "date": day_str,
                "mainshock_size": ms.change_pts,
                "n_aftershocks": len(as_times),
            }
            if omori_fit:
                omori_entry["K"] = omori_fit[0]
                omori_entry["c"] = omori_fit[1]
                omori_entry["p"] = omori_fit[2]
            all_omori_params.append(omori_entry)

            # For each entry delay
            for delay_s in ENTRY_DELAYS:
                entry_delay_ns = delay_s * 1_000_000_000
                target_entry_ts = ms.ts_ns + entry_delay_ns

                # Find tick at/after entry time
                entry_idx = None
                for i in range(ms.idx, len(ticks)):
                    if ticks[i].ts_ns >= target_entry_ts:
                        entry_idx = i
                        break
                if entry_idx is None:
                    continue

                entry_dt = ts_to_tw_datetime(ticks[entry_idx].ts_ns)
                if not is_in_session(entry_dt):
                    continue

                # Get spread at entry
                spread, mid = get_spread_at(ba_ts, ba_spreads, ba_mids, ticks[entry_idx].ts_ns)
                if spread > MAX_SPREAD_PTS:
                    continue  # spread gate

                # Attempt limit fill at mid
                fill_timeout_ns = FILL_TIMEOUT_S * 1_000_000_000
                fill_attempts += 1
                filled, fill_price, fill_idx = check_fill(
                    ticks, entry_idx, mid, fill_timeout_ns, "SELL",
                )

                if not filled:
                    # Record missed trade for all configs
                    for cfg_name in CONFIGS:
                        key = f"{cfg_name}_{delay_s}"
                        all_trades[key].append(Trade(
                            config=cfg_name, entry_delay_s=delay_s,
                            event_id=event_counter,
                            entry_time=entry_dt.isoformat(),
                            exit_time="", entry_price=mid, exit_price=0.0,
                            pnl_pts=0.0, exit_reason="unfilled",
                            mainshock_size=ms.change_pts,
                            mainshock_direction="DOWN", symbol=sym,
                            day=day_str, week=week_str,
                            fill_attempted=True, filled=False,
                        ))
                    continue

                fill_successes += 1

                # For each config, simulate trade
                for cfg_name, cfg in CONFIGS.items():
                    key = f"{cfg_name}_{delay_s}"
                    max_hold_ns = cfg["max_hold_s"] * 1_000_000_000

                    # Entry guard: enough time before session end?
                    if not is_entry_allowed(entry_dt, cfg["max_hold_s"]):
                        continue

                    # Daily limits
                    day_trades = [t for t in all_trades[key] if t.day == day_str and t.filled]
                    if len(day_trades) >= MAX_EVENTS_DAY:
                        continue
                    daily_pnl = sum(t.pnl_pts for t in day_trades)
                    if daily_pnl <= -DAILY_LOSS_LIMIT_PTS:
                        continue

                    exit_price, pnl, reason, exit_idx = simulate_trade(
                        ticks, fill_idx, mid, cfg["sl"], cfg["tp"],
                        max_hold_ns, "SELL",
                    )

                    exit_dt = ts_to_tw_datetime(ticks[exit_idx].ts_ns)

                    all_trades[key].append(Trade(
                        config=cfg_name, entry_delay_s=delay_s,
                        event_id=event_counter,
                        entry_time=entry_dt.isoformat(),
                        exit_time=exit_dt.isoformat(),
                        entry_price=mid, exit_price=exit_price,
                        pnl_pts=pnl, exit_reason=reason,
                        mainshock_size=ms.change_pts,
                        mainshock_direction="DOWN", symbol=sym,
                        day=day_str, week=week_str,
                    ))

                    # Collect for IC: signal = |mainshock_size|, return = 5min return after mainshock
                    # (only once per event, not per config)

                # Collect 5-min return for IC (once per delay variant)
                five_min_ns = 5 * 60 * 1_000_000_000
                ret_5m = 0.0
                for i in range(ms.idx, len(ticks)):
                    if ticks[i].ts_ns - ms.ts_ns >= five_min_ns:
                        ret_5m = ms.price_pts - ticks[i].price_pts  # for SHORT: positive if price fell
                        break
                all_mainshock_signals.append({
                    "event_id": event_counter,
                    "signal": abs(ms.change_pts),
                    "ret_5m": ret_5m,
                    "week": week_str,
                    "day": day_str,
                    "symbol": sym,
                })

    return {
        "trades": all_trades,
        "omori": all_omori_params,
        "signals": all_mainshock_signals,
        "fill_attempts": fill_attempts,
        "fill_successes": fill_successes,
        "n_events": event_counter,
    }


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze_config(trades: list[Trade], config_key: str) -> dict[str, Any]:
    """Compute per-config summary statistics."""
    filled = [t for t in trades if t.filled]
    unfilled = [t for t in trades if not t.filled]

    total = len(trades)
    n_filled = len(filled)
    fill_rate = n_filled / total if total > 0 else 0.0

    if not filled:
        return {
            "config": config_key,
            "total_trades": total,
            "filled_trades": 0,
            "fill_rate": 0.0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_pts": 0.0,
            "max_consecutive_losses": 0,
            "annualized_sharpe": 0.0,
            "net_pnl_pts": 0.0,
            "gross_pnl_pts": 0.0,
            "spread_cost_pts": 0.0,
        }

    pnls = [t.pnl_pts for t in filled]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate = len(wins) / len(pnls) if pnls else 0.0
    avg_win = np.mean(wins) if wins else 0.0
    avg_loss = np.mean(losses) if losses else 0.0
    gross_pnl = sum(pnls)
    spread_cost = n_filled * SPREAD_COST_PTS  # round-trip spread cost
    net_pnl = gross_pnl - spread_cost

    # Profit factor
    total_wins = sum(wins) if wins else 0.0
    total_losses = abs(sum(losses)) if losses else 0.001
    profit_factor = total_wins / total_losses if total_losses > 0 else 0.0

    # Max drawdown
    cumsum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cumsum)
    drawdown = peak - cumsum
    max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    # Max consecutive losses
    max_consec = 0
    cur_consec = 0
    for p in pnls:
        if p <= 0:
            cur_consec += 1
            max_consec = max(max_consec, cur_consec)
        else:
            cur_consec = 0

    # Annualized Sharpe
    # Get unique trading days
    days = set(t.day for t in filled)
    n_days = len(days)
    if n_days > 1 and len(pnls) > 1:
        daily_pnl: dict[str, float] = defaultdict(float)
        for t in filled:
            daily_pnl[t.day] += t.pnl_pts - SPREAD_COST_PTS
        daily_returns = list(daily_pnl.values())
        mean_daily = np.mean(daily_returns)
        std_daily = np.std(daily_returns, ddof=1)
        sharpe = (mean_daily / std_daily) * np.sqrt(252) if std_daily > 0 else 0.0
    else:
        sharpe = 0.0

    # Weekly breakdown
    weekly: dict[str, float] = defaultdict(float)
    for t in filled:
        weekly[t.week] += t.pnl_pts - SPREAD_COST_PTS
    n_weeks = len(weekly)
    n_negative_weeks = sum(1 for v in weekly.values() if v < 0)

    # Exit reason breakdown
    exit_reasons: dict[str, int] = defaultdict(int)
    for t in filled:
        exit_reasons[t.exit_reason] += 1

    return {
        "config": config_key,
        "total_trades": total,
        "filled_trades": n_filled,
        "unfilled_trades": len(unfilled),
        "fill_rate": round(fill_rate, 4),
        "win_rate": round(win_rate, 4),
        "avg_win_pts": round(float(avg_win), 2),
        "avg_loss_pts": round(float(avg_loss), 2),
        "profit_factor": round(profit_factor, 4),
        "max_drawdown_pts": round(max_dd, 2),
        "max_consecutive_losses": max_consec,
        "annualized_sharpe": round(float(sharpe), 4),
        "net_pnl_pts": round(net_pnl, 2),
        "gross_pnl_pts": round(gross_pnl, 2),
        "spread_cost_pts": round(spread_cost, 2),
        "n_trading_days": n_days,
        "weekly_pnl": {k: round(v, 2) for k, v in sorted(weekly.items())},
        "n_weeks": n_weeks,
        "n_negative_weeks": n_negative_weeks,
        "pct_negative_weeks": round(n_negative_weeks / n_weeks, 4) if n_weeks > 0 else 0.0,
        "exit_reasons": dict(exit_reasons),
    }


def analyze_omori(omori_params: list[dict]) -> dict[str, Any]:
    """Analyze Omori p distribution across events."""
    fitted = [o for o in omori_params if "p" in o]
    if not fitted:
        return {"n_fitted": 0, "mean_p": None, "std_p": None, "p_values": []}

    p_vals = [o["p"] for o in fitted]
    return {
        "n_events": len(omori_params),
        "n_fitted": len(fitted),
        "mean_p": round(float(np.mean(p_vals)), 4),
        "std_p": round(float(np.std(p_vals, ddof=1)), 4) if len(p_vals) > 1 else 0.0,
        "median_p": round(float(np.median(p_vals)), 4),
        "p_values": [round(p, 4) for p in p_vals],
        "mean_K": round(float(np.mean([o["K"] for o in fitted])), 4),
        "mean_n_aftershocks": round(float(np.mean([o["n_aftershocks"] for o in fitted])), 1),
    }


def analyze_ic(signals: list[dict]) -> dict[str, Any]:
    """Detrended IC gate: rank IC between |mainshock_size| and 5-min return, by week."""
    if len(signals) < 3:
        return {"overall_ic": 0.0, "weekly_ic": {}, "monotonic_trend": False}

    try:
        from scipy.stats import spearmanr
    except ImportError:
        return {"overall_ic": 0.0, "weekly_ic": {}, "monotonic_trend": False, "error": "scipy not available"}

    sig = [s["signal"] for s in signals]
    ret = [s["ret_5m"] for s in signals]
    overall_ic, _ = spearmanr(sig, ret)
    if np.isnan(overall_ic):
        overall_ic = 0.0

    # Weekly IC
    weekly_data: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
    for s in signals:
        weekly_data[s["week"]][0].append(s["signal"])
        weekly_data[s["week"]][1].append(s["ret_5m"])

    weekly_ic = {}
    for week, (sigs, rets) in sorted(weekly_data.items()):
        if len(sigs) >= 3:
            ic, _ = spearmanr(sigs, rets)
            weekly_ic[week] = round(float(ic) if not np.isnan(ic) else 0.0, 4)

    # Check monotonic trend
    ic_values = list(weekly_ic.values())
    monotonic = False
    if len(ic_values) >= 3:
        diffs = np.diff(ic_values)
        if np.all(diffs > 0) or np.all(diffs < 0):
            monotonic = True

    return {
        "overall_ic": round(float(overall_ic), 4),
        "weekly_ic": weekly_ic,
        "monotonic_trend": monotonic,
        "n_signals": len(signals),
    }


# ---------------------------------------------------------------------------
# Kill criteria check
# ---------------------------------------------------------------------------

def check_kill_criteria(summaries: dict[str, dict], omori: dict, ic: dict) -> list[str]:
    """Check all kill criteria. Returns list of triggered kills."""
    kills = []

    for key, s in summaries.items():
        if s["filled_trades"] == 0:
            kills.append(f"{key}: No filled trades")
            continue

        if s["annualized_sharpe"] < 1.0:
            kills.append(f"{key}: Sharpe {s['annualized_sharpe']:.4f} < 1.0")

        if s["net_pnl_pts"] < 0:
            kills.append(f"{key}: Net PnL {s['net_pnl_pts']:.2f} < 0")

        if s["win_rate"] < 0.50:
            kills.append(f"{key}: Win rate {s['win_rate']:.4f} < 50%")

        if s["max_drawdown_pts"] > 500:
            kills.append(f"{key}: Max DD {s['max_drawdown_pts']:.2f} > 500 pts")

        if s["max_consecutive_losses"] > 8:
            kills.append(f"{key}: Consec losses {s['max_consecutive_losses']} > 8")

        if s.get("pct_negative_weeks", 0) > 0.50:
            kills.append(f"{key}: Negative weeks {s['pct_negative_weeks']:.1%} > 50%")

    if omori.get("std_p") is not None and omori["std_p"] > 0.4:
        kills.append(f"Omori p std {omori['std_p']:.4f} > 0.4")

    if ic.get("monotonic_trend"):
        kills.append("Detrended IC shows monotonic trend contamination")

    return kills


# ---------------------------------------------------------------------------
# Output generation
# ---------------------------------------------------------------------------

def generate_trade_log(all_trades: dict[str, list[Trade]]) -> list[dict]:
    """Generate JSON trade log."""
    log = []
    for key, trades in all_trades.items():
        for t in trades:
            log.append({
                "config": t.config,
                "entry_delay_s": t.entry_delay_s,
                "event_id": t.event_id,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "entry_price": round(t.entry_price, 1),
                "exit_price": round(t.exit_price, 1),
                "pnl_pts": round(t.pnl_pts, 2),
                "exit_reason": t.exit_reason,
                "mainshock_size": round(t.mainshock_size, 1),
                "symbol": t.symbol,
                "day": t.day,
                "week": t.week,
                "filled": t.filled,
            })
    return log


def main() -> None:
    print("=" * 60)
    print("R30 Stage 3 Backtest — Omori DOWN-SHOCK Continuation")
    print("=" * 60)

    results = run_backtest()

    print(f"\nTotal mainshock events detected: {results['n_events']}")
    print(f"Fill attempts: {results['fill_attempts']}, successes: {results['fill_successes']}")

    # Analyze each config
    summaries = {}
    for key, trades in results["trades"].items():
        summary = analyze_config(trades, key)
        summaries[key] = summary

    # Omori analysis
    omori = analyze_omori(results["omori"])
    print(f"\nOmori: {omori['n_fitted']}/{omori['n_events']} events fitted")
    if omori["mean_p"] is not None:
        print(f"  Mean p = {omori['mean_p']:.4f}, Std p = {omori['std_p']:.4f}")

    # IC analysis
    ic = analyze_ic(results["signals"])
    print(f"\nDetrended IC: overall = {ic['overall_ic']:.4f}, monotonic = {ic.get('monotonic_trend')}")

    # Kill criteria
    kills = check_kill_criteria(summaries, omori, ic)

    # Print summaries
    for key in sorted(summaries.keys()):
        s = summaries[key]
        print(f"\n--- Config {key} ---")
        print(f"  Trades: {s['total_trades']} (filled: {s['filled_trades']}, unfilled: {s.get('unfilled_trades', 0)})")
        print(f"  Fill rate: {s['fill_rate']:.1%}")
        print(f"  Win rate: {s['win_rate']:.1%}")
        print(f"  Avg win: {s['avg_win_pts']:.1f} pts, Avg loss: {s['avg_loss_pts']:.1f} pts")
        print(f"  Profit factor: {s['profit_factor']:.2f}")
        print(f"  Net PnL: {s['net_pnl_pts']:.1f} pts (gross: {s['gross_pnl_pts']:.1f}, cost: {s['spread_cost_pts']:.1f})")
        print(f"  Max DD: {s['max_drawdown_pts']:.1f} pts, Max consec losses: {s['max_consecutive_losses']}")
        print(f"  Sharpe: {s['annualized_sharpe']:.4f}")
        print(f"  Weeks: {s['n_weeks']} total, {s['n_negative_weeks']} negative ({s['pct_negative_weeks']:.1%})")
        if s.get("exit_reasons"):
            print(f"  Exit reasons: {s['exit_reasons']}")

    if kills:
        print(f"\n{'='*60}")
        print(f"KILL CRITERIA TRIGGERED ({len(kills)}):")
        for k in kills:
            print(f"  - {k}")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print("ALL KILL CRITERIA PASSED")
        print(f"{'='*60}")

    # Save outputs
    out_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "outputs", "team_artifacts", "alpha-research")
    os.makedirs(out_dir, exist_ok=True)

    # Trade log
    trade_log = generate_trade_log(results["trades"])
    trade_log_path = os.path.join(out_dir, "r30_stage3_trades.json")
    with open(trade_log_path, "w") as f:
        json.dump(trade_log, f, indent=2)
    print(f"\nTrade log saved: {trade_log_path}")

    # Report
    report = generate_report(summaries, omori, ic, kills, results)
    report_path = os.path.join(out_dir, "r30_stage3_backtest.md")
    with open(report_path, "w") as f:
        f.write(report)
    print(f"Report saved: {report_path}")

    # Return for programmatic use
    return {
        "summaries": summaries,
        "omori": omori,
        "ic": ic,
        "kills": kills,
        "n_events": results["n_events"],
        "fill_rate": results["fill_successes"] / results["fill_attempts"] if results["fill_attempts"] > 0 else 0,
    }


def generate_report(
    summaries: dict[str, dict],
    omori: dict,
    ic: dict,
    kills: list[str],
    results: dict,
) -> str:
    """Generate markdown report."""
    lines = []
    lines.append("# R30 Stage 3 Backtest Report — Omori DOWN-SHOCK Continuation\n")
    lines.append(f"**Date**: {datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M')}\n")
    lines.append(f"**Signal**: 150-pt downward move within 30-min rolling window, SHORT continuation\n")
    lines.append(f"**Instruments**: All TMF contracts in ClickHouse\n")
    lines.append(f"**Total mainshock events**: {results['n_events']}\n")
    fr = results['fill_successes'] / results['fill_attempts'] if results['fill_attempts'] > 0 else 0
    lines.append(f"**Fill rate (limit at mid, 10s timeout)**: {fr:.1%} ({results['fill_successes']}/{results['fill_attempts']})\n")

    # Verdict
    if kills:
        lines.append("## VERDICT: KILL R30\n")
        lines.append(f"**{len(kills)} kill criteria triggered.**\n")
        for k in kills:
            lines.append(f"- {k}")
        lines.append("")
    else:
        lines.append("## VERDICT: PASS (proceed to Stage 4)\n")

    # Per-config summaries
    lines.append("## Per-Config Summary\n")

    # Primary configs (60s delay)
    for cfg in ["A", "B", "C"]:
        key60 = f"{cfg}_60"
        key30 = f"{cfg}_30"
        s = summaries.get(key60, {})
        s30 = summaries.get(key30, {})
        sl_tp = CONFIGS[cfg]
        desc = f"SL={sl_tp['sl']}, TP={sl_tp['tp']}, hold={sl_tp['max_hold_s']//60}min"

        lines.append(f"### Config {cfg} ({desc})\n")
        lines.append("| Metric | 60s delay | 30s delay |")
        lines.append("|--------|-----------|-----------|")

        def v(d: dict, k: str, fmt: str = ".2f") -> str:
            val = d.get(k, 0)
            if isinstance(val, float):
                return f"{val:{fmt}}"
            return str(val)

        def pct(d: dict, k: str) -> str:
            val = d.get(k, 0)
            return f"{val:.1%}" if isinstance(val, float) else str(val)

        lines.append(f"| Total trades | {s.get('total_trades', 0)} | {s30.get('total_trades', 0)} |")
        lines.append(f"| Filled trades | {s.get('filled_trades', 0)} | {s30.get('filled_trades', 0)} |")
        lines.append(f"| Fill rate | {pct(s, 'fill_rate')} | {pct(s30, 'fill_rate')} |")
        lines.append(f"| Win rate | {pct(s, 'win_rate')} | {pct(s30, 'win_rate')} |")
        lines.append(f"| Avg win (pts) | {v(s, 'avg_win_pts')} | {v(s30, 'avg_win_pts')} |")
        lines.append(f"| Avg loss (pts) | {v(s, 'avg_loss_pts')} | {v(s30, 'avg_loss_pts')} |")
        lines.append(f"| Profit factor | {v(s, 'profit_factor', '.4f')} | {v(s30, 'profit_factor', '.4f')} |")
        lines.append(f"| Gross PnL (pts) | {v(s, 'gross_pnl_pts')} | {v(s30, 'gross_pnl_pts')} |")
        lines.append(f"| Spread cost (pts) | {v(s, 'spread_cost_pts')} | {v(s30, 'spread_cost_pts')} |")
        lines.append(f"| **Net PnL (pts)** | **{v(s, 'net_pnl_pts')}** | **{v(s30, 'net_pnl_pts')}** |")
        lines.append(f"| Max DD (pts) | {v(s, 'max_drawdown_pts')} | {v(s30, 'max_drawdown_pts')} |")
        lines.append(f"| Max consec losses | {s.get('max_consecutive_losses', 0)} | {s30.get('max_consecutive_losses', 0)} |")
        lines.append(f"| **Sharpe** | **{v(s, 'annualized_sharpe', '.4f')}** | **{v(s30, 'annualized_sharpe', '.4f')}** |")
        lines.append(f"| Negative weeks | {s.get('n_negative_weeks', 0)}/{s.get('n_weeks', 0)} ({pct(s, 'pct_negative_weeks')}) | {s30.get('n_negative_weeks', 0)}/{s30.get('n_weeks', 0)} ({pct(s30, 'pct_negative_weeks')}) |")
        lines.append("")

        if s.get("exit_reasons"):
            lines.append(f"Exit reasons (60s): {s['exit_reasons']}")
        if s30.get("exit_reasons"):
            lines.append(f"Exit reasons (30s): {s30['exit_reasons']}")
        lines.append("")

    # Weekly breakdown
    lines.append("## Weekly PnL Breakdown (60s delay)\n")
    lines.append("| Week | Config A | Config B | Config C |")
    lines.append("|------|----------|----------|----------|")
    all_weeks = set()
    for cfg in ["A", "B", "C"]:
        key = f"{cfg}_60"
        s = summaries.get(key, {})
        all_weeks.update(s.get("weekly_pnl", {}).keys())
    for week in sorted(all_weeks):
        vals = []
        for cfg in ["A", "B", "C"]:
            key = f"{cfg}_60"
            s = summaries.get(key, {})
            v = s.get("weekly_pnl", {}).get(week, 0)
            vals.append(f"{v:.1f}")
        lines.append(f"| {week} | {' | '.join(vals)} |")
    lines.append("")

    # Omori analysis
    lines.append("## Omori Decay Analysis\n")
    lines.append(f"- Events detected: {omori.get('n_events', 0)}")
    lines.append(f"- Events fitted (>= 5 aftershocks): {omori.get('n_fitted', 0)}")
    if omori.get("mean_p") is not None:
        lines.append(f"- **Mean p**: {omori['mean_p']:.4f}")
        lines.append(f"- **Std p**: {omori['std_p']:.4f}")
        lines.append(f"- Median p: {omori['median_p']:.4f}")
        lines.append(f"- Mean K: {omori['mean_K']:.4f}")
        lines.append(f"- Mean aftershocks/event: {omori['mean_n_aftershocks']:.1f}")
        lines.append(f"- p values: {omori['p_values']}")
    lines.append("")

    # IC analysis
    lines.append("## Detrended IC Gate\n")
    lines.append(f"- Overall rank IC (|mainshock_size| vs 5-min return): {ic.get('overall_ic', 0):.4f}")
    lines.append(f"- N signals: {ic.get('n_signals', 0)}")
    lines.append(f"- Monotonic trend: {'YES (KILL)' if ic.get('monotonic_trend') else 'NO'}")
    if ic.get("weekly_ic"):
        lines.append("\n| Week | IC |")
        lines.append("|------|----|")
        for week, v in sorted(ic["weekly_ic"].items()):
            lines.append(f"| {week} | {v:.4f} |")
    lines.append("")

    # Kill criteria summary
    lines.append("## Kill Criteria Evaluation\n")
    lines.append("| Criterion | Threshold | Status |")
    lines.append("|-----------|-----------|--------|")

    criteria_map = [
        ("Sharpe < 1.0", "Sharpe"),
        ("Net PnL < 0", "Net PnL"),
        ("Win rate < 50%", "Win rate"),
        ("Max DD > 500 pts", "Max DD"),
        ("Consec losses > 8", "Consec losses"),
        ("Omori p std > 0.4", "Omori"),
        ("Negative weeks > 50%", "Negative weeks"),
        ("IC monotonic trend", "IC"),
    ]
    kill_set = set(kills)
    for desc, _ in criteria_map:
        triggered = any(desc.lower()[:10] in k.lower() for k in kills)
        status = "TRIGGERED" if triggered else "PASS"
        lines.append(f"| {desc} | See above | {status} |")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
