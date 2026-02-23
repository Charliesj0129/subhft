"""RLStrategyAdapter — bridge from RL research models to the production BaseStrategy SDK.

Wires :class:`~research.rl.alpha_adapter.RLAlphaAdapter` into the event-driven
:class:`~hft_platform.strategy.base.BaseStrategy` interface so that trained RL
policies can be evaluated through the live feed pipeline without modification.

Usage
-----
::

    from research.rl.alpha_adapter import RLAlphaAdapter, RLAlphaConfig
    from research.rl.rl_strategy_adapter import RLStrategyAdapter

    config = RLAlphaConfig(
        alpha_id="ppo_v5",
        feature_fields=("imbalance", "spread_scaled", "price", "volume"),
        model_path="research/rl/ppo_v5.onnx",
    )
    adapter = RLAlphaAdapter(config)
    strategy = RLStrategyAdapter("rl_ppo_v5", adapter=adapter, symbols={"TXFF4"})
"""
from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent
from hft_platform.strategy.base import BaseStrategy

logger = get_logger("rl_strategy_adapter")


class RLStrategyAdapter(BaseStrategy):
    """Adapts an :class:`RLAlphaAdapter` to the :class:`BaseStrategy` event system.

    The adapter updates the RL model on every :meth:`on_stats` event (the
    richest LOB-derived event) and optionally on :meth:`on_tick` for
    additional flow features.  The resulting signal drives execution through
    the standard :meth:`~BaseStrategy.buy`/:meth:`~BaseStrategy.sell` helpers.

    Args:
        strategy_id:   Unique strategy identifier.
        adapter:       Trained :class:`RLAlphaAdapter` instance.
        signal_threshold: Minimum absolute signal to generate an order
                          (default ``0.3``).
        max_pos:       Maximum position size (default ``5``).
        lot_size:      Order lot size (default ``1``).
        **kwargs:      Forwarded to :class:`BaseStrategy` (e.g. ``symbols``).
    """

    def __init__(
        self,
        strategy_id: str,
        *,
        adapter: Any,
        signal_threshold: float = 0.3,
        max_pos: int = 5,
        lot_size: int = 1,
        **kwargs: Any,
    ) -> None:
        super().__init__(strategy_id, **kwargs)
        self._rl_adapter = adapter
        self._signal_threshold = float(signal_threshold)
        self._max_pos = int(max_pos)
        self._lot_size = int(lot_size)
        self._last_signal: float = 0.0
        self._best_bid: int = 0
        self._best_ask: int = 0
        logger.info(
            "RLStrategyAdapter initialized",
            strategy_id=strategy_id,
            alpha_id=getattr(getattr(adapter, "_config", None), "alpha_id", "unknown"),
            threshold=signal_threshold,
        )

    # ── Event handlers ─────────────────────────────────────────────────────

    def on_stats(self, event: LOBStatsEvent) -> None:
        """Primary hook — LOBStatsEvent carries all LOB-derived features."""
        self._best_bid = int(event.best_bid or 0)
        self._best_ask = int(event.best_ask or 0)

        tick_dict = {
            "imbalance": float(getattr(event, "imbalance", 0.0) or 0.0),
            "spread_scaled": int(event.spread_scaled or 0),
            "bid_depth": int(event.bid_depth or 0),
            "ask_depth": int(event.ask_depth or 0),
            "best_bid": self._best_bid,
            "best_ask": self._best_ask,
            "mid_price_x2": int(event.mid_price_x2 or 0),
            "symbol": event.symbol,
            "ts": int(getattr(event, "ts", 0) or 0),
        }
        try:
            signal = float(self._rl_adapter.update(**tick_dict))
        except Exception as exc:
            logger.warning("RL adapter update failed", error=str(exc))
            return

        self._last_signal = signal
        self._execute_signal(event.symbol, signal)

    def on_tick(self, event: TickEvent) -> None:
        """Secondary hook — enriches the RL model with trade flow features."""
        tick_dict = {
            "price": int(event.price or 0),
            "volume": int(event.volume or 0),
            "total_volume": int(getattr(event, "total_volume", 0) or 0),
            "bid_side_total_vol": int(getattr(event, "bid_side_total_vol", 0) or 0),
            "ask_side_total_vol": int(getattr(event, "ask_side_total_vol", 0) or 0),
            "symbol": event.symbol,
            "ts": int(getattr(event.meta, "source_ts", 0) or 0),
        }
        try:
            self._rl_adapter.update(**tick_dict)
        except Exception as exc:
            logger.warning("RL adapter tick update failed", error=str(exc))

    def on_book_update(self, event: BidAskEvent) -> None:
        """Update BBO cache from incremental LOB updates."""
        if event.bids:
            best = max((int(p) for p, _ in event.bids), default=0)
            if best > 0:
                self._best_bid = best
        if event.asks:
            best = min((int(p) for p, _ in event.asks), default=0)
            if best > 0:
                self._best_ask = best

    # ── Execution ──────────────────────────────────────────────────────────

    def _execute_signal(self, symbol: str, signal: float) -> None:
        """Translate RL signal → buy/sell/hold decision."""
        threshold = self._signal_threshold
        pos = self.position(symbol)
        max_pos = self._max_pos
        qty = self._lot_size

        if signal > threshold and pos < max_pos and self._best_bid > 0:
            self.buy(symbol, self._best_bid, qty)
        elif signal < -threshold and pos > -max_pos and self._best_ask > 0:
            self.sell(symbol, self._best_ask, qty)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def reset_alpha(self) -> None:
        """Reset the RL adapter's internal state (call between sessions)."""
        try:
            self._rl_adapter.reset()
        except Exception:
            pass
        self._last_signal = 0.0
        self._best_bid = 0
        self._best_ask = 0

    @property
    def last_signal(self) -> float:
        """Most recent RL signal value."""
        return self._last_signal
