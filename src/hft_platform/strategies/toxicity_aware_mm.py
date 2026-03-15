"""toxicity_aware_mm.py — Concrete alpha-driven Market Making strategy.

Uses 6 pre-computed LOB features to dynamically adjust:
1. Quote placement (skew based on directional signal)
2. Spread width (widen in toxic regimes, tighten in safe)
3. Inventory management (asymmetric quoting based on position)

Feature set (from large-scale exploration, all IC_IR > 1.5):
- queue_imbalance:        IC=+0.097, slow decay, baseline directional
- toxicity_timescale_div: IC=+0.085, medium decay, toxicity detection
- microprice_spread_ratio: IC=+0.109, microprice adjustment signal
- cross_ema_qi:           IC=+0.077, IR=2.00, fast-slow QI crossover
- depth_velocity_diff:    IC=+0.071, IR=2.19, depth change velocity
- adverse_momentum:       IC=+0.058, IR=2.00, hidden alpha detection

Usage::

    from research.backtest.feature_precompute import precompute_all_mm_features, save_precomputed_features
    data = np.load("research/data/raw/txfc6/TXFC6_all_l1.npy")
    ts, feats, names = precompute_all_mm_features(data)

    strategy = ToxicityAwareMM(
        feature_timestamps=ts,
        feature_array=feats,
        feature_names=names,
        symbol="TXFC6",
        tick_size=1,      # 1 point for TXF
        max_position=5,
    )
"""

from __future__ import annotations

import numpy as np
from structlog import get_logger

from hft_platform.strategies.alpha_driven_mm import (
    AlphaDrivenMMStrategy,
    DepthInfo,
    QuoteDecision,
)

logger = get_logger("toxicity_aware_mm")


class ToxicityAwareMM(AlphaDrivenMMStrategy):
    """Toxicity-aware Market Making strategy.

    Adjusts quotes based on:
    - **Direction**: weighted signal from QI + cross_ema_qi + microprice_ratio
    - **Toxicity**: toxicity_timescale_div gates spread widening
    - **Momentum**: adverse_momentum and depth_velocity_diff for timing
    - **Inventory**: skew quotes to reduce position toward zero
    """

    __slots__ = (
        "_tick_size",
        "_max_position",
        "_base_half_spread_ticks",
        "_skew_sensitivity",
        "_tox_widen_factor",
        "_inv_skew_per_lot",
        "_qty_per_side",
    )

    def __init__(
        self,
        *,
        feature_timestamps: np.ndarray,
        feature_array: np.ndarray,
        feature_names: list[str],
        symbol: str,
        tick_size: float = 1.0,
        max_position: int = 5,
        base_half_spread_ticks: float = 2.0,
        skew_sensitivity: float = 1.5,
        tox_widen_factor: float = 2.0,
        inv_skew_per_lot: float = 0.3,
        qty_per_side: int = 1,
        requote_interval_ns: int = 100_000_000,
    ):
        super().__init__(
            feature_timestamps=feature_timestamps,
            feature_array=feature_array,
            feature_names=feature_names,
            symbol=symbol,
            strategy_id="toxicity_aware_mm",
            requote_interval_ns=requote_interval_ns,
        )
        self._tick_size = float(tick_size)
        self._max_position = int(max_position)
        self._base_half_spread_ticks = float(base_half_spread_ticks)
        self._skew_sensitivity = float(skew_sensitivity)
        self._tox_widen_factor = float(tox_widen_factor)
        self._inv_skew_per_lot = float(inv_skew_per_lot)
        self._qty_per_side = int(qty_per_side)

    def compute_quotes(
        self,
        depth: DepthInfo,
        features: np.ndarray,
        position: int,
    ) -> QuoteDecision | None:
        """Compute bid/ask quotes using alpha features + toxicity gating.

        Strategy logic:
        1. Compute directional signal (weighted sum of directional alphas)
        2. Detect toxicity regime (toxicity_timescale_div)
        3. Adjust spread: widen in toxic regime, tighten in safe
        4. Skew quotes by directional signal + inventory
        5. Cap position within max_position
        """
        if depth.spread_scaled <= 0:
            return None

        # --- Extract features ---
        qi = self.feature_by_name(features, "queue_imbalance")
        tox_ts = self.feature_by_name(features, "toxicity_timescale_div")
        micro_ratio = self.feature_by_name(features, "microprice_spread_ratio")
        cross_qi = self.feature_by_name(features, "cross_ema_qi")
        depth_vel = self.feature_by_name(features, "depth_velocity_diff")
        adv_mom = self.feature_by_name(features, "adverse_momentum")
        ofi_asym = self.feature_by_name(features, "ofi_asymmetry")

        # --- 1. Directional signal (weighted, [-1, 1] range) ---
        # Weights based on IC × IR product from exploration
        direction = (
            0.20 * qi + 0.20 * micro_ratio + 0.20 * ofi_asym + 0.15 * cross_qi + 0.15 * depth_vel + 0.10 * adv_mom
        )
        direction = max(-1.0, min(1.0, direction))

        # --- 2. Toxicity regime ---
        # tox_ts > 0.1 = toxic (informed flow), tox_ts < -0.1 = reverse toxicity
        tox_level = abs(tox_ts)

        # --- 3. Spread adjustment ---
        mid = depth.mid_price_x2 / 2.0
        base_half = self._base_half_spread_ticks * self._tick_size

        # Widen in toxic regime (linear scaling up to tox_widen_factor)
        tox_multiplier = 1.0 + tox_level * (self._tox_widen_factor - 1.0)
        half_spread = base_half * tox_multiplier

        # --- 4. Skew ---
        # Directional skew: shift mid toward predicted direction
        dir_skew = direction * self._skew_sensitivity * self._tick_size

        # Inventory skew: push quotes to reduce position
        inv_skew = -position * self._inv_skew_per_lot * self._tick_size

        total_skew = dir_skew + inv_skew

        # --- 5. Final quote prices ---
        bid_price = mid - half_spread + total_skew
        ask_price = mid + half_spread + total_skew

        # Snap to tick grid (floor bid, ceil ask)
        tick = self._tick_size
        bid_price_int = int(bid_price / tick) * int(tick)
        ask_price_int = int(ask_price / tick + 0.999) * int(tick)

        # Ensure bid < ask
        if bid_price_int >= ask_price_int:
            ask_price_int = bid_price_int + int(tick)

        # --- 6. Position limits ---
        bid_qty = self._qty_per_side if position < self._max_position else 0
        ask_qty = self._qty_per_side if position > -self._max_position else 0

        # Skip quoting if both sides blocked
        if bid_qty == 0 and ask_qty == 0:
            return None

        return QuoteDecision(
            bid_price=bid_price_int,
            bid_qty=bid_qty,
            ask_price=ask_price_int,
            ask_qty=ask_qty,
        )
