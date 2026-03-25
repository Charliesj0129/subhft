"""MM P0 Parameter Sweep v2 — corrected latency + improved fill model.

Critical fixes from v1:
  1. LATENCY_TICKS = 1 (was 500, ~500x too high for 3.7 ticks/sec L1 data)
  2. Fill model: mid touches or crosses quote (was strict cross only)
  3. All 4 scenarios (A/B/C/D) + sweep on best + VPIN overlay

Usage:
    uv run python research/tools/backtest_mm_p0_v2.py

Outputs: outputs/team_artifacts/alpha-research/stage4_mm_p0_v2.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger("backtest.mm_p0_v2")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_mm_p0_v2.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # Mini-TAIEX: 1 point = 10 NTD
RT_COST_NTD: int = TICK_SIZE_POINTS * POINT_VALUE_NTD // 2  # 5 NTD per RT

# CORRECTED: 1 tick delay (~125ms median interval, conservative for 36ms RTT)
LATENCY_TICKS: int = 1

SAMPLE_INTERVAL: int = 1000

# VPIN constants
VPIN_BAR_VOLUME_TARGET: int = 500
VPIN_N_BUCKETS: int = 50
VPIN_WARMUP_BARS: int = 60

_EPS: float = 1e-12
_SQRT2: float = math.sqrt(2.0)

# ---------------------------------------------------------------------------
# Sweep grid (150 combinations: 5 x 5 x 3 x 2)
# ---------------------------------------------------------------------------

SWEEP_GRID: dict[str, list[float | int]] = {
    "gamma": [0.001, 0.005, 0.01, 0.02, 0.05],
    "phi": [0.005, 0.01, 0.02, 0.05, 0.1],
    "alpha_weight": [0.0, 0.0005, 0.001],
    "max_pos": [5, 10],
}


# ---------------------------------------------------------------------------
# OFI computation
# ---------------------------------------------------------------------------


def _compute_ofi_l1(
    bid_qty: float, ask_qty: float,
    prev_bid_qty: float, prev_ask_qty: float,
    bid_px: float, ask_px: float,
    prev_bid_px: float, prev_ask_px: float,
) -> float:
    """L1 Order Flow Imbalance with Lee-Ready price-level adjustments."""
    if bid_px > prev_bid_px:
        delta_bid = bid_qty
    elif bid_px < prev_bid_px:
        delta_bid = -prev_bid_qty
    else:
        delta_bid = bid_qty - prev_bid_qty

    if ask_px < prev_ask_px:
        delta_ask = ask_qty
    elif ask_px > prev_ask_px:
        delta_ask = -prev_ask_qty
    else:
        delta_ask = ask_qty - prev_ask_qty

    return delta_bid - delta_ask


# ---------------------------------------------------------------------------
# VPIN components
# ---------------------------------------------------------------------------


class _VolumeBarBuilder:
    __slots__ = (
        "_target", "_acc_vol", "_buy_vol", "_sell_vol",
        "_open", "_high", "_low", "_close",
        "_ts_start", "_ts_end",
        "_prev_bid", "_prev_ask", "_init",
    )

    def __init__(self, target: int = 500) -> None:
        self._target = max(1, target)
        self._acc_vol = 0
        self._buy_vol = 0
        self._sell_vol = 0
        self._open = 0
        self._high = 0
        self._low = 0
        self._close = 0
        self._ts_start = 0
        self._ts_end = 0
        self._prev_bid = 0
        self._prev_ask = 0
        self._init = False

    def update(
        self, mid_x2: int, bid_d: int, ask_d: int, ts: int,
    ) -> tuple[int, int, int, int, int] | None:
        """Returns (open, close, total_vol, buy_vol, sell_vol) or None."""
        price = mid_x2 // 2
        if not self._init:
            self._prev_bid = bid_d
            self._prev_ask = ask_d
            self._init = True
            return None

        delta_bid = abs(bid_d - self._prev_bid)
        delta_ask = abs(ask_d - self._prev_ask)
        churn = delta_bid + delta_ask
        bid_consumed = max(self._prev_bid - bid_d, 0)
        ask_consumed = max(self._prev_ask - ask_d, 0)
        self._prev_bid = bid_d
        self._prev_ask = ask_d

        if churn <= 0:
            return None

        total_consumed = bid_consumed + ask_consumed
        if total_consumed > 0:
            buy_frac = bid_consumed / total_consumed
            buy_vol = int(churn * buy_frac)
            sell_vol = churn - buy_vol
        else:
            buy_vol = churn // 2
            sell_vol = churn - buy_vol

        if self._acc_vol == 0:
            self._open = price
            self._high = price
            self._low = price
            self._ts_start = ts
        else:
            if price > self._high:
                self._high = price
            if price < self._low:
                self._low = price

        self._close = price
        self._ts_end = ts
        self._acc_vol += churn
        self._buy_vol += buy_vol
        self._sell_vol += sell_vol

        if self._acc_vol >= self._target:
            result = (self._open, self._close, self._acc_vol, self._buy_vol, self._sell_vol)
            self._acc_vol = 0
            self._buy_vol = 0
            self._sell_vol = 0
            return result
        return None


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


class _VPINTracker:
    """Lightweight combined VPIN + regime tracker."""

    __slots__ = (
        "_bar_builder", "_sigma_sq_ema", "_sigma_alpha", "_sigma_init",
        "_vpin_ratios", "_vpin_head", "_vpin_count", "_vpin_sum",
        "_n_buckets", "_bars_seen",
        "_ema_vpin", "_ema_alpha", "_ema_init",
        "_thr_elev", "_thr_toxic", "_calibrated",
        "_cal_buf", "_warmup_bars",
        "_regime", "_raw_vpin",
    )

    def __init__(
        self,
        bar_volume_target: int = VPIN_BAR_VOLUME_TARGET,
        n_buckets: int = VPIN_N_BUCKETS,
        warmup_bars: int = VPIN_WARMUP_BARS,
    ) -> None:
        self._bar_builder = _VolumeBarBuilder(target=bar_volume_target)
        self._sigma_sq_ema: float = 0.0
        self._sigma_alpha: float = 0.1
        self._sigma_init: bool = False
        self._vpin_ratios: list[float] = [0.0] * n_buckets
        self._vpin_head: int = 0
        self._vpin_count: int = 0
        self._vpin_sum: float = 0.0
        self._n_buckets: int = n_buckets
        self._bars_seen: int = 0
        self._ema_vpin: float = 0.0
        self._ema_alpha: float = 0.1175
        self._ema_init: bool = False
        self._thr_elev: float = 0.4
        self._thr_toxic: float = 0.7
        self._calibrated: bool = False
        self._cal_buf: list[float] = []
        self._warmup_bars: int = warmup_bars
        self._regime: int = 0  # LOW
        self._raw_vpin: float = 0.0

    def update(self, mid_x2: int, bid_d: int, ask_d: int, ts: int) -> None:
        bar = self._bar_builder.update(mid_x2, bid_d, ask_d, ts)
        if bar is None:
            return
        open_p, close_p, total_vol, buy_vol, sell_vol = bar
        self._bars_seen += 1

        delta_price = float(close_p - open_p)
        dp_sq = delta_price * delta_price
        if not self._sigma_init:
            self._sigma_sq_ema = dp_sq if dp_sq > 0 else 1.0
            self._sigma_init = True
        else:
            self._sigma_sq_ema += self._sigma_alpha * (dp_sq - self._sigma_sq_ema)
        sigma = math.sqrt(max(self._sigma_sq_ema, _EPS))
        z = delta_price / sigma
        buy_frac = _norm_cdf(z)

        if total_vol > 0:
            bv = total_vol * buy_frac
            sv = total_vol - bv
            ratio = abs(bv - sv) / total_vol
        else:
            ratio = 0.0

        if self._vpin_count >= self._n_buckets:
            self._vpin_sum -= self._vpin_ratios[self._vpin_head]
        else:
            self._vpin_count += 1
        self._vpin_ratios[self._vpin_head] = ratio
        self._vpin_sum += ratio
        self._vpin_head = (self._vpin_head + 1) % self._n_buckets
        self._raw_vpin = self._vpin_sum / max(self._vpin_count, 1)

        if not self._ema_init:
            self._ema_vpin = self._raw_vpin
            self._ema_init = True
        else:
            self._ema_vpin += self._ema_alpha * (self._raw_vpin - self._ema_vpin)

        if not self._calibrated:
            self._cal_buf.append(self._raw_vpin)
            if (
                self._bars_seen >= self._warmup_bars
                and self._vpin_count >= self._n_buckets
                and len(self._cal_buf) >= 20
            ):
                s = sorted(self._cal_buf)
                n = len(s)
                p75_idx = 0.75 * (n - 1)
                lo75 = int(p75_idx)
                hi75 = min(lo75 + 1, n - 1)
                p75 = s[lo75] * (1.0 - (p75_idx - lo75)) + s[hi75] * (p75_idx - lo75)
                p95_idx = 0.95 * (n - 1)
                lo95 = int(p95_idx)
                hi95 = min(lo95 + 1, n - 1)
                p95 = s[lo95] * (1.0 - (p95_idx - lo95)) + s[hi95] * (p95_idx - lo95)
                if p75 >= p95:
                    p95 = p75 + 0.05
                if p75 <= 0.0:
                    p75 = 0.01
                self._thr_elev = p75
                self._thr_toxic = p95
                self._calibrated = True
                self._cal_buf = []

        v = self._ema_vpin
        if v >= self._thr_toxic:
            self._regime = 2  # TOXIC
        elif v >= self._thr_elev:
            if self._regime == 2 and v >= self._thr_toxic * 0.95:
                pass
            else:
                self._regime = 1  # ELEVATED
        else:
            if self._regime == 1 and v >= self._thr_elev * 0.95:
                pass
            elif self._regime == 2:
                self._regime = 1
            else:
                self._regime = 0  # LOW

    @property
    def regime(self) -> int:
        return self._regime


# ---------------------------------------------------------------------------
# Precomputed data arrays
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PrecomputedData:
    """Arrays precomputed once, shared across all sweep runs."""
    mid_prices: np.ndarray       # int64, mid price in points
    spreads: np.ndarray          # int64, spread in points
    bid_px: np.ndarray
    ask_px: np.ndarray
    bid_qty: np.ndarray
    ask_qty: np.ndarray
    local_ts: np.ndarray
    n: int
    ofi_raw: np.ndarray          # float64
    realized_vol: np.ndarray     # float64


def precompute(data: np.ndarray) -> PrecomputedData:
    """Precompute data arrays shared across all parameter combinations."""
    logger.info("precomputing_shared_arrays")
    t0 = time.monotonic()

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    local_ts = data["local_ts"]
    n = len(data)

    mid_prices = np.round((bid_px + ask_px) / 2.0).astype(np.int64)
    spreads = np.round(ask_px - bid_px).astype(np.int64)

    # Precompute OFI for each tick
    ofi_raw = np.zeros(n, dtype=np.float64)
    for i in range(1, n):
        ofi_raw[i] = _compute_ofi_l1(
            float(bid_qty[i]), float(ask_qty[i]),
            float(bid_qty[i - 1]), float(ask_qty[i - 1]),
            float(bid_px[i]), float(ask_px[i]),
            float(bid_px[i - 1]), float(ask_px[i - 1]),
        )

    # Precompute realized vol (rolling std over 500 ticks)
    vol_window = 500
    realized_vol = np.ones(n, dtype=np.float64)
    returns = np.diff(mid_prices.astype(np.float64))
    returns = np.concatenate([[0.0], returns])
    for i in range(vol_window, n):
        window = returns[i - vol_window + 1 : i + 1]
        std_val = float(np.std(window))
        realized_vol[i] = max(0.1, std_val)
    if n > vol_window:
        for i in range(1, vol_window):
            if i >= 20:
                window = returns[1 : i + 1]
                std_val = float(np.std(window))
                realized_vol[i] = max(0.1, std_val)

    elapsed = time.monotonic() - t0
    logger.info("precompute_done", elapsed_s=round(elapsed, 2), n_rows=n)

    return PrecomputedData(
        mid_prices=mid_prices,
        spreads=spreads,
        bid_px=bid_px,
        ask_px=ask_px,
        bid_qty=bid_qty,
        ask_qty=ask_qty,
        local_ts=local_ts,
        n=n,
        ofi_raw=ofi_raw,
        realized_vol=realized_vol,
    )


# ---------------------------------------------------------------------------
# Fill model helper — limit-order fill: market crosses through our quote
# ---------------------------------------------------------------------------


def _check_fills(
    best_bid: int,
    best_ask: int,
    a_bid: int,
    a_ask: int,
    position: int,
    max_pos: int,
) -> tuple[bool, bool]:
    """Return (buy_fill, sell_fill) using limit-order cross model.

    Buy fill:  best_ask <= bid_quote (market offer drops to/through our bid)
    Sell fill: best_bid >= ask_quote (market bid rises to/through our ask)

    This is the standard limit-order fill model — our resting order gets
    matched when the opposite side crosses our price level.
    """
    buy_fill = best_ask <= a_bid and position < max_pos
    sell_fill = best_bid >= a_ask and position > -max_pos
    return buy_fill, sell_fill


# ---------------------------------------------------------------------------
# Metrics computation helper
# ---------------------------------------------------------------------------


def _compute_metrics(
    equity_samples: list[int],
    realized_pnl: int,
    position: int,
    final_mid: int,
    peak_equity: int,
    max_drawdown: int,
    n_fills: int,
    n_buys: int,
    n_sells: int,
    n_adverse: int,
    n_adverse_total: int,
    sum_abs_inv: int,
    inv_samples: int,
    n: int,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compute standard metrics from simulation state."""
    scale_pv = POINT_VALUE_NTD * 10000
    final_unrealized = position * final_mid * scale_pv
    final_equity = realized_pnl + final_unrealized
    if final_equity > peak_equity:
        peak_equity = final_equity
    dd = peak_equity - final_equity
    if dd > max_drawdown:
        max_drawdown = dd
    equity_samples.append(final_equity)

    eq_arr = np.array(equity_samples, dtype=np.float64)
    returns = np.diff(eq_arr)
    if len(returns) > 1 and float(returns.std()) > 1e-15:
        samples_per_day = max(1, n // SAMPLE_INTERVAL // 4)
        sharpe = float(returns.mean() / returns.std()) * math.sqrt(252 * samples_per_day)
    else:
        sharpe = 0.0

    adverse_rate = n_adverse / max(n_adverse_total, 1)
    scale = 10000.0
    mean_inv = sum_abs_inv / max(inv_samples, 1)

    result: dict[str, Any] = {
        "sharpe": round(sharpe, 4),
        "total_pnl_ntd": round(final_equity / scale, 2),
        "max_drawdown_ntd": round(max_drawdown / scale, 2),
        "n_fills": n_fills,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "adverse_fill_rate_pct": round(adverse_rate * 100, 2),
        "adverse_fills": n_adverse,
        "adverse_total": n_adverse_total,
        "mean_abs_inventory": round(mean_inv, 3),
        "final_position": position,
    }
    if extra_fields:
        result.update(extra_fields)
    return result


# ---------------------------------------------------------------------------
# Adverse fill tracker helper
# ---------------------------------------------------------------------------


def _eval_adverse(
    pending_fills: list[tuple[int, int, int]],
    i: int,
    mid: int,
    adverse_horizon: int,
) -> tuple[list[tuple[int, int, int]], int, int]:
    """Evaluate pending fills, return (remaining, n_adverse_new, n_total_evaluated)."""
    still: list[tuple[int, int, int]] = []
    n_adv = 0
    n_tot = 0
    for ft, fs, fp in pending_fills:
        elapsed = i - ft
        if elapsed >= adverse_horizon:
            n_tot += 1
            if fs > 0 and (fp - mid) > 0:
                n_adv += 1
            elif fs < 0 and (mid - fp) > 0:
                n_adv += 1
        else:
            still.append((ft, fs, fp))
    return still, n_adv, n_tot


# ---------------------------------------------------------------------------
# Scenario A: Baseline — Symmetric Linear Skew
# ---------------------------------------------------------------------------


def run_scenario_a(
    pc: PrecomputedData,
    gamma: float = 0.01,
    max_pos: int = 5,
) -> dict[str, Any]:
    """Scenario A: symmetric linear inventory skew, no OFI, no VPIN."""
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    scale_pv = POINT_VALUE_NTD * 10000
    half_rt = RT_COST_NTD * 10000 // 2

    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0
    equity_samples: list[int] = []
    pending_fills: list[tuple[int, int, int]] = []
    n_adverse: int = 0
    n_adverse_total: int = 0

    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)


    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])
        if mid <= 0 or spread < 0:
            continue

        # Linear skew: reservation = mid - gamma * position
        reservation = mid - gamma * position
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)
        bid_q = res_int - half_spread
        ask_q = res_int + half_spread
        buf_bid[i] = bid_q
        buf_ask[i] = ask_q

        # Activate delayed quotes
        if i >= LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
        else:
            a_bid = 0
            a_ask = 999999

        # Fill simulation (touch-or-cross)
        cur_bid = int(round(pc.bid_px[i]))
        cur_ask = int(round(pc.ask_px[i]))
        buy_fill, sell_fill = _check_fills(cur_bid, cur_ask, a_bid, a_ask, position, max_pos)
        if buy_fill:
            realized_pnl -= a_bid * scale_pv
            position += 1
            n_fills += 1
            n_buys += 1
            realized_pnl -= half_rt
            pending_fills.append((i, 1, mid))
        if sell_fill:
            realized_pnl += a_ask * scale_pv
            position -= 1
            n_fills += 1
            n_sells += 1
            realized_pnl -= half_rt
            pending_fills.append((i, -1, mid))

        # Adverse fill evaluation
        if pending_fills:
            pending_fills, adv_new, adv_tot = _eval_adverse(pending_fills, i, mid, 150)
            n_adverse += adv_new
            n_adverse_total += adv_tot

        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    return _compute_metrics(
        equity_samples, realized_pnl, position, int(mid_prices[-1]),
        peak_equity, max_drawdown, n_fills, n_buys, n_sells,
        n_adverse, n_adverse_total, sum_abs_inv, inv_samples, n,
        extra_fields={"scenario": "A_baseline", "gamma": gamma, "max_pos": max_pos},
    )


