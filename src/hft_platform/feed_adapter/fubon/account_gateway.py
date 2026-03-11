"""Fubon account and inventory queries.

Wraps Fubon SDK (fubon_neo) account balance, inventory, and margin queries.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.account_gateway")


class FubonAccountGateway:
    """Fubon account and inventory queries."""

    __slots__ = ("_sdk", "log")

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self.log = logger

    def get_inventories(self) -> list[Any]:
        """Get stock inventories."""
        try:
            result = self._sdk.stock.inventories()
            self.log.info("fubon_get_inventories", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_inventories_failed", error=str(exc))
            raise

    def get_accounting(self) -> Any:
        """Get account balance info."""
        try:
            result = self._sdk.accounting()
            self.log.info("fubon_get_accounting")
            return result
        except Exception as exc:
            self.log.error("fubon_get_accounting_failed", error=str(exc))
            raise

    def get_margin(self) -> Any:
        """Get margin info for futures/options."""
        try:
            result = self._sdk.futopt_accounting()
            self.log.info("fubon_get_margin")
            return result
        except Exception as exc:
            self.log.error("fubon_get_margin_failed", error=str(exc))
            raise

    def get_settlements(self) -> list[Any]:
        """Get settlement information."""
        try:
            result = self._sdk.settlements()
            self.log.info("fubon_get_settlements", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_settlements_failed", error=str(exc))
            raise
