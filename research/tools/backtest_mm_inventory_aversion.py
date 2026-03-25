"""MM Inventory Aversion Backtest — P0 regime-aware market-making strategy.

Implements a quadratic-inventory-penalty MM with VPIN regime-aware spread
widening, OFI-based adverse selection detection, and adverse fill tracking.

Four scenarios are compared:
  A. Baseline         — symmetric linear-skew MM (current simple_mm logic)
  B. Quadratic only   — quadratic inventory penalty, no regime
  C. Quad + VPIN      — add regime-aware spread widening + position limits
  D. Full P0          — quadratic + VPIN + OFI spike + adverse fill tracker

Usage:
    uv run python research/tools/backtest_mm_inventory_aversion.py

Outputs: outputs/team_artifacts/alpha-research/stage4_mm_p0_backtest.json
"""

from __future__ import annotations

import json
import math
import sys
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any, NamedTuple

import numpy as np
import structlog

logger = structlog.get_logger("backtest.mm_inventory_aversion")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_DATA = _REPO_ROOT / "research" / "data" / "raw" / "txfd6" / "TXFD6_all_l1.npy"
_OUT_PATH = (
    _REPO_ROOT
    / "outputs"
    / "team_artifacts"
    / "alpha-research"
    / "stage4_mm_p0_backtest.json"
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TICK_SIZE_POINTS: int = 1
POINT_VALUE_NTD: int = 10  # Mini-TAIEX: 1 point = 10 NTD
RT_COST_NTD: int = TICK_SIZE_POINTS * POINT_VALUE_NTD // 2  # 5 NTD per RT

LATENCY_TICKS: int = 500  # ~36ms at TXFD6 tick rate

VPIN_BAR_VOLUME_TARGET: int = 500
VPIN_N_BUCKETS: int = 50
VPIN_WARMUP_BARS: int = 60
CALIBRATION_ROWS: int = 200_000

SAMPLE_INTERVAL: int = 1000

_EPS: float = 1e-12
_SQRT2: float = math.sqrt(2.0)


# ---------------------------------------------------------------------------
# Regime enum
# ---------------------------------------------------------------------------


class Regime(IntEnum):
    LOW = 0
    ELEVATED = 1
    TOXIC = 2


# ---------------------------------------------------------------------------
# VPIN components (self-contained, no external deps)
# ---------------------------------------------------------------------------


class VolumeBar(NamedTuple):
    open_price: int
    high_price: int
    low_price: int
    close_price: int
    total_volume: int
    buy_volume: int
    sell_volume: int
    ts_start: int
    ts_end: int


class VolumeBarBuilder:
    """Accumulates depth-churn updates into volume-synchronized bars."""

    __slots__ = (
        "_bar_volume_target",
        "_accumulated_volume",
        "_buy_volume",
        "_sell_volume",
        "_open_price",
        "_high_price",
        "_low_price",
        "_close_price",
        "_ts_start",
        "_ts_end",
        "_prev_bid_depth",
        "_prev_ask_depth",
        "_initialized",
    )

    def __init__(self, bar_volume_target: int = 500) -> None:
        self._bar_volume_target: int = max(1, bar_volume_target)
        self._accumulated_volume: int = 0
        self._buy_volume: int = 0
        self._sell_volume: int = 0
        self._open_price: int = 0
        self._high_price: int = 0
        self._low_price: int = 0
        self._close_price: int = 0
        self._ts_start: int = 0
        self._ts_end: int = 0
        self._prev_bid_depth: int = 0
        self._prev_ask_depth: int = 0
        self._initialized: bool = False

    def add_depth_update(
        self,
        mid_price_x2: int,
        bid_depth: int,
        ask_depth: int,
        ts: int,
    ) -> VolumeBar | None:
        price = mid_price_x2 // 2
        if not self._initialized:
            self._prev_bid_depth = bid_depth
            self._prev_ask_depth = ask_depth
            self._initialized = True
            return None

        delta_bid = abs(bid_depth - self._prev_bid_depth)
        delta_ask = abs(ask_depth - self._prev_ask_depth)
        churn = delta_bid + delta_ask

        bid_consumed = max(self._prev_bid_depth - bid_depth, 0)
        ask_consumed = max(self._prev_ask_depth - ask_depth, 0)

        self._prev_bid_depth = bid_depth
        self._prev_ask_depth = ask_depth

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

        return self._accumulate(price, churn, buy_vol, sell_vol, ts)

    def _accumulate(
        self, price: int, volume: int, buy_vol: int, sell_vol: int, ts: int
    ) -> VolumeBar | None:
        if self._accumulated_volume == 0:
            self._open_price = price
            self._high_price = price
            self._low_price = price
            self._ts_start = ts
        else:
            if price > self._high_price:
                self._high_price = price
            if price < self._low_price:
                self._low_price = price

        self._close_price = price
        self._ts_end = ts
        self._accumulated_volume += volume
        self._buy_volume += buy_vol
        self._sell_volume += sell_vol

        if self._accumulated_volume >= self._bar_volume_target:
            bar = VolumeBar(
                self._open_price, self._high_price, self._low_price,
                self._close_price, self._accumulated_volume,
                self._buy_volume, self._sell_volume,
                self._ts_start, self._ts_end,
            )
            self._accumulated_volume = 0
            self._buy_volume = 0
            self._sell_volume = 0
            return bar
        return None


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / _SQRT2)


