"""Position flattener — generates FORCE_FLAT intents to close all open positions.

Used by the autonomy control plane when entering HALT or by the operator
via the ``hft ops flatten`` CLI command.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.core import timebase

logger = get_logger("ops.position_flattener")


class FlattenResult:
    """Outcome of a flatten attempt for a single symbol."""

    __slots__ = ("symbol", "qty", "success", "error", "intent")

    def __init__(
        self,
        symbol: str,
        qty: int,
        success: bool,
        error: str = "",
        intent: OrderIntent | None = None,
    ) -> None:
        self.symbol = symbol
        self.qty = qty
        self.success = success
        self.error = error
        self.intent = intent

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": self.qty,
            "success": self.success,
            "error": self.error,
        }


class PositionFlattener:
    """Generates FORCE_FLAT OrderIntents for all open positions.

    The flattener reads positions from a position store and emits
    FORCE_FLAT intents that bypass normal risk checks.
    """

    __slots__ = ("_strategy_id", "_next_intent_id")

    def __init__(self, strategy_id: str = "FLATTENER") -> None:
        self._strategy_id = strategy_id
        self._next_intent_id: int = 0

    def flatten_all(self, positions: dict[str, int]) -> list[FlattenResult]:
        """Generate FORCE_FLAT intents for all non-zero positions.

        Args:
            positions: Mapping of symbol -> net quantity (positive=long, negative=short).

        Returns:
            List of FlattenResult, one per symbol with non-zero position.
        """
        results: list[FlattenResult] = []
        for symbol, net_qty in positions.items():
            if net_qty == 0:
                continue
            try:
                intent = self._make_intent(symbol, net_qty)
                results.append(
                    FlattenResult(
                        symbol=symbol,
                        qty=abs(net_qty),
                        success=True,
                        intent=intent,
                    )
                )
                logger.info(
                    "flatten_intent_generated",
                    symbol=symbol,
                    side="SELL" if net_qty > 0 else "BUY",
                    qty=abs(net_qty),
                )
            except Exception as exc:
                results.append(
                    FlattenResult(
                        symbol=symbol,
                        qty=abs(net_qty),
                        success=False,
                        error=str(exc),
                    )
                )
                logger.error("flatten_intent_failed", symbol=symbol, error=str(exc))
        return results

    def _make_intent(self, symbol: str, net_qty: int) -> OrderIntent:
        self._next_intent_id += 1
        side = Side.SELL if net_qty > 0 else Side.BUY
        return OrderIntent(
            intent_id=self._next_intent_id,
            strategy_id=self._strategy_id,
            symbol=symbol,
            intent_type=IntentType.FORCE_FLAT,
            side=side,
            price=0,
            qty=abs(net_qty),
            tif=TIF.IOC,
            timestamp_ns=timebase.now_ns(),
            reason="position_flatten",
        )
