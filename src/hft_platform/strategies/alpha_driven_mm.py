"""alpha_driven_mm.py — BaseStrategy for alpha-driven Market Making.

Uses pre-computed alpha features (numpy arrays) as decision inputs.
Features are looked up by timestamp with O(1) amortized cost (monotonic index).

Subclasses implement:
    - compute_quotes(depth_info, features, position) → QuoteDecision
    - on_fill_update(fill_event) → None  (optional inventory tracking)

Usage with HftBacktestAdapter (elapse mode):
    features = load_precomputed_features("alpha_features.npz")
    strategy = MyMMStrategy(
        feature_timestamps=features.timestamps,
        feature_array=features.values,
        feature_names=features.names,
        symbol="2330",
    )
    adapter = HftBacktestAdapter(
        strategy=strategy,
        tick_mode="elapse",
        elapse_ns=100_000_000,  # 100ms
        ...
    )
    adapter.run()
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass

import numpy as np
from structlog import get_logger

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.events import LOBStatsEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("alpha_driven_mm")


@dataclass(slots=True, frozen=True)
class DepthInfo:
    """Immutable L1 depth snapshot for MM decision-making."""

    best_bid: int  # scaled integer (x10000)
    best_ask: int
    bid_depth: int
    ask_depth: int
    mid_price_x2: int  # best_bid + best_ask (no division)
    spread_scaled: int  # best_ask - best_bid
    imbalance: float  # [-1, 1]
    ts_ns: int


@dataclass(slots=True, frozen=True)
class QuoteDecision:
    """Immutable output from compute_quotes().

    Prices are scaled integers (x10000).  Set qty=0 to skip a side.
    """

    bid_price: int
    bid_qty: int
    ask_price: int
    ask_qty: int


class AlphaDrivenMMStrategy(BaseStrategy):
    """Base class for alpha-feature-driven Market Making strategies.

    Pre-computed alpha features are passed as numpy arrays at construction
    time.  During backtest, features are looked up by timestamp with O(1)
    amortized cost (timestamps are monotonically increasing).

    Subclasses MUST implement ``compute_quotes()``.
    Subclasses MAY override ``on_fill_update()`` for inventory tracking.
    """

    __slots__ = (
        "_feature_ts",
        "_feature_arr",
        "_feature_names",
        "_feat_idx",
        "_symbol",
        "_next_order_id",
        "_bid_order_id",
        "_ask_order_id",
        "_requote_interval_ns",
        "_last_requote_ts",
    )

    def __init__(
        self,
        *,
        feature_timestamps: np.ndarray,
        feature_array: np.ndarray,
        feature_names: list[str],
        symbol: str,
        strategy_id: str = "alpha_mm",
        requote_interval_ns: int = 100_000_000,  # 100ms default
    ):
        super().__init__(strategy_id=strategy_id, subscribe_symbols=[symbol])
        self._feature_ts = np.asarray(feature_timestamps, dtype=np.int64)
        self._feature_arr = np.asarray(feature_array, dtype=np.float64)
        self._feature_names = list(feature_names)
        self._feat_idx = 0
        self._symbol = symbol
        self._next_order_id = 1
        self._bid_order_id = 0
        self._ask_order_id = 0
        self._requote_interval_ns = int(requote_interval_ns)
        self._last_requote_ts = 0

    # ------------------------------------------------------------------
    # Feature lookup — O(1) amortized (monotonic advance)
    # ------------------------------------------------------------------

    def _lookup_features(self, ts_ns: int) -> np.ndarray:
        """Return the feature row closest to (but not after) ts_ns."""
        idx = self._feat_idx
        ts_arr = self._feature_ts
        limit = len(ts_arr) - 1
        while idx < limit and ts_arr[idx + 1] <= ts_ns:
            idx += 1
        self._feat_idx = idx
        return self._feature_arr[idx]

    def feature_by_name(self, features: np.ndarray, name: str) -> float:
        """Extract a named feature from the feature row."""
        try:
            col = self._feature_names.index(name)
        except ValueError:
            return 0.0
        return float(features[col])

    # ------------------------------------------------------------------
    # Abstract: subclasses implement MM logic here
    # ------------------------------------------------------------------

    @abstractmethod
    def compute_quotes(
        self,
        depth: DepthInfo,
        features: np.ndarray,
        position: int,
    ) -> QuoteDecision | None:
        """Compute bid/ask quotes given current depth, alpha features, and position.

        Args:
            depth: Immutable L1 depth snapshot.
            features: 1-D float64 array of pre-computed alpha values at current ts.
            position: Current net position (signed integer).

        Returns:
            QuoteDecision with bid/ask prices and quantities, or None to skip.
        """
        ...

    # ------------------------------------------------------------------
    # BaseStrategy event dispatch
    # ------------------------------------------------------------------

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Respond to LOBStatsEvent: look up features, compute quotes, manage orders."""
        if not self.ctx:
            return

        ts_ns = int(event.ts)
        pos = self.position(self._symbol)

        depth = DepthInfo(
            best_bid=int(event.best_bid),
            best_ask=int(event.best_ask),
            bid_depth=int(event.bid_depth),
            ask_depth=int(event.ask_depth),
            mid_price_x2=int(event.mid_price_x2)
            if event.mid_price_x2 is not None
            else int(event.best_bid) + int(event.best_ask),
            spread_scaled=int(event.spread_scaled)
            if event.spread_scaled is not None
            else int(event.best_ask) - int(event.best_bid),
            imbalance=float(event.imbalance),
            ts_ns=ts_ns,
        )

        features = self._lookup_features(ts_ns)
        decision = self.compute_quotes(depth, features, pos)

        if decision is None:
            return

        # Requote interval gate
        if ts_ns - self._last_requote_ts < self._requote_interval_ns:
            return
        self._last_requote_ts = ts_ns

        # Cancel stale orders
        if self._bid_order_id > 0:
            self.cancel(self._symbol, str(self._bid_order_id))
            self._bid_order_id = 0
        if self._ask_order_id > 0:
            self.cancel(self._symbol, str(self._ask_order_id))
            self._ask_order_id = 0

        # Place new quotes — track intent_id as hbt order_id
        if decision.bid_qty > 0 and decision.bid_price > 0:
            self._place(self._symbol, Side.BUY, decision.bid_price, decision.bid_qty, TIF.LIMIT)
            # Last appended intent has the order_id
            if self._generated_intents:
                self._bid_order_id = self._generated_intents[-1].intent_id

        if decision.ask_qty > 0 and decision.ask_price > 0:
            self._place(self._symbol, Side.SELL, decision.ask_price, decision.ask_qty, TIF.LIMIT)
            if self._generated_intents:
                self._ask_order_id = self._generated_intents[-1].intent_id
