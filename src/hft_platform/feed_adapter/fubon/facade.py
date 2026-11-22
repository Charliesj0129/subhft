"""FubonClientFacade — BrokerProtocol-conformant composition layer.

Integrates all Fubon runtimes/gateways into a single facade that can be
used interchangeably with ``ShioajiClientFacade`` via the structural
``BrokerProtocol`` protocol.

Each BrokerProtocol method delegates to the appropriate sub-component
using explicit delegation (no ``__getattr__``).
"""

from __future__ import annotations

import os
from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.execution_callbacks import (
    FubonCallbackRegistrationError,
    FubonExecutionCallbackAdapter,
    bridge_order_callback,
)
from hft_platform.feed_adapter.fubon.session_runtime import FubonSimSafetyError, build_fubon_sdk

# Lazy SDK import — fubon_neo may not be installed.
try:
    from fubon_neo.sdk import FubonSDK
except ImportError:
    FubonSDK = None

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

        def set_account(self, account: Any) -> None:
            pass


try:
    from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway as _AccountGW
except ImportError:

    class _AccountGW:  # type: ignore[no-redef]
        """Inline stub when account_gateway is not yet available."""

        __slots__ = ()

        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        def set_account(self, account: Any) -> None:
            pass

        def get_positions(self) -> list[Any] | None:
            return None

        def get_account_balance(self, account: Any = None) -> Any:
            return {}

        def get_margin(self, account: Any = None) -> Any:
            return None

        def list_position_detail(self, account: Any = None) -> list[Any]:
            return []

        def list_profit_loss(
            self,
            account: Any = None,
            begin_date: str | None = None,
            end_date: str | None = None,
        ) -> list[Any]:
            return []


logger = structlog.get_logger("feed_adapter.fubon.facade")


class _SessionAdapter:
    """Wraps the raw FubonSessionRuntime with a stable interface for the facade.

    Provides ``is_logged_in``, ``account``, ``login``, ``reconnect``, and
    ``logout``.  Without an SDK (not installed, no broker config, or refused by
    the simulation-safety guard) every login attempt fails closed.

    ``sdk_url`` is the endpoint the SDK was built with; the runtime checks it
    before authenticating outside live ``HFT_ORDER_MODE``.
    """

    __slots__ = ("_rt",)

    def __init__(self, sdk: Any, sdk_url: str | None = None) -> None:
        self._rt: Any = None
        if _RawSessionRT is not None and sdk is not None:
            self._rt = _RawSessionRT(sdk, {"sdk_url": sdk_url})

    @property
    def is_logged_in(self) -> bool:
        return self._rt is not None and self._rt.is_logged_in

    @property
    def account(self) -> Any:
        return None if self._rt is None else self._rt.account

    def login_with_retry(self, *args: Any, **kwargs: Any) -> bool:
        """Single login attempt (no retry: repeating a rejected credential risks a lockout)."""
        if self._rt is None:
            return False
        try:
            return bool(self._rt.login(*args, **kwargs))
        except NotImplementedError:
            return False

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if self._rt is None:
            return False
        try:
            return bool(self._rt.reconnect(reason=reason, force=force))
        except NotImplementedError:
            return False

    def logout(self) -> None:
        if self._rt is None:
            return
        try:
            self._rt.logout()
        except NotImplementedError:
            pass


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
        "_on_order_cb",
        "_on_deal_cb",
        "_exec_adapter",
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

        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._exec_adapter: FubonExecutionCallbackAdapter | None = None

        # Instantiate SDK (None if fubon_neo not installed).  Constructing it
        # connects the trading websocket, so outside live HFT_ORDER_MODE it is
        # only built against the official test endpoint; without one it is not
        # built at all and login fails closed.
        sdk: Any = None
        sdk_url: str | None = None
        if FubonSDK is not None and broker_config:
            try:
                sdk, sdk_url = build_fubon_sdk(FubonSDK)
            except FubonSimSafetyError as exc:
                self.log.error("fubon_sdk_refused", error=str(exc))
        self._sdk = sdk

        # Compose sub-components.
        self._session_runtime = _SessionAdapter(sdk, sdk_url)
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

    def _bind_session(self) -> bool:
        """After a successful (re)login: hand the account to the gateways, re-wire fills.

        Returns False (and logs out) when the execution callbacks cannot be wired:
        a session whose fills go nowhere must not stay up.
        """
        account = self._session_runtime.account
        self._order_gateway.set_account(account)
        self._account_gateway.set_account(account)
        if self._on_order_cb is not None and self._on_deal_cb is not None:
            try:
                self._wire_execution_callbacks()
            except FubonCallbackRegistrationError as exc:
                self.log.error("fubon_login_failed", reason="execution callbacks", error=str(exc))
                self._session_runtime.logout()
                return False
        return True

    def login(self, *args: Any, **kwargs: Any) -> bool:
        return self._session_runtime.login_with_retry(*args, **kwargs) and self._bind_session()

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        return self._session_runtime.reconnect(reason=reason, force=force) and self._bind_session()

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

    def _wire_execution_callbacks(self) -> None:
        """Register the stored platform callbacks on the SDK (raises if it cannot)."""
        on_order = self._on_order_cb
        on_deal = self._on_deal_cb
        if self._sdk is None or on_order is None or on_deal is None:
            raise FubonCallbackRegistrationError("no SDK or no execution callbacks to wire")
        adapter = FubonExecutionCallbackAdapter(self._sdk)
        adapter.register(on_order=bridge_order_callback(on_order), on_deal=on_deal)
        self._exec_adapter = adapter

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store the platform's ``on_order(state, payload)`` / ``on_deal(payload)`` and wire them.

        Raises ``FubonCallbackRegistrationError`` when the SDK is absent or lacks a
        required setter: orders must not be placed on a session that cannot see fills.
        """
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        self._wire_execution_callbacks()

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
        price: int | None = None,
        qty: int | None = None,
    ) -> Any:
        return self._order_gateway.update_order(trade, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any] | None:
        return self._account_gateway.get_positions()

    def get_account_balance(self, account: Any = None) -> Any:
        return self._account_gateway.get_account_balance(account)

    def get_margin(self, account: Any = None) -> Any:
        return self._account_gateway.get_margin(account)

    def list_position_detail(self, account: Any = None) -> list[Any]:
        return self._account_gateway.list_position_detail(account)

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        return self._account_gateway.list_profit_loss(account, begin_date, end_date)

    # ------------------------------------------------------------------ #
    # Symbols / contracts
    # ------------------------------------------------------------------ #

    def validate_symbols(self) -> list[str]:
        return self._contracts_runtime.validate_symbols()

    def get_default_account_id(self) -> str:
        """Return the canonical Fubon account ID from environment config."""
        return os.getenv("HFT_FUBON_ACCOUNT", "")

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return self._contracts_runtime.refresh_status()