# ---------------------------------------------------------------------------
# Scenario B: Quadratic Inventory Penalty
# ---------------------------------------------------------------------------


def run_scenario_b(
    pc: PrecomputedData,
    gamma: float,
    phi: float,
    alpha_weight: float,
    max_pos: int = 5,
) -> dict[str, Any]:
    """Scenario B: quadratic inventory penalty + OFI alpha."""
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    ofi_raw_arr = pc.ofi_raw
    realized_vol_arr = pc.realized_vol
    scale_pv = POINT_VALUE_NTD * 10000
    half_rt = RT_COST_NTD * 10000 // 2

    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0
    equity_samples: list[int] = []
    pending_fills: list[tuple[int, int, int]] = []
    n_adverse: int = 0
    n_adverse_total: int = 0
    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)


    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])
        if mid <= 0 or spread < 0:
            continue

        ofi_ema += ofi_ema_alpha * (ofi_raw_arr[i] - ofi_ema)
        rv = realized_vol_arr[i]

        alpha_adj = ofi_ema * alpha_weight
        inv_penalty = gamma * position * rv + phi * position * abs(position)
        reservation = mid + alpha_adj - inv_penalty
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)
        bid_q = res_int - half_spread
        ask_q = res_int + half_spread
        buf_bid[i] = bid_q
        buf_ask[i] = ask_q

        if i >= LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
        else:
            a_bid = 0
            a_ask = 999999

        cur_bid = int(round(pc.bid_px[i]))
        cur_ask = int(round(pc.ask_px[i]))
        buy_fill, sell_fill = _check_fills(cur_bid, cur_ask, a_bid, a_ask, position, max_pos)
        if buy_fill:
            realized_pnl -= a_bid * scale_pv
            position += 1
            n_fills += 1
            n_buys += 1
            realized_pnl -= half_rt
            pending_fills.append((i, 1, mid))
        if sell_fill:
            realized_pnl += a_ask * scale_pv
            position -= 1
            n_fills += 1
            n_sells += 1
            realized_pnl -= half_rt
            pending_fills.append((i, -1, mid))

        if pending_fills:
            pending_fills, adv_new, adv_tot = _eval_adverse(pending_fills, i, mid, 150)
            n_adverse += adv_new
            n_adverse_total += adv_tot

        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    return _compute_metrics(
        equity_samples, realized_pnl, position, int(mid_prices[-1]),
        peak_equity, max_drawdown, n_fills, n_buys, n_sells,
        n_adverse, n_adverse_total, sum_abs_inv, inv_samples, n,
        extra_fields={
            "scenario": "B_quadratic",
            "gamma": gamma, "phi": phi,
            "alpha_weight": alpha_weight, "max_pos": max_pos,
        },
    )


