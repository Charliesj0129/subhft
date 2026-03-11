"""Fubon quote / market data stub."""

from __future__ import annotations

from typing import Any, Callable

import structlog

logger = structlog.get_logger(__name__)


class FubonQuoteRuntime:
    """Quote feed management for Fubon TradeAPI.

    All methods raise ``NotImplementedError`` until the Fubon SDK
    integration is implemented.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def subscribe(self, symbol: str, **kwargs: Any) -> bool:
        raise NotImplementedError("FubonQuoteRuntime.subscribe not yet implemented")

    def unsubscribe(self, symbol: str, **kwargs: Any) -> bool:
        raise NotImplementedError("FubonQuoteRuntime.unsubscribe not yet implemented")

    def on_quote_callback(self, cb: Callable[..., Any]) -> None:
        raise NotImplementedError("FubonQuoteRuntime.on_quote_callback not yet implemented")
