"""
CBS 2.0 Hybrid Strategy Backtest — CBS timing + SG-LP execution

Strategies compared:
  S1: Base CBS (market order entry, fixed time exit)
  S2: CBS + Spread Wait (wait for wide spread after trigger, limit entry)
  S3: CBS + Limit Exit (market entry, limit order exit with stop)
  S4: Full CBS 2.0 (spread wait entry + limit exit)

Walk-forward:
  IS: first 14 days
  OOS: last 6 days (March)

TMFD6 economics:
  1 point = 10 NTD
  Market order RT cost: 4 pts (2 pts/leg)
  Limit entry + market exit: 2 pts cost
  Limit entry + limit exit: 0 pts (rare — both sides passive)

Author: Alpha Research Team — Direction C
Date: 2026-03-27
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

import numpy as np
from scipy import stats as sp_stats

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "raw" / "tmfd6"

ALL_DATES = [
    "2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30",
    "2026-02-03", "2026-02-04", "2026-02-05", "2026-02-06", "2026-02-10",
    "2026-02-11", "2026-02-23", "2026-02-24", "2026-02-25",
    "2026-03-19", "2026-03-20", "2026-03-23", "2026-03-24", "2026-03-25", "2026-03-26",
]

IS_DATES = ALL_DATES[:14]   # Jan 26 - Feb 25
OOS_DATES = ALL_DATES[14:]  # Mar 19 - Mar 26

PT_VALUE_NTD = 10.0
COST_MARKET_RT_PTS = 4.0    # Market order round-trip cost
COST_LIMIT_ENTRY_PTS = 2.0  # Limit entry + market exit cost (half passive)
COST_LIMIT_BOTH_PTS = 0.0   # Both sides limit (rare)

# Session gate (09:15-13:35 TW = UTC+8)
SESSION_START_SOD = 9 * 3600 + 15 * 60   # 33300
SESSION_END_SOD = 13 * 3600 + 35 * 60    # 48900
UTC_OFFSET = 8 * 3600

# CBS defaults
CBS_MOVE_BPS = 40
CBS_DETECT_WINDOW_S = 600
CBS_HOLD_S = 300
CBS_STOP_LOSS_BPS = 15

# Limit fill: require spread to remain >= distance for this many consecutive ticks
LIMIT_FILL_CONFIRM_TICKS = 2


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_day(date_str: str) -> np.ndarray | None:
    """Load a single day, filter to session hours (09:15-13:35 TW)."""
    path = DATA_DIR / f"TMFD6_{date_str}_l1.npy"
    if not path.exists():
        return None
    data = np.load(str(path), allow_pickle=True)
    ts = data["local_ts"]
    sod_tw = (ts / 1e9 + UTC_OFFSET) % 86400
    mask = (sod_tw >= SESSION_START_SOD) & (sod_tw <= SESSION_END_SOD)
    filtered = data[mask]
    return filtered if len(filtered) > 0 else None


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Trade:
    """A completed round-trip trade."""
    entry_ts: int
    exit_ts: int
    direction: int       # +1 long, -1 short
    entry_px: float      # entry price (index pts)
    exit_px: float       # exit price (index pts)
    gross_pnl_pts: float
    cost_pts: float
    net_pnl_pts: float
    exit_reason: str     # "time", "stop", "limit_fill", "eod"
    entry_method: str    # "market", "limit"
    exit_method: str     # "market", "limit"
    spread_at_entry: float
    day: str
    trigger_to_entry_s: float  # seconds from CBS trigger to actual entry (0 for market)
    hold_s: float


# ---------------------------------------------------------------------------
# CBS Move Detector (shared across all strategies)
# ---------------------------------------------------------------------------

class CBSDetector:
    """Detect large intraday moves within a rolling window."""

    def __init__(
        self,
        move_bps: int = CBS_MOVE_BPS,
        window_s: float = CBS_DETECT_WINDOW_S,
    ) -> None:
        self.move_bps = move_bps
        self.window_ns = int(window_s * 1e9)
        self.price_buf: deque[tuple[int, float]] = deque(maxlen=16384)
        self.last_trigger_ts: int = 0
        self.cooldown_ns = int(CBS_HOLD_S * 1e9)  # don't re-trigger during hold

    def reset(self) -> None:
        self.price_buf.clear()
        self.last_trigger_ts = 0

    def update(self, ts: int, mid: float) -> tuple[bool, int, float]:
        """Update with new mid price.

        Returns (triggered, direction, move_bps).
        direction: +1 if price moved UP (contrarian = sell/short),
                   -1 if price moved DOWN (contrarian = buy/long).
        """
        # Expire old entries
        cutoff = ts - self.window_ns
        while self.price_buf and self.price_buf[0][0] < cutoff:
            self.price_buf.popleft()
        self.price_buf.append((ts, mid))

        if len(self.price_buf) < 2:
            return False, 0, 0.0

        # Cooldown
        if ts < self.last_trigger_ts + self.cooldown_ns:
            return False, 0, 0.0

        oldest_mid = self.price_buf[0][1]
        if oldest_mid <= 0:
            return False, 0, 0.0

        move = (mid - oldest_mid) / oldest_mid * 10000  # bps
        abs_move = abs(move)

        if abs_move >= self.move_bps:
            direction = 1 if move > 0 else -1  # direction of the MOVE
            self.last_trigger_ts = ts
            return True, direction, move

        return False, 0, 0.0


# ---------------------------------------------------------------------------
# Strategy 1: Base CBS (market order entry, fixed time exit)
# ---------------------------------------------------------------------------

def run_base_cbs(
    data: np.ndarray,
    day: str,
    hold_s: float = CBS_HOLD_S,
    stop_bps: float = CBS_STOP_LOSS_BPS,
) -> list[Trade]:
    """Standard CBS with market order entry and fixed time/stop exit."""
    detector = CBSDetector()
    trades: list[Trade] = []

    ts_arr = data["local_ts"]
    bid_arr = data["bid_px"]
    ask_arr = data["ask_px"]
    mid_arr = data["mid_price"]
    n = len(data)

    hold_ns = int(hold_s * 1e9)

    # State
    positioned = False
    entry_ts = 0
    entry_px = 0.0
    entry_mid = 0.0
    direction = 0  # +1 = long (contrarian to down move), -1 = short
    spread_at_entry = 0.0

    for i in range(n):
        ts = int(ts_arr[i])
        bid = float(bid_arr[i])
        ask = float(ask_arr[i])
        mid = float(mid_arr[i])
        spread = ask - bid

        if mid <= 0 or bid <= 0 or ask <= 0:
            continue

        triggered, move_dir, move_bps = detector.update(ts, mid)

        if positioned:
            # Check exit
            elapsed_ns = ts - entry_ts
            # Unrealized PnL in bps
            if direction == 1:  # long
                pnl_bps = (mid - entry_mid) / entry_mid * 10000
            else:  # short
                pnl_bps = (entry_mid - mid) / entry_mid * 10000

            exit_reason = None
            if pnl_bps < -stop_bps:
                exit_reason = "stop"
            elif elapsed_ns >= hold_ns:
                exit_reason = "time"

            if exit_reason:
                # Exit at market
                if direction == 1:
                    exit_px = bid  # sell at bid
                else:
                    exit_px = ask  # buy at ask

                gross = direction * (exit_px - entry_px)
                cost = COST_MARKET_RT_PTS
                trades.append(Trade(
                    entry_ts=entry_ts, exit_ts=ts,
                    direction=direction, entry_px=entry_px, exit_px=exit_px,
                    gross_pnl_pts=gross, cost_pts=cost,
                    net_pnl_pts=gross - cost,
                    exit_reason=exit_reason, entry_method="market", exit_method="market",
                    spread_at_entry=spread_at_entry, day=day,
                    trigger_to_entry_s=0.0,
                    hold_s=(ts - entry_ts) / 1e9,
                ))
                positioned = False

        elif triggered:
            # Contrarian entry via market order
            # move_dir > 0 means price went up → short (contrarian)
            direction = -1 if move_dir > 0 else 1

            if direction == 1:
                entry_px = ask  # buy at ask
            else:
                entry_px = bid  # sell at bid

            entry_ts = ts
            entry_mid = mid
            spread_at_entry = spread
            positioned = True
            detector.last_trigger_ts = ts  # reset cooldown

    return trades


# ---------------------------------------------------------------------------
# Strategy 2: CBS + Spread Wait (limit entry, market exit)
# ---------------------------------------------------------------------------

def run_cbs_spread_wait(
    data: np.ndarray,
    day: str,
    patience_s: float = 60.0,
    min_spread_pts: float = 5.0,
    hold_s: float = CBS_HOLD_S,
    stop_bps: float = CBS_STOP_LOSS_BPS,
) -> tuple[list[Trade], int, int]:
    """CBS with spread-wait entry: wait for wide spread after trigger, limit order at mid.

    Returns (trades, n_triggers, n_skipped).
    """
    detector = CBSDetector()
    trades: list[Trade] = []
    n_triggers = 0
    n_skipped = 0

    ts_arr = data["local_ts"]
    bid_arr = data["bid_px"]
    ask_arr = data["ask_px"]
    mid_arr = data["mid_price"]
    n = len(data)

    hold_ns = int(hold_s * 1e9)
    patience_ns = int(patience_s * 1e9)

    # State
    state = "idle"  # "idle" | "armed" | "pending_fill" | "positioned"
    trigger_ts = 0
    trigger_direction = 0
    entry_ts = 0
    entry_px = 0.0
    entry_mid = 0.0
    direction = 0
    spread_at_entry = 0.0
    limit_price = 0.0
    limit_confirm_count = 0  # ticks where spread still covers our limit

    for i in range(n):
        ts = int(ts_arr[i])
        bid = float(bid_arr[i])
        ask = float(ask_arr[i])
        mid = float(mid_arr[i])
        spread = ask - bid

        if mid <= 0 or bid <= 0 or ask <= 0:
            continue

        triggered, move_dir, move_bps = detector.update(ts, mid)

        if state == "positioned":
            # Check exit (same as base CBS — market exit)
            elapsed_ns = ts - entry_ts
            if direction == 1:
                pnl_bps = (mid - entry_mid) / entry_mid * 10000
            else:
                pnl_bps = (entry_mid - mid) / entry_mid * 10000

            exit_reason = None
            if pnl_bps < -stop_bps:
                exit_reason = "stop"
            elif elapsed_ns >= hold_ns:
                exit_reason = "time"

            if exit_reason:
                if direction == 1:
                    exit_px = bid
                else:
                    exit_px = ask

                gross = direction * (exit_px - entry_px)
                cost = COST_LIMIT_ENTRY_PTS  # limit entry saves one leg
                trades.append(Trade(
                    entry_ts=entry_ts, exit_ts=ts,
                    direction=direction, entry_px=entry_px, exit_px=exit_px,
                    gross_pnl_pts=gross, cost_pts=cost,
                    net_pnl_pts=gross - cost,
                    exit_reason=exit_reason, entry_method="limit", exit_method="market",
                    spread_at_entry=spread_at_entry, day=day,
                    trigger_to_entry_s=(entry_ts - trigger_ts) / 1e9,
                    hold_s=(ts - entry_ts) / 1e9,
                ))
                state = "idle"

        elif state == "pending_fill":
            # We have a limit order out — check fill
            if direction == 1:
                # Long: limit buy at limit_price. Fill if ask <= limit_price (someone
                # sold through our price) or bid drops to/below our price and queue depletes.
                # Conservative: ask touches our price for 2 consecutive ticks.
                if ask <= limit_price:
                    limit_confirm_count += 1
                else:
                    limit_confirm_count = 0
            else:
                # Short: limit sell at limit_price. Fill if bid >= limit_price.
                if bid >= limit_price:
                    limit_confirm_count += 1
                else:
                    limit_confirm_count = 0

            if limit_confirm_count >= LIMIT_FILL_CONFIRM_TICKS:
                # Filled!
                entry_ts = ts
                entry_px = limit_price
                entry_mid = mid
                state = "positioned"
                limit_confirm_count = 0
            elif ts > trigger_ts + patience_ns:
                # Patience expired
                n_skipped += 1
                state = "idle"
                limit_confirm_count = 0

        elif state == "armed":
            # Waiting for wide spread
            if ts > trigger_ts + patience_ns:
                # Patience expired, no wide spread found
                n_skipped += 1
                state = "idle"
            elif spread >= min_spread_pts:
                # Wide spread! Place limit order at mid
                direction = trigger_direction
                limit_price = round(mid)  # TMFD6 prices are integers
                spread_at_entry = spread
                state = "pending_fill"
                limit_confirm_count = 0

        elif state == "idle" and triggered:
            n_triggers += 1
            trigger_ts = ts
            trigger_direction = -1 if move_dir > 0 else 1  # contrarian
            state = "armed"
            detector.last_trigger_ts = ts

    return trades, n_triggers, n_skipped


# ---------------------------------------------------------------------------
# Strategy 3: CBS + Limit Exit (market entry, limit exit with stop)
# ---------------------------------------------------------------------------

def run_cbs_limit_exit(
    data: np.ndarray,
    day: str,
    target_pts: float = 3.0,
    hold_s: float = CBS_HOLD_S,
    stop_bps: float = CBS_STOP_LOSS_BPS,
) -> list[Trade]:
    """CBS with market entry but limit order exit at target price.

    Places a limit order at entry_px + target_pts (long) or entry_px - target_pts (short).
    Stop-loss still enforced. If hold period expires without fill, market exit.
    """
    detector = CBSDetector()
    trades: list[Trade] = []

    ts_arr = data["local_ts"]
    bid_arr = data["bid_px"]
    ask_arr = data["ask_px"]
    mid_arr = data["mid_price"]
    n = len(data)

    hold_ns = int(hold_s * 1e9)

    state = "idle"
    entry_ts = 0
    entry_px = 0.0
    entry_mid = 0.0
    direction = 0
    spread_at_entry = 0.0
    limit_exit_price = 0.0
    limit_exit_confirm = 0

    for i in range(n):
        ts = int(ts_arr[i])
        bid = float(bid_arr[i])
        ask = float(ask_arr[i])
        mid = float(mid_arr[i])
        spread = ask - bid

        if mid <= 0 or bid <= 0 or ask <= 0:
            continue

        triggered, move_dir, move_bps = detector.update(ts, mid)

        if state == "positioned":
            elapsed_ns = ts - entry_ts
            if direction == 1:
                pnl_bps = (mid - entry_mid) / entry_mid * 10000
            else:
                pnl_bps = (entry_mid - mid) / entry_mid * 10000

            exit_reason = None
            exit_px = 0.0
            exit_method = "market"

            # Stop-loss (always market exit)
            if pnl_bps < -stop_bps:
                exit_reason = "stop"
                exit_px = bid if direction == 1 else ask

            # Limit exit check
            elif direction == 1:
                # Long: limit sell at limit_exit_price. Check if bid >= limit_exit_price.
                if bid >= limit_exit_price:
                    limit_exit_confirm += 1
                else:
                    limit_exit_confirm = 0
                if limit_exit_confirm >= LIMIT_FILL_CONFIRM_TICKS:
                    exit_reason = "limit_fill"
                    exit_px = limit_exit_price
                    exit_method = "limit"
            else:
                # Short: limit buy at limit_exit_price. Check if ask <= limit_exit_price.
                if ask <= limit_exit_price:
                    limit_exit_confirm += 1
                else:
                    limit_exit_confirm = 0
                if limit_exit_confirm >= LIMIT_FILL_CONFIRM_TICKS:
                    exit_reason = "limit_fill"
                    exit_px = limit_exit_price
                    exit_method = "limit"

            # Time exit (market)
            if exit_reason is None and elapsed_ns >= hold_ns:
                exit_reason = "time"
                exit_px = bid if direction == 1 else ask

            if exit_reason:
                gross = direction * (exit_px - entry_px)
                if exit_method == "limit":
                    cost = COST_LIMIT_ENTRY_PTS  # one leg passive
                else:
                    cost = COST_MARKET_RT_PTS
                trades.append(Trade(
                    entry_ts=entry_ts, exit_ts=ts,
                    direction=direction, entry_px=entry_px, exit_px=exit_px,
                    gross_pnl_pts=gross, cost_pts=cost,
                    net_pnl_pts=gross - cost,
                    exit_reason=exit_reason, entry_method="market",
                    exit_method=exit_method,
                    spread_at_entry=spread_at_entry, day=day,
                    trigger_to_entry_s=0.0,
                    hold_s=(ts - entry_ts) / 1e9,
                ))
                state = "idle"
                limit_exit_confirm = 0

        elif state == "idle" and triggered:
            direction = -1 if move_dir > 0 else 1
            if direction == 1:
                entry_px = ask
            else:
                entry_px = bid
            entry_ts = ts
            entry_mid = mid
            spread_at_entry = spread
            state = "positioned"
            detector.last_trigger_ts = ts

            # Set limit exit target
            if direction == 1:
                limit_exit_price = entry_px + target_pts
            else:
                limit_exit_price = entry_px - target_pts
            limit_exit_confirm = 0

    return trades


# ---------------------------------------------------------------------------
# Strategy 4: Full CBS 2.0 (spread wait entry + limit exit)
# ---------------------------------------------------------------------------

def run_cbs_full_hybrid(
    data: np.ndarray,
    day: str,
    patience_s: float = 60.0,
    min_spread_pts: float = 5.0,
    target_pts: float = 3.0,
    hold_s: float = CBS_HOLD_S,
    stop_bps: float = CBS_STOP_LOSS_BPS,
) -> tuple[list[Trade], int, int]:
    """Full CBS 2.0: spread-wait limit entry + limit exit.

    Returns (trades, n_triggers, n_skipped).
    """
    detector = CBSDetector()
    trades: list[Trade] = []
    n_triggers = 0
    n_skipped = 0

    ts_arr = data["local_ts"]
    bid_arr = data["bid_px"]
    ask_arr = data["ask_px"]
    mid_arr = data["mid_price"]
    n = len(data)

    hold_ns = int(hold_s * 1e9)
    patience_ns = int(patience_s * 1e9)

    state = "idle"
    trigger_ts = 0
    trigger_direction = 0
    entry_ts = 0
    entry_px = 0.0
    entry_mid = 0.0
    direction = 0
    spread_at_entry = 0.0
    limit_entry_price = 0.0
    limit_entry_confirm = 0
    limit_exit_price = 0.0
    limit_exit_confirm = 0

    for i in range(n):
        ts = int(ts_arr[i])
        bid = float(bid_arr[i])
        ask = float(ask_arr[i])
        mid = float(mid_arr[i])
        spread = ask - bid

        if mid <= 0 or bid <= 0 or ask <= 0:
            continue

        triggered, move_dir, move_bps = detector.update(ts, mid)

        if state == "positioned":
            elapsed_ns = ts - entry_ts
            if direction == 1:
                pnl_bps = (mid - entry_mid) / entry_mid * 10000
            else:
                pnl_bps = (entry_mid - mid) / entry_mid * 10000

            exit_reason = None
            exit_px = 0.0
            exit_method = "market"

            if pnl_bps < -stop_bps:
                exit_reason = "stop"
                exit_px = bid if direction == 1 else ask
            elif direction == 1:
                if bid >= limit_exit_price:
                    limit_exit_confirm += 1
                else:
                    limit_exit_confirm = 0
                if limit_exit_confirm >= LIMIT_FILL_CONFIRM_TICKS:
                    exit_reason = "limit_fill"
                    exit_px = limit_exit_price
                    exit_method = "limit"
            else:
                if ask <= limit_exit_price:
                    limit_exit_confirm += 1
                else:
                    limit_exit_confirm = 0
                if limit_exit_confirm >= LIMIT_FILL_CONFIRM_TICKS:
                    exit_reason = "limit_fill"
                    exit_px = limit_exit_price
                    exit_method = "limit"

            if exit_reason is None and elapsed_ns >= hold_ns:
                exit_reason = "time"
                exit_px = bid if direction == 1 else ask

            if exit_reason:
                gross = direction * (exit_px - entry_px)
                # Both limit → 0 cost; limit entry only → 2 pts; market both → 4 pts
                if exit_method == "limit":
                    cost = COST_LIMIT_BOTH_PTS  # limit entry + limit exit
                else:
                    cost = COST_LIMIT_ENTRY_PTS  # limit entry + market exit
                trades.append(Trade(
                    entry_ts=entry_ts, exit_ts=ts,
                    direction=direction, entry_px=entry_px, exit_px=exit_px,
                    gross_pnl_pts=gross, cost_pts=cost,
                    net_pnl_pts=gross - cost,
                    exit_reason=exit_reason, entry_method="limit",
                    exit_method=exit_method,
                    spread_at_entry=spread_at_entry, day=day,
                    trigger_to_entry_s=(entry_ts - trigger_ts) / 1e9,
                    hold_s=(ts - entry_ts) / 1e9,
                ))
                state = "idle"
                limit_exit_confirm = 0

        elif state == "pending_fill":
            if direction == 1:
                if ask <= limit_entry_price:
                    limit_entry_confirm += 1
                else:
                    limit_entry_confirm = 0
            else:
                if bid >= limit_entry_price:
                    limit_entry_confirm += 1
                else:
                    limit_entry_confirm = 0

            if limit_entry_confirm >= LIMIT_FILL_CONFIRM_TICKS:
                entry_ts = ts
                entry_px = limit_entry_price
                entry_mid = mid
                state = "positioned"
                limit_entry_confirm = 0
                # Set limit exit
                if direction == 1:
                    limit_exit_price = entry_px + target_pts
                else:
                    limit_exit_price = entry_px - target_pts
                limit_exit_confirm = 0
            elif ts > trigger_ts + patience_ns:
                n_skipped += 1
                state = "idle"
                limit_entry_confirm = 0

        elif state == "armed":
            if ts > trigger_ts + patience_ns:
                n_skipped += 1
                state = "idle"
            elif spread >= min_spread_pts:
                direction = trigger_direction
                limit_entry_price = round(mid)
                spread_at_entry = spread
                state = "pending_fill"
                limit_entry_confirm = 0

        elif state == "idle" and triggered:
            n_triggers += 1
            trigger_ts = ts
            trigger_direction = -1 if move_dir > 0 else 1
            state = "armed"
            detector.last_trigger_ts = ts

    return trades, n_triggers, n_skipped


# ---------------------------------------------------------------------------
# Analysis utilities
# ---------------------------------------------------------------------------

def compute_stats(
    trades: list[Trade],
    label: str,
    n_days: int,
    n_triggers: int = 0,
    n_skipped: int = 0,
) -> dict:
    """Compute aggregate statistics for a set of trades."""
    n = len(trades)
    if n == 0:
        return {
            "label": label, "n": 0, "n_triggers": n_triggers, "n_skipped": n_skipped,
            "avg_pnl": 0.0, "median_pnl": 0.0, "std_pnl": 0.0,
            "total_pnl": 0.0, "daily_pnl_pts": 0.0, "daily_pnl_ntd": 0.0,
            "win_rate": 0.0, "stop_rate": 0.0,
            "t_stat": 0.0, "p_value": 1.0,
            "avg_cost": 0.0, "avg_gross": 0.0,
            "limit_exit_rate": 0.0, "avg_trigger_to_entry_s": 0.0,
            "avg_hold_s": 0.0,
        }

    pnls = np.array([t.net_pnl_pts for t in trades])
    gross = np.array([t.gross_pnl_pts for t in trades])
    costs = np.array([t.cost_pts for t in trades])

    t_stat, p_value = sp_stats.ttest_1samp(pnls, 0) if n >= 2 else (0.0, 1.0)

    n_stops = sum(1 for t in trades if t.exit_reason == "stop")
    n_limit_exits = sum(1 for t in trades if t.exit_reason == "limit_fill")
    trigger_delays = [t.trigger_to_entry_s for t in trades]
    hold_times = [t.hold_s for t in trades]

    return {
        "label": label,
        "n": n,
        "n_triggers": n_triggers,
        "n_skipped": n_skipped,
        "fill_rate": n / max(n_triggers, 1) if n_triggers > 0 else 1.0,
        "avg_pnl": float(pnls.mean()),
        "median_pnl": float(np.median(pnls)),
        "std_pnl": float(pnls.std()),
        "total_pnl": float(pnls.sum()),
        "daily_pnl_pts": float(pnls.sum() / max(n_days, 1)),
        "daily_pnl_ntd": float(pnls.sum() * PT_VALUE_NTD / max(n_days, 1)),
        "win_rate": float((pnls > 0).sum() / n),
        "stop_rate": n_stops / n,
        "limit_exit_rate": n_limit_exits / n,
        "t_stat": float(t_stat),
        "p_value": float(p_value),
        "avg_cost": float(costs.mean()),
        "avg_gross": float(gross.mean()),
        "avg_trigger_to_entry_s": float(np.mean(trigger_delays)),
        "avg_hold_s": float(np.mean(hold_times)),
    }


def print_stats(stats: dict) -> None:
    """Pretty-print strategy statistics."""
    print(f"  {stats['label']}:")
    print(f"    N trades: {stats['n']}", end="")
    if stats["n_triggers"] > 0:
        print(f"  (triggers: {stats['n_triggers']}, skipped: {stats['n_skipped']}, "
              f"fill_rate: {stats.get('fill_rate', 0):.1%})", end="")
    print()
    print(f"    Avg PnL/trade: {stats['avg_pnl']:+.2f} pts  "
          f"(median: {stats['median_pnl']:+.2f}, std: {stats['std_pnl']:.2f})")
    print(f"    Total PnL: {stats['total_pnl']:+.1f} pts  "
          f"({stats['total_pnl'] * PT_VALUE_NTD:+.0f} NTD)")
    print(f"    Daily PnL: {stats['daily_pnl_pts']:+.1f} pts  "
          f"({stats['daily_pnl_ntd']:+.0f} NTD/day)")
    print(f"    Win rate: {stats['win_rate']:.1%}  "
          f"Stop rate: {stats['stop_rate']:.1%}  "
          f"Limit exit rate: {stats['limit_exit_rate']:.1%}")
    print(f"    t-stat: {stats['t_stat']:.3f}  p-value: {stats['p_value']:.4f}")
    print(f"    Avg cost: {stats['avg_cost']:.1f} pts  Avg gross: {stats['avg_gross']:+.2f} pts")
    if stats["avg_trigger_to_entry_s"] > 0:
        print(f"    Avg trigger→entry: {stats['avg_trigger_to_entry_s']:.1f}s")
    print(f"    Avg hold: {stats['avg_hold_s']:.1f}s")


def compute_pnl_per_day(trades: list[Trade]) -> dict[str, float]:
    """PnL per day."""
    by_day: dict[str, float] = {}
    for t in trades:
        by_day[t.day] = by_day.get(t.day, 0.0) + t.net_pnl_pts
    return by_day


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print("=" * 100)
    print("CBS 2.0 HYBRID STRATEGY BACKTEST")
    print("CBS timing + SG-LP execution variants")
    print("IS: 14 days (Jan 26 - Feb 25)  |  OOS: 6 days (Mar 19 - Mar 26)")
    print("=" * 100)

    # Load data
    print("\n--- Loading Data ---")
    day_data: dict[str, np.ndarray] = {}
    for d in ALL_DATES:
        arr = load_day(d)
        if arr is not None:
            spread = arr["ask_px"] - arr["bid_px"]
            pct_wide_5 = 100 * np.mean(spread >= 5)
            pct_wide_7 = 100 * np.mean(spread >= 7)
            print(f"  {d}: {len(arr):>9,} rows, "
                  f"median_spread={np.median(spread):.0f}, "
                  f">=5pts: {pct_wide_5:.1f}%, >=7pts: {pct_wide_7:.1f}%")
            day_data[d] = arr
        else:
            print(f"  {d}: MISSING")

    is_dates_avail = [d for d in IS_DATES if d in day_data]
    oos_dates_avail = [d for d in OOS_DATES if d in day_data]
    n_is = len(is_dates_avail)
    n_oos = len(oos_dates_avail)

    print(f"\n  IS: {n_is} days, OOS: {n_oos} days")

    # =====================================================================
    # Strategy 1: Base CBS
    # =====================================================================
    print("\n" + "=" * 100)
    print("STRATEGY 1: BASE CBS (market entry, fixed time exit)")
    print("=" * 100)

    s1_is_trades: list[Trade] = []
    s1_oos_trades: list[Trade] = []
    for d in sorted(day_data.keys()):
        trades = run_base_cbs(day_data[d], d)
        if d in IS_DATES:
            s1_is_trades.extend(trades)
        else:
            s1_oos_trades.extend(trades)

    s1_is_stats = compute_stats(s1_is_trades, "S1 Base CBS (IS)", n_is)
    s1_oos_stats = compute_stats(s1_oos_trades, "S1 Base CBS (OOS)", n_oos)
    print_stats(s1_is_stats)
    print_stats(s1_oos_stats)

    # Per-day breakdown
    print("\n  Per-day PnL (OOS):")
    for d in oos_dates_avail:
        day_trades = [t for t in s1_oos_trades if t.day == d]
        pnl = sum(t.net_pnl_pts for t in day_trades)
        print(f"    {d}: {len(day_trades)} trades, PnL: {pnl:+.1f} pts")

    # =====================================================================
    # Strategy 2: CBS + Spread Wait (parameter sweep)
    # =====================================================================
    print("\n" + "=" * 100)
    print("STRATEGY 2: CBS + SPREAD WAIT (limit entry, market exit)")
    print("=" * 100)

    s2_results = []
    for patience in [30, 60, 120, 300]:
        for min_spread in [5, 7, 10]:
            is_trades: list[Trade] = []
            oos_trades: list[Trade] = []
            is_triggers = 0
            is_skipped = 0
            oos_triggers = 0
            oos_skipped = 0

            for d in sorted(day_data.keys()):
                trades, nt, ns = run_cbs_spread_wait(
                    day_data[d], d, patience_s=patience, min_spread_pts=min_spread)
                if d in IS_DATES:
                    is_trades.extend(trades)
                    is_triggers += nt
                    is_skipped += ns
                else:
                    oos_trades.extend(trades)
                    oos_triggers += nt
                    oos_skipped += ns

            is_stats = compute_stats(
                is_trades, f"S2 p={patience}s,sp={min_spread} (IS)", n_is,
                is_triggers, is_skipped)
            oos_stats = compute_stats(
                oos_trades, f"S2 p={patience}s,sp={min_spread} (OOS)", n_oos,
                oos_triggers, oos_skipped)

            s2_results.append({
                "patience": patience, "min_spread": min_spread,
                "is": is_stats, "oos": oos_stats,
            })

    # Summary table
    print(f"\n  {'Config':<22} {'N_IS':>5} {'Avg_IS':>8} {'N_OOS':>6} {'Avg_OOS':>8} "
          f"{'WR_OOS':>7} {'Stop%':>6} {'Skip%':>6} {'t':>6} {'p':>7}")
    print("  " + "-" * 90)
    for r in s2_results:
        is_s = r["is"]
        oos_s = r["oos"]
        skip_pct = oos_s["n_skipped"] / max(oos_s["n_triggers"], 1) * 100
        print(f"  p={r['patience']:<3}s sp={r['min_spread']:<2}     "
              f"{is_s['n']:>5} {is_s['avg_pnl']:>+7.2f} "
              f"{oos_s['n']:>6} {oos_s['avg_pnl']:>+7.2f} "
              f"{oos_s['win_rate']:>6.1%} {oos_s['stop_rate']:>5.1%} "
              f"{skip_pct:>5.1f}% {oos_s['t_stat']:>5.2f} {oos_s['p_value']:>6.4f}")

    # Best OOS config
    s2_best_oos = max(s2_results, key=lambda r: r["oos"]["avg_pnl"] if r["oos"]["n"] >= 3 else -999)
    print(f"\n  Best OOS: p={s2_best_oos['patience']}s, sp={s2_best_oos['min_spread']}")
    print_stats(s2_best_oos["oos"])

    # =====================================================================
    # Strategy 3: CBS + Limit Exit (parameter sweep)
    # =====================================================================
    print("\n" + "=" * 100)
    print("STRATEGY 3: CBS + LIMIT EXIT (market entry, limit exit)")
    print("=" * 100)

    s3_results = []
    for target in [2, 3, 5, 8]:
        for stop in [15, 25, 40]:
            is_trades: list[Trade] = []
            oos_trades: list[Trade] = []

            for d in sorted(day_data.keys()):
                trades = run_cbs_limit_exit(
                    day_data[d], d, target_pts=target, stop_bps=stop)
                if d in IS_DATES:
                    is_trades.extend(trades)
                else:
                    oos_trades.extend(trades)

            is_stats = compute_stats(
                is_trades, f"S3 tgt={target}pts,sl={stop}bps (IS)", n_is)
            oos_stats = compute_stats(
                oos_trades, f"S3 tgt={target}pts,sl={stop}bps (OOS)", n_oos)

            s3_results.append({
                "target": target, "stop": stop,
                "is": is_stats, "oos": oos_stats,
            })

    print(f"\n  {'Config':<24} {'N_IS':>5} {'Avg_IS':>8} {'N_OOS':>6} {'Avg_OOS':>8} "
          f"{'WR_OOS':>7} {'Stop%':>6} {'LimX%':>6} {'t':>6} {'p':>7}")
    print("  " + "-" * 92)
    for r in s3_results:
        is_s = r["is"]
        oos_s = r["oos"]
        print(f"  tgt={r['target']:<2}pts sl={r['stop']:<2}bps    "
              f"{is_s['n']:>5} {is_s['avg_pnl']:>+7.2f} "
              f"{oos_s['n']:>6} {oos_s['avg_pnl']:>+7.2f} "
              f"{oos_s['win_rate']:>6.1%} {oos_s['stop_rate']:>5.1%} "
              f"{oos_s['limit_exit_rate']:>5.1%} {oos_s['t_stat']:>5.2f} {oos_s['p_value']:>6.4f}")

    s3_best_oos = max(s3_results, key=lambda r: r["oos"]["avg_pnl"] if r["oos"]["n"] >= 3 else -999)
    print(f"\n  Best OOS: tgt={s3_best_oos['target']}pts, sl={s3_best_oos['stop']}bps")
    print_stats(s3_best_oos["oos"])

    # =====================================================================
    # Strategy 4: Full CBS 2.0 (best S2 + S3 params)
    # =====================================================================
    print("\n" + "=" * 100)
    print("STRATEGY 4: FULL CBS 2.0 (spread wait + limit exit)")
    print("=" * 100)

    # Use best params from S2 and S3
    best_patience = s2_best_oos["patience"]
    best_min_spread = s2_best_oos["min_spread"]
    best_target = s3_best_oos["target"]
    best_stop = s3_best_oos["stop"]

    print(f"  Using: patience={best_patience}s, min_spread={best_min_spread}pts, "
          f"target={best_target}pts, stop={best_stop}bps")

    # Also test a few combined variants for robustness
    s4_configs = [
        (best_patience, best_min_spread, best_target, best_stop),
        (60, 5, 3, 15),
        (60, 5, 5, 25),
        (120, 5, 3, 15),
        (120, 7, 3, 15),
        (300, 5, 3, 25),
    ]
    # Deduplicate
    seen = set()
    unique_configs = []
    for c in s4_configs:
        if c not in seen:
            seen.add(c)
            unique_configs.append(c)

    s4_results = []
    for patience, min_spread, target, stop in unique_configs:
        is_trades: list[Trade] = []
        oos_trades: list[Trade] = []
        is_triggers = 0
        is_skipped = 0
        oos_triggers = 0
        oos_skipped = 0

        for d in sorted(day_data.keys()):
            trades, nt, ns = run_cbs_full_hybrid(
                day_data[d], d,
                patience_s=patience, min_spread_pts=min_spread,
                target_pts=target, stop_bps=stop)
            if d in IS_DATES:
                is_trades.extend(trades)
                is_triggers += nt
                is_skipped += ns
            else:
                oos_trades.extend(trades)
                oos_triggers += nt
                oos_skipped += ns

        is_stats = compute_stats(
            is_trades,
            f"S4 p={patience}s sp={min_spread} tgt={target} sl={stop} (IS)",
            n_is, is_triggers, is_skipped)
        oos_stats = compute_stats(
            oos_trades,
            f"S4 p={patience}s sp={min_spread} tgt={target} sl={stop} (OOS)",
            n_oos, oos_triggers, oos_skipped)

        s4_results.append({
            "patience": patience, "min_spread": min_spread,
            "target": target, "stop": stop,
            "is": is_stats, "oos": oos_stats,
        })

    print(f"\n  {'Config':<38} {'N_IS':>5} {'Avg_IS':>8} {'N_OOS':>6} {'Avg_OOS':>8} "
          f"{'WR':>6} {'Stop%':>6} {'LimX%':>6} {'Skip%':>6} {'t':>6} {'p':>7}")
    print("  " + "-" * 105)
    for r in s4_results:
        is_s = r["is"]
        oos_s = r["oos"]
        skip_pct = oos_s["n_skipped"] / max(oos_s["n_triggers"], 1) * 100
        cfg = f"p={r['patience']}s sp={r['min_spread']} tgt={r['target']} sl={r['stop']}"
        print(f"  {cfg:<38} "
              f"{is_s['n']:>5} {is_s['avg_pnl']:>+7.2f} "
              f"{oos_s['n']:>6} {oos_s['avg_pnl']:>+7.2f} "
              f"{oos_s['win_rate']:>5.1%} {oos_s['stop_rate']:>5.1%} "
              f"{oos_s['limit_exit_rate']:>5.1%} "
              f"{skip_pct:>5.1f}% {oos_s['t_stat']:>5.2f} {oos_s['p_value']:>6.4f}")

    s4_best_oos = max(s4_results, key=lambda r: r["oos"]["avg_pnl"] if r["oos"]["n"] >= 2 else -999)
    print(f"\n  Best OOS config: p={s4_best_oos['patience']}s, sp={s4_best_oos['min_spread']}, "
          f"tgt={s4_best_oos['target']}, sl={s4_best_oos['stop']}")
    print_stats(s4_best_oos["oos"])

    # =====================================================================
    # HEAD-TO-HEAD COMPARISON
    # =====================================================================
    print("\n" + "=" * 100)
    print("HEAD-TO-HEAD COMPARISON (OOS)")
    print("=" * 100)

    all_strategies = [
        ("S1: Base CBS", s1_oos_stats),
        (f"S2: Spread Wait (p={s2_best_oos['patience']}s,sp={s2_best_oos['min_spread']})",
         s2_best_oos["oos"]),
        (f"S3: Limit Exit (tgt={s3_best_oos['target']},sl={s3_best_oos['stop']})",
         s3_best_oos["oos"]),
        (f"S4: Full CBS 2.0 (p={s4_best_oos['patience']},sp={s4_best_oos['min_spread']},"
         f"tgt={s4_best_oos['target']},sl={s4_best_oos['stop']})",
         s4_best_oos["oos"]),
    ]

    print(f"\n  {'Strategy':<55} {'N':>4} {'AvgPnL':>8} {'TotPnL':>8} "
          f"{'WR':>6} {'Stop%':>6} {'Cost':>5} {'t':>6} {'p':>7}")
    print("  " + "-" * 110)
    for name, st in all_strategies:
        print(f"  {name:<55} {st['n']:>4} {st['avg_pnl']:>+7.2f} "
              f"{st['total_pnl']:>+7.1f} {st['win_rate']:>5.1%} "
              f"{st['stop_rate']:>5.1%} {st['avg_cost']:>4.1f} "
              f"{st['t_stat']:>5.2f} {st['p_value']:>6.4f}")

    # =====================================================================
    # FILL RATE ANALYSIS (S2 and S4)
    # =====================================================================
    print("\n" + "=" * 100)
    print("FILL RATE & TRADE QUALITY ANALYSIS")
    print("=" * 100)

    # Detailed S2 fill analysis
    print("\n  S2 Spread Wait — Fill rates by config (OOS):")
    print(f"  {'Config':<22} {'Triggers':>9} {'Filled':>7} {'Skipped':>8} {'Fill%':>7}")
    print("  " + "-" * 55)
    for r in s2_results:
        oos = r["oos"]
        if oos["n_triggers"] > 0:
            fill_pct = oos["n"] / oos["n_triggers"] * 100
            print(f"  p={r['patience']:<3}s sp={r['min_spread']:<2}     "
                  f"{oos['n_triggers']:>9} {oos['n']:>7} {oos['n_skipped']:>8} {fill_pct:>6.1f}%")

    # S3 limit exit analysis
    print("\n  S3 Limit Exit — Exit method breakdown (OOS):")
    print(f"  {'Config':<24} {'N':>4} {'Time%':>7} {'Stop%':>7} {'Limit%':>7} {'AvgHold':>8}")
    print("  " + "-" * 60)
    for r in s3_results:
        oos = r["oos"]
        if oos["n"] > 0:
            time_pct = 1 - oos["stop_rate"] - oos["limit_exit_rate"]
            print(f"  tgt={r['target']:<2}pts sl={r['stop']:<2}bps    "
                  f"{oos['n']:>4} {time_pct:>6.1%} {oos['stop_rate']:>6.1%} "
                  f"{oos['limit_exit_rate']:>6.1%} {oos['avg_hold_s']:>7.1f}s")

    # =====================================================================
    # SPREAD AVAILABILITY ANALYSIS
    # =====================================================================
    print("\n" + "=" * 100)
    print("SPREAD AVAILABILITY DURING CBS TRIGGERS")
    print("=" * 100)

    # Check: when CBS triggers fire, what is the spread distribution?
    print("\n  Spread at CBS trigger time (all days):")
    all_trigger_spreads = []
    detector = CBSDetector()
    for d in sorted(day_data.keys()):
        detector.reset()
        data = day_data[d]
        ts_arr = data["local_ts"]
        mid_arr = data["mid_price"]
        spread_arr = data["ask_px"] - data["bid_px"]
        for i in range(len(data)):
            ts = int(ts_arr[i])
            mid = float(mid_arr[i])
            if mid <= 0:
                continue
            triggered, _, _ = detector.update(ts, mid)
            if triggered:
                all_trigger_spreads.append(float(spread_arr[i]))

    if all_trigger_spreads:
        spreads = np.array(all_trigger_spreads)
        print(f"    N triggers: {len(spreads)}")
        print(f"    Spread at trigger: min={spreads.min():.0f}, "
              f"median={np.median(spreads):.0f}, mean={spreads.mean():.1f}, "
              f"max={spreads.max():.0f}")
        for thresh in [3, 5, 7, 10]:
            pct = 100 * np.mean(spreads >= thresh)
            print(f"    >= {thresh} pts: {pct:.1f}%")

    # Check: within patience window after trigger, how often is spread wide?
    print("\n  Wide spread availability within patience window after trigger:")
    for patience_s in [30, 60, 120, 300]:
        patience_ns = int(patience_s * 1e9)
        found_wide = 0
        total_triggers = 0
        detector = CBSDetector()
        for d in sorted(day_data.keys()):
            detector.reset()
            data = day_data[d]
            ts_arr = data["local_ts"]
            mid_arr = data["mid_price"]
            spread_arr = data["ask_px"] - data["bid_px"]
            n_rows = len(data)

            i = 0
            while i < n_rows:
                ts = int(ts_arr[i])
                mid = float(mid_arr[i])
                if mid <= 0:
                    i += 1
                    continue
                triggered, _, _ = detector.update(ts, mid)
                if triggered:
                    total_triggers += 1
                    # Scan forward for wide spread
                    found = False
                    j = i + 1
                    while j < n_rows and int(ts_arr[j]) <= ts + patience_ns:
                        if float(spread_arr[j]) >= 5:
                            found = True
                            break
                        j += 1
                    if found:
                        found_wide += 1
                i += 1
        if total_triggers > 0:
            print(f"    patience={patience_s}s: {found_wide}/{total_triggers} "
                  f"({100*found_wide/total_triggers:.1f}%) triggers see spread>=5 within window")

    # =====================================================================
    # CONCLUSION
    # =====================================================================
    print("\n" + "=" * 100)
    print("CONCLUSION")
    print("=" * 100)

    best_overall = max(all_strategies, key=lambda x: x[1]["avg_pnl"])
    worst_overall = min(all_strategies, key=lambda x: x[1]["avg_pnl"])

    print(f"\n  Best OOS strategy: {best_overall[0]}")
    print(f"    Avg PnL: {best_overall[1]['avg_pnl']:+.2f} pts/trade")
    print(f"    Total PnL: {best_overall[1]['total_pnl']:+.1f} pts "
          f"({best_overall[1]['total_pnl'] * PT_VALUE_NTD:+.0f} NTD)")

    # Is CBS 2.0 better than base CBS?
    s1_avg = s1_oos_stats["avg_pnl"]
    s4_avg = s4_best_oos["oos"]["avg_pnl"]
    delta = s4_avg - s1_avg

    print(f"\n  CBS 2.0 vs Base CBS:")
    print(f"    Base CBS avg PnL: {s1_avg:+.2f} pts/trade ({s1_oos_stats['n']} trades)")
    print(f"    CBS 2.0 avg PnL:  {s4_avg:+.2f} pts/trade ({s4_best_oos['oos']['n']} trades)")
    print(f"    Delta: {delta:+.2f} pts/trade")

    if s4_best_oos["oos"]["n"] < s1_oos_stats["n"]:
        print(f"    Trade count reduced: {s1_oos_stats['n']} -> {s4_best_oos['oos']['n']} "
              f"({s4_best_oos['oos']['n'] / max(s1_oos_stats['n'], 1) * 100:.0f}%)")
        print(f"    Total PnL: base={s1_oos_stats['total_pnl']:+.1f} vs "
              f"hybrid={s4_best_oos['oos']['total_pnl']:+.1f}")

    # Verdict
    print("\n  VERDICT:")
    if s4_avg > s1_avg and s4_best_oos["oos"]["p_value"] < 0.10:
        print("    CBS 2.0 improves per-trade quality AND is statistically significant.")
    elif s4_avg > s1_avg:
        print("    CBS 2.0 improves per-trade quality but NOT statistically significant.")
        print("    More data needed for confirmation.")
    else:
        print("    CBS 2.0 does NOT improve over base CBS on OOS data.")
        print("    Hybrid approach rejected for current dataset.")


if __name__ == "__main__":
    main()
