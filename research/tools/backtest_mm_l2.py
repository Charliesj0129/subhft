#!/usr/bin/env python3
"""L2 queue-aware MM backtest using hftbacktest engine on real TXFD6 data.

Runs 4 strategy variants on each day's L2 data:
  A: Simple Symmetric MM
  B: Quadratic Inventory MM
  C: Hawkes-Propagator MM
  D: Selective Quoting (OFI-guided)

Usage::

    uv run python research/tools/backtest_mm_l2.py
    uv run python research/tools/backtest_mm_l2.py --days 2026-03-23
    uv run python research/tools/backtest_mm_l2.py --strategies A,B
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
from numba import float64, int64, njit
from numba.experimental import jitclass
from structlog import get_logger

logger = get_logger("backtest_mm_l2")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TICK_SIZE = 1.0
POINT_VALUE = 200  # NTD per point for TXFD6
LATENCY_NS = 36_000_000  # 36ms
ELAPSE_NS = 1_000_000  # 1ms
REQUOTE_NS = 100_000_000  # 100ms
ORDER_QTY = 1.0
MAX_POSITION = 5
WARMUP_NS = 60_000_000_000  # 60s warmup to skip pre-market wide spreads

DATA_DIR = Path("research/data/raw/txfd6")
DATA_FILES = {
    "2026-03-19": DATA_DIR / "TXFD6_2026-03-19_l2.hftbt.npz",
    "2026-03-20": DATA_DIR / "TXFD6_2026-03-20_l2.hftbt.npz",
    "2026-03-23": DATA_DIR / "TXFD6_2026-03-23_l2.hftbt.npz",
    "2026-03-24": DATA_DIR / "TXFD6_2026-03-24_l2.hftbt.npz",
}

OUTPUT_PATH = Path("outputs/team_artifacts/alpha-research/stage4_mm_l2_backtest.json")

# ---------------------------------------------------------------------------
# Numba jitclass helpers
# ---------------------------------------------------------------------------
K_PROPAGATOR = 3


@jitclass([
    ("mu", float64),
    ("alpha", float64),
    ("beta", float64),
    ("intensity", float64),
    ("last_ts", int64),
])
class HawkesTracker:
    """Hawkes process intensity tracker."""

    def __init__(self, mu: float, alpha: float, beta: float):
        self.mu = mu
        self.alpha = alpha
        self.beta = beta
        self.intensity = mu
        self.last_ts = 0

    def update(self, current_ts: int, is_event: bool) -> None:
        if self.last_ts == 0:
            self.last_ts = current_ts
            return
        dt_ns = current_ts - self.last_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9
            decay = np.exp(-self.beta * dt_sec)
            self.intensity = self.mu + (self.intensity - self.mu) * decay
            if is_event:
                self.intensity += self.alpha
            self.last_ts = current_ts


@jitclass([
    ("weights", float64[:]),
    ("betas", float64[:]),
    ("components", float64[:]),
    ("last_ts", int64),
    ("total_impact", float64),
])
class PropagatorTracker:
    """Multi-timescale propagator for price impact estimation."""

    def __init__(self) -> None:
        self.weights = np.array([0.5, 0.3, 0.2], dtype=np.float64)
        self.betas = np.array([100.0, 10.0, 1.0], dtype=np.float64)
        self.components = np.zeros(K_PROPAGATOR, dtype=np.float64)
        self.last_ts = 0
        self.total_impact = 0.0

    def update(self, current_ts: int) -> None:
        if self.last_ts == 0:
            self.last_ts = current_ts
            return
        dt_ns = current_ts - self.last_ts
        if dt_ns > 0:
            dt_sec = float(dt_ns) * 1e-9
            for k in range(K_PROPAGATOR):
                self.components[k] *= np.exp(-self.betas[k] * dt_sec)
            self.last_ts = current_ts
            self._recalc()

    def add_event(self, sign: float, qty: float) -> None:
        impact = sign * np.log(1.0 + qty)
        for k in range(K_PROPAGATOR):
            self.components[k] += self.weights[k] * impact
        self._recalc()

    def _recalc(self) -> None:
        s = 0.0
        for k in range(K_PROPAGATOR):
            s += self.components[k]
        self.total_impact = s


# ---------------------------------------------------------------------------
# Strategy functions (@njit)
# ---------------------------------------------------------------------------
# Each returns a results array:
#   [total_pnl, n_buys, n_sells, max_abs_pos, sum_abs_pos, n_samples,
#    max_drawdown, peak_equity, n_requotes]

from hftbacktest import GTC, GTX, LIMIT  # noqa: E402


@njit
def _round_price(px: float, tick: float) -> float:
    return round(px / tick) * tick


@njit
def strategy_a_symmetric(hbt) -> np.ndarray:  # type: ignore[type-arg]
    """Strategy A: Simple Symmetric MM with linear inventory skew."""
    asset_no = 0
    risk_aversion = 0.1
    base_spread_ticks = 2.0
    tick = TICK_SIZE

    next_order_id = 1
    last_quote_ts = np.int64(0)
    bid_oid = np.int64(0)
    ask_oid = np.int64(0)
    warmup_done = False
    start_ts = np.int64(0)

    # Metrics
    peak_equity = 0.0
    max_dd = 0.0
    sum_abs_pos = 0.0
    max_abs_pos = 0.0
    n_samples = np.int64(0)
    n_requotes = np.int64(0)

    while hbt.elapse(ELAPSE_NS) == 0:
        current_ts = hbt.current_timestamp
        if start_ts == 0:
            start_ts = current_ts
        if not warmup_done:
            if current_ts - start_ts < WARMUP_NS:
                continue
            warmup_done = True

        depth = hbt.depth(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask

        if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
            continue

        spread = best_ask - best_bid
        if spread > 50.0 * tick:
            continue

        mid = (best_bid + best_ask) / 2.0
        position = hbt.position(asset_no)

        # Track metrics
        sv = hbt.state_values(asset_no)
        equity = sv.balance + position * mid
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd

        abs_pos = abs(position)
        sum_abs_pos += abs_pos
        if abs_pos > max_abs_pos:
            max_abs_pos = abs_pos
        n_samples += 1

        # Calculate prices
        skew = -risk_aversion * position * tick
        reservation = mid + skew
        half_spread = base_spread_ticks * tick / 2.0

        bid_price = _round_price(reservation - half_spread, tick)
        ask_price = _round_price(reservation + half_spread, tick)

        # Ensure we don't cross
        if bid_price >= ask_price:
            bid_price = mid - tick
            ask_price = mid + tick

        # Position limits
        can_buy = position < MAX_POSITION
        can_sell = position > -MAX_POSITION

        # Requote logic
        if current_ts - last_quote_ts > REQUOTE_NS:
            if bid_oid != 0:
                hbt.cancel(asset_no, bid_oid, False)
                bid_oid = 0
            if ask_oid != 0:
                hbt.cancel(asset_no, ask_oid, False)
                ask_oid = 0

            hbt.clear_inactive_orders(asset_no)

            if can_buy:
                bid_oid = next_order_id
                next_order_id += 1
                hbt.submit_buy_order(
                    asset_no, bid_oid, bid_price, ORDER_QTY, GTC, LIMIT, False
                )

            if can_sell:
                ask_oid = next_order_id
                next_order_id += 1
                hbt.submit_sell_order(
                    asset_no, ask_oid, ask_price, ORDER_QTY, GTC, LIMIT, False
                )

            last_quote_ts = current_ts
            n_requotes += 1

    # Final metrics
    sv = hbt.state_values(asset_no)
    position = hbt.position(asset_no)
    depth = hbt.depth(asset_no)
    mid = (depth.best_bid + depth.best_ask) / 2.0 if depth.best_bid > 0 and depth.best_ask > 0 else 0.0
    final_pnl = sv.balance + position * mid

    results = np.zeros(9, dtype=np.float64)
    results[0] = final_pnl
    results[1] = float(sv.num_trades)  # total fills
    results[2] = position
    results[3] = max_abs_pos
    results[4] = sum_abs_pos / max(n_samples, 1)  # mean abs pos
    results[5] = float(n_samples)
    results[6] = max_dd
    results[7] = sv.balance  # realized component
    results[8] = float(n_requotes)
    return results


@njit
def strategy_b_quadratic(hbt) -> np.ndarray:  # type: ignore[type-arg]
    """Strategy B: Quadratic Inventory MM."""
    asset_no = 0
    gamma = 0.01
    phi = 0.02
    base_spread_ticks = 2.0
    tick = TICK_SIZE

    next_order_id = 1
    last_quote_ts = np.int64(0)
    bid_oid = np.int64(0)
    ask_oid = np.int64(0)
    warmup_done = False
    start_ts = np.int64(0)

    # Volatility estimate (rolling)
    mid_prev = 0.0
    vol_sum_sq = 0.0
    vol_count = np.int64(0)

    peak_equity = 0.0
    max_dd = 0.0
    sum_abs_pos = 0.0
    max_abs_pos = 0.0
    n_samples = np.int64(0)
    n_requotes = np.int64(0)

    while hbt.elapse(ELAPSE_NS) == 0:
        current_ts = hbt.current_timestamp
        if start_ts == 0:
            start_ts = current_ts
        if not warmup_done:
            if current_ts - start_ts < WARMUP_NS:
                continue
            warmup_done = True

        depth = hbt.depth(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
            continue
        spread = best_ask - best_bid
        if spread > 50.0 * tick:
            continue

        mid = (best_bid + best_ask) / 2.0
        position = hbt.position(asset_no)

        # Rolling volatility
        if mid_prev > 0.0:
            ret = (mid - mid_prev) / mid_prev
            vol_sum_sq += ret * ret
            vol_count += 1
        mid_prev = mid
        sigma = np.sqrt(vol_sum_sq / max(vol_count, 1))

        # Metrics
        sv = hbt.state_values(asset_no)
        equity = sv.balance + position * mid
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
        abs_pos = abs(position)
        sum_abs_pos += abs_pos
        if abs_pos > max_abs_pos:
            max_abs_pos = abs_pos
        n_samples += 1

        # Quadratic inventory penalty
        inv_penalty = gamma * position * sigma * mid + phi * position * abs(position) * tick
        reservation = mid - inv_penalty
        half_spread = base_spread_ticks * tick / 2.0

        bid_price = _round_price(reservation - half_spread, tick)
        ask_price = _round_price(reservation + half_spread, tick)

        if bid_price >= ask_price:
            bid_price = mid - tick
            ask_price = mid + tick

        can_buy = position < MAX_POSITION
        can_sell = position > -MAX_POSITION

        if current_ts - last_quote_ts > REQUOTE_NS:
            if bid_oid != 0:
                hbt.cancel(asset_no, bid_oid, False)
                bid_oid = 0
            if ask_oid != 0:
                hbt.cancel(asset_no, ask_oid, False)
                ask_oid = 0
            hbt.clear_inactive_orders(asset_no)

            if can_buy:
                bid_oid = next_order_id
                next_order_id += 1
                hbt.submit_buy_order(
                    asset_no, bid_oid, bid_price, ORDER_QTY, GTC, LIMIT, False
                )
            if can_sell:
                ask_oid = next_order_id
                next_order_id += 1
                hbt.submit_sell_order(
                    asset_no, ask_oid, ask_price, ORDER_QTY, GTC, LIMIT, False
                )
            last_quote_ts = current_ts
            n_requotes += 1

    sv = hbt.state_values(asset_no)
    position = hbt.position(asset_no)
    depth = hbt.depth(asset_no)
    mid = (depth.best_bid + depth.best_ask) / 2.0 if depth.best_bid > 0 and depth.best_ask > 0 else 0.0
    final_pnl = sv.balance + position * mid

    results = np.zeros(9, dtype=np.float64)
    results[0] = final_pnl
    results[1] = float(sv.num_trades)
    results[2] = position
    results[3] = max_abs_pos
    results[4] = sum_abs_pos / max(n_samples, 1)
    results[5] = float(n_samples)
    results[6] = max_dd
    results[7] = sv.balance
    results[8] = float(n_requotes)
    return results


@njit
def strategy_c_hawkes(hbt) -> np.ndarray:  # type: ignore[type-arg]
    """Strategy C: Hawkes-Propagator MM."""
    asset_no = 0
    risk_aversion = 0.1
    base_spread_ticks = 2.0
    hawkes_coeff = 0.5
    prop_skew_coeff = 0.5
    tick = TICK_SIZE

    hawkes = HawkesTracker(1.0, 0.5, 10.0)
    propagator = PropagatorTracker()

    next_order_id = 1
    last_quote_ts = np.int64(0)
    bid_oid = np.int64(0)
    ask_oid = np.int64(0)
    warmup_done = False
    start_ts = np.int64(0)

    peak_equity = 0.0
    max_dd = 0.0
    sum_abs_pos = 0.0
    max_abs_pos = 0.0
    n_samples = np.int64(0)
    n_requotes = np.int64(0)

    while hbt.elapse(ELAPSE_NS) == 0:
        current_ts = hbt.current_timestamp
        if start_ts == 0:
            start_ts = current_ts
        if not warmup_done:
            if current_ts - start_ts < WARMUP_NS:
                continue
            warmup_done = True

        depth = hbt.depth(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
            continue
        spread = best_ask - best_bid
        if spread > 50.0 * tick:
            continue

        mid = (best_bid + best_ask) / 2.0
        position = hbt.position(asset_no)

        # Update signal trackers
        trades = hbt.last_trades(asset_no)
        is_event = len(trades) > 0
        hawkes.update(current_ts, is_event)
        propagator.update(current_ts)
        for i in range(len(trades)):
            trade = trades[i]
            sign = float(trade.ival) if trade.ival != 0 else 1.0
            qty = float(trade.qty)
            propagator.add_event(sign, qty)
        hbt.clear_last_trades(asset_no)

        # Metrics
        sv = hbt.state_values(asset_no)
        equity = sv.balance + position * mid
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
        abs_pos = abs(position)
        sum_abs_pos += abs_pos
        if abs_pos > max_abs_pos:
            max_abs_pos = abs_pos
        n_samples += 1

        # Spread widens with Hawkes intensity
        adj_spread = base_spread_ticks * tick * (1.0 + hawkes_coeff * hawkes.intensity)
        half_spread = adj_spread / 2.0

        # Skew from inventory + propagator
        inv_penalty = risk_aversion * position * tick
        prop_skew = prop_skew_coeff * propagator.total_impact * tick
        reservation = mid - inv_penalty + prop_skew

        bid_price = _round_price(reservation - half_spread, tick)
        ask_price = _round_price(reservation + half_spread, tick)

        if bid_price >= ask_price:
            bid_price = mid - tick
            ask_price = mid + tick

        can_buy = position < MAX_POSITION
        can_sell = position > -MAX_POSITION

        if current_ts - last_quote_ts > REQUOTE_NS:
            if bid_oid != 0:
                hbt.cancel(asset_no, bid_oid, False)
                bid_oid = 0
            if ask_oid != 0:
                hbt.cancel(asset_no, ask_oid, False)
                ask_oid = 0
            hbt.clear_inactive_orders(asset_no)

            if can_buy:
                bid_oid = next_order_id
                next_order_id += 1
                hbt.submit_buy_order(
                    asset_no, bid_oid, bid_price, ORDER_QTY, GTC, LIMIT, False
                )
            if can_sell:
                ask_oid = next_order_id
                next_order_id += 1
                hbt.submit_sell_order(
                    asset_no, ask_oid, ask_price, ORDER_QTY, GTC, LIMIT, False
                )
            last_quote_ts = current_ts
            n_requotes += 1

    sv = hbt.state_values(asset_no)
    position = hbt.position(asset_no)
    depth = hbt.depth(asset_no)
    mid = (depth.best_bid + depth.best_ask) / 2.0 if depth.best_bid > 0 and depth.best_ask > 0 else 0.0
    final_pnl = sv.balance + position * mid

    results = np.zeros(9, dtype=np.float64)
    results[0] = final_pnl
    results[1] = float(sv.num_trades)
    results[2] = position
    results[3] = max_abs_pos
    results[4] = sum_abs_pos / max(n_samples, 1)
    results[5] = float(n_samples)
    results[6] = max_dd
    results[7] = sv.balance
    results[8] = float(n_requotes)
    return results


@njit
def strategy_d_selective(hbt) -> np.ndarray:  # type: ignore[type-arg]
    """Strategy D: Selective Quoting with OFI from depth changes."""
    asset_no = 0
    ofi_threshold = 1.5
    base_spread_ticks = 2.0
    risk_aversion = 0.15
    phi = 0.03
    tick = TICK_SIZE
    max_hold_requotes = 500  # 500 * 100ms = 50 seconds max hold

    next_order_id = 1
    last_quote_ts = np.int64(0)
    bid_oid = np.int64(0)
    ask_oid = np.int64(0)
    warmup_done = False
    start_ts = np.int64(0)

    # OFI tracking
    prev_bid = 0.0
    prev_ask = 0.0
    ofi_accum = 0.0
    ofi_decay = 0.99  # per-elapse EMA decay (slow, since 1ms elapse)

    # Hold timer for position exit (counts requotes, not elapses)
    hold_requotes = np.int64(0)

    peak_equity = 0.0
    max_dd = 0.0
    sum_abs_pos = 0.0
    max_abs_pos = 0.0
    n_samples = np.int64(0)
    n_requotes = np.int64(0)

    while hbt.elapse(ELAPSE_NS) == 0:
        current_ts = hbt.current_timestamp
        if start_ts == 0:
            start_ts = current_ts
        if not warmup_done:
            if current_ts - start_ts < WARMUP_NS:
                continue
            warmup_done = True

        depth = hbt.depth(asset_no)
        best_bid = depth.best_bid
        best_ask = depth.best_ask
        if best_bid <= 0.0 or best_ask <= 0.0 or best_ask <= best_bid:
            continue
        spread = best_ask - best_bid
        if spread > 50.0 * tick:
            continue

        mid = (best_bid + best_ask) / 2.0
        position = hbt.position(asset_no)

        # Compute OFI from depth changes
        if prev_bid > 0.0:
            delta_bid = 0.0
            if best_bid > prev_bid:
                delta_bid = 1.0
            elif best_bid < prev_bid:
                delta_bid = -1.0

            delta_ask = 0.0
            if best_ask < prev_ask:
                delta_ask = -1.0  # ask tightened = bullish
            elif best_ask > prev_ask:
                delta_ask = 1.0  # ask widened = bearish

            ofi_tick = delta_bid + delta_ask
            ofi_accum = ofi_decay * ofi_accum + ofi_tick

        prev_bid = best_bid
        prev_ask = best_ask

        # Metrics
        sv = hbt.state_values(asset_no)
        equity = sv.balance + position * mid
        if equity > peak_equity:
            peak_equity = equity
        dd = peak_equity - equity
        if dd > max_dd:
            max_dd = dd
        abs_pos = abs(position)
        sum_abs_pos += abs_pos
        if abs_pos > max_abs_pos:
            max_abs_pos = abs_pos
        n_samples += 1

        # Only act on requote boundaries
        if current_ts - last_quote_ts < REQUOTE_NS:
            continue

        # Track hold time in requote intervals
        if abs_pos > 0:
            hold_requotes += 1
        else:
            hold_requotes = 0

        # Selective quoting with quadratic inventory decay
        inv_penalty = risk_aversion * position * tick + phi * position * abs(position) * tick
        reservation = mid - inv_penalty
        half_spread = base_spread_ticks * tick / 2.0

        bid_price = _round_price(reservation - half_spread, tick)
        ask_price = _round_price(reservation + half_spread, tick)

        if bid_price >= ask_price:
            bid_price = mid - tick
            ask_price = mid + tick

        # Decision: which sides to quote
        quote_bid = False
        quote_ask = False

        if hold_requotes > max_hold_requotes and abs_pos > 0:
            # Tighten spread on exit side only (do NOT cross)
            if position > 0:
                quote_ask = True
                # Tighten ask toward mid but keep it at or above best_ask
                tight_ask = _round_price(reservation, tick)
                if tight_ask > best_bid:
                    ask_price = tight_ask
            else:
                quote_bid = True
                tight_bid = _round_price(reservation, tick)
                if tight_bid < best_ask:
                    bid_price = tight_bid
        elif ofi_accum > ofi_threshold:
            # Buy pressure: quote bid only (lean into flow)
            if position < MAX_POSITION:
                quote_bid = True
            # Also quote ask to capture spread if inventory allows
            if position > -MAX_POSITION:
                quote_ask = True
        elif ofi_accum < -ofi_threshold:
            # Sell pressure: quote ask only
            if position > -MAX_POSITION:
                quote_ask = True
            if position < MAX_POSITION:
                quote_bid = True
        else:
            # Neutral: two-sided with position limits
            if position < MAX_POSITION:
                quote_bid = True
            if position > -MAX_POSITION:
                quote_ask = True

        if current_ts - last_quote_ts > REQUOTE_NS:
            if bid_oid != 0:
                hbt.cancel(asset_no, bid_oid, False)
                bid_oid = 0
            if ask_oid != 0:
                hbt.cancel(asset_no, ask_oid, False)
                ask_oid = 0
            hbt.clear_inactive_orders(asset_no)

            if quote_bid:
                bid_oid = next_order_id
                next_order_id += 1
                hbt.submit_buy_order(
                    asset_no, bid_oid, bid_price, ORDER_QTY, GTC, LIMIT, False
                )
            if quote_ask:
                ask_oid = next_order_id
                next_order_id += 1
                hbt.submit_sell_order(
                    asset_no, ask_oid, ask_price, ORDER_QTY, GTC, LIMIT, False
                )
            last_quote_ts = current_ts
            n_requotes += 1

    sv = hbt.state_values(asset_no)
    position = hbt.position(asset_no)
    depth = hbt.depth(asset_no)
    mid = (depth.best_bid + depth.best_ask) / 2.0 if depth.best_bid > 0 and depth.best_ask > 0 else 0.0
    final_pnl = sv.balance + position * mid

    results = np.zeros(9, dtype=np.float64)
    results[0] = final_pnl
    results[1] = float(sv.num_trades)
    results[2] = position
    results[3] = max_abs_pos
    results[4] = sum_abs_pos / max(n_samples, 1)
    results[5] = float(n_samples)
    results[6] = max_dd
    results[7] = sv.balance
    results[8] = float(n_requotes)
    return results


# ---------------------------------------------------------------------------
# Engine builder
# ---------------------------------------------------------------------------

from hftbacktest import BacktestAsset, HashMapMarketDepthBacktest  # noqa: E402


def build_backtest(data_path: str) -> HashMapMarketDepthBacktest:
    """Build hftbacktest engine for a single day's L2 data."""
    asset = (
        BacktestAsset()
        .data([data_path])
        .linear_asset(1.0)
        .constant_order_latency(LATENCY_NS, LATENCY_NS)
        .tick_size(TICK_SIZE)
        .no_partial_fill_exchange()
        .risk_adverse_queue_model()
        .trading_value_fee_model(0.0, 0.0)
        .last_trades_capacity(100)
    )
    return HashMapMarketDepthBacktest([asset])


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