# ---------------------------------------------------------------------------
# Scenario C: Quadratic + VPIN Spread-Only Overlay
# ---------------------------------------------------------------------------


def run_scenario_c(
    pc: PrecomputedData,
    gamma: float,
    phi: float,
    alpha_weight: float,
    max_pos: int = 5,
) -> dict[str, Any]:
    """Scenario C: quadratic + VPIN spread widening (no position caps from VPIN)."""
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    ofi_raw_arr = pc.ofi_raw
    realized_vol_arr = pc.realized_vol
    scale_pv = POINT_VALUE_NTD * 10000
    half_rt = RT_COST_NTD * 10000 // 2

    # Gentle VPIN regime multipliers (spread only)
    vpin_spread_mult = {0: 1.0, 1: 1.2, 2: 1.8}

    vpin = _VPINTracker()
    regime_buffer: list[int] = []

    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0
    equity_samples: list[int] = []
    pending_fills: list[tuple[int, int, int]] = []
    n_adverse: int = 0
    n_adverse_total: int = 0
    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)


    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])
        if mid <= 0 or spread < 0:
            continue

        # VPIN update
        mid_x2 = int(round(pc.bid_px[i])) + int(round(pc.ask_px[i]))
        vpin.update(mid_x2, int(pc.bid_qty[i]), int(pc.ask_qty[i]), int(pc.local_ts[i]))
        regime_buffer.append(vpin.regime)

        if len(regime_buffer) > LATENCY_TICKS:
            current_regime = regime_buffer[-LATENCY_TICKS - 1]
        else:
            current_regime = 0

        ofi_ema += ofi_ema_alpha * (ofi_raw_arr[i] - ofi_ema)
        rv = realized_vol_arr[i]

        alpha_adj = ofi_ema * alpha_weight
        inv_penalty = gamma * position * rv + phi * position * abs(position)
        reservation = mid + alpha_adj - inv_penalty
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)

        regime_mult = vpin_spread_mult.get(current_regime, 1.0)
        adjusted_half = max(1, int(round(half_spread * regime_mult)))

        bid_q = res_int - adjusted_half
        ask_q = res_int + adjusted_half
        buf_bid[i] = bid_q
        buf_ask[i] = ask_q

        if i >= LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
        else:
            a_bid = 0
            a_ask = 999999

        cur_bid = int(round(pc.bid_px[i]))
        cur_ask = int(round(pc.ask_px[i]))
        buy_fill, sell_fill = _check_fills(cur_bid, cur_ask, a_bid, a_ask, position, max_pos)
        if buy_fill:
            realized_pnl -= a_bid * scale_pv
            position += 1
            n_fills += 1
            n_buys += 1
            realized_pnl -= half_rt
            pending_fills.append((i, 1, mid))
        if sell_fill:
            realized_pnl += a_ask * scale_pv
            position -= 1
            n_fills += 1
            n_sells += 1
            realized_pnl -= half_rt
            pending_fills.append((i, -1, mid))

        if pending_fills:
            pending_fills, adv_new, adv_tot = _eval_adverse(pending_fills, i, mid, 150)
            n_adverse += adv_new
            n_adverse_total += adv_tot

        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    return _compute_metrics(
        equity_samples, realized_pnl, position, int(mid_prices[-1]),
        peak_equity, max_drawdown, n_fills, n_buys, n_sells,
        n_adverse, n_adverse_total, sum_abs_inv, inv_samples, n,
        extra_fields={
            "scenario": "C_quad_vpin",
            "gamma": gamma, "phi": phi,
            "alpha_weight": alpha_weight, "max_pos": max_pos,
            "vpin_spread_mult": vpin_spread_mult,
        },
    )


