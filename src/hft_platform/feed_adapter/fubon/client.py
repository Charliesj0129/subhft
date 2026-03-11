"""Fubon TradeAPI client skeleton implementing BrokerProtocol (duck-typed)."""

from __future__ import annotations

import os
from typing import Any, Callable

import structlog

try:
    from fubon_neo.sdk import FubonSDK
except ImportError:
    FubonSDK = None

logger = structlog.get_logger(__name__)


class FubonClient:
    """Fubon TradeAPI client skeleton implementing BrokerProtocol.

    All methods raise ``NotImplementedError`` until the Fubon SDK integration
    is implemented.  The interface mirrors ``ShioajiClientFacade`` so the two
    brokers can be used interchangeably via duck-typed BrokerProtocol.
    """

    __slots__ = (
        "_api",
        "_logged_in",
        "_api_key",
        "_password",
        "_on_order_cb",
        "_on_deal_cb",
    )

    def __init__(self) -> None:
        import warnings

        warnings.warn(
            "FubonClient is deprecated, use FubonClientFacade from "
            "hft_platform.feed_adapter.fubon.facade instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self._api: Any = None
        self._logged_in: bool = False
        self._api_key: str = os.environ.get("HFT_FUBON_API_KEY", "")
        self._password: str = os.environ.get("HFT_FUBON_PASSWORD", "")
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None

        if not self._api_key:
            logger.warning("HFT_FUBON_API_KEY not set; login will fail")

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def api(self) -> Any:
        return self._api

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    def login(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError("FubonClient.login not yet implemented")

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        raise NotImplementedError("FubonClient.reconnect not yet implemented")

    def close(self, logout: bool = False) -> None:
        raise NotImplementedError("FubonClient.close not yet implemented")

    def shutdown(self, logout: bool = False) -> None:
        raise NotImplementedError("FubonClient.shutdown not yet implemented")

    # ------------------------------------------------------------------ #
    # Quote / subscription
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        raise NotImplementedError("FubonClient.subscribe_basket not yet implemented")

    def fetch_snapshots(self) -> list[Any]:
        raise NotImplementedError("FubonClient.fetch_snapshots not yet implemented")

    def resubscribe(self) -> bool:
        raise NotImplementedError("FubonClient.resubscribe not yet implemented")

    def reload_symbols(self) -> None:
        raise NotImplementedError("FubonClient.reload_symbols not yet implemented")

    # ------------------------------------------------------------------ #
    # Execution callbacks
    # ------------------------------------------------------------------ #

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        raise NotImplementedError("FubonClient.set_execution_callbacks not yet implemented")

    # ------------------------------------------------------------------ #
    # Order operations
    # ------------------------------------------------------------------ #

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("FubonClient.place_order not yet implemented")

    def cancel_order(self, trade: Any) -> Any:
        raise NotImplementedError("FubonClient.cancel_order not yet implemented")

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        raise NotImplementedError("FubonClient.update_order not yet implemented")

    # ------------------------------------------------------------------ #
    # Account queries
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        raise NotImplementedError("FubonClient.get_positions not yet implemented")

    def get_account_balance(self, account: Any = None) -> Any:
        raise NotImplementedError("FubonClient.get_account_balance not yet implemented")

    def get_margin(self, account: Any = None) -> Any:
        raise NotImplementedError("FubonClient.get_margin not yet implemented")

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        raise NotImplementedError("FubonClient.list_profit_loss not yet implemented")

    def list_position_detail(self, account: Any = None) -> Any:
        raise NotImplementedError("FubonClient.list_position_detail not yet implemented")

    # ------------------------------------------------------------------ #
    # Contract helpers
    # ------------------------------------------------------------------ #

    def get_exchange(self, symbol: str) -> str:
        raise NotImplementedError("FubonClient.get_exchange not yet implemented")

    def validate_symbols(self) -> list[str]:
        raise NotImplementedError("FubonClient.validate_symbols not yet implemented")