class BulkVolumeClassifier:
    """Classify volume bars into buy/sell fractions using BVC."""

    __slots__ = ("_last_bar_close", "_last_buy_frac", "_sigma_sq_ema", "_sigma_alpha", "_init")

    def __init__(self, sigma_ema_alpha: float = 0.1) -> None:
        self._last_bar_close: int = 0
        self._last_buy_frac: float = 0.5
        self._sigma_sq_ema: float = 0.0
        self._sigma_alpha: float = sigma_ema_alpha
        self._init: bool = False

    def classify(self, bar: VolumeBar) -> float:
        total = bar.total_volume
        if total <= 0:
            return 0.5
        delta_price = float(bar.close_price - bar.open_price)
        dp_sq = delta_price * delta_price
        if not self._init:
            self._sigma_sq_ema = dp_sq if dp_sq > 0 else 1.0
            self._init = True
        else:
            self._sigma_sq_ema += self._sigma_alpha * (dp_sq - self._sigma_sq_ema)
        sigma = math.sqrt(max(self._sigma_sq_ema, _EPS))
        z = delta_price / sigma
        buy_frac = _norm_cdf(z)
        self._last_bar_close = bar.close_price
        self._last_buy_frac = buy_frac
        return buy_frac


class VPINCalculator:
    """Rolling VPIN over N volume buckets."""

    __slots__ = ("_n_buckets", "_ratios", "_head", "_count", "_sum")

    def __init__(self, n_buckets: int = 50) -> None:
        self._n_buckets: int = max(1, n_buckets)
        self._ratios: list[float] = [0.0] * self._n_buckets
        self._head: int = 0
        self._count: int = 0
        self._sum: float = 0.0

    def add_bar(self, bar: VolumeBar, buy_fraction: float) -> float:
        total = bar.total_volume
        if total <= 0:
            return self._current()
        buy_vol = total * buy_fraction
        sell_vol = total - buy_vol
        ratio = abs(buy_vol - sell_vol) / total

        if self._count >= self._n_buckets:
            self._sum -= self._ratios[self._head]
        else:
            self._count += 1
        self._ratios[self._head] = ratio
        self._sum += ratio
        self._head = (self._head + 1) % self._n_buckets
        return self._current()

    def _current(self) -> float:
        if self._count <= 0:
            return 0.0
        return self._sum / self._count

    @property
    def is_warm(self) -> bool:
        return self._count >= self._n_buckets


class RegimeDetector:
    """3-state VPIN regime classifier with hysteresis."""

    __slots__ = (
        "_thr_elev", "_thr_toxic", "_ema_alpha", "_ema_vpin",
        "_regime", "_initialized", "_calibrated",
    )

    def __init__(
        self,
        threshold_elevated: float = 0.4,
        threshold_toxic: float = 0.7,
        ema_alpha: float = 0.1175,  # 1 - exp(-1/8)
    ) -> None:
        self._thr_elev: float = threshold_elevated
        self._thr_toxic: float = threshold_toxic
        self._ema_alpha: float = ema_alpha
        self._ema_vpin: float = 0.0
        self._regime: int = Regime.LOW
        self._initialized: bool = False
        self._calibrated: bool = False

    def calibrate(self, vpin_history: list[float]) -> None:
        n = len(vpin_history)
        if n < 20:
            return
        s = sorted(vpin_history)
        p75 = self._pctl(s, 0.75)
        p95 = self._pctl(s, 0.95)
        if p75 >= p95:
            p95 = p75 + 0.05
        if p75 <= 0.0:
            p75 = 0.01
        self._thr_elev = p75
        self._thr_toxic = p95
        self._calibrated = True

    @staticmethod
    def _pctl(s: list[float], p: float) -> float:
        n = len(s)
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return s[lo] * (1.0 - frac) + s[hi] * frac

    def update(self, raw_vpin: float) -> int:
        if not self._initialized:
            self._ema_vpin = raw_vpin
            self._initialized = True
        else:
            self._ema_vpin += self._ema_alpha * (raw_vpin - self._ema_vpin)

        v = self._ema_vpin
        if v >= self._thr_toxic:
            self._regime = Regime.TOXIC
        elif v >= self._thr_elev:
            if self._regime == Regime.TOXIC:
                if v < self._thr_toxic * 0.95:
                    self._regime = Regime.ELEVATED
            else:
                self._regime = Regime.ELEVATED
        else:
            if self._regime == Regime.ELEVATED:
                if v < self._thr_elev * 0.95:
                    self._regime = Regime.LOW
            elif self._regime == Regime.TOXIC:
                self._regime = Regime.ELEVATED
            else:
                self._regime = Regime.LOW

        return self._regime

    @property
    def regime(self) -> int:
        return self._regime

    @property
    def threshold_elevated(self) -> float:
        return self._thr_elev

    @property
    def threshold_toxic(self) -> float:
        return self._thr_toxic


