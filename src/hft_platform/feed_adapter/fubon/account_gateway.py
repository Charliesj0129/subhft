"""Fubon account gateway — implements AccountProvider protocol."""
from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("fubon.account")


def _unwrap_list(result: Any) -> list[Any]:
    """Extract ``.data`` from an SDK response as a list, or return ``[]``."""
    if result is not None and hasattr(result, "data"):
        return list(result.data)
    return []


def _unwrap_scalar(result: Any) -> Any:
    """Extract ``.data`` from an SDK response, or return ``None``."""
    if result is not None and hasattr(result, "data"):
        return result.data
    return None


class FubonAccountGateway:
    """Account queries for Fubon Neo SDK.

    Wraps ``sdk.accounting.*`` calls behind the ``AccountProvider`` protocol
    so the platform can query positions, balances, and P&L without
    coupling to a specific broker SDK.
    """

    __slots__ = ("_sdk", "_account")

    def __init__(self, sdk: Any, account: Any) -> None:
        self._sdk = sdk
        self._account = account

    # ------------------------------------------------------------------
    # AccountProvider protocol
    # ------------------------------------------------------------------

    def _resolve_account(self, account: Any) -> Any:
        """Return *account* if provided, otherwise fall back to default."""
        return account if account is not None else self._account

    def _inventories(self, account: Any) -> list[Any]:
        """Shared helper for position/inventory queries."""
        acc = self._resolve_account(account)
        try:
            return _unwrap_list(self._sdk.accounting.inventories(acc))
        except Exception:
            logger.exception("fubon_inventories_failed")
            return []

    def get_positions(self) -> list[Any]:
        """Query current inventory/positions via ``sdk.accounting.inventories``."""
        return self._inventories(None)

    def get_account_balance(self, account: Any = None) -> Any:
        """Query settlement/balance via ``sdk.accounting.query_settlement``."""
        acc = self._resolve_account(account)
        try:
            return _unwrap_scalar(
                self._sdk.accounting.query_settlement(acc, "0d"),
            )
        except Exception:
            logger.exception("fubon_get_balance_failed")
            return None

    def get_margin(self, account: Any = None) -> Any:
        """Query margin information.

        Fubon stock accounts may not expose a margin endpoint.
        Returns ``None`` when the SDK lacks the capability.
        """
        acc = self._resolve_account(account)
        try:
            if not hasattr(self._sdk.accounting, "maintenance"):
                return None
            return _unwrap_scalar(self._sdk.accounting.maintenance(acc))
        except Exception:
            logger.warning("fubon_get_margin_not_available")
            return None

    def list_position_detail(self, account: Any = None) -> list[Any]:
        """List detailed position information via inventories."""
        return self._inventories(account)

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        """List unrealised P&L via ``sdk.accounting.unrealized_gains_and_loses``."""
        acc = self._resolve_account(account)
        try:
            return _unwrap_list(
                self._sdk.accounting.unrealized_gains_and_loses(acc),
            )
        except Exception:
            logger.exception("fubon_list_profit_loss_failed")
            return []
