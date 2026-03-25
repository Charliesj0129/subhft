"""MM P0 Parameter Sweep — find optimal (gamma, phi, alpha_weight) for Scenario B.

Sweeps 168 parameter combinations of the quadratic-inventory-only MM strategy
on TXFD6 L1 data, then tests VPIN spread-only overlay on the best params.

Usage:
    uv run python research/tools/backtest_mm_p0_sweep.py

Outputs: outputs/team_artifacts/alpha-research/stage4_mm_p0_sweep.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger("backtest.mm_p0_sweep")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_mm_p0_sweep.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # Mini-TAIEX: 1 point = 10 NTD
RT_COST_NTD: int = TICK_SIZE_POINTS * POINT_VALUE_NTD // 2  # 5 NTD per RT

LATENCY_TICKS: int = 500  # ~36ms at TXFD6 tick rate

SAMPLE_INTERVAL: int = 1000

# VPIN constants (for overlay test)
VPIN_BAR_VOLUME_TARGET: int = 500
VPIN_N_BUCKETS: int = 50
VPIN_WARMUP_BARS: int = 60
CALIBRATION_ROWS: int = 200_000

_EPS: float = 1e-12
_SQRT2: float = math.sqrt(2.0)

# ---------------------------------------------------------------------------
# Sweep grid
# ---------------------------------------------------------------------------

SWEEP_GRID: dict[str, list[float]] = {
    "gamma": [0.001, 0.003, 0.005, 0.008, 0.01, 0.015, 0.02],
    "phi": [0.005, 0.01, 0.02, 0.04, 0.06, 0.08],
    "alpha_weight": [0.0, 0.0002, 0.0005, 0.001],
}

MAX_POS: int = 5


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
# VPIN components (for overlay test)
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

        # BVC classify
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

        # VPIN update
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

        # Regime EMA
        if not self._ema_init:
            self._ema_vpin = self._raw_vpin
            self._ema_init = True
        else:
            self._ema_vpin += self._ema_alpha * (self._raw_vpin - self._ema_vpin)

        # Calibration
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

        # Regime classification
        v = self._ema_vpin
        if v >= self._thr_toxic:
            self._regime = 2  # TOXIC
        elif v >= self._thr_elev:
            if self._regime == 2 and v >= self._thr_toxic * 0.95:
                pass  # stay TOXIC
            else:
                self._regime = 1  # ELEVATED
        else:
            if self._regime == 1 and v >= self._thr_elev * 0.95:
                pass  # stay ELEVATED
            elif self._regime == 2:
                self._regime = 1  # drop to ELEVATED
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
    # Precomputed OFI per tick
    ofi_raw: np.ndarray          # float64
    # Precomputed realized vol (rolling std of returns)
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
    # Fill early ticks with first valid vol
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
# Core simulation — Scenario B (quadratic inventory only)
# ---------------------------------------------------------------------------


def run_quadratic_sim(
    pc: PrecomputedData,
    gamma: float,
    phi: float,
    alpha_weight: float,
    max_pos: int = MAX_POS,
) -> dict[str, Any]:
    """Run Scenario B (quadratic inventory only) with given parameters."""
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    ofi_raw_arr = pc.ofi_raw
    realized_vol_arr = pc.realized_vol

    # State
    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0

    # OFI EMA state
    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    # Quote buffer for latency delay
    # Store as parallel arrays for speed
    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)
    buf_qty = np.zeros(n, dtype=np.int8)

    # Equity curve (sampled)
    equity_samples: list[int] = []

    # Adverse fill tracking
    pending_fills: list[tuple[int, int, int]] = []  # (tick, side, price)
    n_adverse: int = 0
    n_adverse_total: int = 0
    adverse_horizon: int = 150

    scale_pv = POINT_VALUE_NTD * 10000  # 100000
    half_rt = RT_COST_NTD * 10000 // 2  # 25000

    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])

        if mid <= 0 or spread < 0:
            continue

        # OFI EMA
        ofi_ema += ofi_ema_alpha * (ofi_raw_arr[i] - ofi_ema)
        rv = realized_vol_arr[i]

        # Quote computation
        alpha_adj = ofi_ema * alpha_weight
        inv_penalty = gamma * position * rv + phi * position * abs(position)
        reservation = mid + alpha_adj - inv_penalty
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)
        bid_q = res_int - half_spread
        ask_q = res_int + half_spread
        qty_q = 1 if abs(position) < max_pos else 0

        buf_bid[i] = bid_q
        buf_ask[i] = ask_q
        buf_qty[i] = qty_q

        # Activate delayed quotes
        if i > LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
            a_qty = int(buf_qty[act_idx])
        else:
            a_bid = 0
            a_ask = 999999
            a_qty = 0

        # Fill simulation
        if a_qty > 0:
            if mid <= a_bid and position < max_pos:
                realized_pnl -= a_bid * scale_pv
                position += 1
                n_fills += 1
                n_buys += 1
                realized_pnl -= half_rt
                pending_fills.append((i, 1, mid))

            if mid >= a_ask and position > -max_pos:
                realized_pnl += a_ask * scale_pv
                position -= 1
                n_fills += 1
                n_sells += 1
                realized_pnl -= half_rt
                pending_fills.append((i, -1, mid))

        # Adverse fill evaluation
        if pending_fills:
            still: list[tuple[int, int, int]] = []
            for ft, fs, fp in pending_fills:
                elapsed = i - ft
                if elapsed >= adverse_horizon:
                    n_adverse_total += 1
                    # Buy adverse if price fell; Sell adverse if price rose
                    if fs > 0 and (fp - mid) > 0:
                        n_adverse += 1
                    elif fs < 0 and (mid - fp) > 0:
                        n_adverse += 1
                else:
                    still.append((ft, fs, fp))
            pending_fills = still

        # Inventory sampling
        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        # Equity tracking
        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    # Final equity
    final_mid = int(mid_prices[-1])
    final_unrealized = position * final_mid * scale_pv
    final_equity = realized_pnl + final_unrealized
    if final_equity > peak_equity:
        peak_equity = final_equity
    dd = peak_equity - final_equity
    if dd > max_drawdown:
        max_drawdown = dd
    equity_samples.append(final_equity)

    # Sharpe
    eq_arr = np.array(equity_samples, dtype=np.float64)
    returns = np.diff(eq_arr)
    if len(returns) > 1 and float(returns.std()) > 1e-15:
        samples_per_day = max(1, n // SAMPLE_INTERVAL // 4)
        sharpe = float(returns.mean() / returns.std()) * math.sqrt(252 * samples_per_day)
    else:
        sharpe = 0.0

    # Adverse fill rate
    adverse_rate = n_adverse / max(n_adverse_total, 1)

    scale = 10000.0
    mean_inv = sum_abs_inv / max(inv_samples, 1)

    return {
        "gamma": gamma,
        "phi": phi,
        "alpha_weight": alpha_weight,
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


# ---------------------------------------------------------------------------
# VPIN spread-only overlay simulation
# ---------------------------------------------------------------------------


def run_quadratic_vpin_overlay(
    pc: PrecomputedData,
    gamma: float,
    phi: float,
    alpha_weight: float,
    max_pos: int = MAX_POS,
) -> dict[str, Any]:
    """Run Scenario B + VPIN spread widening only (no position limits, no flatten)."""
    n = pc.n
    mid_prices = pc.mid_prices
    spreads = pc.spreads
    ofi_raw_arr = pc.ofi_raw
    realized_vol_arr = pc.realized_vol

    # Gentle VPIN regime multipliers
    vpin_spread_mult = {0: 1.0, 1: 1.2, 2: 1.8}

    # VPIN tracker
    vpin = _VPINTracker()
    regime_buffer: list[int] = []

    # State
    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    sum_abs_inv: int = 0
    inv_samples: int = 0

    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    buf_bid = np.zeros(n, dtype=np.int64)
    buf_ask = np.zeros(n, dtype=np.int64)
    buf_qty = np.zeros(n, dtype=np.int8)

    equity_samples: list[int] = []

    pending_fills: list[tuple[int, int, int]] = []
    n_adverse: int = 0
    n_adverse_total: int = 0
    adverse_horizon: int = 150

    scale_pv = POINT_VALUE_NTD * 10000
    half_rt = RT_COST_NTD * 10000 // 2

    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])

        if mid <= 0 or spread < 0:
            continue

        # VPIN update
        mid_x2 = int(round(pc.bid_px[i])) + int(round(pc.ask_px[i]))
        vpin.update(mid_x2, int(pc.bid_qty[i]), int(pc.ask_qty[i]), int(pc.local_ts[i]))
        regime_buffer.append(vpin.regime)

        # Delayed regime (same latency as quotes)
        if len(regime_buffer) > LATENCY_TICKS:
            current_regime = regime_buffer[-LATENCY_TICKS - 1]
        else:
            current_regime = 0  # LOW

        # OFI EMA
        ofi_ema += ofi_ema_alpha * (ofi_raw_arr[i] - ofi_ema)
        rv = realized_vol_arr[i]

        # Quote computation — quadratic + VPIN spread widening ONLY
        alpha_adj = ofi_ema * alpha_weight
        inv_penalty = gamma * position * rv + phi * position * abs(position)
        reservation = mid + alpha_adj - inv_penalty
        res_int = int(round(reservation))
        half_spread = max(1, spread // 2)

        # VPIN spread widening (no position limits from VPIN)
        regime_mult = vpin_spread_mult.get(current_regime, 1.0)
        adjusted_half = max(1, int(round(half_spread * regime_mult)))

        bid_q = res_int - adjusted_half
        ask_q = res_int + adjusted_half
        qty_q = 1 if abs(position) < max_pos else 0  # only quadratic pos limit

        buf_bid[i] = bid_q
        buf_ask[i] = ask_q
        buf_qty[i] = qty_q

        # Activate delayed quotes
        if i > LATENCY_TICKS:
            act_idx = i - LATENCY_TICKS
            a_bid = int(buf_bid[act_idx])
            a_ask = int(buf_ask[act_idx])
            a_qty = int(buf_qty[act_idx])
        else:
            a_bid = 0
            a_ask = 999999
            a_qty = 0

        # Fill simulation
        if a_qty > 0:
            if mid <= a_bid and position < max_pos:
                realized_pnl -= a_bid * scale_pv
                position += 1
                n_fills += 1
                n_buys += 1
                realized_pnl -= half_rt
                pending_fills.append((i, 1, mid))

            if mid >= a_ask and position > -max_pos:
                realized_pnl += a_ask * scale_pv
                position -= 1
                n_fills += 1
                n_sells += 1
                realized_pnl -= half_rt
                pending_fills.append((i, -1, mid))

        # Adverse fill evaluation
        if pending_fills:
            still: list[tuple[int, int, int]] = []
            for ft, fs, fp in pending_fills:
                elapsed = i - ft
                if elapsed >= adverse_horizon:
                    n_adverse_total += 1
                    if fs > 0 and (fp - mid) > 0:
                        n_adverse += 1
                    elif fs < 0 and (mid - fp) > 0:
                        n_adverse += 1
                else:
                    still.append((ft, fs, fp))
            pending_fills = still

        # Inventory sampling
        if i % 100 == 0:
            sum_abs_inv += abs(position)
            inv_samples += 1

        # Equity tracking
        if i % SAMPLE_INTERVAL == 0:
            unrealized = position * mid * scale_pv
            equity = realized_pnl + unrealized
            if equity > peak_equity:
                peak_equity = equity
            dd = peak_equity - equity
            if dd > max_drawdown:
                max_drawdown = dd
            equity_samples.append(equity)

    # Final equity
    final_mid = int(mid_prices[-1])
    final_unrealized = position * final_mid * scale_pv
    final_equity = realized_pnl + final_unrealized
    if final_equity > peak_equity:
        peak_equity = final_equity
    dd = peak_equity - final_equity
    if dd > max_drawdown:
        max_drawdown = dd
    equity_samples.append(final_equity)

    # Sharpe
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

    return {
        "gamma": gamma,
        "phi": phi,
        "alpha_weight": alpha_weight,
        "vpin_overlay": True,
        "vpin_spread_mult": {0: 1.0, 1: 1.2, 2: 1.8},
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


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


def main() -> None:
    data_path = _DEFAULT_DATA
    if not data_path.exists():
        logger.error("data_not_found", path=str(data_path))
        sys.exit(1)

    logger.info("loading_data", path=str(data_path))
    data = np.load(str(data_path), allow_pickle=True)
    logger.info("data_loaded", rows=len(data), fields=list(data.dtype.names or []))

    pc = precompute(data)

    # Build parameter grid
    gammas = SWEEP_GRID["gamma"]
    phis = SWEEP_GRID["phi"]
    alphas = SWEEP_GRID["alpha_weight"]
    total_combos = len(gammas) * len(phis) * len(alphas)
    logger.info("sweep_starting", total_combinations=total_combos)

    results: list[dict[str, Any]] = []
    # Track dominated gamma-phi pairs for early pruning
    dominated: dict[tuple[float, float], bool] = {}
    combo_idx = 0
    t_sweep_start = time.monotonic()

    for gamma in gammas:
        for phi_val in phis:
            # Check if this (gamma, phi) is dominated from prior alpha_weight=0 test
            if dominated.get((gamma, phi_val), False):
                combo_idx += len(alphas)
                continue

            for aw in alphas:
                combo_idx += 1
                if combo_idx % 20 == 0 or combo_idx == 1:
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

                result = run_quadratic_sim(pc, gamma, phi_val, aw)
                results.append(result)

                # Pruning: if alpha_weight=0 gives Sharpe < -50, skip rest for this (gamma, phi)
                if aw == 0.0 and result["sharpe"] < -50.0:
                    dominated[(gamma, phi_val)] = True
                    logger.info(
                        "pruning_dominated",
                        gamma=gamma,
                        phi=phi_val,
                        sharpe=result["sharpe"],
                    )
                    break

    sweep_elapsed = time.monotonic() - t_sweep_start
    logger.info(
        "sweep_complete",
        n_results=len(results),
        elapsed_s=round(sweep_elapsed, 2),
    )

    # Sort by Sharpe desc, then n_fills desc
    results.sort(key=lambda r: (-r["sharpe"], -r["n_fills"]))

    # --- VPIN overlay test on best quadratic result ---
    best = results[0] if results else None
    vpin_result = None
    if best is not None:
        logger.info(
            "vpin_overlay_test",
            best_gamma=best["gamma"],
            best_phi=best["phi"],
            best_alpha=best["alpha_weight"],
            best_sharpe=best["sharpe"],
            best_fills=best["n_fills"],
        )
        vpin_result = run_quadratic_vpin_overlay(
            pc,
            gamma=best["gamma"],
            phi=best["phi"],
            alpha_weight=best["alpha_weight"],
        )
        logger.info(
            "vpin_overlay_done",
            sharpe=vpin_result["sharpe"],
            n_fills=vpin_result["n_fills"],
            pnl=vpin_result["total_pnl_ntd"],
        )

    # --- Output ---
    output = {
        "sweep_grid": SWEEP_GRID,
        "total_combinations": total_combos,
        "results_evaluated": len(results),
        "top_20": results[:20],
        "vpin_overlay_test": vpin_result,
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

    # --- Print top 10 table ---
    print("\n" + "=" * 120)
    print("TOP 10 PARAMETER COMBINATIONS (Scenario B — Quadratic Inventory Only)")
    print("=" * 120)
    header = (
        f"{'Rank':>4}  {'gamma':>7}  {'phi':>7}  {'alpha_w':>8}  "
        f"{'Sharpe':>8}  {'PnL(NTD)':>12}  {'MaxDD(NTD)':>12}  "
        f"{'Fills':>7}  {'Adv%':>6}  {'MeanInv':>8}  {'FinalPos':>8}"
    )
    print(header)
    print("-" * 120)
    for rank, r in enumerate(results[:10], 1):
        line = (
            f"{rank:>4}  {r['gamma']:>7.4f}  {r['phi']:>7.4f}  {r['alpha_weight']:>8.4f}  "
            f"{r['sharpe']:>8.2f}  {r['total_pnl_ntd']:>12.2f}  {r['max_drawdown_ntd']:>12.2f}  "
            f"{r['n_fills']:>7}  {r['adverse_fill_rate_pct']:>5.1f}%  "
            f"{r['mean_abs_inventory']:>8.3f}  {r['final_position']:>8}"
        )
        print(line)
    print("=" * 120)

    # --- Print VPIN overlay comparison ---
    if best is not None and vpin_result is not None:
        print("\n" + "=" * 100)
        print("VPIN SPREAD-ONLY OVERLAY COMPARISON")
        print(f"Best params: gamma={best['gamma']}, phi={best['phi']}, alpha_weight={best['alpha_weight']}")
        print("=" * 100)
        comp_header = f"{'Variant':<30}  {'Sharpe':>8}  {'PnL(NTD)':>12}  {'MaxDD(NTD)':>12}  {'Fills':>7}  {'Adv%':>6}"
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

        # Verdict
        better_sharpe = vpin_result["sharpe"] > best["sharpe"]
        better_adverse = vpin_result["adverse_fill_rate_pct"] < best["adverse_fill_rate_pct"]
        print(f"\nVPIN overlay Sharpe {'BETTER' if better_sharpe else 'WORSE'} "
              f"({vpin_result['sharpe']:.2f} vs {best['sharpe']:.2f})")
        print(f"VPIN overlay adverse rate {'BETTER' if better_adverse else 'WORSE'} "
              f"({vpin_result['adverse_fill_rate_pct']:.1f}% vs {best['adverse_fill_rate_pct']:.1f}%)")

    # --- Summary of goal metrics ---
    print("\n" + "=" * 80)
    print("GOAL CHECK (best result)")
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


if __name__ == "__main__":
    main()
