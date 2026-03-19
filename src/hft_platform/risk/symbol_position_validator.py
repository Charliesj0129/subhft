"""Per-symbol position limit validator (WU-09).

Enforces maximum absolute net position per symbol by aggregating
current positions from PositionStore before allowing new orders.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.execution.positions import PositionStore
from hft_platform.risk.validators import RiskValidator

logger = get_logger("symbol_position_validator")


class SymbolPositionLimitValidator(RiskValidator):
    """Reject NEW orders that would breach per-symbol position limits.

    Config resolution:
      1. config["symbol_limits"][symbol]["max_position_lots"]
      2. config.get("default_max_position_lots", 1000)

    CANCEL and AMEND intents are always passed through.
    """

    __slots__ = ("_position_store", "_default_max_lots", "_limit_cache")

    def __init__(
        self,
        config: Dict[str, Any],
        position_store: Optional[PositionStore] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(config, **kwargs)
        self._position_store = position_store
        self._default_max_lots: int = int(config.get("default_max_position_lots", 1000))
        # Cache: symbol -> max_position_lots
        self._limit_cache: Dict[str, int] = {}

    def _get_limit(self, symbol: str) -> int:
        """Return cached per-symbol position limit."""
        limit = self._limit_cache.get(symbol)
        if limit is not None:
            return limit
        symbol_limits = self.config.get("symbol_limits", {})
        sym_cfg = symbol_limits.get(symbol, {})
        limit = int(sym_cfg.get("max_position_lots", self._default_max_lots))
        self._limit_cache[symbol] = limit
        return limit

    def _current_net_qty(self, symbol: str) -> int:
        """Aggregate net_qty across all positions matching *symbol*."""
        if self._position_store is None:
            return 0
        total = 0
        for pos in self._position_store.positions.values():
            if pos.symbol == symbol:
                total += pos.net_qty
        return total

    def check(self, intent: OrderIntent) -> Tuple[bool, str]:
        # Always allow CANCEL and AMEND
        if intent.intent_type in (IntentType.CANCEL, IntentType.AMEND):
            return True, "OK"

        current_net = self._current_net_qty(intent.symbol)
        signed_qty = intent.qty if intent.side == Side.BUY else -intent.qty
        projected = abs(current_net + signed_qty)
        limit = self._get_limit(intent.symbol)

        if projected > limit:
            reason = (
                f"SYMBOL_POSITION_LIMIT: symbol={intent.symbol} "
                f"current_net={current_net} intent={signed_qty} "
                f"projected={projected} limit={limit}"
            )
            logger.warning(
                "Symbol position limit breach",
                symbol=intent.symbol,
                current_net=current_net,
                signed_intent=signed_qty,
                projected=projected,
                limit=limit,
            )
            return False, reason

        return True, "OK"
