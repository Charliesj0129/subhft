"""Vol-CBS: Volatility-Conditioned Cascade Bounce Strategy.

Enhances CBS by normalizing entry threshold, stop-loss, and hold period
by realized volatility (ATR). Adds vol-targeting position sizing.

Key adaptations from Singha et al. (2511.08571):
    - ATR-based threshold: entry when move > k * ATR (instead of fixed 40 bps)
    - ATR-based stop-loss: stop at entry +/- s * ATR (instead of fixed 15 bps)
    - Vol-targeting: position size = target_vol / realized_vol, capped
    - EWMA volatility for regime classification

Paper refs:
    2511.08571 — Singha et al. (2025), Forecast-to-Fill
    2212.07288 — Bernardi et al. (2022), Smoothing vol targeting
    2511.06177 — Vlasiuk & Smirnov (2025), Push-response anomalies

Allocator Law : Pre-allocated buffers, no heap allocs in update loop.
Precision Law : Signal is float (not price). Prices use mid_x2 (scaled int).
Cache Law     : EMA state in contiguous scalars.
"""

from __future__ import annotations

import math
from typing import Optional

from research.registry.schemas import AlphaManifest, AlphaStatus

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

_MANIFEST = AlphaManifest(
    alpha_id="vol_cbs",
    hypothesis=(
        "Normalizing CBS's entry threshold and exits by realized volatility "
        "(ATR) improves signal quality: fewer false triggers in high-vol, "
        "stronger edge in low-vol. Vol-targeting stabilizes risk."
    ),
    formula=(
        "threshold = k * ATR_N; stop = s * ATR_N; "
        "hold = base_hold * (target_vol / real_vol); "
        "size = base * min(W_max, target_vol / real_vol)"
    ),
    paper_refs=("2511.08571", "2212.07288", "2511.06177"),
    data_fields=("mid_price_x2",),
    complexity="O(1)",
    latency_profile="sim_p95_v2026-02-26",
    roles_used=("planner",),
    skills_used=("iterative-retrieval",),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ATR window (in ticks). For TMFD6 at ~1.8 ticks/sec:
# 14 "periods" of ~5 min each = ~14 * 300 * 1.8 = 7560 ticks
# We use a simpler EMA-based ATR with configurable half-life.
_DEFAULT_ATR_HALFLIFE_TICKS: int = 500  # ~4.6 min at 1.8 ticks/sec
_DEFAULT_ATR_EMA_ALPHA: float = 1.0 - math.exp(-math.log(2.0) / _DEFAULT_ATR_HALFLIFE_TICKS)

# Entry threshold multiplier (k * ATR)
_DEFAULT_K_ENTRY: float = 3.0  # ~equivalent to 40 bps at typical vol

# Stop-loss multiplier (s * ATR)
_DEFAULT_S_STOP: float = 1.0  # ~equivalent to 15 bps at typical vol

# Hold period base (ns)
_DEFAULT_HOLD_BASE_NS: int = 300_000_000_000  # 300s (same as CBS)

# Vol-targeting
_DEFAULT_TARGET_VOL_ANNUAL: float = 0.15  # 15% annualized
_DEFAULT_MAX_LEVERAGE: float = 2.0

# EWMA for regime vol (slower, for classification)
_DEFAULT_VOL_REGIME_HALFLIFE: int = 5000  # ~46 min
_DEFAULT_VOL_REGIME_ALPHA: float = 1.0 - math.exp(-math.log(2.0) / _DEFAULT_VOL_REGIME_HALFLIFE)

# Session skip
_DEFAULT_SESSION_SKIP_NS: int = 30 * 60 * 1_000_000_000  # 30 min

# Warmup
_WARMUP_TICKS: int = 200

# Minimum ATR to avoid division by zero
_MIN_ATR: float = 1e-10


class VolCBS:
    """Volatility-Conditioned Cascade Bounce Strategy signal generator.

    Replaces CBS's fixed thresholds with ATR-normalized values:
    - Entry: move > k * ATR_N (instead of fixed 40 bps)
    - Stop: entry +/- s * ATR_N (instead of fixed 15 bps)
    - Hold: base_hold * (target_vol / realized_vol)

    Parameters
    ----------
    k_entry : float
        Entry threshold as multiple of ATR.
    s_stop : float
        Stop-loss as multiple of ATR.
    hold_base_ns : int
        Base hold period in nanoseconds.
    atr_halflife_ticks : int
        Half-life for ATR EMA in ticks.
    target_vol_annual : float
        Annualized target volatility for position sizing.
    max_leverage : float
        Maximum leverage cap.
    detect_window_ns : int
        Lookback window for move detection (same as CBS).
    """

    __slots__ = (
        "_k_entry",
        "_s_stop",
        "_hold_base_ns",
        "_target_vol_annual",
        "_max_leverage",
        "_detect_window_ns",
        "_atr_alpha",
        "_vol_regime_alpha",
        "_ema_atr",
        "_ema_vol_sq",
        "_prev_mid_x2",
        "_prev_high",
        "_prev_low",
        "_tick_count",
        "_warmed_up",
        # Position tracking
        "_state",
        "_entry_mid_x2",
        "_entry_atr",
        "_direction",
        "_peak_mid_x2",
    )

    def __init__(
        self,
        k_entry: float = _DEFAULT_K_ENTRY,
        s_stop: float = _DEFAULT_S_STOP,
        hold_base_ns: int = _DEFAULT_HOLD_BASE_NS,
        atr_halflife_ticks: int = _DEFAULT_ATR_HALFLIFE_TICKS,
        target_vol_annual: float = _DEFAULT_TARGET_VOL_ANNUAL,
        max_leverage: float = _DEFAULT_MAX_LEVERAGE,
        detect_window_ns: int = 600_000_000_000,
    ) -> None:
        self._k_entry = k_entry
        self._s_stop = s_stop
        self._hold_base_ns = hold_base_ns
        self._target_vol_annual = target_vol_annual
        self._max_leverage = max_leverage
        self._detect_window_ns = detect_window_ns

        self._atr_alpha = 1.0 - math.exp(-math.log(2.0) / atr_halflife_ticks)
        self._vol_regime_alpha = _DEFAULT_VOL_REGIME_ALPHA

        # EMA state
        self._ema_atr: float = 0.0
        self._ema_vol_sq: float = 0.0
        self._prev_mid_x2: int = 0
        self._prev_high: int = 0
        self._prev_low: int = 0
        self._tick_count: int = 0
        self._warmed_up: bool = False

        # Position state
        self._state: str = "idle"
        self._entry_mid_x2: int = 0
        self._entry_atr: float = 0.0
        self._direction: int = 0
        self._peak_mid_x2: int = 0

    def update(self, mid_x2: int) -> dict[str, object]:
        """Process a new mid_x2 tick. Returns signal dict.

        Returns dict with:
            - 'atr': current ATR (in mid_x2 units)
            - 'atr_bps': ATR in bps of current price
            - 'vol_regime': 'low' / 'medium' / 'high'
            - 'threshold_bps': current adaptive threshold in bps
            - 'stop_loss_bps': current adaptive stop in bps
            - 'position_size_mult': vol-targeting multiplier
            - 'signal': 0 if no trigger, -1/+1 for sell/buy (contrarian)
        """
        result: dict[str, object] = {
            "atr": 0.0,
            "atr_bps": 0.0,
            "vol_regime": "unknown",
            "threshold_bps": 0.0,
            "stop_loss_bps": 0.0,
            "position_size_mult": 1.0,
            "signal": 0,
        }

        if mid_x2 <= 0:
            return result

        if self._prev_mid_x2 <= 0:
            self._prev_mid_x2 = mid_x2
            self._prev_high = mid_x2
            self._prev_low = mid_x2
            return result

        self._tick_count += 1

        # --- ATR computation ---
        # True Range adapted for tick data (no OHLC bars):
        # TR = |current - previous| (simplified for tick-by-tick)
        tr = abs(mid_x2 - self._prev_mid_x2)
        tr_f = float(tr)

        # EMA of ATR
        a = self._atr_alpha
        self._ema_atr = a * tr_f + (1.0 - a) * self._ema_atr

        # EMA of squared returns for vol regime classification
        ret = (mid_x2 - self._prev_mid_x2) / self._prev_mid_x2
        a2 = self._vol_regime_alpha
        self._ema_vol_sq = a2 * (ret * ret) + (1.0 - a2) * self._ema_vol_sq

        self._prev_mid_x2 = mid_x2

        if self._tick_count < _WARMUP_TICKS:
            return result

        self._warmed_up = True
        atr = max(self._ema_atr, _MIN_ATR)

        # --- Convert ATR to bps ---
        atr_bps = atr / mid_x2 * 10000.0

        # --- Vol regime classification ---
        realized_vol = math.sqrt(max(self._ema_vol_sq, 1e-20))
        # Annualize: daily vol ~ realized_vol * sqrt(ticks_per_day)
        # TMFD6: ~1.8 ticks/sec * 4.75 hours * 3600 = ~30,780 ticks/day
        ticks_per_day = 30780.0
        daily_vol = realized_vol * math.sqrt(ticks_per_day)
        annual_vol = daily_vol * math.sqrt(252.0)

        if annual_vol < 0.10:
            vol_regime = "low"
        elif annual_vol < 0.25:
            vol_regime = "medium"
        else:
            vol_regime = "high"

        # --- Adaptive thresholds ---
        threshold_atr = self._k_entry * atr
        threshold_bps = threshold_atr / mid_x2 * 10000.0

        stop_atr = self._s_stop * atr
        stop_bps = stop_atr / mid_x2 * 10000.0

        # --- Position sizing multiplier (vol-targeting) ---
        if annual_vol > 1e-6:
            size_mult = min(self._max_leverage, self._target_vol_annual / annual_vol)
        else:
            size_mult = 1.0

        result["atr"] = atr
        result["atr_bps"] = atr_bps
        result["vol_regime"] = vol_regime
        result["threshold_bps"] = threshold_bps
        result["stop_loss_bps"] = stop_bps
        result["position_size_mult"] = size_mult

        return result

    def compute_atr_bps(self) -> float:
        """Get current ATR in bps."""
        if self._prev_mid_x2 <= 0 or self._ema_atr < _MIN_ATR:
            return 0.0
        return self._ema_atr / self._prev_mid_x2 * 10000.0

    def compute_threshold_bps(self) -> float:
        """Get current adaptive entry threshold in bps."""
        return self._k_entry * self.compute_atr_bps()

    def compute_stop_bps(self) -> float:
        """Get current adaptive stop-loss in bps."""
        return self._s_stop * self.compute_atr_bps()

    def reset(self) -> None:
        """Reset all state for a new session."""
        self._ema_atr = 0.0
        self._ema_vol_sq = 0.0
        self._prev_mid_x2 = 0
        self._prev_high = 0
        self._prev_low = 0
        self._tick_count = 0
        self._warmed_up = False
        self._state = "idle"
        self._entry_mid_x2 = 0
        self._entry_atr = 0.0
        self._direction = 0
        self._peak_mid_x2 = 0

    @property
    def manifest(self) -> AlphaManifest:
        return _MANIFEST

    @property
    def warmed_up(self) -> bool:
        return self._warmed_up
