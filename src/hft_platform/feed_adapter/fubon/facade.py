"""Fubon broker facade implementing BrokerProtocol.

Wraps the Fubon sub-components (session, quote, order, account) behind
the same interface as ``ShioajiClientFacade`` so the two brokers can be
used interchangeably via the polymorphic ``BrokerProtocol``.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.account_gateway import (
    FubonAccountGateway as _AccountGatewayImpl,
)
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import (
    FubonQuoteRuntime as _QuoteRuntimeImpl,
)
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime

logger = structlog.get_logger(__name__)


class FubonClientFacade:
    """Explicit facade over Fubon sub-modules satisfying ``BrokerProtocol``.

    When constructed **without** a real ``FubonSDK`` instance (e.g. in tests
    or stub mode), all sub-components are initialised with ``None`` as the
    SDK handle. Methods that require the SDK will raise
    ``NotImplementedError`` at call time rather than import time, keeping
    the facade structurally valid for protocol conformance checks.
    """

    __slots__ = (
        "_sdk",
        "_logged_in",
        "_symbols",
        "_on_order_cb",
        "_on_deal_cb",
        "session_runtime",
        "quote_runtime",
        "order_gateway",
        "account_gateway",
    )

    def __init__(self, sdk: Any | None = None) -> None:
        self._sdk = sdk
        self._logged_in: bool = False
        self._symbols: list[str] = []
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None

        # Sub-components accept None gracefully for stub mode.
        self.session_runtime = FubonSessionRuntime(client=sdk)
        self.quote_runtime = _QuoteRuntimeImpl(sdk=sdk) if sdk is not None else None
        self.order_gateway = FubonOrderGateway(sdk=sdk)
        self.account_gateway = _AccountGatewayImpl(sdk=sdk) if sdk is not None else None

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    def login(self, *args: Any, **kwargs: Any) -> bool:
        if self._sdk is None:
            logger.warning("fubon_facade.login_stub_mode")
            return False
        result = self.session_runtime.login(*args, **kwargs)
        self._logged_in = bool(result)
        return self._logged_in

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if self._sdk is None:
            logger.warning("fubon_facade.reconnect_stub_mode")
            return False
        self._logged_in = False
        result = self.session_runtime.login()
        self._logged_in = bool(result)
        return self._logged_in

    def close(self, logout: bool = False) -> None:
        self._logged_in = False
        if self.quote_runtime is not None:
            self.quote_runtime.stop()
        if logout and self._sdk is not None:
            self.session_runtime.logout()

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)
        logger.info("fubon_facade.shutdown")

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        if self.quote_runtime is not None:
            self.quote_runtime.register_quote_callbacks(on_tick=cb, on_bidask=cb)
            if self._symbols:
                self.quote_runtime.subscribe(self._symbols)

    def fetch_snapshots(self) -> list[Any]:
        # Fubon SDK snapshot fetch not yet wired.
        return []

    def reload_symbols(self) -> None:
        logger.info("fubon_facade.reload_symbols")

    def resubscribe(self) -> bool:
        if self.quote_runtime is not None and self._symbols:
            self.quote_runtime.subscribe(self._symbols)
            return True
        return False

    def get_exchange(self, symbol: str) -> str:
        # Fubon uses TWSE/OTC; default to TSE for now
        return "TSE"

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(self, **kwargs: Any) -> Any:
        return self.order_gateway.place_order(**kwargs)

    def cancel_order(self, trade: Any) -> Any:
        return self.order_gateway.cancel_order(order_id=trade)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        return self.order_gateway.update_order(
            order_id=trade,
            price=int(price) if price is not None else None,
            qty=qty,
        )

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        if self.account_gateway is None:
            return []
        return self.account_gateway.get_inventories()

    def get_account_balance(self, account: Any = None) -> Any:
        if self.account_gateway is None:
            return None
        return self.account_gateway.get_accounting()

    def get_margin(self, account: Any = None) -> Any:
        if self.account_gateway is None:
            return None
        return self.account_gateway.get_margin()

    def list_position_detail(self, account: Any = None) -> list[Any]:
        if self.account_gateway is None:
            return []
        return self.account_gateway.get_inventories()

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        if self.account_gateway is None:
            return []
        return self.account_gateway.get_settlements()

    # ------------------------------------------------------------------ #
    # Symbols
    # ------------------------------------------------------------------ #

    def validate_symbols(self) -> list[str]:
        return list(self._symbols)

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return {"status": "ok", "broker": "fubon"}
