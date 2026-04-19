"""Bridge: MakerEngine-style strategies (on_tick) -> BaseStrategy (handle_event).

Translates PostQuote/CancelQuote/Hold Actions into OrderIntent lists,
allowing existing MakerEngine strategies to run inside HftBacktestAdapter
without being rewritten.

New maker strategies should implement BaseStrategy directly; this bridge
is for backward compatibility only.

Key translations:
- PostQuote.side str ("buy"/"sell") -> Side enum (BUY/SELL)
- CancelQuote (side only) -> OrderIntent(CANCEL) with tracked target_order_id per side
- Hold -> []
- TIF.LIMIT used (TIF enum has no GTC)
"""

from __future__ import annotations

from typing import Any, Protocol

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.strategy.base import BaseStrategy


class MakerStrategyProtocol(Protocol):
    """Structural type of a MakerEngine-style strategy."""

    def on_tick(self, event: Any) -> Any: ...


def _side_from_str(side_str: str) -> Side:
    """Convert MakerEngine str side to Side enum."""
    s = side_str.lower()
    if s == "buy":
        return Side.BUY
    if s == "sell":
        return Side.SELL
    raise ValueError(f"Unknown side string: {side_str!r}")


class MakerStrategyBridge(BaseStrategy):
    """Wraps a MakerEngine-style strategy for HftBacktestAdapter.

    Tracks one active order per side so CancelQuote(side) can reference
    the correct target_order_id.
    """

    def __init__(
        self,
        inner: MakerStrategyProtocol,
        strategy_id: str = "maker_bridge",
        symbol: str = "",
    ) -> None:
        super().__init__(strategy_id=strategy_id)
        self._inner = inner
        self._symbol = symbol
        self._intent_counter: int = 0
        # Track active order IDs per side for CancelQuote translation
        self._active_by_side: dict[Side, str] = {}

    def _next_intent_id(self) -> int:
        self._intent_counter += 1
        return self._intent_counter

    def handle_event(self, ctx: Any, event: Any) -> list[OrderIntent]:
        """Translate one on_tick Action to OrderIntent list.

        ctx is the StrategyContext (unused by bridge, passed through by framework).
        event is whatever the MakerEngine strategy expects.
        """
        # Lazy import so this module does not hard-require MakerEngine at import time
        from research.backtest.maker_engine import (  # noqa: PLC0415
            CancelQuote,
            Hold,
            PostQuote,
        )

        action = self._inner.on_tick(event)
        symbol = getattr(event, "symbol", None) or self._symbol

        if isinstance(action, PostQuote):
            side = _side_from_str(action.side)
            intent_id = self._next_intent_id()
            # Track active order id for this side (str form used as target_order_id placeholder)
            self._active_by_side[side] = f"{self.strategy_id}-{intent_id}"
            return [
                OrderIntent(
                    intent_id=intent_id,
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    intent_type=IntentType.NEW,
                    side=side,
                    price=action.price,
                    qty=action.qty,
                    tif=TIF.LIMIT,
                )
            ]

        if isinstance(action, CancelQuote):
            side = _side_from_str(action.side)
            target = self._active_by_side.pop(side, None)
            return [
                OrderIntent(
                    intent_id=self._next_intent_id(),
                    strategy_id=self.strategy_id,
                    symbol=symbol,
                    intent_type=IntentType.CANCEL,
                    side=side,
                    price=0,  # CANCEL intents ignore price/qty
                    qty=0,
                    tif=TIF.LIMIT,
                    target_order_id=target,
                )
            ]

        if isinstance(action, Hold):
            return []

        raise TypeError(f"MakerStrategyBridge: unknown action type {type(action).__name__}")
