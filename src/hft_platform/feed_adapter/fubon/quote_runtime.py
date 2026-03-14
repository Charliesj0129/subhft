"""Fubon real-time market data (quote) runtime.

Manages WebSocket subscriptions via the ``fubon_neo`` SDK and translates
Fubon-specific field names into the platform's canonical format before
forwarding to registered callbacks.

Design notes
------------
- **Allocator Law**: Translation buffers (_tick_buffer, _bidask_buffer) are
  pre-allocated once in ``__init__`` and reused by overwriting values in each
  callback invocation.  No per-tick heap allocation.
- **Precision Law**: Canonical callbacks receive x10000 scaled integer prices.
- **Precision Law**: Float prices are scaled to canonical x10000 integers at
  this boundary before they leave the adapter.
- **Boundary Law**: All Fubon-specific names are translated to canonical keys
  (``code``, ``close``, ``volume``, ``bid_price``, ``ask_price``, etc.) so
  the normalizer can process them without broker awareness.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("feed_adapter.fubon.quote_runtime")

# Precision Law: all prices are scaled int x10000.
_PRICE_SCALE: int = 10_000

# Number of order book levels forwarded from the Fubon L5 feed.
_BOOK_LEVELS: int = 5


class FubonQuoteRuntime:
    """Manages Fubon real-time market data WebSocket subscriptions."""

    __slots__ = (
        "_sdk",
        "_on_tick",
        "_on_bidask",
        "_watchdog_thread",
        "_subscribed",
        "_running",
        "_tick_buffer",
        "_bidask_buffer",
        "_last_data_ts",
        "log",
    )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._on_tick: Callable[..., Any] | None = None
        self._on_bidask: Callable[..., Any] | None = None
        self._watchdog_thread: threading.Thread | None = None
        self._subscribed: set[str] = set()
        self._running: bool = False
        self._last_data_ts: float = 0.0
        self.log = logger

        # Pre-allocated translation buffers (Allocator Law).
        # Values are overwritten on each callback — the *same dict object*
        # is reused every time to avoid per-tick heap allocation.
        self._tick_buffer: dict[str, Any] = {
            "code": "",
            "close": 0,
            "volume": 0,
            "ts": 0,
        }
        self._bidask_buffer: dict[str, Any] = {
            "code": "",
            "bid_price": [0] * _BOOK_LEVELS,
            "bid_volume": [0] * _BOOK_LEVELS,
            "ask_price": [0] * _BOOK_LEVELS,
            "ask_volume": [0] * _BOOK_LEVELS,
            "ts": 0,
        }

    # ------------------------------------------------------------------ #
    # Callback registration
    # ------------------------------------------------------------------ #

    def register_quote_callbacks(
        self,
        on_tick: Callable[..., Any],
        on_bidask: Callable[..., Any],
    ) -> None:
        """Register canonical tick and bidask callbacks."""
        self._on_tick = on_tick
        self._on_bidask = on_bidask
        self.log.info("Fubon quote callbacks registered")

    # ------------------------------------------------------------------ #
    # Subscription management
    # ------------------------------------------------------------------ #

    def subscribe(self, symbols: list[str]) -> None:
        """Subscribe to trades and books channels for the given symbols.

        Calls ``sdk.init_realtime()`` on first subscription, then subscribes
        each symbol to ``trades`` (tick) and ``books`` (L5 bid/ask) channels.
        """
        if not self._running:
            self._sdk.init_realtime()
            self._running = True

        ws = self._sdk.marketdata.websocket_client.stock
        for sym in symbols:
            if sym in self._subscribed:
                continue
            ws.subscribe(
                {"channel": "trades", "symbol": sym},
                on_message=self._on_fubon_trade,
            )
            ws.subscribe(
                {"channel": "books", "symbol": sym},
                on_message=self._on_fubon_book,
            )
            self._subscribed.add(sym)
            self.log.info("Fubon subscribed", symbol=sym)

    def unsubscribe(self, symbols: list[str]) -> None:
        """Unsubscribe from trades and books channels for the given symbols."""
        ws = self._sdk.marketdata.websocket_client.stock
        for sym in symbols:
            if sym not in self._subscribed:
                continue
            try:
                ws.unsubscribe({"channel": "trades", "symbol": sym})
            except Exception as exc:
                self.log.warning(
                    "Fubon unsubscribe trades failed",
                    symbol=sym,
                    error=str(exc),
                )
            try:
                ws.unsubscribe({"channel": "books", "symbol": sym})
            except Exception as exc:
                self.log.warning(
                    "Fubon unsubscribe books failed",
                    symbol=sym,
                    error=str(exc),
                )
            self._subscribed.discard(sym)
            self.log.info("Fubon unsubscribed", symbol=sym)

    # ------------------------------------------------------------------ #
    # Internal callbacks — translate Fubon → canonical
    # ------------------------------------------------------------------ #

    def _on_fubon_trade(self, data: Any) -> None:
        """Translate a Fubon trade event to canonical tick dict and forward."""
        if self._on_tick is None:
            return
        try:
            buf = self._tick_buffer

            symbol = _get(data, "symbol", "")
            price_raw = _get(data, "close", None)
            if price_raw is None:
                price_raw = _get(data, "price", 0)
            volume = int(_get(data, "volume", 0))
            ts_raw = _get(data, "datetime", None)

            # Convert timestamp to nanoseconds
            ts_ns = _ts_to_ns(ts_raw)

            # Overwrite pre-allocated buffer (no new dict)
            buf["code"] = symbol
            buf["close"] = _scale_price(price_raw)
            buf["volume"] = volume
            buf["ts"] = ts_ns

            self._last_data_ts = time.monotonic()
            self._on_tick(buf)
        except Exception as exc:
            self.log.error("Fubon trade callback error", error=str(exc))

    def _on_fubon_book(self, data: Any) -> None:
        """Translate a Fubon book event to canonical bidask dict and forward."""
        if self._on_bidask is None:
            return
        try:
            buf = self._bidask_buffer

            symbol = _get(data, "symbol", "")
            ts_raw = _get(data, "datetime", None)
            ts_ns = _ts_to_ns(ts_raw)

            bid_prices_raw = _get(data, "bid_prices", [])
            bid_sizes_raw = _get(data, "bid_sizes", [])
            ask_prices_raw = _get(data, "ask_prices", [])
            ask_sizes_raw = _get(data, "ask_sizes", [])

            # Cache lengths to avoid repeated calls inside the hot loop.
            n_bp = len(bid_prices_raw)
            n_bv = len(bid_sizes_raw)
            n_ap = len(ask_prices_raw)
            n_av = len(ask_sizes_raw)

            # Reuse the pre-allocated lists inside the buffer.
            bp = buf["bid_price"]
            bv = buf["bid_volume"]
            ap = buf["ask_price"]
            av = buf["ask_volume"]

            for i in range(_BOOK_LEVELS):
                bp[i] = _scale_price(bid_prices_raw[i]) if i < n_bp else 0
                bv[i] = int(bid_sizes_raw[i]) if i < n_bv else 0
                ap[i] = _scale_price(ask_prices_raw[i]) if i < n_ap else 0
                av[i] = int(ask_sizes_raw[i]) if i < n_av else 0

            buf["code"] = symbol
            buf["ts"] = ts_ns

            self._last_data_ts = time.monotonic()
            self._on_bidask(buf)
        except Exception as exc:
            self.log.error("Fubon book callback error", error=str(exc))

    # ------------------------------------------------------------------ #
    # Watchdog
    # ------------------------------------------------------------------ #

    def start_quote_watchdog(self, timeout_s: float = 30.0) -> None:
        """Start a watchdog thread that monitors data freshness.

        If no data arrives for *timeout_s* seconds, a warning is logged.
        """
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._running = True
        self.log.info("Starting Fubon quote watchdog", timeout_s=timeout_s)

        def _watch() -> None:
            while self._running:
                time.sleep(timeout_s)
                if not self._running:
                    break
                last = self._last_data_ts
                if last <= 0:
                    continue
                gap = time.monotonic() - last
                if gap >= timeout_s:
                    self.log.warning(
                        "Fubon quote watchdog: no data",
                        gap_s=round(gap, 3),
                        timeout_s=timeout_s,
                    )

        self._watchdog_thread = threading.Thread(
            target=_watch,
            name="fubon-quote-watchdog",
            daemon=True,
        )
        self._watchdog_thread.start()

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def stop(self) -> None:
        """Stop the watchdog and unsubscribe all symbols."""
        self._running = False
        if self._subscribed:
            self.unsubscribe(list(self._subscribed))
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=5.0)
            self._watchdog_thread = None
        self.log.info("Fubon quote runtime stopped")


# ---------------------------------------------------------------------- #
# Module-level helpers (stateless, no allocation)
# ---------------------------------------------------------------------- #


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Retrieve *key* from a dict or object attribute."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _ts_to_ns(ts_val: Any) -> int:
    """Convert a Fubon timestamp to nanoseconds.

    Delegates to ``timebase.coerce_ns`` which handles int, float, and
    ``datetime`` objects.  Returns 0 for ``None``.
    """
    if ts_val is None:
        return 0
    return timebase.coerce_ns(ts_val)


def _scale_price(price_val: Any) -> int:
    """Scale Fubon prices to canonical x10000 integer units."""
    if price_val is None:
        return 0
    return int(round(float(price_val) * 10_000))
