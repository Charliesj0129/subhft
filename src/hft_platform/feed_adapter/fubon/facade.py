"""FubonClientFacade — BrokerProtocol-conformant composition layer.

Integrates all Fubon runtimes/gateways into a single facade that can be
used interchangeably with ``ShioajiClientFacade`` via the structural
``BrokerProtocol`` protocol.

Each BrokerProtocol method delegates to the appropriate sub-component
using explicit delegation (no ``__getattr__``).
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

# Lazy SDK import — fubon_neo may not be installed.
try:
    from fubon_neo.sdk import FubonSDK
except ImportError:
    FubonSDK = None  # type: ignore[assignment,misc]

# Sub-component imports with inline stub fallbacks for independent mergeability.
try:
    from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime as _RawSessionRT
except ImportError:
    _RawSessionRT = None  # type: ignore[assignment,misc]

try:
    from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime as _QuoteRT
except ImportError:

    class _QuoteRT:  # type: ignore[no-redef]
        """Inline stub when quote_runtime is not yet available."""

        __slots__ = ()

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def stop(self) -> None:
            pass


try:
    from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway as _OrderGW
except ImportError:

    class _OrderGW:  # type: ignore[no-redef]
        """Inline stub when order_gateway is not yet available."""

        __slots__ = ()

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def place_order(self, **kw: Any) -> Any:
            raise NotImplementedError("FubonOrderGateway stub")

        def cancel_order(self, order_id: Any) -> Any:
            raise NotImplementedError("FubonOrderGateway stub")

        def update_order(self, order_id: Any, **kw: Any) -> Any:
            raise NotImplementedError("FubonOrderGateway stub")


try:
    from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway as _AccountGW
except ImportError:

    class _AccountGW:  # type: ignore[no-redef]
        """Inline stub when account_gateway is not yet available."""

        __slots__ = ()

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def get_inventories(self) -> list[Any]:
            return []

        def get_accounting(self) -> Any:
            return None

        def get_margin(self) -> Any:
            return None


logger = structlog.get_logger("feed_adapter.fubon.facade")


class _SessionAdapter:
    """Wraps the raw FubonSessionRuntime with a stable interface for the facade.

    Provides ``is_logged_in``, ``login_with_retry``, ``reconnect``, and
    ``logout`` regardless of whether the underlying runtime has them.
    """

    __slots__ = ("_rt", "_logged_in")

    def __init__(self, sdk: Any) -> None:
        if _RawSessionRT is not None:
            self._rt = _RawSessionRT(sdk)
        else:
            self._rt = None
        self._logged_in: bool = False

    @property
    def is_logged_in(self) -> bool:
        return self._logged_in

    def login_with_retry(self, *args: Any, **kwargs: Any) -> bool:
        if self._rt is None:
            return False
        try:
            result = self._rt.login(*args, **kwargs)
            self._logged_in = bool(result)
            return self._logged_in
        except NotImplementedError:
            return False

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if self._rt is None:
            return False
        try:
            result = self._rt.login()
            self._logged_in = bool(result)
            return self._logged_in
        except NotImplementedError:
            return False

    def logout(self) -> None:
        if self._rt is None:
            return
        try:
            self._rt.logout()
        except NotImplementedError:
            pass
        self._logged_in = False


class _ContractsStub:
    """Minimal contracts runtime stub until a full ContractsRuntime is implemented."""

    __slots__ = ("_symbols_path", "_sdk")

    def __init__(self, symbols_path: str | None, sdk: Any) -> None:
        self._symbols_path = symbols_path
        self._sdk = sdk

    def reload_symbols(self) -> None:
        pass

    def validate_symbols(self) -> list[str]:
        return []

    def get_exchange(self, symbol: str) -> str:
        return ""

    def refresh_status(self) -> dict[str, Any]:
        return {"refreshed": False, "source": "stub"}


class _SubscriptionStub:
    """Minimal subscription manager stub until a full SubscriptionManager is implemented."""

    __slots__ = ("_sdk",)

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        pass

    def resubscribe(self) -> bool:
        return False

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        pass


class FubonClientFacade:
    """BrokerProtocol-conformant facade over Fubon sub-components.

    Composes session, quote, order, account, contracts, and subscription
    modules into a single object matching the ``BrokerProtocol`` interface.
    """

    __slots__ = (
        "_sdk",
        "_symbols_path",
        "_broker_config",
        "_session_runtime",
        "_quote_runtime",
        "_order_gateway",
        "_account_gateway",
        "_contracts_runtime",
        "_subscription_manager",
        "log",
    )

    def __init__(
        self,
        symbols_path: str | None = None,
        broker_config: dict[str, Any] | None = None,
    ) -> None:
        self._symbols_path = symbols_path
        self._broker_config = broker_config or {}
        self.log = logger

        # Instantiate SDK (None if fubon_neo not installed).
        sdk = FubonSDK() if FubonSDK is not None and broker_config else None
        self._sdk = sdk

        # Compose sub-components.
        self._session_runtime = _SessionAdapter(sdk)
        self._quote_runtime = _QuoteRT(sdk)
        self._order_gateway = _OrderGW(sdk=sdk)
        self._account_gateway = _AccountGW(sdk)
        self._contracts_runtime = _ContractsStub(symbols_path, sdk)
        self._subscription_manager = _SubscriptionStub(sdk)

        self.log.info(
            "FubonClientFacade initialized",
            symbols_path=symbols_path,
            sdk_available=sdk is not None,
        )

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        return self._session_runtime.is_logged_in

    # ------------------------------------------------------------------ #
    # Session lifecycle
    # ------------------------------------------------------------------ #

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return self._session_runtime.login_with_retry(*args, **kwargs)

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        return self._session_runtime.reconnect(reason=reason, force=force)

    def close(self, logout: bool = False) -> None:
        self._quote_runtime.stop()
        if logout:
            self._session_runtime.logout()
        self.log.info("FubonClientFacade closed", logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        self._subscription_manager.subscribe_basket(cb)

    def fetch_snapshots(self) -> list[Any]:
        return []

    def reload_symbols(self) -> None:
        self._contracts_runtime.reload_symbols()

    def resubscribe(self) -> bool:
        return self._subscription_manager.resubscribe()

    def get_exchange(self, symbol: str) -> str:
        return self._contracts_runtime.get_exchange(symbol)

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        self._subscription_manager.set_execution_callbacks(on_order, on_deal)

    # ------------------------------------------------------------------ #
    # Orders
    # ------------------------------------------------------------------ #

    def place_order(self, **kwargs: Any) -> Any:
        return self._order_gateway.place_order(**kwargs)

    def cancel_order(self, trade: Any) -> Any:
        return self._order_gateway.cancel_order(trade)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        return self._order_gateway.update_order(trade, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        return self._account_gateway.get_inventories()

    def get_account_balance(self, account: Any = None) -> Any:
        return self._account_gateway.get_accounting()

    def get_margin(self, account: Any = None) -> Any:
        return self._account_gateway.get_margin()

    def list_position_detail(self, account: Any = None) -> list[Any]:
        return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        return []

    # ------------------------------------------------------------------ #
    # Symbols / contracts
    # ------------------------------------------------------------------ #

    def validate_symbols(self) -> list[str]:
        return self._contracts_runtime.validate_symbols()

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return self._contracts_runtime.refresh_status()