# ---------------------------------------------------------------------------
# VPIN Alpha wrapper
# ---------------------------------------------------------------------------


class VpinAlpha:
    """Standalone VPIN alpha for backtest use."""

    __slots__ = (
        "_bar_builder", "_classifier", "_vpin_calc", "_regime_detector",
        "_raw_vpin", "_regime", "_bars_seen", "_cal_buf", "_calibrated",
        "_warmup_bars", "_bar_vol_target", "_n_buckets",
    )

    def __init__(
        self,
        bar_volume_target: int = VPIN_BAR_VOLUME_TARGET,
        n_vpin_buckets: int = VPIN_N_BUCKETS,
        warmup_bars: int = VPIN_WARMUP_BARS,
    ) -> None:
        self._bar_vol_target = bar_volume_target
        self._n_buckets = n_vpin_buckets
        self._warmup_bars = warmup_bars
        self._bar_builder = VolumeBarBuilder(bar_volume_target=bar_volume_target)
        self._classifier = BulkVolumeClassifier()
        self._vpin_calc = VPINCalculator(n_buckets=n_vpin_buckets)
        self._regime_detector = RegimeDetector()
        self._raw_vpin: float = 0.0
        self._regime: int = Regime.LOW
        self._bars_seen: int = 0
        self._cal_buf: list[float] = []
        self._calibrated: bool = False

    def update(
        self, mid_price_x2: int, bid_depth: int, ask_depth: int, ts: int
    ) -> None:
        bar = self._bar_builder.add_depth_update(mid_price_x2, bid_depth, ask_depth, ts)
        if bar is None:
            return
        self._bars_seen += 1
        buy_frac = self._classifier.classify(bar)
        self._raw_vpin = self._vpin_calc.add_bar(bar, buy_frac)
        self._regime_detector.update(self._raw_vpin)
        self._regime = self._regime_detector.regime

        if not self._calibrated:
            self._cal_buf.append(self._raw_vpin)
            if self._bars_seen >= self._warmup_bars and self._vpin_calc.is_warm:
                if len(self._cal_buf) >= 20:
                    self._regime_detector.calibrate(self._cal_buf)
                    self._calibrated = True
                    self._cal_buf = []
                    logger.info(
                        "vpin_calibrated",
                        thr_elev=round(self._regime_detector.threshold_elevated, 4),
                        thr_toxic=round(self._regime_detector.threshold_toxic, 4),
                    )

    @property
    def regime(self) -> int:
        return self._regime

    @property
    def raw_vpin(self) -> float:
        return self._raw_vpin

    @property
    def bars_seen(self) -> int:
        return self._bars_seen

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    def clone_calibrated(self) -> VpinAlpha:
        a = VpinAlpha(self._bar_vol_target, self._n_buckets, self._warmup_bars)
        if self._calibrated:
            a._regime_detector._thr_elev = self._regime_detector.threshold_elevated
            a._regime_detector._thr_toxic = self._regime_detector.threshold_toxic
            a._regime_detector._calibrated = True
            a._calibrated = True
        return a


# ---------------------------------------------------------------------------
# Strategy parameters
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class MMParams:
    """Immutable MM strategy parameters.

    Analytical derivation for MXFD6 (Mini-TAIEX, tick_value=10 NTD):
      phi = tick_value / (max_pos^2) = 10/25 = 0.4 (too aggressive)
    Empirically calibrated: phi=0.08 gives ~1 tick penalty at pos=3,
    which is proportional to the typical spread of 4 points.
    gamma scales with realized_vol (~2.8 pts); 0.02 gives ~0.17 pts at pos=3.
    """

    max_pos: int = 5
    gamma: float = 0.02        # risk aversion (vol-dependent)
    phi: float = 0.08          # quadratic penalty (empirically tuned)
    alpha_weight: float = 0.0005  # OFI to price conversion
    ofi_spike_threshold: float = 5.0
    adverse_horizon_ticks: int = 150
    adverse_premium: int = 1
    adverse_rate_threshold: float = 0.6
    adverse_horizons: tuple[int, ...] = (50, 150, 250)


_DEFAULT_PARAMS = MMParams()

_REGIME_SPREAD_MULT: dict[int, float] = {
    Regime.LOW: 1.0,
    Regime.ELEVATED: 1.3,
    Regime.TOXIC: 2.0,
}

