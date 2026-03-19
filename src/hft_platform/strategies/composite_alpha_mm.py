"""composite_alpha_mm.py — Composite alpha signal market-making strategy.

Combines multiple Feature Engine signals (OFI, depth imbalance, LOB slope)
into a directional composite signal for inventory-aware quoting.

Signal (v1):
    composite = w_ofi * ofi_l1_ema8 + w_depth * depth_imbalance_ema8_ppm
    slope_diff = slope_ask - slope_bid  [from cached LOB]
    signal = normalize(composite) + slope_weight * slope_diff

Quoting:
    mid = (best_bid + best_ask) / 2  [scaled int]
    half_spread = base_half_spread_ticks * tick_size
    skew = signal_skew + inventory_skew
    bid = mid - half_spread + skew
    ask = mid + half_spread + skew

Hot-path constraints:
- __slots__ on class (Allocator Law)
- prices are scaled int x10000, never float (Precision Law)
- pre-allocated buffers, no heap allocation per event (Allocator Law)
- requires HFT_FEATURE_ENGINE_ENABLED=1 to activate
"""

from __future__ import annotations

import os

import numpy as np
from structlog import get_logger

from hft_platform.contracts.strategy import TIF
from hft_platform.events import BidAskEvent, FeatureUpdateEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("composite_alpha_mm")

# Feature tuple indices — lob_shared_v1 schema_version=1.
_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_MID_PRICE_X2 = 2
_IDX_SPREAD_SCALED = 3
_IDX_OFI_L1_RAW = 8
_IDX_OFI_L1_CUM = 9
_IDX_OFI_L1_EMA8 = 13
_IDX_DEPTH_IMBALANCE_EMA8_PPM = 15

# Warmup: rolling features at indices 13 and 15
_WARMUP_REQUIRED_MASK: int = (1 << _IDX_OFI_L1_EMA8) | (1 << _IDX_DEPTH_IMBALANCE_EMA8_PPM)

_LOB_DEPTH_LEVELS = 10

# Default parameters
_BASE_HALF_SPREAD_TICKS = 2
_INV_SKEW_PER_LOT = 5000  # scaled int per lot of inventory skew
_SIGNAL_THRESHOLD = 0.02
_MAX_POSITION = 50
_QTY = 1
_TICK_SIZE_SCALED = 10000  # 1 tick = 10000 in scaled int

# Alpha weights (sum to 1.0)
_W_OFI = 0.4
_W_DEPTH = 0.3
_W_SLOPE = 0.3

# Sentinel
_EMPTY_LOB: np.ndarray = np.zeros((0, 2), dtype=np.int64)


def _compute_slope(levels: np.ndarray, n_levels: int) -> float:
    """Log-linear OLS slope: OLS(level_index, log(qty+1)).

    Same implementation as lob_shape_strategy._compute_slope.
    """
    n = min(len(levels), n_levels)
    if n < 2:
        return 0.0
    qtys = levels[:n, 1].astype(np.float64, copy=False)
    x = np.arange(1, n + 1, dtype=np.float64)
    y = np.log1p(qtys)
    sx = float(x.sum())
    sy = float(y.sum())
    sxy = float((x * y).sum())
    sx2 = float((x * x).sum())
    denom = n * sx2 - sx * sx
    if denom == 0.0:
        return 0.0
    return float((n * sxy - sx * sy) / denom)


