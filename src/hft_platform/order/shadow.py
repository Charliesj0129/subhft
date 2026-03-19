"""Shadow Order Mode - logs orders without sending to broker."""
import os

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.core import timebase

logger = get_logger("order.shadow")


class ShadowOrderSink:
    """Intercepts orders for shadow logging without broker execution."""

    __slots__ = ("_enabled", "_counter")

    def __init__(self, enabled: bool | None = None):
        if enabled is not None:
            self._enabled = enabled
        else:
            self._enabled = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1"
        self._counter = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def counter(self) -> int:
        return self._counter

    def intercept(self, intent: OrderIntent) -> dict:
        """Log the order and return a record dict."""
        self._counter += 1
        record = {
            "ts_ns": timebase.now_ns(),
            "strategy_id": intent.strategy_id,
            "symbol": intent.symbol,
            "side": str(intent.side.name if hasattr(intent.side, "name") else intent.side),
            "price": intent.price,
            "qty": intent.qty,
            "intent_type": str(
                intent.intent_type.name if hasattr(intent.intent_type, "name") else intent.intent_type
            ),
            "intent_id": str(intent.intent_id),
            "shadow": True,
        }
        logger.info("Shadow order captured", **record)
        return record
