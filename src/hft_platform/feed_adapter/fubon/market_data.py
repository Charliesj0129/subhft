"""Fubon market data provider stub."""

from __future__ import annotations

from typing import Any, Callable

from structlog import get_logger

logger = get_logger("fubon.market_data")


class FubonMarketDataProvider:
    """Stub for Fubon market data subscription and snapshot retrieval."""

    __slots__ = ("_sdk", "_account", "_symbols", "_callback", "_connected")

    def __init__(self, sdk: Any, account: Any, symbols: list[str]) -> None:
        self._sdk = sdk
        self._account = account
        self._symbols = symbols
        self._callback: Callable[..., Any] | None = None
        self._connected = False

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Register tick callback and subscribe to configured symbols."""
        self._callback = cb

    def fetch_snapshots(self) -> list[Any]:
        """Return current snapshots for subscribed symbols."""
        return []

    def resubscribe(self) -> bool:
        """Re-establish subscriptions after reconnect."""
        return True

    def reload_symbols(self) -> None:
        """Reload symbol config from disk."""

    def validate_symbols(self) -> list[str]:
        """Return list of invalid/unresolved symbols."""
        return []
