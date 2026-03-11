"""Fubon account gateway stub."""

from __future__ import annotations

from typing import Any

from structlog import get_logger

logger = get_logger("fubon.account")


class FubonAccountGateway:
    """Stub for Fubon account queries: positions, balance, margin, P&L."""

    __slots__ = ("_sdk", "_account")

    def __init__(self, sdk: Any, account: Any) -> None:
        self._sdk = sdk
        self._account = account

    def get_positions(self) -> list[Any]:
        """Return current positions (stub)."""
        return []

    def get_account_balance(self, account: Any = None) -> Any:
        """Return account balance (stub)."""
        return None

    def get_margin(self, account: Any = None) -> Any:
        """Return margin info (stub)."""
        return None

    def list_position_detail(self, account: Any = None) -> list[Any]:
        """Return detailed position list (stub)."""
        return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        """Return P&L records (stub)."""
        return []
