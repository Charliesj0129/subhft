"""Subscription orchestration for Fubon quote feeds.

Wraps ``FubonQuoteRuntime`` with higher-level basket subscription,
resubscribe-with-cooldown, and execution callback storage.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from structlog import get_logger

from .quote_runtime import FubonQuoteRuntime

logger = get_logger("feed_adapter.fubon.subscription_manager")

# Minimum seconds between consecutive resubscribe calls.
_RESUBSCRIBE_COOLDOWN_S: float = 10.0


class FubonSubscriptionManager:
    """Orchestrates WebSocket subscriptions via FubonQuoteRuntime.

    Handles basket subscribe, resubscribe with cooldown enforcement,
    and execution callback wiring.
    """

    __slots__ = (
        "_quote_runtime",
        "_symbols",
        "_subscribed_codes",
        "_on_order_cb",
        "_on_deal_cb",
        "_last_resubscribe_ts",
        "MAX_SUBSCRIPTIONS",
        "log",
    )

    def __init__(
        self,
        quote_runtime: FubonQuoteRuntime,
        symbols: list[dict[str, Any]] | list[str],
        *,
        max_subscriptions: int = 200,
    ) -> None:
        self._quote_runtime = quote_runtime
        self._symbols = symbols
        self._subscribed_codes: set[str] = set()
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._last_resubscribe_ts: float = 0.0
        self.MAX_SUBSCRIPTIONS: int = max_subscriptions
        self.log = logger

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to all configured symbols.

        Registers *cb* as both tick and bidask callback on the underlying
        ``FubonQuoteRuntime``, subscribes extracted symbol codes, and starts
        the quote watchdog.
        """
        codes = self._extract_codes(self._symbols)

        if len(codes) > self.MAX_SUBSCRIPTIONS:
            self.log.warning(
                "Symbol count exceeds MAX_SUBSCRIPTIONS cap",
                requested=len(codes),
                cap=self.MAX_SUBSCRIPTIONS,
            )
            codes = codes[: self.MAX_SUBSCRIPTIONS]

        self._quote_runtime.register_quote_callbacks(cb, cb)
        self._quote_runtime.subscribe(codes)
        self._subscribed_codes = set(codes)
        self._quote_runtime.start_quote_watchdog()

        self.log.info(
            "Fubon basket subscription completed",
            subscribed=len(self._subscribed_codes),
        )

    def resubscribe(self) -> bool:
        """Stop and re-subscribe all symbols.

        Enforces a cooldown of ``_RESUBSCRIBE_COOLDOWN_S`` seconds between
        calls.  Returns ``True`` on success, ``False`` if cooldown has not
        elapsed or if there are no codes to subscribe.
        """
        now = time.monotonic()
        elapsed = now - self._last_resubscribe_ts
        if self._last_resubscribe_ts > 0 and elapsed < _RESUBSCRIBE_COOLDOWN_S:
            self.log.warning(
                "Resubscribe rejected: cooldown active",
                elapsed_s=round(elapsed, 3),
                cooldown_s=_RESUBSCRIBE_COOLDOWN_S,
            )
            return False

        codes = self._extract_codes(self._symbols)
        if not codes:
            self.log.warning("Resubscribe skipped: no symbols configured")
            return False

        self._quote_runtime.stop()
        self._quote_runtime.subscribe(codes)
        self._subscribed_codes = set(codes)
        self._quote_runtime.start_quote_watchdog()
        self._last_resubscribe_ts = time.monotonic()

        self.log.info(
            "Fubon resubscribe completed",
            subscribed=len(self._subscribed_codes),
        )
        return True

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store execution callbacks for later wiring."""
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        self.log.info("Fubon execution callbacks stored")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_codes(
        symbols: list[dict[str, Any]] | list[str],
    ) -> list[str]:
        """Extract symbol code strings from config.

        Handles both ``list[str]`` (codes directly) and ``list[dict]``
        (dicts with a ``"code"`` key).
        """
        codes: list[str] = []
        for sym in symbols:
            if isinstance(sym, str):
                codes.append(sym)
            elif isinstance(sym, dict):
                code = sym.get("code")
                if code is not None:
                    codes.append(str(code))
        return codes

    @property
    def subscribed_codes(self) -> set[str]:
        """Currently subscribed symbol codes (read-only view)."""
        return set(self._subscribed_codes)
