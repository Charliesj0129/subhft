"""vpin_regime_switch.py — Platform VPIN Regime Switch strategy.

Translates the research alpha ``research/alphas/vpin_regime_switch/impl.py``
into a production-ready ``BaseStrategy`` that integrates with the
FeatureEngine → RingBufferBus → StrategyRunner pipeline.

Signal output (risk-adjustment, NOT direct OrderIntents):
  +1.0 = LOW regime   — normal market, full capacity
   0.0 = ELEVATED     — caution, maintain positions
  -1.0 = TOXIC        — adverse selection, reduce/close

Downstream MM strategies read ``signal`` to scale position size.

Auto-calibration (BLOCKING fix from Stage 3-4 review):
  During warmup (first ``warmup_bars`` volume bars), VPIN values are
  collected.  After warmup completes, ``RegimeDetector.calibrate()``
  is called with P75/P95 percentile thresholds.  Signals are only
  emitted after calibration succeeds.

Allocator Law  : ``__slots__`` on all classes; pre-allocated buffers.
Precision Law  : Prices are scaled int x10000.  VPIN/signal are float
                 (non-accounting signal metric — Alpha Module Float Exception).
Cache Law      : Volume bars use contiguous pre-allocated ring buffer.
Async Law      : No blocking IO; pure computation.
"""

from __future__ import annotations

import math
import os
from enum import IntEnum
from typing import NamedTuple

from structlog import get_logger

from hft_platform.events import LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("strategy.vpin_regime_switch")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EPS: float = 1e-12
_SQRT2: float = math.sqrt(2.0)

_DEFAULT_BAR_VOLUME_TARGET: int = 500
_DEFAULT_N_BUCKETS: int = 50
_DEFAULT_EMA_ALPHA: float = 1.0 - math.exp(-1.0 / 8.0)
_DEFAULT_WARMUP_BARS: int = 60
_MAX_BAR_BUFFER: int = 256

# Placeholder thresholds — replaced by auto-calibration at runtime.
_INITIAL_THRESHOLD_ELEVATED: float = 0.4
_INITIAL_THRESHOLD_TOXIC: float = 0.7

# Auto-calibration percentiles
_CALIBRATION_P_ELEVATED: float = 0.75
_CALIBRATION_P_TOXIC: float = 0.95
_MIN_CALIBRATION_SAMPLES: int = 20


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


class VolumeBar(NamedTuple):
    """Immutable volume-synchronized bar."""

    open_price: int  # scaled x10000
    high_price: int  # scaled x10000
    low_price: int  # scaled x10000
    close_price: int  # scaled x10000
    total_volume: int
    buy_volume: int
    sell_volume: int
    ts_start: int  # nanoseconds
    ts_end: int  # nanoseconds


class Regime(IntEnum):
    LOW = 0
    ELEVATED = 1
    TOXIC = 2


_REGIME_SIGNAL: dict[Regime, float] = {
    Regime.LOW: 1.0,
    Regime.ELEVATED: 0.0,
    Regime.TOXIC: -1.0,
}


