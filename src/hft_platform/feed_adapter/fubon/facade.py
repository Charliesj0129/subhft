"""Fubon broker client facade — integration layer composing all Fubon runtime modules.

Mirrors ``ShioajiClientFacade`` and satisfies ``BrokerProtocol`` so the two
brokers can be used interchangeably at runtime.  Each ``BrokerProtocol``
method delegates to the appropriate runtime/gateway module.

When optional runtime modules (contracts, subscription manager) are not yet
available, lightweight inline stubs are used so the facade remains importable
and instantiable.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime

logger = structlog.get_logger("feed_adapter.fubon.facade")


# ---------------------------------------------------------------------- #
# Inline fallback stubs for modules that may not exist yet
# ---------------------------------------------------------------------- #


class _ContractsStub:
    """Minimal stub for ``FubonContractsRuntime`` until the real module lands."""

    __slots__ = ("symbols",)

    def __init__(self) -> None:
        self.symbols: list[str] = []

    def validate_symbols(self) -> list[str]:
        return []

    def get_exchange(self, symbol: str) -> str:  # noqa: ARG002
        return ""

    def reload_symbols(self) -> None:
        pass

    def refresh_status(self) -> dict[str, Any]:
        return {"status": "stub"}


class _SubscriptionStub:
    """Minimal stub for ``FubonSubscriptionManager`` until the real module lands."""

    __slots__ = ("_qr",)

    def __init__(self, quote_runtime: FubonQuoteRuntime) -> None:
        self._qr = quote_runtime

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:  # noqa: ARG002
        pass

    def resubscribe(self) -> bool:
        return False

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],  # noqa: ARG002
        on_deal: Callable[..., Any],  # noqa: ARG002
    ) -> None:
        pass


# ---------------------------------------------------------------------- #
# Facade
# ---------------------------------------------------------------------- #


class FubonClientFacade:
    """Explicit facade over Fubon runtime modules.

    Satisfies ``BrokerProtocol`` via structural subtyping.  Every public
    method delegates to a dedicated runtime/gateway — no ``__getattr__``
    passthrough.
    """

    __slots__ = (
        "_sdk",
        "_config",
        "_config_path",
        "_logged_in",
        "session_runtime",
        "quote_runtime",
        "contracts_runtime",
        "order_gateway",
        "account_gateway",
        "subscription_manager",
    )

    def __init__(
        self,
        config_path: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        # Lazy-import SDK — allow stub mode when fubon_neo is not installed.
        try:
            from fubon_neo.sdk import FubonSDK

            self._sdk: Any = FubonSDK()
        except ImportError:
            self._sdk = None
            logger.warning(
                "fubon_neo not installed; FubonClientFacade will operate in stub mode",
            )

        self._config: dict[str, Any] = config or {}
        self._config_path = config_path
        self._logged_in = False

        # Session
        self.session_runtime = FubonSessionRuntime(self._sdk)

        # Quote / market data
        self.quote_runtime = FubonQuoteRuntime(self._sdk)

        # Orders
        self.order_gateway = FubonOrderGateway(sdk=self._sdk)

        # Account
        self.account_gateway = FubonAccountGateway(self._sdk)

        # Contracts — may not exist yet, use inline stub
        try:
            from hft_platform.feed_adapter.fubon.contracts_runtime import (
                FubonContractsRuntime,
            )

            self.contracts_runtime: Any = FubonContractsRuntime(
                self._sdk,
                config_path,
                self._config,
            )
        except (ImportError, TypeError):
            self.contracts_runtime = _ContractsStub()

        # Subscription manager — may not exist yet, use inline stub
        try:
            from hft_platform.feed_adapter.fubon.subscription_manager import (
                FubonSubscriptionManager,
            )

            self.subscription_manager: Any = FubonSubscriptionManager(
                sdk=self._sdk,
                quote_runtime=self.quote_runtime,
                symbols=getattr(self.contracts_runtime, "symbols", []),
            )
        except (ImportError, TypeError):
            self.subscription_manager = _SubscriptionStub(self.quote_runtime)

        logger.info(
            "FubonClientFacade initialized",
            config_path=config_path,
            sdk_available=self._sdk is not None,
        )

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
        result = self.session_runtime.login(*args, **kwargs)
        self._logged_in = bool(result)
        return self._logged_in

    def reconnect(self, *args: Any, **kwargs: Any) -> bool:
        result = self.session_runtime.login(*args, **kwargs)
        self._logged_in = bool(result)
        return self._logged_in

    def close(self, logout: bool = False) -> None:
        self.quote_runtime.stop()
        if logout:
            self.session_runtime.logout()
        self._logged_in = False

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        self.subscription_manager.subscribe_basket(cb)

    def fetch_snapshots(self) -> list[Any]:
        return []

    def reload_symbols(self) -> None:
        self.contracts_runtime.reload_symbols()

    def resubscribe(self) -> bool:
        return self.subscription_manager.resubscribe()

    def get_exchange(self, symbol: str) -> str:
        return self.contracts_runtime.get_exchange(symbol)

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        self.subscription_manager.set_execution_callbacks(on_order, on_deal)

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(self, **kwargs: Any) -> Any:
        return self.order_gateway.place_order(**kwargs)

    def cancel_order(self, trade: Any) -> Any:
        return self.order_gateway.cancel_order(trade)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        return self.order_gateway.update_order(trade, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        return self.account_gateway.get_inventories()

    def get_account_balance(self, account: Any = None) -> Any:  # noqa: ARG002
        return self.account_gateway.get_accounting()

    def get_margin(self, account: Any = None) -> Any:  # noqa: ARG002
        return self.account_gateway.get_margin()

    def list_position_detail(self, account: Any = None) -> list[Any]:  # noqa: ARG002
        return []

    def list_profit_loss(
        self,
        account: Any = None,  # noqa: ARG002
        begin_date: str | None = None,  # noqa: ARG002
        end_date: str | None = None,  # noqa: ARG002
    ) -> list[Any]:
        return []

    # ------------------------------------------------------------------ #
    # Symbols
    # ------------------------------------------------------------------ #

    def validate_symbols(self) -> list[str]:
        return self.contracts_runtime.validate_symbols()

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return self.contracts_runtime.refresh_status()