_REGIME_MAX_POS: dict[int, int] = {
    Regime.LOW: 5,
    Regime.ELEVATED: 3,
    Regime.TOXIC: 2,
}


# ---------------------------------------------------------------------------
# Adverse Fill Tracker
# ---------------------------------------------------------------------------


class AdverseFillTracker:
    """Track fills and measure adverse selection rate."""

    __slots__ = ("_horizons", "_pending", "_n_adverse", "_n_total", "_adverse_rate")

    def __init__(self, horizons: tuple[int, ...] = (50, 150, 250)) -> None:
        self._horizons = horizons
        self._pending: list[tuple[int, int, int, int]] = []  # (tick, side, price, max_h)
        self._n_adverse: int = 0
        self._n_total: int = 0
        self._adverse_rate: float = 0.0

    def record_fill(self, tick_idx: int, side: int, price_points: int) -> None:
        max_h = max(self._horizons)
        self._pending.append((tick_idx, side, price_points, max_h))

    def update(self, tick_idx: int, mid_price_points: int) -> None:
        primary_h = self._horizons[1] if len(self._horizons) > 1 else self._horizons[0]
        still: list[tuple[int, int, int, int]] = []

        for ft, fs, fp, mh in self._pending:
            elapsed = tick_idx - ft
            if elapsed >= primary_h:
                if fs > 0:
                    adverse = (fp - mid_price_points) > 0
                else:
                    adverse = (mid_price_points - fp) > 0
                self._n_total += 1
                if adverse:
                    self._n_adverse += 1
                self._adverse_rate = self._n_adverse / max(self._n_total, 1)
            elif elapsed < mh:
                still.append((ft, fs, fp, mh))
            else:
                self._n_total += 1
                self._adverse_rate = self._n_adverse / max(self._n_total, 1)

        self._pending = still

    @property
    def adverse_rate(self) -> float:
        return self._adverse_rate

    @property
    def n_adverse(self) -> int:
        return self._n_adverse

    @property
    def n_total(self) -> int:
        return self._n_total


