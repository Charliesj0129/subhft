"""Fubon market data subscription lifecycle manager.

Orchestrates symbol subscriptions by composing ``FubonQuoteRuntime`` for
WebSocket-level subscribe/unsubscribe, watchdog, and callback wiring.

Design notes
------------
- **Rate limit**: Fubon imposes a 15 req/s cap.  Per-symbol operations respect
  this via the underlying ``FubonQuoteRuntime`` which batches calls.
- **Resubscribe cooldown**: ``resubscribe()`` enforces a 10-second cooldown
  to prevent rapid-fire reconnection storms.
- **Allocator Law**: No per-tick allocations.  All hot-path buffers live in
  ``FubonQuoteRuntime``; this manager only handles lifecycle orchestration.
"""

from __future__ import annotations

from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.subscription_manager")

# Minimum seconds between consecutive resubscribe attempts.
_RESUBSCRIBE_COOLDOWN_S: float = 10.0


class FubonSubscriptionManager:
    """Manages Fubon market data subscription lifecycle.

    Composes ``FubonQuoteRuntime`` for low-level WebSocket operations and
    provides basket-level subscribe, resubscribe, and execution callback
    wiring.
    """

    __slots__ = (
        "_sdk",
        "_quote_runtime",
        "_symbols",
        "_subscribed_codes",
        "_on_order_cb",
        "_on_deal_cb",
        "_tick_callback",
        "_last_resubscribe_ts",
        "log",
    )

    def __init__(
        self,
        sdk: Any,
        quote_runtime: Any,
        symbols: list[dict[str, Any]],
    ) -> None:
        self._sdk = sdk
        self._quote_runtime = quote_runtime
        self._symbols = symbols
        self._subscribed_codes: set[str] = set()
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._tick_callback: Callable[..., Any] | None = None
        self._last_resubscribe_ts: float = 0.0
        self.log = logger

    # ------------------------------------------------------------------ #
    # Basket subscription
    # ------------------------------------------------------------------ #

    def _symbol_codes(self) -> list[str]:
        """Extract valid symbol codes from the configured symbols list."""
        return [sym["code"] for sym in self._symbols if sym.get("code")]

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to all configured symbols.

        1. Store the tick callback for later resubscribe use.
        2. Register canonical callbacks on the quote runtime.
        3. Subscribe all symbol codes.
        4. Start the quote watchdog.
        """
        self._tick_callback = cb

        self._quote_runtime.register_quote_callbacks(
            on_tick=cb,
            on_bidask=cb,
        )

        codes = self._symbol_codes()
        self._quote_runtime.subscribe(codes)
        self._subscribed_codes = set(codes)

        self._quote_runtime.start_quote_watchdog()

        self.log.info(
            "Fubon basket subscribed",
            count=len(self._subscribed_codes),
        )

    # ------------------------------------------------------------------ #
    # Resubscribe
    # ------------------------------------------------------------------ #

    def resubscribe(self) -> bool:
        """Stop and re-subscribe all symbols.

        Returns ``True`` on success, ``False`` if skipped (cooldown) or
        on error.
        """
        now_s = timebase.now_s()
        if now_s - self._last_resubscribe_ts < _RESUBSCRIBE_COOLDOWN_S:
            self.log.info(
                "Fubon resubscribe skipped (cooldown)",
                elapsed_s=round(now_s - self._last_resubscribe_ts, 3),
                cooldown_s=_RESUBSCRIBE_COOLDOWN_S,
            )
            return False

        try:
            self._quote_runtime.stop()

            if self._tick_callback is not None:
                self._quote_runtime.register_quote_callbacks(
                    on_tick=self._tick_callback,
                    on_bidask=self._tick_callback,
                )

            codes = self._symbol_codes()
            self._quote_runtime.subscribe(codes)
            self._subscribed_codes = set(codes)

            self._quote_runtime.start_quote_watchdog()

            self._last_resubscribe_ts = now_s
            self.log.info(
                "Fubon resubscribed",
                count=len(self._subscribed_codes),
            )
            return True
        except Exception as exc:
            self.log.error("Fubon resubscribe failed", error=str(exc))
            return False

    # ------------------------------------------------------------------ #
    # Execution callbacks
    # ------------------------------------------------------------------ #

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store execution callbacks and wire to SDK if available.

        The Fubon SDK exposes ``set_on_filled`` / ``set_on_order_changed``
        on the ``Order`` module when available.  If the SDK does not expose
        these hooks, the callbacks are stored for manual polling.
        """
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal

        # Attempt to wire directly to SDK order event hooks.
        sdk = self._sdk
        if sdk is not None:
            if hasattr(sdk, "set_on_order_changed"):
                try:
                    sdk.set_on_order_changed(on_order)
                except Exception as exc:
                    self.log.warning(
                        "Failed to wire SDK on_order_changed",
                        error=str(exc),
                    )
            if hasattr(sdk, "set_on_filled"):
                try:
                    sdk.set_on_filled(on_deal)
                except Exception as exc:
                    self.log.warning(
                        "Failed to wire SDK on_filled",
                        error=str(exc),
                    )

        self.log.info("Fubon execution callbacks registered")

    # ------------------------------------------------------------------ #
    # Per-symbol helpers
    # ------------------------------------------------------------------ #

    def _subscribe_symbol(self, sym: dict[str, Any], cb: Callable[..., Any]) -> bool:
        """Subscribe to a single symbol via the quote runtime.

        Returns ``True`` if the symbol was successfully subscribed.
        """
        code = sym.get("code")
        if not code:
            self.log.warning("Symbol missing 'code' field", symbol=sym)
            return False
        try:
            self._quote_runtime.subscribe([code])
            self._subscribed_codes.add(code)
            return True
        except Exception as exc:
            self.log.error(
                "Fubon per-symbol subscribe failed",
                code=code,
                error=str(exc),
            )
            return False

    def _unsubscribe_symbol(self, sym: dict[str, Any]) -> None:
        """Unsubscribe from a single symbol via the quote runtime."""
        code = sym.get("code")
        if not code:
            return
        try:
            self._quote_runtime.unsubscribe([code])
            self._subscribed_codes.discard(code)
        except Exception as exc:
            self.log.warning(
                "Fubon per-symbol unsubscribe failed",
                code=code,
                error=str(exc),
            )
