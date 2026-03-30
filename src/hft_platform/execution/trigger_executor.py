"""TriggerExecutor — strategy-side helper for price-triggered order firing.

Called by strategy.handle_event() on each tick. NOT mounted in MarketDataService.
All prices are scaled integers (x10000) per the Precision Law.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hft_platform.contracts.strategy import OrderIntent

__all__ = ["TriggerCondition", "TriggerExecutor"]


@dataclass(frozen=True)
class TriggerCondition:
    """Immutable price condition for a trigger.

    direction: "GE" (price >= threshold) or "LE" (price <= threshold)
    threshold: scaled int (x10000)
    """

    __slots__ = ("direction", "threshold")

    direction: str  # "GE" | "LE"
    threshold: int  # scaled int x10000

    @classmethod
    def GE(cls, threshold: int) -> "TriggerCondition":
        """Fire when price >= threshold."""
        return cls(direction="GE", threshold=threshold)

    @classmethod
    def LE(cls, threshold: int) -> "TriggerCondition":
        """Fire when price <= threshold."""
        return cls(direction="LE", threshold=threshold)

    def is_met(self, price: int) -> bool:
        """Return True if the condition is satisfied by the given price."""
        if self.direction == "GE":
            return price >= self.threshold
        return price <= self.threshold


class TriggerExecutor:
    """Strategy-side price-triggered order executor.

    Maintains a bounded registry of (symbol, condition, intent) triples.
    Each trigger is one-shot: it fires at most once, then is removed.

    Usage::

        te = TriggerExecutor()
        tid = te.register("TXFD6", TriggerCondition.LE(195_000_000), intent)
        fired = te.on_tick("TXFD6", latest_price)  # returns list[OrderIntent]
    """

    __slots__ = ("_triggers", "_max_triggers")

    _DEFAULT_MAX = 100

    def __init__(self, max_triggers: int = _DEFAULT_MAX) -> None:
        # _triggers maps trigger_id -> (symbol, condition, intent)
        self._triggers: dict[str, tuple[str, TriggerCondition, "OrderIntent"]] = {}
        self._max_triggers: int = max_triggers

    def register(
        self,
        symbol: str,
        condition: TriggerCondition,
        intent: "OrderIntent",
    ) -> str:
        """Register a new price trigger.

        Args:
            symbol: Instrument symbol (e.g. "TXFD6").
            condition: TriggerCondition instance (GE or LE).
            intent: OrderIntent to fire when condition is met.

        Returns:
            trigger_id: 12-char hex string uniquely identifying this trigger.

        Raises:
            ValueError: If max_triggers capacity is reached.
        """
        if len(self._triggers) >= self._max_triggers:
            raise ValueError(
                f"TriggerExecutor reached max triggers ({self._max_triggers})"
            )
        trigger_id = uuid.uuid4().hex[:12]
        self._triggers[trigger_id] = (symbol, condition, intent)
        return trigger_id

    def cancel(self, trigger_id: str) -> bool:
        """Cancel a registered trigger.

        Returns:
            True if the trigger existed and was removed, False otherwise.
        """
        if trigger_id in self._triggers:
            del self._triggers[trigger_id]
            return True
        return False

    def on_tick(self, symbol: str, price: int) -> list["OrderIntent"]:
        """Evaluate all triggers for the given symbol against the current price.

        Triggers whose condition is met are fired (their OrderIntent returned)
        and immediately removed (one-shot semantics).

        Args:
            symbol: Instrument symbol of the incoming tick.
            price: Current price as scaled int (x10000).

        Returns:
            List of OrderIntent objects whose conditions were met. May be empty.
        """
        fired: list["OrderIntent"] = []
        to_remove: list[str] = []

        for tid, (sym, condition, intent) in self._triggers.items():
            if sym == symbol and condition.is_met(price):
                fired.append(intent)
                to_remove.append(tid)

        for tid in to_remove:
            del self._triggers[tid]

        return fired