# ---------------------------------------------------------------------------
# MM State
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MMState:
    """Mutable MM simulation state. All monetary values in NTD * 10000."""

    position: int = 0
    realized_pnl: int = 0
    n_fills: int = 0
    n_buys: int = 0
    n_sells: int = 0
    peak_equity: int = 0
    max_drawdown: int = 0
    emergency_flatten_count: int = 0
    sum_abs_inventory: int = 0
    inventory_samples: int = 0
    equity_curve: list[int] = field(default_factory=list)
    pnl_in_regime: dict[int, int] = field(
        default_factory=lambda: {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    )
    fills_in_regime: dict[int, int] = field(
        default_factory=lambda: {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    )
    time_in_regime: dict[int, int] = field(
        default_factory=lambda: {Regime.LOW: 0, Regime.ELEVATED: 0, Regime.TOXIC: 0}
    )
    last_equity_snapshot: int = 0

    def mark_to_market(self, mid_price_points: int) -> int:
        unrealized = self.position * mid_price_points * POINT_VALUE_NTD * 10000
        return self.realized_pnl + unrealized

    def update_drawdown(self, equity: int) -> None:
        if equity > self.peak_equity:
            self.peak_equity = equity
        dd = self.peak_equity - equity
        if dd > self.max_drawdown:
            self.max_drawdown = dd

    def attribute_pnl_to_regime(self, current_equity: int, regime: int) -> None:
        delta = current_equity - self.last_equity_snapshot
        self.pnl_in_regime[regime] += delta
        self.last_equity_snapshot = current_equity

    @property
    def mean_abs_inventory(self) -> float:
        if self.inventory_samples <= 0:
            return 0.0
        return self.sum_abs_inventory / self.inventory_samples


# ---------------------------------------------------------------------------
# Scenario enum
# ---------------------------------------------------------------------------


class Scenario(IntEnum):
    BASELINE = 0
    QUADRATIC = 1
    QUAD_VPIN = 2
    FULL_P0 = 3


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
# Core simulation
# ---------------------------------------------------------------------------


def run_simulation(
    data: np.ndarray,
    scenario: Scenario,
    params: MMParams,
    vpin_alpha: VpinAlpha | None = None,
    label: str = "baseline",
) -> dict[str, Any]:
    """Run a single MM simulation scenario."""
    n = len(data)
    state = MMState()
    adverse_tracker = AdverseFillTracker(horizons=params.adverse_horizons)

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    local_ts = data["local_ts"]

    mid_prices = np.round((bid_px + ask_px) / 2.0).astype(np.int64)
    spreads = np.round(ask_px - bid_px).astype(np.int64)

    ofi_ema: float = 0.0
    ofi_ema_alpha: float = 0.05

    vol_window: int = 500
    recent_returns: list[float] = []
    realized_vol: float = 1.0

    prev_mid: int = int(mid_prices[0])
    prev_bid_px_v: float = float(bid_px[0])
    prev_ask_px_v: float = float(ask_px[0])
    prev_bid_qty_v: float = float(bid_qty[0])
    prev_ask_qty_v: float = float(ask_qty[0])

    regime_buffer: list[int] = []
    current_regime: int = Regime.LOW

    # Quote buffer for latency-delayed fills:
    # Quotes computed at tick i are available for fill at tick i + LATENCY_TICKS
    quote_buffer: list[tuple[int, int, int, int, bool]] = []
    # (bid_quote, ask_quote, max_pos_eff, qty, emergency)
    active_bid: int = 0
    active_ask: int = 999999
    active_max_pos: int = params.max_pos
    active_qty: int = 0
    active_emergency: bool = False

    logger.info("starting_simulation", label=label, scenario=scenario.name, n_rows=n)
    t0 = time.monotonic()

    for i in range(1, n):
        mid = int(mid_prices[i])
        spread = int(spreads[i])

        if mid <= 0 or spread < 0:
            prev_mid = mid
            prev_bid_px_v = float(bid_px[i])
            prev_ask_px_v = float(ask_px[i])
            prev_bid_qty_v = float(bid_qty[i])
            prev_ask_qty_v = float(ask_qty[i])
            continue

        # Realized vol
        ret = float(mid - prev_mid)
        recent_returns.append(ret)
        if len(recent_returns) > vol_window:
            recent_returns.pop(0)
        if len(recent_returns) >= 20:
            realized_vol = max(0.1, float(np.std(recent_returns)))

        # OFI
        ofi_raw = _compute_ofi_l1(
            float(bid_qty[i]), float(ask_qty[i]),
            prev_bid_qty_v, prev_ask_qty_v,
            float(bid_px[i]), float(ask_px[i]),
            prev_bid_px_v, prev_ask_px_v,
        )
        ofi_ema += ofi_ema_alpha * (ofi_raw - ofi_ema)

        # VPIN
        if vpin_alpha is not None:
            mid_x2 = int(round(bid_px[i])) + int(round(ask_px[i]))
            vpin_alpha.update(
                mid_price_x2=mid_x2,
                bid_depth=int(bid_qty[i]),
                ask_depth=int(ask_qty[i]),
                ts=int(local_ts[i]),
            )
            regime_buffer.append(vpin_alpha.regime)
            if len(regime_buffer) > LATENCY_TICKS:
                current_regime = regime_buffer[-LATENCY_TICKS - 1]
            else:
                current_regime = Regime.LOW

        # Adverse fill tracker update
        if scenario == Scenario.FULL_P0:
            adverse_tracker.update(i, mid)

        state.time_in_regime[current_regime] += 1

        # --- Quote computation (these become active after LATENCY_TICKS) ---
        pos = state.position
        half_spread = max(1, spread // 2)

        if scenario == Scenario.BASELINE:
            skew = -(pos * half_spread) // params.max_pos
            bid_q = mid + skew - half_spread
            ask_q = mid + skew + half_spread
            max_pos_q = params.max_pos
            qty_q = 1
            emerg_q = False

        elif scenario == Scenario.QUADRATIC:
            alpha_adj = ofi_ema * params.alpha_weight
            inventory_penalty = (
                params.gamma * pos * realized_vol
                + params.phi * pos * abs(pos)
            )
            reservation_price = mid + alpha_adj - inventory_penalty
            res_int = int(round(reservation_price))
            bid_q = res_int - half_spread
            ask_q = res_int + half_spread
            max_pos_q = params.max_pos
            qty_q = 1 if abs(pos) < max_pos_q else 0
            emerg_q = False

        elif scenario == Scenario.QUAD_VPIN:
            alpha_adj = ofi_ema * params.alpha_weight
            inventory_penalty = (
                params.gamma * pos * realized_vol
                + params.phi * pos * abs(pos)
            )
            reservation_price = mid + alpha_adj - inventory_penalty
            res_int = int(round(reservation_price))
            regime_mult = _REGIME_SPREAD_MULT.get(current_regime, 1.0)
            adjusted_half = max(1, int(round(half_spread * regime_mult)))
            bid_q = res_int - adjusted_half
            ask_q = res_int + adjusted_half
            max_pos_q = _REGIME_MAX_POS.get(current_regime, params.max_pos)
            qty_q = 1 if abs(pos) < max_pos_q else 0
            emerg_q = abs(pos) > max_pos_q and abs(pos) > 1

        else:  # FULL_P0
            alpha_adj = ofi_ema * params.alpha_weight
            inventory_penalty = (
                params.gamma * pos * realized_vol
                + params.phi * pos * abs(pos)
            )
            reservation_price = mid + alpha_adj - inventory_penalty
            res_int = int(round(reservation_price))
            regime_mult = _REGIME_SPREAD_MULT.get(current_regime, 1.0)

            ofi_abs = abs(ofi_raw)
            if ofi_abs > params.ofi_spike_threshold:
                ofi_spike_mult = 1.0 + min(
                    ofi_abs / params.ofi_spike_threshold - 1.0, 1.0
                )
            else:
                ofi_spike_mult = 1.0

            adverse_premium_ticks = 0
            if adverse_tracker.adverse_rate > params.adverse_rate_threshold:
                adverse_premium_ticks = params.adverse_premium

            adjusted_half = max(
                1,
                int(round(half_spread * regime_mult * ofi_spike_mult))
                + adverse_premium_ticks,
            )
            bid_q = res_int - adjusted_half
            ask_q = res_int + adjusted_half
            max_pos_q = _REGIME_MAX_POS.get(current_regime, params.max_pos)
            qty_q = 1 if abs(pos) < max_pos_q else 0
            emerg_q = abs(pos) > max_pos_q and abs(pos) > 1

        # Buffer the quote; activate after LATENCY_TICKS delay
        quote_buffer.append((bid_q, ask_q, max_pos_q, qty_q, emerg_q))
        if len(quote_buffer) > LATENCY_TICKS:
            ab, aa, am, aq, ae = quote_buffer[-LATENCY_TICKS - 1]
            active_bid = ab
            active_ask = aa
            active_max_pos = am
            active_qty = aq
            active_emergency = ae

        # Emergency flatten: check CURRENT position against CURRENT regime limit
        # (not delayed — emergency is a real-time safety check)
        cur_max = _REGIME_MAX_POS.get(current_regime, params.max_pos)
        do_emergency = (
            scenario in (Scenario.QUAD_VPIN, Scenario.FULL_P0)
            and abs(state.position) > cur_max
            and abs(state.position) > 1
        )
        if do_emergency:
            state.emergency_flatten_count += 1
            cur_pos = state.position
            if cur_pos > 0:
                sell_price = mid - 1
                state.realized_pnl += sell_price * POINT_VALUE_NTD * 10000
                state.position -= 1
                state.n_fills += 1
                state.n_sells += 1
                state.realized_pnl -= RT_COST_NTD * 10000 // 2
                state.fills_in_regime[current_regime] += 1
                if scenario == Scenario.FULL_P0:
                    adverse_tracker.record_fill(i, -1, mid)
            elif cur_pos < 0:
                buy_price = mid + 1
                state.realized_pnl -= buy_price * POINT_VALUE_NTD * 10000
                state.position += 1
                state.n_fills += 1
                state.n_buys += 1
                state.realized_pnl -= RT_COST_NTD * 10000 // 2
                state.fills_in_regime[current_regime] += 1
                if scenario == Scenario.FULL_P0:
                    adverse_tracker.record_fill(i, 1, mid)

        # Fill simulation: current mid crosses our active (delayed) quotes
        # Position limit uses the ACTIVE quote's max_pos (decided at quote time)
        # but also hard-capped by current regime for safety
        fill_max = min(active_max_pos, cur_max) if scenario in (Scenario.QUAD_VPIN, Scenario.FULL_P0) else active_max_pos
        if active_qty > 0:
            if mid <= active_bid and state.position < fill_max:
                state.realized_pnl -= active_bid * POINT_VALUE_NTD * 10000
                state.position += 1
                state.n_fills += 1
                state.n_buys += 1
                state.realized_pnl -= RT_COST_NTD * 10000 // 2
                state.fills_in_regime[current_regime] += 1
                if scenario == Scenario.FULL_P0:
                    adverse_tracker.record_fill(i, 1, mid)

            if mid >= active_ask and state.position > -fill_max:
                state.realized_pnl += active_ask * POINT_VALUE_NTD * 10000
                state.position -= 1
                state.n_fills += 1
                state.n_sells += 1
                state.realized_pnl -= RT_COST_NTD * 10000 // 2
                state.fills_in_regime[current_regime] += 1
                if scenario == Scenario.FULL_P0:
                    adverse_tracker.record_fill(i, -1, mid)

        # Inventory sampling
        if i % 100 == 0:
            state.sum_abs_inventory += abs(state.position)
            state.inventory_samples += 1

        # Equity tracking
        if i % SAMPLE_INTERVAL == 0:
            equity = state.mark_to_market(mid)
            state.update_drawdown(equity)
            state.equity_curve.append(equity)
            state.attribute_pnl_to_regime(equity, current_regime)

        prev_mid = mid
        prev_bid_px_v = float(bid_px[i])
        prev_ask_px_v = float(ask_px[i])
        prev_bid_qty_v = float(bid_qty[i])
        prev_ask_qty_v = float(ask_qty[i])

    # Final
    final_mid = int(mid_prices[-1])
    final_equity = state.mark_to_market(final_mid)
    state.update_drawdown(final_equity)
    state.equity_curve.append(final_equity)

    elapsed = time.monotonic() - t0
    logger.info(
        "simulation_complete",
        label=label,
        elapsed_s=round(elapsed, 2),
        n_fills=state.n_fills,
        final_pos=state.position,
    )

    # Sharpe
    eq_arr = np.array(state.equity_curve, dtype=np.float64)
    returns = np.diff(eq_arr)
    if len(returns) > 1 and float(returns.std()) > 1e-15:
        samples_per_day = max(1, n // SAMPLE_INTERVAL // 4)
        sharpe = float(returns.mean() / returns.std()) * math.sqrt(252 * samples_per_day)
    else:
        sharpe = 0.0

    scale = 10000.0
    return {
        "label": label,
        "scenario": scenario.name,
        "total_pnl_ntd": round(final_equity / scale, 2),
        "sharpe": round(sharpe, 4),
        "max_drawdown_ntd": round(state.max_drawdown / scale, 2),
        "n_fills": state.n_fills,
        "n_buys": state.n_buys,
        "n_sells": state.n_sells,
        "mean_abs_inventory": round(state.mean_abs_inventory, 3),
        "emergency_flatten_count": state.emergency_flatten_count,
        "adverse_fill_rate_pct": round(adverse_tracker.adverse_rate * 100, 2),
        "adverse_fills": adverse_tracker.n_adverse,
        "adverse_total": adverse_tracker.n_total,
        "final_position": state.position,
        "pnl_per_regime_ntd": {
            Regime(k).name: round(v / scale, 2)
            for k, v in state.pnl_in_regime.items()
        },
        "fills_per_regime": {
            Regime(k).name: v for k, v in state.fills_in_regime.items()
        },
        "time_in_regime_pct": {
            Regime(k).name: round(v / max(n, 1) * 100, 2)
            for k, v in state.time_in_regime.items()
        },
        "equity_curve_ntd": [round(e / scale, 2) for e in state.equity_curve],
    }


# ---------------------------------------------------------------------------
# VPIN calibration
# ---------------------------------------------------------------------------


def calibrate_vpin(data: np.ndarray, n_rows: int) -> VpinAlpha:
    """Calibrate VPIN thresholds on first n_rows of data."""
    logger.info("calibrating_vpin", n_rows=n_rows)
    alpha = VpinAlpha()

    bid_px = data["bid_px"]
    ask_px = data["ask_px"]
    bid_qty = data["bid_qty"]
    ask_qty = data["ask_qty"]
    local_ts = data["local_ts"]

    vpin_history: list[float] = []
    limit = min(n_rows, len(data))

    for i in range(limit):
        mid_x2 = int(round(bid_px[i])) + int(round(ask_px[i]))
        alpha.update(
            mid_price_x2=mid_x2,
            bid_depth=int(bid_qty[i]),
            ask_depth=int(ask_qty[i]),
            ts=int(local_ts[i]),
        )
        if alpha.bars_seen > VPIN_WARMUP_BARS:
            vpin_history.append(alpha.raw_vpin)

    prod_alpha = VpinAlpha()
    if len(vpin_history) >= 20:
        prod_alpha._regime_detector.calibrate(vpin_history)
        prod_alpha._calibrated = True
        logger.info(
            "calibration_complete",
            thr_elev=round(prod_alpha._regime_detector.threshold_elevated, 4),
            thr_toxic=round(prod_alpha._regime_detector.threshold_toxic, 4),
            n_samples=len(vpin_history),
        )
    else:
        logger.warning("calibration_insufficient", n_samples=len(vpin_history))

    return prod_alpha


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

    params = _DEFAULT_PARAMS

    # Calibrate VPIN
    vpin_calibrated = calibrate_vpin(data, n_rows=CALIBRATION_ROWS)

    # Scenario A: Baseline
    result_a = run_simulation(data, Scenario.BASELINE, params, label="A_baseline")

    # Scenario B: Quadratic inventory only
    result_b = run_simulation(data, Scenario.QUADRATIC, params, label="B_quadratic")

    # Scenario C: Quadratic + VPIN
    vpin_c = vpin_calibrated.clone_calibrated()
    result_c = run_simulation(
        data, Scenario.QUAD_VPIN, params, vpin_alpha=vpin_c, label="C_quad_vpin"
    )

    # Scenario D: Full P0
    vpin_d = vpin_calibrated.clone_calibrated()
    result_d = run_simulation(
        data, Scenario.FULL_P0, params, vpin_alpha=vpin_d, label="D_full_p0"
    )

    results = [result_a, result_b, result_c, result_d]

    # Save JSON (strip equity curves)
    output: dict[str, Any] = {
        "scenarios": {},
        "config": {
            "params": {
                "max_pos": params.max_pos,
                "gamma": params.gamma,
                "phi": params.phi,
                "alpha_weight": params.alpha_weight,
                "ofi_spike_threshold": params.ofi_spike_threshold,
                "adverse_horizon_ticks": params.adverse_horizon_ticks,
                "adverse_premium": params.adverse_premium,
                "adverse_rate_threshold": params.adverse_rate_threshold,
            },
            "latency_ticks": LATENCY_TICKS,
            "tick_size_points": TICK_SIZE_POINTS,
            "point_value_ntd": POINT_VALUE_NTD,
            "rt_cost_ntd": RT_COST_NTD,
            "data_rows": len(data),
            "data_source": data_path.name,
            "calibration_rows": CALIBRATION_ROWS,
        },
    }
    for r in results:
        r_copy = dict(r)
        r_copy.pop("equity_curve_ntd", None)
        output["scenarios"][r["label"]] = r_copy

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_OUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)
    logger.info("results_saved", path=str(_OUT_PATH))

    # Print comparison table
    print("\n" + "=" * 100)
    print("  MM INVENTORY AVERSION BACKTEST — P0 COMPARATIVE RESULTS")
    print("=" * 100)
    print(f"  Data: {data_path.name} ({len(data):,} rows)")
    print(f"  Latency: {LATENCY_TICKS} ticks (~36ms P95)")
    print(f"  Cost: {RT_COST_NTD} NTD per round-trip | Point value: {POINT_VALUE_NTD} NTD")
    print(f"  Params: max_pos={params.max_pos}, gamma={params.gamma}, phi={params.phi}")
    print("-" * 100)

    col_w = 18
    hdr = f"  {'Metric':<28}" + "".join(
        f"{h:>{col_w}}" for h in ["A: Baseline", "B: Quadratic", "C: Quad+VPIN", "D: Full P0"]
    )
    print(hdr)
    print("-" * 100)

    def _row(metric: str, key: str, fmt: str = ",.2f") -> None:
        line = f"  {metric:<28}"
        for r in results:
            v = r[key]
            if isinstance(v, float):
                line += f"{v:>{col_w}{fmt}}"
            else:
                line += f"{v:>{col_w},}"
        print(line)

    _row("Total PnL (NTD)", "total_pnl_ntd")
    _row("Sharpe Ratio", "sharpe", ".4f")
    _row("Max Drawdown (NTD)", "max_drawdown_ntd")
    _row("Total Fills", "n_fills", ",")
    _row("Buys", "n_buys", ",")
    _row("Sells", "n_sells", ",")
    _row("Mean |Inventory|", "mean_abs_inventory", ".3f")
    _row("Emergency Flattens", "emergency_flatten_count", ",")
    _row("Adverse Fill Rate (%)", "adverse_fill_rate_pct", ".2f")
    _row("Final Position", "final_position", ",")

    print("-" * 100)
    print("  REGIME TIME DISTRIBUTION (C & D)")
    print("-" * 100)
    for rn in ["LOW", "ELEVATED", "TOXIC"]:
        vc = result_c["time_in_regime_pct"].get(rn, 0.0)
        vd = result_d["time_in_regime_pct"].get(rn, 0.0)
        print(f"    {rn:<12} C: {vc:>6.2f}%   D: {vd:>6.2f}%")

    print("-" * 100)
    print("  PnL BY REGIME (C & D)")
    print("-" * 100)
    for rn in ["LOW", "ELEVATED", "TOXIC"]:
        pc = result_c["pnl_per_regime_ntd"].get(rn, 0.0)
        pd = result_d["pnl_per_regime_ntd"].get(rn, 0.0)
        print(f"    {rn:<12} C: {pc:>12,.2f} NTD   D: {pd:>12,.2f} NTD")

    print("-" * 100)
    print("  IMPROVEMENT vs BASELINE")
    print("-" * 100)
    bl_sharpe = result_a["sharpe"]
    bl_dd = result_a["max_drawdown_ntd"]
    for r in results[1:]:
        sd = r["sharpe"] - bl_sharpe
        ddr = (1.0 - r["max_drawdown_ntd"] / bl_dd) * 100 if bl_dd > 0 else 0.0
        print(f"    {r['label']:<20} Sharpe delta: {sd:>+8.4f}   DD reduction: {ddr:>+7.2f}%")

    print("=" * 100)

    # Verdict
    best = max(results, key=lambda r: r["sharpe"])
    print(f"\n  VERDICT: Best Sharpe = {best['sharpe']:.4f} ({best['label']})")
    d_s = result_d["sharpe"]
    a_s = result_a["sharpe"]
    print(f"  Full P0 Sharpe delta vs baseline: {d_s - a_s:+.4f}")
    if bl_dd > 0:
        dd_imp = (1.0 - result_d["max_drawdown_ntd"] / bl_dd) * 100
        print(f"  Drawdown reduction (D vs A): {dd_imp:+.2f}%")
    print(f"  Adverse fill rate (D): {result_d['adverse_fill_rate_pct']:.2f}%")
    print(f"  Mean |inventory| (D): {result_d['mean_abs_inventory']:.3f}")
    print()


if __name__ == "__main__":
    main()
