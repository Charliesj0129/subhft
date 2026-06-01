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

    def on_session_end(self, ctx: Any) -> list[OrderIntent]:
        """Return FORCE_FLAT MARKET intent(s) for any non-zero residual position.

        Slice B Task 13 (2026-05-05): wired by StrategyRunner on
        SessionPhase.FORCE_FLAT transitions. Empty list when flat.

        Position semantics:
          net_qty > 0 (long)  → SELL to flatten
          net_qty < 0 (short) → BUY to flatten
          net_qty == 0        → no intent

        The returned intent uses ``price_type="MKT"`` so the order adapter
        treats it as a market order. ``intent_type=IntentType.FORCE_FLAT``
        is recognised by ``StrategyRunner.filter_intents_by_phase`` as an
        always-allowed safety intent during CLOSE_ONLY/FORCE_FLAT phases
        (see strategy/runner.py:1597).
        """
        symbol = self._symbol
        positions = getattr(ctx, "positions", None) or {}
        residual_qty = int(positions.get(symbol, 0))
        if residual_qty == 0:
            return []

        opposite_side = Side.SELL if residual_qty > 0 else Side.BUY

        # Best-effort mid price from L1 snapshot. MARKET intents do not depend
        # on price for execution, but we populate it for telemetry/TCA.
        cur_mid = 0
        get_l1 = getattr(ctx, "get_l1_scaled", None)
        if callable(get_l1):
            l1 = get_l1(symbol)
            if l1 is not None:
                # Tuple shape: (ts, best_bid, best_ask, mid_x2, spread, bd, ad).
                # mid_x2 is mid * 2 (scaled int); divide by 2 for mid.
                try:
                    cur_mid = int(l1[3]) // 2
                except (IndexError, TypeError, ValueError):
                    cur_mid = 0

        return [
            OrderIntent(
                intent_id=self._next_intent_id(),
                strategy_id=self.strategy_id,
                symbol=symbol,
                intent_type=IntentType.FORCE_FLAT,
                side=opposite_side,
                price=cur_mid,
                qty=abs(residual_qty),
                tif=TIF.IOC,
                price_type="MKT",
                reason="session_end_force_flat",
            )
        ]
