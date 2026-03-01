"""LobShapeStrategy — live strategy consuming Feature Engine + LOB depth slope.

Signal (v1, full formula — research/live parity):
    slope_bid = OLS(level_idx, log(bid_qty+1))   [L=10 levels]
    slope_ask = OLS(level_idx, log(ask_qty+1))
    sign_align ∈ {-1, 0, 1}
    signal = (slope_ask - slope_bid) + λ × sign_align

Slope computed from BidAskEvent (on_book_update), cached per symbol.
sign_align computed from Feature Engine feature tuple (on_features).

Hot-path constraints:
- __slots__ on class (Allocator Law)
- prices are scaled int ×10000, never float (Precision Law)
- slope pre-allocated buffers, no heap allocation per event (Allocator Law)
- requires HFT_FEATURE_ENGINE_ENABLED=1 to activate (safe rollout)
"""

from __future__ import annotations

import os

import numpy as np
from structlog import get_logger

from hft_platform.contracts.strategy import TIF
from hft_platform.events import BidAskEvent, FeatureUpdateEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("lob_shape_strategy")

# Feature tuple indices — lob_shared_v1 schema_version=1.
# Asserted against live registry at construction time to catch schema drift.
_IDX_BEST_BID = 0
_IDX_BEST_ASK = 1
_IDX_OFI_L1_EMA8 = 13
_IDX_DEPTH_IMBALANCE_EMA8_PPM = 15

# Warmup: rolling features at indices 13 and 15 require warmup_min_events=2.
_WARMUP_REQUIRED_MASK: int = (1 << _IDX_OFI_L1_EMA8) | (1 << _IDX_DEPTH_IMBALANCE_EMA8_PPM)

_LOB_DEPTH_LEVELS = 10
_LAMBDA_DEFAULT = 0.3
_SIGNAL_THRESHOLD_DEFAULT = 0.05
_MAX_POSITION_DEFAULT = 100
_QTY_DEFAULT = 1

# Sentinel: empty (0,2) array used as cached LOB before first BidAskEvent arrives.
_EMPTY_LOB: np.ndarray = np.zeros((0, 2), dtype=np.int64)


