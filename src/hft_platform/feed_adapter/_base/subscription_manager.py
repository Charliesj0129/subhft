"""Shared subscription management abstractions.

Captures the cooldown enforcement pattern used by both Shioaji (2.5s cooldown
in ``_resubscribe_all``) and Fubon (10s cooldown in ``resubscribe``).  Also
defines ``SubscriptionManagerProtocol`` for broker-agnostic subscription
orchestration.

This is an ADDITIVE extraction — existing implementations are not modified.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol, runtime_checkable

import structlog

logger = structlog.get_logger("feed_adapter._base.subscription_manager")


@runtime_checkable
class SubscriptionManagerProtocol(Protocol):
    """Minimal interface for broker subscription managers.

    Both Shioaji and Fubon subscription managers expose these operations.
    This protocol enables broker-agnostic code to drive subscriptions.
    """

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to all configured symbols with the given callback."""
        ...

    def resubscribe(self) -> bool:
        """Re-subscribe all symbols.  Returns ``True`` on success."""
        ...

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Register execution (order update + fill) callbacks."""
        ...


class CooldownManager:
    """Reusable cooldown enforcement for rate-limited operations.

    Both Shioaji and Fubon use time-based cooldowns to prevent storms
    (reconnect storms, resubscribe storms).  This class encapsulates
    the shared pattern with a configurable duration.

    Usage::

        cd = CooldownManager(cooldown_s=10.0, name="resubscribe")

        if cd.try_acquire():
            # proceed with the operation
            ...
        else:
            # cooldown active, skip
            ...
    """

    __slots__ = ("_cooldown_s", "_last_ts", "_name")

    def __init__(self, cooldown_s: float, name: str = "cooldown") -> None:
        """Initialise the cooldown manager.

        Args:
            cooldown_s: Minimum seconds between consecutive operations.
            name: Human-readable name for log messages.
        """
        self._cooldown_s: float = cooldown_s
        self._last_ts: float = 0.0
        self._name: str = name

    def try_acquire(self) -> bool:
        """Attempt to acquire permission to proceed.

        Returns ``True`` if the cooldown has elapsed (or this is the first
        call), and records the current time.  Returns ``False`` if the
        cooldown is still active.
        """
        now = time.monotonic()
        elapsed = now - self._last_ts

        if self._last_ts > 0 and elapsed < self._cooldown_s:
            logger.info(
                "cooldown_active",
                name=self._name,
                elapsed_s=round(elapsed, 3),
                cooldown_s=self._cooldown_s,
            )
            return False

        self._last_ts = now
        return True

    def reset(self) -> None:
        """Reset the cooldown timer, allowing the next call to proceed."""
        self._last_ts = 0.0

    @property
    def elapsed_s(self) -> float:
        """Seconds elapsed since the last successful acquire."""
        if self._last_ts <= 0:
            return 0.0
        return time.monotonic() - self._last_ts

    @property
    def cooldown_s(self) -> float:
        """The configured cooldown duration."""
        return self._cooldown_s

    @property
    def is_ready(self) -> bool:
        """Return ``True`` if the cooldown has elapsed (without acquiring)."""
        if self._last_ts <= 0:
            return True
        return (time.monotonic() - self._last_ts) >= self._cooldown_s
