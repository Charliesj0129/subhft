"""FubonClientFacade — BrokerProtocol-conformant composition layer.

Integrates all Fubon runtimes/gateways into a single facade that can be
used interchangeably with ``ShioajiClientFacade`` via the structural
``BrokerProtocol`` protocol.

Each BrokerProtocol method delegates to the appropriate sub-component
using explicit delegation (no ``__getattr__``).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.execution_callbacks import (
    FubonCallbackRegistrationError,
    FubonExecutionCallbackAdapter,
    bridge_order_callback,
)
from hft_platform.feed_adapter.fubon.order_codec import translate_adapter_order_kwargs
from hft_platform.feed_adapter.fubon.session_runtime import FubonSimSafetyError, build_fubon_sdk
from hft_platform.feed_adapter.fubon.subscription_manager import FubonSubscriptionManager, taipei_now

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


class FubonSubscriptionConfigError(RuntimeError):
    """No usable Fubon subscription list: refuse to subscribe to nothing silently."""


# Strategy aliases (TMFR1, TXFR2, C0/C1 forms). Fubon cannot subscribe to them
# until alias resolution exists, so they are rejected rather than dropped quietly.
_ALIAS_CODE_RE = re.compile(r"^[A-Z]+[RC][0-2]$")


def _configured_subscription_codes() -> list[str]:
    """Codes from HFT_SYMBOLS (the operator's --symbols list), validated fail-closed."""
    raw = os.getenv("HFT_SYMBOLS", "")
    codes = [code.strip() for code in raw.split(",") if code.strip()]
    if not codes:
        raise FubonSubscriptionConfigError(
            "HFT_SYMBOLS is empty: export HFT_SYMBOLS=<codes> (the run --symbols flag does not set it)"
        )
    aliases = [code for code in codes if _ALIAS_CODE_RE.match(code)]
    if aliases:
        raise FubonSubscriptionConfigError(f"alias codes cannot be subscribed on Fubon yet: {aliases}")
    return codes


def _session_clock() -> dt.datetime:
    """Clock for the futopt day/night flag. Read at call time so a test can pin it."""
    return taipei_now()


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
        # Built in subscribe_basket from HFT_SYMBOLS; None until then.
        self._subscription_manager: FubonSubscriptionManager | None = None

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
        manager = self._subscription_manager
        if manager is None:
            # Nothing was subscribed, so there is nothing to restore. Log in only when the session
            # is down (orders need it). force is not honored here: the monitor keeps a rollover
            # reconnect pending until one returns True, so a forced relogin would repeat every
            # reconnect cooldown. Success is never reported: CONNECTED means the basket is subscribed.
            if not self._session_runtime.is_logged_in and self._session_runtime.reconnect(reason=reason, force=force):
                self._bind_session()
            return False
        if not (self._session_runtime.reconnect(reason=reason, force=force) and self._bind_session()):
            return False
        # The relogin replaces the session the basket was subscribed on, so subscribe again
        # before reporting success, as the Shioaji reconnect does.
        return manager.resubscribe(force=True)

    def close(self, logout: bool = False) -> None:
        if self._subscription_manager is not None:
            self._subscription_manager.close()
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
        codes = _configured_subscription_codes()
        if self._subscription_manager is not None:
            self._subscription_manager.close()
        # The lambda reads _session_clock on each call, so a pinned clock takes effect.
        manager = FubonSubscriptionManager(self._quote_runtime, codes, session_clock=lambda: _session_clock())
        manager.subscribe_basket(cb)
        self._subscription_manager = manager

    def fetch_snapshots(self) -> list[Any]:
        return []

    def reload_symbols(self) -> None:
        self._contracts_runtime.reload_symbols()

    def resubscribe(self) -> bool:
        if self._subscription_manager is None:
            return False
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
        """Place a futopt order; OrderAdapter's kwargs (keyed by contract_code) are translated first."""
        if "contract_code" in kwargs:
            kwargs = translate_adapter_order_kwargs(kwargs)
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
        """Return the account selected at login, which is the one fills are booked under.

        ``HFT_FUBON_ACCOUNT`` only chooses among several accounts; with one account (or
        before any login) it may be unset, so reading it alone answered "" for a logged-in
        session and every fill was then rejected as having no account.  Falls back to the
        environment value only when no account has been selected yet.
        """
        account = self._session_runtime.account
        selected = "" if account is None else str(getattr(account, "account", "") or "")
        return selected or os.getenv("HFT_FUBON_ACCOUNT", "")

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return self._contracts_runtime.refresh_status()
