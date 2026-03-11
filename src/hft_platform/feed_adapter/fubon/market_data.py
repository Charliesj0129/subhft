"""Fubon market data provider — WebSocket-based real-time data feed.

Implements the ``MarketDataProvider`` protocol for the Fubon Neo
WebSocket API, translating Fubon trade/book messages into the platform's
normalised tick and bidask dictionaries.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Callable

import numpy as np
from structlog import get_logger

logger = get_logger("fubon.market_data")

BOOK_DEPTH = 5  # Fubon provides top 5
PRICE_SCALE = 10000  # Platform-wide price scaling

# Control-plane events that should be silently skipped
_CONTROL_EVENTS = frozenset(
    {"authenticated", "subscribed", "unsubscribed", "heartbeat", "pong"}
)


class FubonMarketDataProvider:
    """Implements MarketDataProvider protocol for Fubon Neo WebSocket API."""

    __slots__ = (
        "_sdk",
        "_account",
        "_symbols",
        "_callback",
        "_ws_client",
        "_bid_buf",
        "_ask_buf",
        "_connected",
    )

    def __init__(self, sdk: Any, account: Any, symbols: list[str]) -> None:
        self._sdk = sdk
        self._account = account
        self._symbols = list(symbols)
        self._callback: Callable[..., Any] | None = None
        self._ws_client: Any = None
        self._connected = False

        # Pre-allocate book buffers (Allocator Law)
        self._bid_buf = np.zeros((BOOK_DEPTH, 2), dtype=np.int64)
        self._ask_buf = np.zeros((BOOK_DEPTH, 2), dtype=np.int64)

    # ── MarketDataProvider protocol ──────────────────────────────

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to trades + books channels for all configured symbols."""
        self._callback = cb

        try:
            self._sdk.init_realtime()
        except Exception:
            logger.exception("fubon_init_realtime_failed")
            raise

        self._ws_client = self._sdk.marketdata.websocket_client.stock
        self._ws_client.on("message", self._on_message)
        self._ws_client.on("connect", self._on_connect)
        self._ws_client.on("disconnect", self._on_disconnect)
        self._ws_client.on("error", self._on_error)

        self._ws_client.connect()
        self._connected = True

        self._subscribe_all_symbols()
        logger.info("fubon_subscribed", symbol_count=len(self._symbols))

    def fetch_snapshots(self) -> list[Any]:
        """Fetch current snapshots for all symbols."""
        snapshots: list[Any] = []
        for symbol in self._symbols:
            try:
                result = self._sdk.marketdata.rest_client.stock.intraday.quote(
                    symbol=symbol,
                )
                if result and hasattr(result, "data"):
                    snapshots.append(result.data)
            except Exception:
                logger.warning("fubon_snapshot_failed", symbol=symbol)
        return snapshots

    def resubscribe(self) -> bool:
        """Resubscribe after reconnection."""
        if not self._ws_client or not self._connected:
            return False
        try:
            self._subscribe_all_symbols()
            logger.info("fubon_resubscribed", symbol_count=len(self._symbols))
            return True
        except Exception:
            logger.exception("fubon_resubscribe_failed")
            return False

    def reload_symbols(self) -> None:
        """Reload symbol list (no-op for now, symbols come from config)."""
        logger.info("fubon_reload_symbols")

    def validate_symbols(self) -> list[str]:
        """Return list of invalid symbols.

        Fubon does not expose a preflight symbol validation API,
        so we return an empty list (all assumed valid).
        """
        return []

    # ── Internal helpers ────────────────────────────────────────

    def _subscribe_all_symbols(self) -> None:
        """Subscribe to trades + books for every symbol in the basket."""
        for symbol in self._symbols:
            self._ws_client.subscribe({"channel": "trades", "symbol": symbol})
            self._ws_client.subscribe({"channel": "books", "symbol": symbol})

    # ── WebSocket event handlers ─────────────────────────────────

    def _on_message(self, message: str) -> None:
        """Handle incoming WebSocket message."""
        if not self._callback:
            return
        try:
            data = json.loads(message) if isinstance(message, str) else message
            event = data.get("event", "")

            if event in _CONTROL_EVENTS:
                return

            if event == "error":
                logger.warning("fubon_ws_error_event", data=data.get("data", {}))
                return

            channel = data.get("channel", "")
            if channel == "trades":
                self._handle_trade(data)
            elif channel == "books":
                self._handle_book(data)

        except Exception:
            logger.exception("fubon_message_parse_error")

    def _handle_trade(self, data: dict[str, Any]) -> None:
        """Convert Fubon trade message to platform tick format."""
        d = data.get("data", data)
        symbol = d.get("symbol", "")
        price_raw = d.get("price")
        if price_raw is None:
            return

        # Precision Law: string -> Decimal -> scaled int
        price_scaled = int(Decimal(str(price_raw)) * PRICE_SCALE)

        bid_raw = d.get("bid")
        ask_raw = d.get("ask")
        bid_scaled = (
            int(Decimal(str(bid_raw)) * PRICE_SCALE) if bid_raw is not None else 0
        )
        ask_scaled = (
            int(Decimal(str(ask_raw)) * PRICE_SCALE) if ask_raw is not None else 0
        )

        tick = {
            "code": symbol,
            "close": price_scaled,
            "volume": d.get("size", 0),
            "total_volume": d.get("volume", 0),
            "ts": d.get("time", 0),
            "bid_price": bid_scaled,
            "ask_price": ask_scaled,
            "simtrade": d.get("isTrial", False),
            "intraday_odd": d.get("intradayOddLot", False),
        }

        # _callback is guaranteed non-None by _on_message guard
        self._callback(tick)  # type: ignore[misc]

    def _handle_book(self, data: dict[str, Any]) -> None:
        """Convert Fubon book message to platform bidask format.

        Flattens bids[{price,size}] into parallel arrays (Cache Law).
        """
        d = data.get("data", data)
        symbol = d.get("symbol", "")
        raw_bids = d.get("bids", [])
        raw_asks = d.get("asks", [])

        # Flatten into pre-allocated buffers (Allocator Law)
        self._bid_buf[:] = 0
        self._ask_buf[:] = 0

        for i, bid in enumerate(raw_bids[:BOOK_DEPTH]):
            self._bid_buf[i, 0] = int(
                Decimal(str(bid.get("price", 0))) * PRICE_SCALE,
            )
            self._bid_buf[i, 1] = int(bid.get("size", 0))

        for i, ask in enumerate(raw_asks[:BOOK_DEPTH]):
            self._ask_buf[i, 0] = int(
                Decimal(str(ask.get("price", 0))) * PRICE_SCALE,
            )
            self._ask_buf[i, 1] = int(ask.get("size", 0))

        bidask = {
            "code": symbol,
            "bid_price": self._bid_buf[:, 0].copy(),
            "bid_volume": self._bid_buf[:, 1].copy(),
            "ask_price": self._ask_buf[:, 0].copy(),
            "ask_volume": self._ask_buf[:, 1].copy(),
            "ts": d.get("time", 0),
            "simtrade": False,
            "intraday_odd": False,
        }

        # _callback is guaranteed non-None by _on_message guard
        self._callback(bidask)  # type: ignore[misc]

    def _on_connect(self) -> None:
        self._connected = True
        logger.info("fubon_ws_connected")

    def _on_disconnect(self, *args: Any) -> None:
        self._connected = False
        logger.warning("fubon_ws_disconnected")

    def _on_error(self, error: Any) -> None:
        logger.error("fubon_ws_error", error=str(error))