# ---------------------------------------------------------------------------
# Normal CDF approximation (scipy-free)
# ---------------------------------------------------------------------------


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erfc. Accurate to ~15 digits."""
    return 0.5 * math.erfc(-x / _SQRT2)


# ---------------------------------------------------------------------------
# VolumeBarBuilder
# ---------------------------------------------------------------------------


class VolumeBarBuilder:
    """Accumulates ticks into volume-synchronized bars.

    Supports two modes:
      (a) TickEvent volume: real exchange volume from tick data.
      (b) Depth-churn proxy: |delta_bid_qty| + |delta_ask_qty| from LOB updates.
    """

    __slots__ = (
        "_bar_volume_target",
        "_use_tick_volume",
        "_accumulated_volume",
        "_buy_volume",
        "_sell_volume",
        "_open_price",
        "_high_price",
        "_low_price",
        "_close_price",
        "_ts_start",
        "_ts_end",
        "_last_price",
        "_prev_bid_depth",
        "_prev_ask_depth",
        "_initialized",
        "_total_churn",
        "_consumption_churn",
    )

    def __init__(
        self,
        bar_volume_target: int = _DEFAULT_BAR_VOLUME_TARGET,
        use_tick_volume: bool = True,
    ) -> None:
        self._bar_volume_target: int = max(1, bar_volume_target)
        self._use_tick_volume: bool = use_tick_volume
        self._accumulated_volume: int = 0
        self._buy_volume: int = 0
        self._sell_volume: int = 0
        self._open_price: int = 0
        self._high_price: int = 0
        self._low_price: int = 0
        self._close_price: int = 0
        self._ts_start: int = 0
        self._ts_end: int = 0
        self._last_price: int = 0
        self._prev_bid_depth: int = 0
        self._prev_ask_depth: int = 0
        self._initialized: bool = False
        self._total_churn: int = 0
        self._consumption_churn: int = 0

    def add_tick(self, price: int, volume: int, ts: int) -> VolumeBar | None:
        """Add a tick with real volume. Returns completed bar or None."""
        if not self._use_tick_volume:
            return None
        if volume <= 0:
            return None

        if self._last_price > 0:
            if price > self._last_price:
                buy_vol = volume
                sell_vol = 0
            elif price < self._last_price:
                buy_vol = 0
                sell_vol = volume
            else:
                buy_vol = volume // 2
                sell_vol = volume - buy_vol
        else:
            buy_vol = volume // 2
            sell_vol = volume - buy_vol

        self._last_price = price
        return self._accumulate(price, volume, buy_vol, sell_vol, ts)

    def add_depth_update(
        self,
        mid_price_x2: int,
        bid_depth: int,
        ask_depth: int,
        ts: int,
    ) -> VolumeBar | None:
        """Add a depth update, using churn as volume proxy."""
        if self._use_tick_volume:
            return None

        price = mid_price_x2 // 2

        if not self._initialized:
            self._prev_bid_depth = bid_depth
            self._prev_ask_depth = ask_depth
            self._initialized = True
            self._last_price = price
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

        self._total_churn += churn
        self._consumption_churn += bid_consumed + ask_consumed

        total_consumed = bid_consumed + ask_consumed
        if total_consumed > 0:
            buy_fraction = bid_consumed / total_consumed
            buy_vol = int(churn * buy_fraction)
            sell_vol = churn - buy_vol
        else:
            buy_vol = churn // 2
            sell_vol = churn - buy_vol

        self._last_price = price
        return self._accumulate(price, churn, buy_vol, sell_vol, ts)

    @property
    def proxy_quality_ratio(self) -> float:
        """Ratio of consumption-like depth changes to total depth changes."""
        if self._total_churn <= 0:
            return 0.0
        return self._consumption_churn / self._total_churn

    def _accumulate(
        self,
        price: int,
        volume: int,
        buy_vol: int,
        sell_vol: int,
        ts: int,
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
                open_price=self._open_price,
                high_price=self._high_price,
                low_price=self._low_price,
                close_price=self._close_price,
                total_volume=self._accumulated_volume,
                buy_volume=self._buy_volume,
                sell_volume=self._sell_volume,
                ts_start=self._ts_start,
                ts_end=self._ts_end,
            )
            self._accumulated_volume = 0
            self._buy_volume = 0
            self._sell_volume = 0
            return bar
        return None

    def reset(self) -> None:
        self._accumulated_volume = 0
        self._buy_volume = 0
        self._sell_volume = 0
        self._open_price = 0
        self._high_price = 0
        self._low_price = 0
        self._close_price = 0
        self._ts_start = 0
        self._ts_end = 0
        self._last_price = 0
        self._prev_bid_depth = 0
        self._prev_ask_depth = 0
        self._initialized = False
        self._total_churn = 0
        self._consumption_churn = 0


# ---------------------------------------------------------------------------
# BulkVolumeClassifier
# ---------------------------------------------------------------------------


class BulkVolumeClassifier:
    """Classify each volume bar's buy/sell fractions.

    Tick-rule mode (primary) or BVC mode (for depth-churn proxy).
    """

    __slots__ = (
        "_last_bar_close",
        "_last_buy_fraction",
        "_use_bvc",
        "_sigma_sq_ema",
        "_sigma_ema_alpha",
        "_bvc_initialized",
    )

    def __init__(self, use_bvc: bool = False, sigma_ema_alpha: float = 0.1) -> None:
        self._last_bar_close: int = 0
        self._last_buy_fraction: float = 0.5
        self._use_bvc: bool = use_bvc
        self._sigma_sq_ema: float = 0.0
        self._sigma_ema_alpha: float = sigma_ema_alpha
        self._bvc_initialized: bool = False

    def classify(self, bar: VolumeBar) -> float:
        """Return buy_fraction in [0, 1] for the volume bar."""
        total = bar.total_volume
        if total <= 0:
            return 0.5

        if self._use_bvc:
            buy_fraction = self._classify_bvc(bar)
        else:
            buy_fraction = self._classify_tick_rule(bar)

        self._last_bar_close = bar.close_price
        self._last_buy_fraction = buy_fraction
        return buy_fraction

    def _classify_tick_rule(self, bar: VolumeBar) -> float:
        total = bar.total_volume
        within_buy_frac = bar.buy_volume / total

        if self._last_bar_close > 0:
            if bar.close_price > self._last_bar_close:
                cross_bar_frac = 1.0
            elif bar.close_price < self._last_bar_close:
                cross_bar_frac = 0.0
            else:
                cross_bar_frac = self._last_buy_fraction
        else:
            cross_bar_frac = 0.5

        if bar.high_price > bar.low_price:
            return within_buy_frac
        else:
            return 0.5 * within_buy_frac + 0.5 * cross_bar_frac

    def _classify_bvc(self, bar: VolumeBar) -> float:
        delta_price = float(bar.close_price - bar.open_price)
        dp_sq = delta_price * delta_price
        if not self._bvc_initialized:
            self._sigma_sq_ema = dp_sq if dp_sq > 0 else 1.0
            self._bvc_initialized = True
        else:
            self._sigma_sq_ema += self._sigma_ema_alpha * (dp_sq - self._sigma_sq_ema)

        sigma = math.sqrt(max(self._sigma_sq_ema, _EPS))
        z = delta_price / sigma
        return _norm_cdf(z)

    def reset(self) -> None:
        self._last_bar_close = 0
        self._last_buy_fraction = 0.5
        self._sigma_sq_ema = 0.0
        self._bvc_initialized = False


# ---------------------------------------------------------------------------
# VPINCalculator
# ---------------------------------------------------------------------------


class VPINCalculator:
    """Rolling VPIN over N volume buckets.

    Canonical mean-of-ratios formula (Easley et al. 2012):
      VPIN = (1/N) * sum(|V_buy_i - V_sell_i| / V_total_i).
    """

    __slots__ = (
        "_n_buckets",
        "_toxicity_ratios",
        "_head",
        "_count",
        "_sum_ratios",
    )

    def __init__(self, n_buckets: int = _DEFAULT_N_BUCKETS) -> None:
        self._n_buckets: int = max(1, n_buckets)
        self._toxicity_ratios: list[float] = [0.0] * self._n_buckets
        self._head: int = 0
        self._count: int = 0
        self._sum_ratios: float = 0.0

    def add_bar(self, bar: VolumeBar, buy_fraction: float) -> float:
        """Add a classified volume bar and return updated VPIN."""
        total_vol = bar.total_volume
        if total_vol <= 0:
            return self._current_vpin()

        buy_vol = total_vol * buy_fraction
        sell_vol = total_vol - buy_vol
        ratio = abs(buy_vol - sell_vol) / total_vol

        if self._count >= self._n_buckets:
            evict_idx = self._head
            self._sum_ratios -= self._toxicity_ratios[evict_idx]
        else:
            self._count += 1

        self._toxicity_ratios[self._head] = ratio
        self._sum_ratios += ratio
        self._head = (self._head + 1) % self._n_buckets

        return self._current_vpin()

    def _current_vpin(self) -> float:
        if self._count <= 0:
            return 0.0
        return self._sum_ratios / self._count

    @property
    def is_warm(self) -> bool:
        return self._count >= self._n_buckets

    def reset(self) -> None:
        for i in range(self._n_buckets):
            self._toxicity_ratios[i] = 0.0
        self._head = 0
        self._count = 0
        self._sum_ratios = 0.0


# ---------------------------------------------------------------------------
# RegimeDetector
# ---------------------------------------------------------------------------


class RegimeDetector:
    """3-state VPIN regime classifier with hysteresis and auto-calibration."""

    __slots__ = (
        "_threshold_elevated",
        "_threshold_toxic",
        "_ema_alpha",
        "_ema_vpin",
        "_regime",
        "_initialized",
        "_calibrated",
    )

    def __init__(
        self,
        threshold_elevated: float = _INITIAL_THRESHOLD_ELEVATED,
        threshold_toxic: float = _INITIAL_THRESHOLD_TOXIC,
        ema_alpha: float = _DEFAULT_EMA_ALPHA,
    ) -> None:
        if threshold_elevated >= threshold_toxic:
            raise ValueError(f"threshold_elevated ({threshold_elevated}) must be < threshold_toxic ({threshold_toxic})")
        self._threshold_elevated: float = threshold_elevated
        self._threshold_toxic: float = threshold_toxic
        self._ema_alpha: float = ema_alpha
        self._ema_vpin: float = 0.0
        self._regime: Regime = Regime.LOW
        self._initialized: bool = False
        self._calibrated: bool = False

    def calibrate(self, vpin_history: list[float]) -> None:
        """Set thresholds from historical VPIN percentiles (P75/P95)."""
        n = len(vpin_history)
        if n < _MIN_CALIBRATION_SAMPLES:
            raise ValueError(f"calibrate() requires >= {_MIN_CALIBRATION_SAMPLES} data points, got {n}")

        sorted_vals = sorted(vpin_history)
        p75 = self._percentile(sorted_vals, _CALIBRATION_P_ELEVATED)
        p95 = self._percentile(sorted_vals, _CALIBRATION_P_TOXIC)

        if p75 >= p95:
            p95 = p75 + 0.05
        if p75 <= 0.0:
            p75 = 0.01

        self._threshold_elevated = p75
        self._threshold_toxic = p95
        self._calibrated = True

    @staticmethod
    def _percentile(sorted_vals: list[float], p: float) -> float:
        n = len(sorted_vals)
        idx = p * (n - 1)
        lo = int(idx)
        hi = min(lo + 1, n - 1)
        frac = idx - lo
        return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    def update(self, raw_vpin: float) -> tuple[Regime, float]:
        """Update regime based on new VPIN value."""
        if not self._initialized:
            self._ema_vpin = raw_vpin
            self._initialized = True
        else:
            self._ema_vpin += self._ema_alpha * (raw_vpin - self._ema_vpin)

        smoothed = self._ema_vpin

        if smoothed >= self._threshold_toxic:
            self._regime = Regime.TOXIC
        elif smoothed >= self._threshold_elevated:
            if self._regime == Regime.TOXIC:
                hysteresis = self._threshold_toxic * 0.95
                if smoothed < hysteresis:
                    self._regime = Regime.ELEVATED
            else:
                self._regime = Regime.ELEVATED
        else:
            if self._regime == Regime.ELEVATED:
                hysteresis = self._threshold_elevated * 0.95
                if smoothed < hysteresis:
                    self._regime = Regime.LOW
            elif self._regime == Regime.TOXIC:
                self._regime = Regime.ELEVATED
            else:
                self._regime = Regime.LOW

        return self._regime, smoothed

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def threshold_elevated(self) -> float:
        return self._threshold_elevated

    @property
    def threshold_toxic(self) -> float:
        return self._threshold_toxic

    def reset(self) -> None:
        self._ema_vpin = 0.0
        self._regime = Regime.LOW
        self._initialized = False
        self._calibrated = False


# ---------------------------------------------------------------------------
# VpinRegimeSwitchStrategy (Platform Alpha)
# ---------------------------------------------------------------------------


class VpinRegimeSwitchStrategy(BaseStrategy):
    """VPIN regime switch strategy for the platform pipeline.

    Consumes both TickEvent (for tick-volume mode) and LOBStatsEvent
    (for depth-churn proxy mode) to build volume bars, compute VPIN,
    and classify the information toxicity regime.

    Auto-calibration:
      During warmup, VPIN values are collected into a buffer.  After
      ``warmup_bars`` volume bars complete, ``RegimeDetector.calibrate()``
      is called with P75/P95 thresholds.  Signals are emitted only
      after successful calibration.

    Signal output (read via ``signal`` property):
      +1.0 = LOW regime    (normal, full capacity)
       0.0 = ELEVATED      (caution, maintain)
      -1.0 = TOXIC         (adverse selection, reduce/close)

    Configuration (via strategy config YAML ``params:``):
      bar_volume_target : int   — Volume per bar (HIGH overfitting risk)
      n_vpin_buckets    : int   — VPIN lookback window (HIGH risk)
      ema_alpha         : float — EMA smoothing (LOW risk)
      warmup_bars       : int   — Bars before calibration (LOW risk)
      use_tick_volume   : bool  — True=tick volume, False=depth proxy (LOW risk)

    Env var overrides (prefix ``HFT_VPIN_``):
      HFT_VPIN_BAR_VOLUME_TARGET
      HFT_VPIN_N_BUCKETS
      HFT_VPIN_WARMUP_BARS
      HFT_VPIN_USE_TICK_VOLUME  (0/1)
      HFT_VPIN_EMA_ALPHA
    """

    __slots__ = (
        "_bar_builder",
        "_classifier",
        "_vpin_calc",
        "_regime_detector",
        "_signal",
        "_raw_vpin",
        "_smoothed_vpin",
        "_regime",
        "_warmup_bars",
        "_bars_seen",
        "_use_tick_volume",
        "_calibration_buffer",
        "_calibrated",
    )

    def __init__(self, strategy_id: str, **kwargs: object) -> None:
        super().__init__(strategy_id, **kwargs)

        params: dict = kwargs.get("params", {}) or {}  # type: ignore[assignment]

        # Resolve params with env var overrides
        bar_volume_target = int(
            os.getenv("HFT_VPIN_BAR_VOLUME_TARGET", "") or params.get("bar_volume_target", _DEFAULT_BAR_VOLUME_TARGET)
        )
        n_vpin_buckets = int(os.getenv("HFT_VPIN_N_BUCKETS", "") or params.get("n_vpin_buckets", _DEFAULT_N_BUCKETS))
        ema_alpha = float(os.getenv("HFT_VPIN_EMA_ALPHA", "") or params.get("ema_alpha", _DEFAULT_EMA_ALPHA))
        warmup_bars = int(os.getenv("HFT_VPIN_WARMUP_BARS", "") or params.get("warmup_bars", _DEFAULT_WARMUP_BARS))
        use_tick_volume_env = os.getenv("HFT_VPIN_USE_TICK_VOLUME", "")
        if use_tick_volume_env:
            use_tick_volume = use_tick_volume_env.lower() not in {"0", "false", "no", "off"}
        else:
            use_tick_volume = bool(params.get("use_tick_volume", True))

        self._use_tick_volume: bool = use_tick_volume
        self._warmup_bars: int = max(1, warmup_bars)

        self._bar_builder: VolumeBarBuilder = VolumeBarBuilder(
            bar_volume_target=bar_volume_target,
            use_tick_volume=use_tick_volume,
        )
        self._classifier: BulkVolumeClassifier = BulkVolumeClassifier(
            use_bvc=not use_tick_volume,
        )
        self._vpin_calc: VPINCalculator = VPINCalculator(n_buckets=n_vpin_buckets)
        self._regime_detector: RegimeDetector = RegimeDetector(
            threshold_elevated=_INITIAL_THRESHOLD_ELEVATED,
            threshold_toxic=_INITIAL_THRESHOLD_TOXIC,
            ema_alpha=ema_alpha,
        )

        self._signal: float = 0.0
        self._raw_vpin: float = 0.0
        self._smoothed_vpin: float = 0.0
        self._regime: Regime = Regime.LOW
        self._bars_seen: int = 0
        self._calibration_buffer: list[float] = []
        self._calibrated: bool = False

        logger.info(
            "vpin_regime_switch_init",
            strategy_id=strategy_id,
            bar_volume_target=bar_volume_target,
            n_vpin_buckets=n_vpin_buckets,
            ema_alpha=round(ema_alpha, 6),
            warmup_bars=self._warmup_bars,
            use_tick_volume=use_tick_volume,
        )

    # --- BaseStrategy event handlers ---

    def on_tick(self, event: TickEvent) -> None:
        """Process tick events for tick-volume mode."""
        if not self._use_tick_volume:
            return
        if event.volume <= 0:
            return
        bar = self._bar_builder.add_tick(event.price, event.volume, event.meta.source_ts)
        if bar is not None:
            self._process_bar(bar)

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Process LOBStatsEvent for depth-churn proxy mode."""
        if self._use_tick_volume:
            return
        mid_x2 = event.mid_price_x2
        if mid_x2 is None or mid_x2 <= 0:
            return
        bar = self._bar_builder.add_depth_update(
            mid_x2,
            event.bid_depth,
            event.ask_depth,
            event.ts,
        )
        if bar is not None:
            self._process_bar(bar)

    def _process_bar(self, bar: VolumeBar) -> None:
        """Process a completed volume bar: classify, compute VPIN, detect regime."""
        self._bars_seen += 1
        buy_fraction = self._classifier.classify(bar)
        self._raw_vpin = self._vpin_calc.add_bar(bar, buy_fraction)

        # Always update regime detector EMA to warm it up (matches research impl).
        # During warmup we still gate signal output on _calibrated.
        self._regime, self._smoothed_vpin = self._regime_detector.update(self._raw_vpin)

        if not self._calibrated:
            # Collect VPIN values during warmup for calibration
            self._calibration_buffer.append(self._raw_vpin)

            if self._bars_seen >= self._warmup_bars and self._vpin_calc.is_warm:
                self._run_calibration()
            else:
                # Not calibrated yet — emit neutral signal
                self._signal = 0.0
            return

        # Post-calibration: emit regime signal
        self._signal = _REGIME_SIGNAL[self._regime]

    def _run_calibration(self) -> None:
        """Execute auto-calibration using collected VPIN values."""
        n = len(self._calibration_buffer)
        if n < _MIN_CALIBRATION_SAMPLES:
            logger.warning(
                "vpin_calibration_insufficient_samples",
                strategy_id=self.strategy_id,
                samples=n,
                required=_MIN_CALIBRATION_SAMPLES,
            )
            return

        try:
            self._regime_detector.calibrate(self._calibration_buffer)
            self._calibrated = True
            logger.info(
                "vpin_calibration_complete",
                strategy_id=self.strategy_id,
                samples=n,
                threshold_elevated=round(self._regime_detector.threshold_elevated, 6),
                threshold_toxic=round(self._regime_detector.threshold_toxic, 6),
                bars_seen=self._bars_seen,
            )
            # Free the calibration buffer — no longer needed
            self._calibration_buffer = []

            # EMA is already warm from per-bar updates during warmup.
            # Emit signal based on current regime state.
            self._signal = _REGIME_SIGNAL[self._regime]

        except ValueError as exc:
            logger.error(
                "vpin_calibration_failed",
                strategy_id=self.strategy_id,
                error=str(exc),
                samples=n,
            )

    # --- Public API ---

    @property
    def signal(self) -> float:
        """Current regime signal in [-1, 1]."""
        return self._signal

    @property
    def raw_vpin(self) -> float:
        return self._raw_vpin

    @property
    def smoothed_vpin(self) -> float:
        return self._smoothed_vpin

    @property
    def regime(self) -> Regime:
        return self._regime

    @property
    def bars_seen(self) -> int:
        return self._bars_seen

    @property
    def is_calibrated(self) -> bool:
        return self._calibrated

    def reset(self) -> None:
        """Reset all state to initial."""
        self._bar_builder.reset()
        self._classifier.reset()
        self._vpin_calc.reset()
        self._regime_detector.reset()
        self._signal = 0.0
        self._raw_vpin = 0.0
        self._smoothed_vpin = 0.0
        self._regime = Regime.LOW
        self._bars_seen = 0
        self._calibration_buffer = []
        self._calibrated = False
        logger.info("vpin_regime_switch_reset", strategy_id=self.strategy_id)


__all__ = [
    "VolumeBar",
    "VolumeBarBuilder",
    "BulkVolumeClassifier",
    "VPINCalculator",
    "RegimeDetector",
    "Regime",
    "VpinRegimeSwitchStrategy",
]