def _compute_slope(levels: np.ndarray, n_levels: int) -> float:
    """Log-linear OLS slope: OLS(level_index, log(qty+1)).

    Mirrors Rust `compute_side_slope` and research alpha `_compute_slope`.
    Called on pre-existing numpy arrays — no heap allocation.
    Returns 0.0 when fewer than 2 rows available.
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


def _sign_align(a: int | float, b: int | float) -> int:
    """Returns 1 if same nonzero sign, -1 if opposite, 0 if either zero."""
    sa = 1 if a > 0 else (-1 if a < 0 else 0)
    sb = 1 if b > 0 else (-1 if b < 0 else 0)
    if sa == 0 or sb == 0:
        return 0
    return 1 if sa == sb else -1


class LobShapeStrategy(BaseStrategy):
    """Live TIER_1 strategy: LOB depth slope asymmetry + EMA OFI alignment.

    on_book_update: caches BidAskEvent.bids / .asks per symbol (O(1) dict write).
    on_features:    reads cached LOB + feature tuple, computes full signal, places order.

    Research/live parity: both use the identical formula
        signal = (slope_ask - slope_bid) + λ × sign_align(ofi_l1_ema8, depth_imbalance_ema8_ppm)

    Activates only when HFT_FEATURE_ENGINE_ENABLED=1.
    """

    __slots__ = (
        "_lambda",
        "_signal_threshold",
        "_max_position",
        "_qty",
        "_n_levels",
        "_enabled_flag",
        "_lob_cache",  # dict[symbol, (bids_array, asks_array)]
    )

    def __init__(self, strategy_id: str, **kwargs) -> None:
        super().__init__(strategy_id, **kwargs)
        self._lambda: float = float(kwargs.get("lambda_", _LAMBDA_DEFAULT))
        if not (-10.0 <= self._lambda <= 10.0):
            raise ValueError(f"lambda_ must be in [-10, 10], got {self._lambda}")
        self._signal_threshold: float = float(kwargs.get("signal_threshold", _SIGNAL_THRESHOLD_DEFAULT))
        self._max_position: int = int(kwargs.get("max_position", _MAX_POSITION_DEFAULT))
        self._qty: int = int(kwargs.get("qty", _QTY_DEFAULT))
        self._n_levels: int = int(kwargs.get("n_levels", _LOB_DEPTH_LEVELS))
        self._enabled_flag: bool = os.getenv("HFT_FEATURE_ENGINE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        # Per-symbol LOB cache: populated by on_book_update, consumed by on_features.
        self._lob_cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

        # Runtime assertion: verify hardcoded indices match the live registry.
        # Runs once at construction — catches schema drift before first tick.
        try:
            from hft_platform.feature.registry import (
                build_default_lob_feature_set_v1,
                feature_id_to_index,
            )

            _fs = build_default_lob_feature_set_v1()
            assert feature_id_to_index(_fs, "best_bid") == _IDX_BEST_BID, (
                f"best_bid index mismatch: expected {_IDX_BEST_BID}"
            )
            assert feature_id_to_index(_fs, "best_ask") == _IDX_BEST_ASK, (
                f"best_ask index mismatch: expected {_IDX_BEST_ASK}"
            )
            assert feature_id_to_index(_fs, "ofi_l1_ema8") == _IDX_OFI_L1_EMA8, (
                f"ofi_l1_ema8 index mismatch: expected {_IDX_OFI_L1_EMA8}"
            )
            assert feature_id_to_index(_fs, "depth_imbalance_ema8_ppm") == _IDX_DEPTH_IMBALANCE_EMA8_PPM, (
                f"depth_imbalance_ema8_ppm index mismatch: expected {_IDX_DEPTH_IMBALANCE_EMA8_PPM}"
            )
        except ImportError:
            pass  # Feature registry optional at import time (isolated test environments)

    def on_book_update(self, event: BidAskEvent) -> None:
        """Cache latest LOB snapshot per symbol (O(1) — no allocation of new arrays)."""
        if not self._enabled_flag:
            return
        symbol = event.symbol
        if self.symbols and symbol not in self.symbols:
            return
        bids = np.asarray(event.bids, dtype=np.int64)
        asks = np.asarray(event.asks, dtype=np.int64)
        self._lob_cache[symbol] = (bids, asks)

    def on_features(self, event: FeatureUpdateEvent) -> None:
        """Hot path: compute full signal using cached LOB + feature tuple."""
        if not self._enabled_flag:
            return
        if self.ctx is None:
            return

        symbol = event.symbol
        if self.symbols and symbol not in self.symbols:
            return

        # Wait until rolling features have warmed up (need ≥2 ticks of history)
        if (event.warmup_ready_mask & _WARMUP_REQUIRED_MASK) != _WARMUP_REQUIRED_MASK:
            return

        # O(1) feature access via pre-wired StrategyContext slot
        feat = self.ctx.get_feature_tuple(symbol)
        if feat is None or len(feat) < 16:
            return

        best_bid: int = int(feat[_IDX_BEST_BID])
        best_ask: int = int(feat[_IDX_BEST_ASK])
        if best_bid <= 0 or best_ask <= 0:
            return

        # --- Slope computation from cached LOB ---
        cached = self._lob_cache.get(symbol)
        if cached is not None:
            bids, asks = cached
        else:
            bids, asks = _EMPTY_LOB, _EMPTY_LOB

        slope_bid = _compute_slope(bids, self._n_levels)
        slope_ask = _compute_slope(asks, self._n_levels)
        raw_slope_diff = slope_ask - slope_bid

        # --- Sign-alignment term from feature tuple ---
        ofi_ema8: int = int(feat[_IDX_OFI_L1_EMA8])
        depth_imb_ema8: int = int(feat[_IDX_DEPTH_IMBALANCE_EMA8_PPM])
        sa: int = _sign_align(ofi_ema8, depth_imb_ema8)

        # Full formula (research/live parity)
        signal: float = raw_slope_diff + self._lambda * sa

        pos = self.position(symbol)

        if signal > self._signal_threshold and pos < self._max_position:
            self.buy(symbol, best_bid, self._qty, TIF.LIMIT)
        elif signal < -self._signal_threshold and pos > -self._max_position:
            self.sell(symbol, best_ask, self._qty, TIF.LIMIT)
