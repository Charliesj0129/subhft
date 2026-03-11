"""Fubon order operations stub."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class FubonOrderGateway:
    """Order entry / cancel / update gateway for Fubon TradeAPI.

    All methods raise ``NotImplementedError`` until the Fubon SDK
    integration is implemented.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("FubonOrderGateway.place_order not yet implemented")

    def cancel_order(self, trade: Any) -> Any:
        raise NotImplementedError("FubonOrderGateway.cancel_order not yet implemented")

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        raise NotImplementedError("FubonOrderGateway.update_order not yet implemented")

    def batch_place_orders(self, orders: list[dict[str, Any]]) -> list[Any]:
        raise NotImplementedError("FubonOrderGateway.batch_place_orders not yet implemented")
