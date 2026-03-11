"""Fubon account queries stub."""

from __future__ import annotations

from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class FubonAccountGateway:
    """Account query gateway for Fubon TradeAPI.

    All methods raise ``NotImplementedError`` until the Fubon SDK
    integration is implemented.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    def get_positions(self) -> list[Any]:
        raise NotImplementedError("FubonAccountGateway.get_positions not yet implemented")

    def get_balance(self) -> Any:
        raise NotImplementedError("FubonAccountGateway.get_balance not yet implemented")

    def get_margin(self, account: Any = None) -> Any:
        raise NotImplementedError("FubonAccountGateway.get_margin not yet implemented")

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        raise NotImplementedError("FubonAccountGateway.list_profit_loss not yet implemented")
