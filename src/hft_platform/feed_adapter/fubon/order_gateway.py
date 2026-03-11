"""Fubon order gateway stub."""

from __future__ import annotations

from typing import Any, Callable

from structlog import get_logger

logger = get_logger("fubon.order")


class FubonOrderGateway:
    """Stub for Fubon order placement, cancellation, and modification."""

    __slots__ = ("_sdk", "_account", "_symbols_meta", "_on_order_cb", "_on_deal_cb")

    def __init__(self, sdk: Any, account: Any, symbols_meta: dict | None = None) -> None:
        self._sdk = sdk
        self._account = account
        self._symbols_meta = symbols_meta or {}
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str = "stock",
        tif: str = "ROD",
        **kwargs: Any,
    ) -> Any:
        """Submit a new order (stub)."""
        return None

    def cancel_order(self, trade: Any) -> Any:
        """Cancel an existing order (stub)."""
        return None

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any:
        """Modify an existing order (stub)."""
        return None

    def get_exchange(self, symbol: str) -> str:
        """Resolve exchange for a symbol."""
        return self._symbols_meta.get(symbol, {}).get("exchange", "TSE")

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]) -> None:
        """Wire order/deal event callbacks."""
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