STRATEGIES = {
    "A": ("Simple Symmetric MM", strategy_a_symmetric),
    "B": ("Quadratic Inventory MM", strategy_b_quadratic),
    "C": ("Hawkes-Propagator MM", strategy_c_hawkes),
    "D": ("Selective Quoting (OFI)", strategy_d_selective),
}


def parse_results(results: np.ndarray, day: str, strat_key: str) -> dict:
    """Parse the results array into a structured dict."""
    pnl_points = results[0]
    pnl_ntd = pnl_points * POINT_VALUE
    n_fills = int(results[1])
    final_pos = results[2]
    max_abs_pos = results[3]
    mean_abs_pos = results[4]
    n_samples = int(results[5])
    max_dd_points = results[6]
    max_dd_ntd = max_dd_points * POINT_VALUE
    realized_points = results[7]
    n_requotes = int(results[8])

    return {
        "day": day,
        "strategy": strat_key,
        "strategy_name": STRATEGIES[strat_key][0],
        "pnl_points": round(pnl_points, 2),
        "pnl_ntd": round(pnl_ntd, 2),
        "realized_pnl_points": round(realized_points, 2),
        "realized_pnl_ntd": round(realized_points * POINT_VALUE, 2),
        "n_fills": n_fills,
        "final_position": final_pos,
        "max_abs_position": max_abs_pos,
        "mean_abs_position": round(mean_abs_pos, 3),
        "max_drawdown_points": round(max_dd_points, 2),
        "max_drawdown_ntd": round(max_dd_ntd, 2),
        "n_samples": n_samples,
        "n_requotes": n_requotes,
        "fills_per_requote": round(n_fills / max(n_requotes, 1), 4),
    }


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_backtest(
    days: list[str] | None = None,
    strategies: list[str] | None = None,
) -> dict:
    """Run all strategy/day combinations and return results."""
    if days is None:
        days = sorted(DATA_FILES.keys())
    if strategies is None:
        strategies = sorted(STRATEGIES.keys())

    # Validate
    for d in days:
        if d not in DATA_FILES:
            logger.error("unknown_day", day=d, available=list(DATA_FILES.keys()))
            sys.exit(1)
        if not DATA_FILES[d].exists():
            logger.error("data_not_found", day=d, path=str(DATA_FILES[d]))
            sys.exit(1)
    for s in strategies:
        if s not in STRATEGIES:
            logger.error("unknown_strategy", strategy=s, available=list(STRATEGIES.keys()))
            sys.exit(1)

    all_results: list[dict] = []

    for day in days:
        data_path = str(DATA_FILES[day])
        logger.info("day_start", day=day, data=data_path)

        for strat_key in strategies:
            strat_name, strat_fn = STRATEGIES[strat_key]
            logger.info("strategy_start", day=day, strategy=strat_key, name=strat_name)

            hbt = build_backtest(data_path)

            t0 = time.monotonic()
            results = strat_fn(hbt)
            elapsed = time.monotonic() - t0

            parsed = parse_results(results, day, strat_key)
            parsed["elapsed_s"] = round(elapsed, 2)
            all_results.append(parsed)

            logger.info(
                "strategy_done",
                day=day,
                strategy=strat_key,
                pnl_ntd=parsed["pnl_ntd"],
                fills=parsed["n_fills"],
                max_dd_ntd=parsed["max_drawdown_ntd"],
                elapsed_s=parsed["elapsed_s"],
            )

    # Aggregate per strategy
    aggregates = {}
    for strat_key in strategies:
        strat_results = [r for r in all_results if r["strategy"] == strat_key]
        if not strat_results:
            continue
        total_pnl = sum(r["pnl_ntd"] for r in strat_results)
        total_fills = sum(r["n_fills"] for r in strat_results)
        max_dd = max(r["max_drawdown_ntd"] for r in strat_results)
        mean_pnl = total_pnl / len(strat_results)
        pnl_std = float(np.std([r["pnl_ntd"] for r in strat_results]))
        # Daily Sharpe (annualized from daily returns)
        daily_pnls = [r["pnl_ntd"] for r in strat_results]
        sharpe = (np.mean(daily_pnls) / np.std(daily_pnls) * np.sqrt(252.0)) if np.std(daily_pnls) > 0 else 0.0

        aggregates[strat_key] = {
            "strategy_name": STRATEGIES[strat_key][0],
            "n_days": len(strat_results),
            "total_pnl_ntd": round(total_pnl, 2),
            "mean_daily_pnl_ntd": round(mean_pnl, 2),
            "pnl_std_ntd": round(pnl_std, 2),
            "sharpe_annualized": round(float(sharpe), 3),
            "total_fills": total_fills,
            "worst_drawdown_ntd": round(max_dd, 2),
            "mean_fills_per_day": round(total_fills / len(strat_results), 1),
        }

    output = {
        "metadata": {
            "instrument": "TXFD6",
            "tick_size": TICK_SIZE,
            "point_value_ntd": POINT_VALUE,
            "latency_ns": LATENCY_NS,
            "elapse_ns": ELAPSE_NS,
            "requote_ns": REQUOTE_NS,
            "order_qty": ORDER_QTY,
            "max_position": MAX_POSITION,
            "warmup_ns": WARMUP_NS,
            "queue_model": "risk_adverse",
            "fill_model": "no_partial_fill",
            "engine": "hftbacktest",
            "engine_version": "2.4.3",
        },
        "per_day_results": all_results,
        "aggregate": aggregates,
    }

    return output