# ---------------------------------------------------------------------------
# Scenario D: Full P0 — Quad + VPIN + OFI spike + Adverse fill tracker
# ---------------------------------------------------------------------------


def run_scenario_d(
    pc: PrecomputedData,
    gamma: float,
    phi: float,
    alpha_weight: float,
    max_pos: int = 5,
) -> dict[str, Any]:
    """Scenario D: quadratic + VPIN spread + OFI spike guard + adverse fill tracker.

    OFI spike guard: widen spread when |OFI_EMA| > 2 * std(OFI_EMA)
    Adverse fill tracker: track recent adverse fill rate, widen if > 60%
    """
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    ofi_raw_arr = pc.ofi_raw
    realized_vol_arr = pc.realized_vol
    scale_pv = POINT_VALUE_NTD * 10000
    half_rt = RT_COST_NTD * 10000 // 2

    vpin_spread_mult = {0: 1.0, 1: 1.2, 2: 1.8}
    vpin = _VPINTracker()
    regime_buffer: list[int] = []

    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0
    equity_samples: list[int] = []
    pending_fills: list[tuple[int, int, int]] = []
    n_adverse: int = 0
    n_adverse_total: int = 0
    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    # OFI spike detector state
    ofi_ema_sq: float = 0.0  # EMA of OFI^2 for variance
    ofi_spike_mult: float = 1.0

    # Rolling adverse fill rate (last 100 fills)
    recent_adverse: list[bool] = []
    adverse_spread_mult: float = 1.0

    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)


    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])
        if mid <= 0 or spread < 0:
            continue

        # VPIN update
        mid_x2 = int(round(pc.bid_px[i])) + int(round(pc.ask_px[i]))
        vpin.update(mid_x2, int(pc.bid_qty[i]), int(pc.ask_qty[i]), int(pc.local_ts[i]))
        regime_buffer.append(vpin.regime)

        if len(regime_buffer) > LATENCY_TICKS:
            current_regime = regime_buffer[-LATENCY_TICKS - 1]
        else:
            current_regime = 0

        ofi_ema += ofi_ema_alpha * (ofi_raw_arr[i] - ofi_ema)
        rv = realized_vol_arr[i]

        # OFI spike detection
        ofi_sq = ofi_ema * ofi_ema
        ofi_ema_sq += ofi_ema_alpha * (ofi_sq - ofi_ema_sq)
        ofi_std = math.sqrt(max(ofi_ema_sq - ofi_ema * ofi_ema, _EPS))
        if abs(ofi_ema) > 2.0 * ofi_std and ofi_std > 0.1:
            ofi_spike_mult = 1.5
        else:
            ofi_spike_mult = 1.0

        # Adverse fill rate tracker
        if len(recent_adverse) >= 20:
            adv_rate = sum(recent_adverse[-100:]) / len(recent_adverse[-100:])
            adverse_spread_mult = 1.3 if adv_rate > 0.6 else 1.0
        else:
            adverse_spread_mult = 1.0

        alpha_adj = ofi_ema * alpha_weight
        inv_penalty = gamma * position * rv + phi * position * abs(position)
        reservation = mid + alpha_adj - inv_penalty
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)

        regime_mult = vpin_spread_mult.get(current_regime, 1.0)
        total_mult = regime_mult * ofi_spike_mult * adverse_spread_mult
        adjusted_half = max(1, int(round(half_spread * total_mult)))

        bid_q = res_int - adjusted_half
        ask_q = res_int + adjusted_half
        buf_bid[i] = bid_q
        buf_ask[i] = ask_q

        if i >= LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
        else:
            a_bid = 0
            a_ask = 999999

        cur_bid = int(round(pc.bid_px[i]))
        cur_ask = int(round(pc.ask_px[i]))
        buy_fill, sell_fill = _check_fills(cur_bid, cur_ask, a_bid, a_ask, position, max_pos)
        if buy_fill:
            realized_pnl -= a_bid * scale_pv
            position += 1
            n_fills += 1
            n_buys += 1
            realized_pnl -= half_rt
            pending_fills.append((i, 1, mid))
        if sell_fill:
            realized_pnl += a_ask * scale_pv
            position -= 1
            n_fills += 1
            n_sells += 1
            realized_pnl -= half_rt
            pending_fills.append((i, -1, mid))

        if pending_fills:
            still: list[tuple[int, int, int]] = []
            for ft, fs, fp in pending_fills:
                elapsed = i - ft
                if elapsed >= 150:
                    n_adverse_total += 1
                    is_adverse = False
                    if fs > 0 and (fp - mid) > 0:
                        n_adverse += 1
                        is_adverse = True
                    elif fs < 0 and (mid - fp) > 0:
                        n_adverse += 1
                        is_adverse = True
                    recent_adverse.append(is_adverse)
                else:
                    still.append((ft, fs, fp))
            pending_fills = still

        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    return _compute_metrics(
        equity_samples, realized_pnl, position, int(mid_prices[-1]),
        peak_equity, max_drawdown, n_fills, n_buys, n_sells,
        n_adverse, n_adverse_total, sum_abs_inv, inv_samples, n,
        extra_fields={
            "scenario": "D_full_p0",
            "gamma": gamma, "phi": phi,
            "alpha_weight": alpha_weight, "max_pos": max_pos,
            "vpin_spread_mult": vpin_spread_mult,
            "ofi_spike_guard": True,
            "adverse_fill_tracker": True,
        },
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    data_path = _DEFAULT_DATA
    if not data_path.exists():
        logger.error("data_not_found", path=str(data_path))
        sys.exit(1)

    logger.info("loading_data", path=str(data_path))
    data = np.load(str(data_path), allow_pickle=True)
    logger.info("data_loaded", rows=len(data), fields=list(data.dtype.names or []))

    # Quick data stats
    ts = data["local_ts"]
    if len(ts) > 1:
        diffs = np.diff(ts.astype(np.float64))
        diffs_pos = diffs[diffs > 0]
        if len(diffs_pos) > 0:
            median_ms = float(np.median(diffs_pos)) / 1e6
            mean_ms = float(np.mean(diffs_pos)) / 1e6
            logger.info(
                "tick_interval_stats",
                median_ms=round(median_ms, 1),
                mean_ms=round(mean_ms, 1),
                ticks_per_sec=round(1000.0 / median_ms, 2) if median_ms > 0 else 0,
            )

    pc = precompute(data)

    # -----------------------------------------------------------------------
    # Phase 1: Run all 4 scenarios with default params
    # -----------------------------------------------------------------------
    default_gamma = 0.01
    default_phi = 0.02
    default_alpha = 0.0005
    default_max_pos = 5

    logger.info("running_scenario_comparison", latency_ticks=LATENCY_TICKS)
    t0 = time.monotonic()

    scenario_a = run_scenario_a(pc, gamma=default_gamma, max_pos=default_max_pos)
    logger.info("scenario_a_done", sharpe=scenario_a["sharpe"], fills=scenario_a["n_fills"])

    scenario_b = run_scenario_b(
        pc, gamma=default_gamma, phi=default_phi,
        alpha_weight=default_alpha, max_pos=default_max_pos,
    )
    logger.info("scenario_b_done", sharpe=scenario_b["sharpe"], fills=scenario_b["n_fills"])

    scenario_c = run_scenario_c(
        pc, gamma=default_gamma, phi=default_phi,
        alpha_weight=default_alpha, max_pos=default_max_pos,
    )
    logger.info("scenario_c_done", sharpe=scenario_c["sharpe"], fills=scenario_c["n_fills"])

    scenario_d = run_scenario_d(
        pc, gamma=default_gamma, phi=default_phi,
        alpha_weight=default_alpha, max_pos=default_max_pos,
    )
    logger.info("scenario_d_done", sharpe=scenario_d["sharpe"], fills=scenario_d["n_fills"])

    scenarios = [scenario_a, scenario_b, scenario_c, scenario_d]
    scenario_elapsed = time.monotonic() - t0

    # Print scenario comparison table
    print("\n" + "=" * 130)
    print("SCENARIO COMPARISON (corrected: LATENCY_TICKS=1, touch-or-cross fill model)")
    print("=" * 130)
    header = (
        f"{'Scenario':<20}  {'Sharpe':>8}  {'PnL(NTD)':>12}  {'MaxDD(NTD)':>12}  "
        f"{'Fills':>7}  {'Buys':>6}  {'Sells':>6}  {'Adv%':>6}  {'MeanInv':>8}  {'FinalPos':>8}"
    )
    print(header)
    print("-" * 130)
    for s in scenarios:
        line = (
            f"{s['scenario']:<20}  {s['sharpe']:>8.2f}  "
            f"{s['total_pnl_ntd']:>12.2f}  {s['max_drawdown_ntd']:>12.2f}  "
            f"{s['n_fills']:>7}  {s['n_buys']:>6}  {s['n_sells']:>6}  "
            f"{s['adverse_fill_rate_pct']:>5.1f}%  "
            f"{s['mean_abs_inventory']:>8.3f}  {s['final_position']:>8}"
        )
        print(line)
    print(f"\nScenario comparison elapsed: {scenario_elapsed:.1f}s")
    print("=" * 130)

    # -----------------------------------------------------------------------
    # Phase 2: Parameter sweep on best scenario (B — quadratic)
    # -----------------------------------------------------------------------
    gammas = SWEEP_GRID["gamma"]
    phis = SWEEP_GRID["phi"]
    alphas = SWEEP_GRID["alpha_weight"]
    max_positions = [int(x) for x in SWEEP_GRID["max_pos"]]
    total_combos = len(gammas) * len(phis) * len(alphas) * len(max_positions)
    logger.info("sweep_starting", total_combinations=total_combos)

    sweep_results: list[dict[str, Any]] = []
    combo_idx = 0
    t_sweep_start = time.monotonic()

    for gamma in gammas:
        for phi_val in phis:
            for aw in alphas:
                for mp in max_positions:
                    combo_idx += 1
                    if combo_idx % 30 == 0 or combo_idx == 1:
                        elapsed = time.monotonic() - t_sweep_start
                        rate = combo_idx / max(elapsed, 0.01)
                        eta = (total_combos - combo_idx) / max(rate, 0.01)
                        logger.info(
                            "sweep_progress",
                            combo=combo_idx,
                            total=total_combos,
                            elapsed_s=round(elapsed, 1),
                            eta_s=round(eta, 1),
                        )

                    result = run_scenario_b(pc, gamma, phi_val, aw, max_pos=mp)
                    sweep_results.append(result)

    sweep_elapsed = time.monotonic() - t_sweep_start
    logger.info(
        "sweep_complete",
        n_results=len(sweep_results),
        elapsed_s=round(sweep_elapsed, 2),
    )

    # Sort by Sharpe desc, then n_fills desc
    sweep_results.sort(key=lambda r: (-r["sharpe"], -r["n_fills"]))

    # -----------------------------------------------------------------------
    # Phase 3: VPIN overlay on best quadratic params
    # -----------------------------------------------------------------------
    best = sweep_results[0] if sweep_results else None
    vpin_result = None
    if best is not None:
        logger.info(
            "vpin_overlay_test",
            best_gamma=best["gamma"],
            best_phi=best["phi"],
            best_alpha=best["alpha_weight"],
            best_max_pos=best["max_pos"],
            best_sharpe=best["sharpe"],
        )
        vpin_result = run_scenario_c(
            pc,
            gamma=best["gamma"],
            phi=best["phi"],
            alpha_weight=best["alpha_weight"],
            max_pos=best["max_pos"],
        )
        logger.info(
            "vpin_overlay_done",
            sharpe=vpin_result["sharpe"],
            n_fills=vpin_result["n_fills"],
            pnl=vpin_result["total_pnl_ntd"],
        )

    # -----------------------------------------------------------------------
    # Output
    # -----------------------------------------------------------------------
    output: dict[str, Any] = {
        "version": "v2",
        "fixes": [
            "LATENCY_TICKS=1 (was 500, ~500x too high)",
            "Fill model: touch-or-cross (was strict cross only)",
        ],
        "latency_ticks": LATENCY_TICKS,
        "fill_model": "touch_or_cross",
        "scenario_comparison": scenarios,
        "sweep_grid": SWEEP_GRID,
        "total_combinations": total_combos,
        "results_evaluated": len(sweep_results),
        "top_20": sweep_results[:20],
        "vpin_overlay_on_best": vpin_result,
        "best_vs_vpin_comparison": None,
    }

    if best is not None and vpin_result is not None:
        output["best_vs_vpin_comparison"] = {
            "quadratic_only": {
                "sharpe": best["sharpe"],
                "n_fills": best["n_fills"],
                "pnl": best["total_pnl_ntd"],
                "max_dd": best["max_drawdown_ntd"],
                "adverse_rate": best["adverse_fill_rate_pct"],
                "params": {
                    "gamma": best["gamma"],
                    "phi": best["phi"],
                    "alpha_weight": best["alpha_weight"],
                    "max_pos": best["max_pos"],
                },
            },
            "quadratic_plus_vpin_spread": {
                "sharpe": vpin_result["sharpe"],
                "n_fills": vpin_result["n_fills"],
                "pnl": vpin_result["total_pnl_ntd"],
                "max_dd": vpin_result["max_drawdown_ntd"],
                "adverse_rate": vpin_result["adverse_fill_rate_pct"],
            },
        }

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info("output_written", path=str(_OUT_PATH))

    # -----------------------------------------------------------------------
    # Print top 10 sweep results
    # -----------------------------------------------------------------------
    print("\n" + "=" * 140)
    print("TOP 10 PARAMETER COMBINATIONS (Scenario B — Quadratic Inventory, sweep)")
    print("=" * 140)
    header = (
        f"{'Rank':>4}  {'gamma':>7}  {'phi':>7}  {'alpha_w':>8}  {'max_pos':>7}  "
        f"{'Sharpe':>8}  {'PnL(NTD)':>12}  {'MaxDD(NTD)':>12}  "
        f"{'Fills':>7}  {'Adv%':>6}  {'MeanInv':>8}  {'FinalPos':>8}"
    )
    print(header)
    print("-" * 140)
    for rank, r in enumerate(sweep_results[:10], 1):
        line = (
            f"{rank:>4}  {r['gamma']:>7.4f}  {r['phi']:>7.4f}  {r['alpha_weight']:>8.4f}  "
            f"{r['max_pos']:>7}  "
            f"{r['sharpe']:>8.2f}  {r['total_pnl_ntd']:>12.2f}  {r['max_drawdown_ntd']:>12.2f}  "
            f"{r['n_fills']:>7}  {r['adverse_fill_rate_pct']:>5.1f}%  "
            f"{r['mean_abs_inventory']:>8.3f}  {r['final_position']:>8}"
        )
        print(line)
    print("=" * 140)

    # -----------------------------------------------------------------------
    # Print VPIN overlay comparison
    # -----------------------------------------------------------------------
    if best is not None and vpin_result is not None:
        print("\n" + "=" * 100)
        print("VPIN SPREAD-ONLY OVERLAY COMPARISON")
        print(f"Best params: gamma={best['gamma']}, phi={best['phi']}, "
              f"alpha_weight={best['alpha_weight']}, max_pos={best['max_pos']}")
        print("=" * 100)
        comp_header = (
            f"{'Variant':<30}  {'Sharpe':>8}  {'PnL(NTD)':>12}  "
            f"{'MaxDD(NTD)':>12}  {'Fills':>7}  {'Adv%':>6}"
        )
        print(comp_header)
        print("-" * 100)
        print(
            f"{'Quadratic only':<30}  {best['sharpe']:>8.2f}  "
            f"{best['total_pnl_ntd']:>12.2f}  {best['max_drawdown_ntd']:>12.2f}  "
            f"{best['n_fills']:>7}  {best['adverse_fill_rate_pct']:>5.1f}%"
        )
        print(
            f"{'Quad + VPIN spread (gentle)':<30}  {vpin_result['sharpe']:>8.2f}  "
            f"{vpin_result['total_pnl_ntd']:>12.2f}  {vpin_result['max_drawdown_ntd']:>12.2f}  "
            f"{vpin_result['n_fills']:>7}  {vpin_result['adverse_fill_rate_pct']:>5.1f}%"
        )
        print("=" * 100)

        better_sharpe = vpin_result["sharpe"] > best["sharpe"]
        better_adverse = vpin_result["adverse_fill_rate_pct"] < best["adverse_fill_rate_pct"]
        print(f"\nVPIN overlay Sharpe {'BETTER' if better_sharpe else 'WORSE'} "
              f"({vpin_result['sharpe']:.2f} vs {best['sharpe']:.2f})")
        print(f"VPIN overlay adverse rate {'BETTER' if better_adverse else 'WORSE'} "
              f"({vpin_result['adverse_fill_rate_pct']:.1f}% vs {best['adverse_fill_rate_pct']:.1f}%)")

    # -----------------------------------------------------------------------
    # Goal check
    # -----------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("GOAL CHECK (best sweep result)")
    print("=" * 80)
    if best:
        sharpe_ok = best["sharpe"] > 0
        fills_ok = best["n_fills"] > 4000
        dd_ok = best["max_drawdown_ntd"] < 100000
        adv_ok = best["adverse_fill_rate_pct"] < 55
        print(f"  Sharpe > 0:           {'PASS' if sharpe_ok else 'FAIL'} ({best['sharpe']:.2f})")
        print(f"  Fills > 4000:         {'PASS' if fills_ok else 'FAIL'} ({best['n_fills']})")
        print(f"  Max DD < 100k NTD:    {'PASS' if dd_ok else 'FAIL'} ({best['max_drawdown_ntd']:.0f})")
        print(f"  Adverse rate < 55%:   {'PASS' if adv_ok else 'FAIL'} ({best['adverse_fill_rate_pct']:.1f}%)")
    print("=" * 80)

    total_elapsed = time.monotonic() - t0
    logger.info("all_done", total_elapsed_s=round(total_elapsed, 1))


if __name__ == "__main__":
    main()
