"""Fubon account and inventory queries.

Wraps Fubon SDK (fubon_neo) account balance, inventory, and margin queries.
Provides both low-level SDK wrapper methods and BrokerProtocol-aligned methods.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("feed_adapter.fubon.account_gateway")


class FubonAccountGateway:
    """Fubon account and inventory queries."""

    __slots__ = ("_sdk", "_account", "log")

    def __init__(self, sdk: Any, account: Any = None) -> None:
        self._sdk = sdk
        self._account = account
        self.log = logger

    # ------------------------------------------------------------------ #
    # Low-level SDK wrappers (unchanged)
    # ------------------------------------------------------------------ #

    def get_inventories(self) -> list[Any]:
        """Get stock inventories.

        Returns:
            List of inventory items from the SDK.
        """
        try:
            result = self._sdk.stock.inventories()
            self.log.info("fubon_get_inventories", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_inventories_failed", error=str(exc))
            raise

    def get_accounting(self) -> Any:
        """Get account balance info.

        Returns:
            Account balance data from the SDK.
        """
        try:
            result = self._sdk.accounting()
            self.log.info("fubon_get_accounting")
            return result
        except Exception as exc:
            self.log.error("fubon_get_accounting_failed", error=str(exc))
            raise

    def get_margin(self) -> Any:
        """Get margin info for futures/options.

        Returns:
            Margin data from the SDK.
        """
        try:
            result = self._sdk.futopt_accounting()
            self.log.info("fubon_get_margin")
            return result
        except Exception as exc:
            self.log.error("fubon_get_margin_failed", error=str(exc))
            raise

    def get_settlements(self) -> list[Any]:
        """Get settlement information.

        Returns:
            List of settlement records.
        """
        try:
            result = self._sdk.settlements()
            self.log.info("fubon_get_settlements", count=len(result))
            return result
        except Exception as exc:
            self.log.error("fubon_get_settlements_failed", error=str(exc))
            raise

    # ------------------------------------------------------------------ #
    # BrokerProtocol-aligned methods
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        """Get positions (BrokerProtocol-aligned).

        Delegates to :meth:`get_inventories`.

        Returns:
            List of position/inventory items.
        """
        try:
            return self.get_inventories()
        except Exception as exc:
            self.log.error("fubon_get_positions_failed", error=str(exc))
            return []

    def get_account_balance(self, account: Any = None) -> Any:
        """Get account balance (BrokerProtocol-aligned).

        Delegates to :meth:`get_accounting`. The *account* parameter is
        accepted for protocol compatibility but Fubon SDK uses the
        internally stored account.

        Args:
            account: Ignored; kept for BrokerProtocol compatibility.

        Returns:
            Account balance data, or empty dict on failure.
        """
        try:
            return self.get_accounting()
        except Exception as exc:
            self.log.error("fubon_get_account_balance_failed", error=str(exc))
            return {}

    def list_position_detail(self, account: Any = None) -> list[Any]:
        """Query unrealized P&L (BrokerProtocol-aligned).

        Attempts ``self._sdk.accounting.unrealized_gains_and_loses(account)``.
        Falls back to an empty list if the SDK method is not available.

        Args:
            account: Optional account reference. Falls back to stored account.

        Returns:
            List of position detail records, or ``[]`` on failure.
        """
        acct = account if account is not None else self._account
        try:
            if hasattr(self._sdk, "accounting") and hasattr(
                self._sdk.accounting, "unrealized_gains_and_loses"
            ):
                result = self._sdk.accounting.unrealized_gains_and_loses(acct)
                self.log.info(
                    "fubon_list_position_detail",
                    count=len(result) if isinstance(result, list) else 1,
                )
                return result if isinstance(result, list) else [result]
            self.log.warning(
                "fubon_list_position_detail_unavailable",
                reason="SDK method not found",
            )
            return []
        except Exception as exc:
            self.log.error("fubon_list_position_detail_failed", error=str(exc))
            return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        """Query settlement / profit-loss history (BrokerProtocol-aligned).

        Attempts ``self._sdk.accounting.query_settlement(account, period)``
        where *period* is derived from *begin_date* / *end_date*.
        Falls back to an empty list if the SDK method is not available.

        Args:
            account: Optional account reference. Falls back to stored account.
            begin_date: Start date string (e.g. ``"2026-01-01"``).
            end_date: End date string (e.g. ``"2026-03-01"``).

        Returns:
            List of settlement/profit-loss records, or ``[]`` on failure.
        """
        acct = account if account is not None else self._account
        try:
            if hasattr(self._sdk, "accounting") and hasattr(
                self._sdk.accounting, "query_settlement"
            ):
                kwargs: dict[str, Any] = {}
                if begin_date is not None:
                    kwargs["begin_date"] = begin_date
                if end_date is not None:
                    kwargs["end_date"] = end_date
                result = self._sdk.accounting.query_settlement(acct, **kwargs)
                self.log.info(
                    "fubon_list_profit_loss",
                    count=len(result) if isinstance(result, list) else 1,
                    begin_date=begin_date,
                    end_date=end_date,
                )
                return result if isinstance(result, list) else [result]
            self.log.warning(
                "fubon_list_profit_loss_unavailable",
                reason="SDK method not found",
            )
            return []
        except Exception as exc:
            self.log.error(
                "fubon_list_profit_loss_failed",
                error=str(exc),
                begin_date=begin_date,
                end_date=end_date,
            )
            return []