def print_report(output: dict) -> None:
    """Print formatted comparison table."""
    results = output["per_day_results"]
    aggregates = output["aggregate"]

    print("\n" + "=" * 100)
    print("L2 Queue-Aware MM Backtest Results — TXFD6")
    print("=" * 100)

    # Per-day table
    print(f"\n{'Day':<12} {'Strat':<6} {'PnL (NTD)':>12} {'Fills':>8} {'MaxDD (NTD)':>14} "
          f"{'MaxPos':>8} {'MeanPos':>9} {'Time(s)':>8}")
    print("-" * 100)

    for r in results:
        print(
            f"{r['day']:<12} {r['strategy']:<6} {r['pnl_ntd']:>12,.0f} "
            f"{r['n_fills']:>8d} {r['max_drawdown_ntd']:>14,.0f} "
            f"{r['max_abs_position']:>8.0f} {r['mean_abs_position']:>9.3f} "
            f"{r['elapsed_s']:>8.1f}"
        )

    # Aggregate table
    print("\n" + "=" * 100)
    print("Aggregate Summary")
    print("=" * 100)
    print(f"\n{'Strategy':<30} {'Total PnL':>12} {'Mean Daily':>12} {'Sharpe':>8} "
          f"{'Fills':>8} {'Worst DD':>12}")
    print("-" * 100)

    for key in sorted(aggregates.keys()):
        agg = aggregates[key]
        print(
            f"{agg['strategy_name']:<30} {agg['total_pnl_ntd']:>12,.0f} "
            f"{agg['mean_daily_pnl_ntd']:>12,.0f} {agg['sharpe_annualized']:>8.3f} "
            f"{agg['total_fills']:>8d} {agg['worst_drawdown_ntd']:>12,.0f}"
        )

    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="L2 queue-aware MM backtest on TXFD6 data")
    parser.add_argument(
        "--days",
        default=None,
        help="Comma-separated dates (e.g., 2026-03-23,2026-03-24). Default: all",
    )
    parser.add_argument(
        "--strategies",
        default=None,
        help="Comma-separated strategy keys (e.g., A,B). Default: all (A,B,C,D)",
    )
    parser.add_argument(
        "--output",
        default=str(OUTPUT_PATH),
        help="Output JSON path",
    )
    args = parser.parse_args()

    days = args.days.split(",") if args.days else None
    strategies = args.strategies.split(",") if args.strategies else None

    logger.info(
        "backtest_start",
        days=days or "all",
        strategies=strategies or "all",
    )

    output = run_backtest(days=days, strategies=strategies)

    # Save JSON
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")
    logger.info("results_saved", path=str(out_path))

    print_report(output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
