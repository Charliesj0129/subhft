"""toxicity_aware_mm_v2.py — Improved MM strategy with toxicity pause + contrarian signal.

Changes from v1:
1. **Toxicity pause**: stop quoting when toxicity_timescale_div > threshold (don't widen, WITHDRAW)
2. **cum_ofi_revert**: contrarian mean-reversion signal reduces adverse selection
3. **Microprice-centered mid**: use microprice instead of raw mid for quote center
4. **Wider default spread**: 4 ticks base (was 2)
5. **Asymmetric position decay**: aggressive unwinding when position is large

Feature set (8 features):
- queue_imbalance, toxicity_timescale_div, microprice_spread_ratio, cross_ema_qi,
  depth_velocity_diff, adverse_momentum, cum_ofi_revert, ofi_asymmetry
"""

from __future__ import annotations

import numpy as np
from structlog import get_logger

from hft_platform.strategies.alpha_driven_mm import (
    AlphaDrivenMMStrategy,
    DepthInfo,
    QuoteDecision,
)

logger = get_logger("toxicity_mm_v2")


class ToxicityAwareMMv2(AlphaDrivenMMStrategy):
    """v2 Market Making strategy with toxicity pause and contrarian signals."""

    __slots__ = (
        "_tick_size",
        "_max_position",
        "_base_half_spread_ticks",
        "_skew_sensitivity",
        "_tox_pause_threshold",
        "_tox_widen_factor",
        "_inv_skew_per_lot",
        "_qty_per_side",
        "_contrarian_weight",
    )

    def __init__(
        self,
        *,
        feature_timestamps: np.ndarray,
        feature_array: np.ndarray,
        feature_names: list[str],
        symbol: str,
        tick_size: float = 1.0,
        max_position: int = 3,
        base_half_spread_ticks: float = 4.0,
        skew_sensitivity: float = 1.0,
        tox_pause_threshold: float = 0.3,
        tox_widen_factor: float = 1.5,
        inv_skew_per_lot: float = 0.5,
        contrarian_weight: float = 0.15,
        qty_per_side: int = 1,
        requote_interval_ns: int = 100_000_000,
    ):
        super().__init__(
            feature_timestamps=feature_timestamps,
            feature_array=feature_array,
            feature_names=feature_names,
            symbol=symbol,
            strategy_id="toxicity_mm_v2",
            requote_interval_ns=requote_interval_ns,
        )
        self._tick_size = float(tick_size)
        self._max_position = int(max_position)
        self._base_half_spread_ticks = float(base_half_spread_ticks)
        self._skew_sensitivity = float(skew_sensitivity)
        self._tox_pause_threshold = float(tox_pause_threshold)
        self._tox_widen_factor = float(tox_widen_factor)
        self._inv_skew_per_lot = float(inv_skew_per_lot)
        self._contrarian_weight = float(contrarian_weight)
        self._qty_per_side = int(qty_per_side)

    def compute_quotes(
        self,
        depth: DepthInfo,
        features: np.ndarray,
        position: int,
    ) -> QuoteDecision | None:
        if depth.spread_scaled <= 0:
            return None

        # --- Extract features ---
        qi = self.feature_by_name(features, "queue_imbalance")
        tox_ts = self.feature_by_name(features, "toxicity_timescale_div")
        micro_ratio = self.feature_by_name(features, "microprice_spread_ratio")
        cross_qi = self.feature_by_name(features, "cross_ema_qi")
        depth_vel = self.feature_by_name(features, "depth_velocity_diff")
        adv_mom = self.feature_by_name(features, "adverse_momentum")
        cum_ofi_rev = self.feature_by_name(features, "cum_ofi_revert")
        ofi_asym = self.feature_by_name(features, "ofi_asymmetry")

        # --- 1. Toxicity pause ---
        # If toxicity is extreme, WITHDRAW entirely (don't just widen)
        tox_level = abs(tox_ts)
        if tox_level > self._tox_pause_threshold:
            return None  # no quotes in toxic regime

        # --- 2. Directional signal ---
        # Momentum signals (predict continuation)
        momentum = (
            0.20 * qi + 0.20 * micro_ratio + 0.15 * ofi_asym + 0.15 * cross_qi + 0.10 * depth_vel + 0.05 * adv_mom
        )

        # Contrarian signal (cum_ofi_revert has NEGATIVE IC — predicts reversal)
        # When cum_ofi is extreme positive → price will revert down → we want to sell
        contrarian = -cum_ofi_rev * self._contrarian_weight

        direction = max(-1.0, min(1.0, momentum + contrarian))

        # --- 3. Microprice-centered mid ---
        # Use microprice ratio to adjust fair value estimate
        # micro_ratio ∈ [-1, 1]: positive means fair value is closer to ask
        raw_mid = depth.mid_price_x2 / 2.0
        micro_adjust = micro_ratio * depth.spread_scaled * 0.3
        fair_mid = raw_mid + micro_adjust

        # --- 4. Spread adjustment ---
        tick = self._tick_size
        base_half = self._base_half_spread_ticks * tick

        # Mild widening in elevated (but not paused) toxicity
        tox_multiplier = 1.0 + tox_level * (self._tox_widen_factor - 1.0)
        half_spread = base_half * tox_multiplier

        # --- 5. Directional + inventory skew ---
        dir_skew = direction * self._skew_sensitivity * tick

        # Inventory skew: aggressive unwinding for large positions
        abs_pos = abs(position)
        if abs_pos >= self._max_position - 1:
            # Near limit: strong unwinding pressure
            inv_skew = -position * self._inv_skew_per_lot * 2.0 * tick
        else:
            inv_skew = -position * self._inv_skew_per_lot * tick

        total_skew = dir_skew + inv_skew

        # --- 6. Final quote prices ---
        bid_price = fair_mid - half_spread + total_skew
        ask_price = fair_mid + half_spread + total_skew

        bid_price_int = int(bid_price / tick) * int(tick)
        ask_price_int = int(ask_price / tick + 0.999) * int(tick)

        if bid_price_int >= ask_price_int:
            ask_price_int = bid_price_int + int(tick)

        # --- 7. Position limits + asymmetric qty ---
        bid_qty = self._qty_per_side if position < self._max_position else 0
        ask_qty = self._qty_per_side if position > -self._max_position else 0

        # When near limits, only quote the unwinding side
        if position >= self._max_position - 1:
            bid_qty = 0  # stop buying
        if position <= -(self._max_position - 1):
            ask_qty = 0  # stop selling

        if bid_qty == 0 and ask_qty == 0:
            return None

        return QuoteDecision(
            bid_price=bid_price_int,
            bid_qty=bid_qty,
            ask_price=ask_price_int,
            ask_qty=ask_qty,
        )