class CompositeAlphaMM(BaseStrategy):
    """Composite alpha signal market-making strategy.

    Combines OFI EMA, depth imbalance EMA, and LOB slope asymmetry into
    a directional signal for inventory-aware quoting.

    on_book_update: caches BidAskEvent per symbol
    on_features: computes composite signal, generates bid/ask quotes

    Requires HFT_FEATURE_ENGINE_ENABLED=1.
    """

    __slots__ = (
        "_w_ofi",
        "_w_depth",
        "_w_slope",
        "_base_half_spread_ticks",
        "_inv_skew_per_lot",
        "_signal_threshold",
        "_max_position",
        "_qty",
        "_tick_size_scaled",
        "_n_levels",
        "_enabled_flag",
        "_lob_cache",
        # EMA state for signal normalization (running mean/var)
        "_signal_ema",
        "_signal_emvar",
        "_ema_alpha",
    )

    def __init__(self, strategy_id: str, **kwargs) -> None:
        super().__init__(strategy_id, **kwargs)

        # Alpha weights
        self._w_ofi: float = float(kwargs.get("w_ofi", _W_OFI))
        self._w_depth: float = float(kwargs.get("w_depth", _W_DEPTH))
        self._w_slope: float = float(kwargs.get("w_slope", _W_SLOPE))

        # Quoting parameters
        self._base_half_spread_ticks: int = int(
            kwargs.get("base_half_spread_ticks", _BASE_HALF_SPREAD_TICKS),
        )
        self._inv_skew_per_lot: int = int(
            kwargs.get("inv_skew_per_lot", _INV_SKEW_PER_LOT),
        )
        self._signal_threshold: float = float(
            kwargs.get("signal_threshold", _SIGNAL_THRESHOLD),
        )
        self._max_position: int = int(kwargs.get("max_position", _MAX_POSITION))
        self._qty: int = int(kwargs.get("qty", _QTY))
        self._tick_size_scaled: int = int(
            kwargs.get("tick_size_scaled", _TICK_SIZE_SCALED),
        )
        self._n_levels: int = int(kwargs.get("n_levels", _LOB_DEPTH_LEVELS))

        # Feature engine gate
        self._enabled_flag: bool = os.getenv(
            "HFT_FEATURE_ENGINE_ENABLED", "0",
        ).lower() in {"1", "true", "yes", "on"}

        # LOB cache: symbol -> (bids_array, asks_array)
        self._lob_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        # Online signal normalization (exponential moving avg/var)
        self._signal_ema: float = 0.0
        self._signal_emvar: float = 1.0
        self._ema_alpha: float = float(kwargs.get("ema_alpha", 0.01))

        # Schema assertion (same pattern as LobShapeStrategy)
        try:
            from hft_platform.feature.registry import (
                build_default_lob_feature_set_v1,
                feature_id_to_index,
            )

            _fs = build_default_lob_feature_set_v1()
            assert feature_id_to_index(_fs, "best_bid") == _IDX_BEST_BID
            assert feature_id_to_index(_fs, "best_ask") == _IDX_BEST_ASK
            assert feature_id_to_index(_fs, "ofi_l1_ema8") == _IDX_OFI_L1_EMA8
            assert (
                feature_id_to_index(_fs, "depth_imbalance_ema8_ppm")
                == _IDX_DEPTH_IMBALANCE_EMA8_PPM
            )
        except ImportError:
            pass

    def on_book_update(self, event: BidAskEvent) -> None:
        """Cache latest LOB snapshot per symbol."""
        if not self._enabled_flag:
            return
        symbol = event.symbol
        if self.symbols and symbol not in self.symbols:
            return
        bids = np.asarray(event.bids, dtype=np.int64)
        asks = np.asarray(event.asks, dtype=np.int64)
        self._lob_cache[symbol] = (bids, asks)

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Compute composite signal and generate bid/ask quotes."""
        if not self._enabled_flag:
            return
        if self.ctx is None:
            return

        symbol = event.symbol
        if self.symbols and symbol not in self.symbols:
            return

        # Wait for warmup
        if (event.warmup_ready_mask & _WARMUP_REQUIRED_MASK) != _WARMUP_REQUIRED_MASK:
            return

        feat = self.ctx.get_feature_tuple(symbol)
        if feat is None or len(feat) < 16:
            return

        best_bid: int = int(feat[_IDX_BEST_BID])
        best_ask: int = int(feat[_IDX_BEST_ASK])
        if best_bid <= 0 or best_ask <= 0:
            return

        spread_scaled: int = int(feat[_IDX_SPREAD_SCALED])
        if spread_scaled <= 0:
            return

        # --- Feature signals ---
        ofi_ema8: int = int(feat[_IDX_OFI_L1_EMA8])
        depth_imb_ema8: int = int(feat[_IDX_DEPTH_IMBALANCE_EMA8_PPM])

        # Normalize OFI and depth imbalance to approximately [-1, 1]
        ofi_norm = float(ofi_ema8) / max(abs(float(ofi_ema8)), 1.0)
        depth_norm = float(depth_imb_ema8) / 10000.0  # ppm scale

        # LOB slope
        cached = self._lob_cache.get(symbol)
        if cached is not None:
            bids, asks = cached
        else:
            bids, asks = _EMPTY_LOB, _EMPTY_LOB
        slope_bid = _compute_slope(bids, self._n_levels)
        slope_ask = _compute_slope(asks, self._n_levels)
        slope_diff = slope_ask - slope_bid

        # --- Composite signal ---
        raw_signal = (
            self._w_ofi * ofi_norm
            + self._w_depth * depth_norm
            + self._w_slope * slope_diff
        )

        # Online normalization via EMA
        alpha = self._ema_alpha
        self._signal_ema = (1 - alpha) * self._signal_ema + alpha * raw_signal
        diff = raw_signal - self._signal_ema
        self._signal_emvar = (1 - alpha) * self._signal_emvar + alpha * diff * diff
        sigma = max(self._signal_emvar**0.5, 1e-8)
        signal = (raw_signal - self._signal_ema) / sigma

        # Clamp signal to [-3, 3]
        signal = max(-3.0, min(3.0, signal))

        # --- Position ---
        pos = self.position(symbol)

        # --- Quoting ---
        mid_x2: int = best_bid + best_ask  # mid_price * 2 in scaled int
        tick = self._tick_size_scaled
        half_spread = self._base_half_spread_ticks * tick

        # Directional skew from signal (in scaled int units)
        signal_skew = int(signal * tick * 0.5)

        # Inventory skew: push quotes to reduce position
        inv_skew = -pos * self._inv_skew_per_lot

        total_skew = signal_skew + inv_skew

        # Compute bid/ask prices
        # mid_x2 / 2 gives mid price in scaled int
        bid_price = (mid_x2 // 2) - half_spread + total_skew
        ask_price = (mid_x2 // 2) + half_spread + total_skew

        # Snap to tick grid
        bid_price = (bid_price // tick) * tick
        ask_price = ((ask_price + tick - 1) // tick) * tick

        # Ensure bid < ask
        if bid_price >= ask_price:
            ask_price = bid_price + tick

        # Position limits
        bid_qty = self._qty if pos < self._max_position else 0
        ask_qty = self._qty if pos > -self._max_position else 0

        if bid_qty == 0 and ask_qty == 0:
            return

        # Place orders
        if bid_qty > 0 and abs(signal) > self._signal_threshold:
            self.buy(symbol, bid_price, bid_qty, TIF.LIMIT)
        if ask_qty > 0 and abs(signal) > self._signal_threshold:
            self.sell(symbol, ask_price, ask_qty, TIF.LIMIT)
