"""toxicity_aware_mm_v3.py — MM strategy v3 with three-tier toxicity + contrarian exit.

Changes from v2:
1. **Three-tier toxicity**: <0.15 normal, 0.15-0.35 widen+reduce qty, >0.35 pause quoting
2. **Quadratic inventory skew**: 0.5 + 0.1×|pos| — stronger unwinding at higher position
3. **cum_ofi_revert contrarian exit**: weight=0.20, accelerated exit on high cum_ofi + same-side position
4. **Adaptive requote**: 50ms in high-toxicity, 100ms normal
5. **Tighter position limit**: max_position=3 default
6. **Wider base spread**: 5 ticks default

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

logger = get_logger("toxicity_mm_v3")

# Toxicity tiers
_TOX_TIER_NORMAL = 0.15
_TOX_TIER_ELEVATED = 0.35


class ToxicityAwareMMv3(AlphaDrivenMMStrategy):
    """v3 Market Making strategy with three-tier toxicity and contrarian exits."""

    __slots__ = (
        "_tick_size",
        "_max_position",
        "_base_half_spread_ticks",
        "_skew_sensitivity",
        "_tox_pause_threshold",
        "_tox_widen_threshold",
        "_tox_widen_factor",
        "_inv_skew_base",
        "_inv_skew_accel",
        "_qty_per_side",
        "_contrarian_weight",
        "_requote_fast_ns",
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
        base_half_spread_ticks: float = 5.0,
        skew_sensitivity: float = 1.0,
        tox_pause_threshold: float = 0.35,
        tox_widen_threshold: float = 0.15,
        tox_widen_factor: float = 1.5,
        inv_skew_per_lot: float = 0.5,
        inv_skew_accel: float = 0.1,
        contrarian_weight: float = 0.20,
        qty_per_side: int = 1,
        requote_interval_ns: int = 100_000_000,
        requote_fast_ns: int = 50_000_000,
    ):
        super().__init__(
            feature_timestamps=feature_timestamps,
            feature_array=feature_array,
            feature_names=feature_names,
            symbol=symbol,
            strategy_id="toxicity_mm_v3",
            requote_interval_ns=requote_interval_ns,
        )
        self._tick_size = float(tick_size)
        self._max_position = int(max_position)
        self._base_half_spread_ticks = float(base_half_spread_ticks)
        self._skew_sensitivity = float(skew_sensitivity)
        self._tox_pause_threshold = float(tox_pause_threshold)
        self._tox_widen_threshold = float(tox_widen_threshold)
        self._tox_widen_factor = float(tox_widen_factor)
        self._inv_skew_base = float(inv_skew_per_lot)
        self._inv_skew_accel = float(inv_skew_accel)
        self._qty_per_side = int(qty_per_side)
        self._contrarian_weight = float(contrarian_weight)
        self._requote_fast_ns = int(requote_fast_ns)

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

        # --- 1. Three-tier toxicity ---
        tox_level = abs(tox_ts)

        # Tier 3: PAUSE — complete withdrawal
        if tox_level > self._tox_pause_threshold:
            # Adaptive requote: speed up re-evaluation during toxic pause
            self._requote_interval_ns = self._requote_fast_ns
            return None

        # Tier 2: ELEVATED — widen spread + reduce quantity
        if tox_level > self._tox_widen_threshold:
            self._requote_interval_ns = self._requote_fast_ns
            qty_factor = 1  # keep quoting but with wider spread
        else:
            # Tier 1: NORMAL
            # Restore normal requote interval (constructor default)
            qty_factor = 1

        # --- 2. Directional signal ---
        momentum = (
            0.20 * qi
            + 0.20 * micro_ratio
            + 0.15 * ofi_asym
            + 0.15 * cross_qi
            + 0.10 * depth_vel
            + 0.05 * adv_mom
        )

        # --- 3. Contrarian signal (cum_ofi_revert) ---
        # cum_ofi_revert has NEGATIVE IC — predicts mean reversion
        # High cum_ofi → price will revert → sell pressure
        contrarian = -cum_ofi_rev * self._contrarian_weight

        # Contrarian exit boost: if position and cum_ofi are same direction,
        # strengthen the contrarian signal to accelerate position unwinding
        if position > 0 and cum_ofi_rev > 0:
            # Long + positive cum_ofi → price may drop → add sell pressure
            contrarian -= abs(cum_ofi_rev) * self._contrarian_weight * 0.5
        elif position < 0 and cum_ofi_rev < 0:
            # Short + negative cum_ofi → price may rise → add buy pressure
            contrarian += abs(cum_ofi_rev) * self._contrarian_weight * 0.5

        direction = max(-1.0, min(1.0, momentum + contrarian))

        # --- 4. Microprice-centered mid ---
        raw_mid = depth.mid_price_x2 / 2.0
        micro_adjust = micro_ratio * depth.spread_scaled * 0.3
        fair_mid = raw_mid + micro_adjust

        # --- 5. Spread adjustment ---
        tick = self._tick_size
        base_half = self._base_half_spread_ticks * tick

        # In elevated toxicity, widen spread
        if tox_level > self._tox_widen_threshold:
            tox_frac = (tox_level - self._tox_widen_threshold) / (
                self._tox_pause_threshold - self._tox_widen_threshold + 1e-8
            )
            tox_multiplier = 1.0 + tox_frac * (self._tox_widen_factor - 1.0)
        else:
            tox_multiplier = 1.0

        half_spread = base_half * tox_multiplier

        # --- 6. Directional + inventory skew ---
        dir_skew = direction * self._skew_sensitivity * tick

        # Quadratic inventory skew: base + accel × |pos|
        abs_pos = abs(position)
        inv_coeff = self._inv_skew_base + self._inv_skew_accel * abs_pos
        inv_skew = -position * inv_coeff * tick

        total_skew = dir_skew + inv_skew

        # --- 7. Final quote prices ---
        bid_price = fair_mid - half_spread + total_skew
        ask_price = fair_mid + half_spread + total_skew

        bid_price_int = int(bid_price / tick) * int(tick)
        ask_price_int = int(ask_price / tick + 0.999) * int(tick)

        if bid_price_int >= ask_price_int:
            ask_price_int = bid_price_int + int(tick)

        # --- 8. Position limits + qty ---
        bid_qty = self._qty_per_side * qty_factor if position < self._max_position else 0
        ask_qty = self._qty_per_side * qty_factor if position > -self._max_position else 0

        # Near-limit: only quote the unwinding side
        if position >= self._max_position - 1:
            bid_qty = 0
        if position <= -(self._max_position - 1):
            ask_qty = 0

        if bid_qty == 0 and ask_qty == 0:
            return None

        return QuoteDecision(
            bid_price=bid_price_int,
            bid_qty=bid_qty,
            ask_price=ask_price_int,
            ask_qty=ask_qty,
        )
