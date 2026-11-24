"""Fubon TradeAPI client implementing BrokerProtocol (duck-typed).

Composes real sub-components (FubonOrderGateway, FubonAccountGateway,
FubonQuoteRuntime, FubonSnapshotFetcher) and delegates all operations to them.
FubonClient itself is a lightweight shim.

Login goes through ``FubonSessionRuntime`` and the SDK is built by
``build_fubon_sdk``: outside live ``HFT_ORDER_MODE`` it targets the official test
endpoint or is never constructed, so a simulation run cannot reach production.
Credentials come from ``HFT_FUBON_*`` environment variables only and are not
kept on this object.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.account_gateway import (
    FubonAccountGateway,
)
from hft_platform.feed_adapter.fubon.execution_callbacks import (
    FubonCallbackRegistrationError,
    FubonExecutionCallbackAdapter,
    bridge_order_callback,
)
from hft_platform.feed_adapter.fubon.order_codec import translate_adapter_order_kwargs
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.session_runtime import (
    FubonSessionRuntime,
    FubonSimSafetyError,
    build_fubon_sdk,
)
from hft_platform.feed_adapter.fubon.snapshot_fetcher import FubonSnapshotFetcher

try:
    from fubon_neo.sdk import FubonSDK
except ImportError:
    FubonSDK = None

logger = structlog.get_logger(__name__)


class FubonClient:
    """Fubon TradeAPI client implementing BrokerProtocol.

    Delegates to ``FubonOrderGateway``, ``FubonAccountGateway``, and
    ``FubonQuoteRuntime`` which contain the real logic.  The interface
    mirrors ``ShioajiClientFacade`` so the two brokers can be used
    interchangeably via duck-typed BrokerProtocol.
    """

    __slots__ = (
        "_api",
        "_logged_in",
        "_session",
        "_on_order_cb",
        "_on_deal_cb",
        "_exec_adapter",
        "_order_gateway",
        "_account_gateway",
        "_quote_runtime",
        "_symbols",
    )

    def __init__(self) -> None:
        import warnings

        warnings.warn(
            "FubonClient is deprecated, use FubonClientFacade from hft_platform.feed_adapter.fubon.facade instead",
            DeprecationWarning,
            stacklevel=2,
        )
        self._api: Any = None
        self._logged_in: bool = False
        self._session: FubonSessionRuntime | None = None
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._exec_adapter: FubonExecutionCallbackAdapter | None = None
        self._symbols: list[str] = []

        # Sub-components start with sdk=None (graceful degradation).
        # Re-initialised with a real SDK handle after login().
        self._order_gateway: FubonOrderGateway = FubonOrderGateway(sdk=None)
        self._account_gateway: FubonAccountGateway = FubonAccountGateway(sdk=None)
        self._quote_runtime: FubonQuoteRuntime = FubonQuoteRuntime(sdk=None)

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

    @staticmethod
    def _discard_sdk(sdk: Any) -> None:
        """Best-effort shutdown of an SDK whose login did not complete."""
        try:
            sdk.shutdown()
        except Exception as exc:
            logger.warning("fubon_sdk_shutdown_failed", error=str(exc))

    def login(self, *args: Any, **kwargs: Any) -> bool:
        """Create FubonSDK, authenticate, and wire sub-components.

        Arguments are forwarded to ``FubonSessionRuntime.login``
        (``api_key, password, cert_path, *, personal_id, cert_pass``); anything
        omitted comes from the ``HFT_FUBON_*`` environment.
        """
        if FubonSDK is None:
            logger.error("fubon_neo SDK not installed; cannot login")
            return False
        try:
            sdk, sdk_url = build_fubon_sdk(FubonSDK)
        except FubonSimSafetyError as exc:
            logger.error("fubon_login_refused", error=str(exc))
            return False
        except Exception as exc:
            logger.error("fubon_sdk_init_failed", error=str(exc))
            return False

        session = FubonSessionRuntime(sdk, {"sdk_url": sdk_url})
        if not session.login(*args, **kwargs):
            self._discard_sdk(sdk)
            return False

        account = session.account
        self._api = sdk
        self._session = session
        self._order_gateway = FubonOrderGateway(sdk=sdk, account=account)
        self._account_gateway = FubonAccountGateway(sdk=sdk, account=account)
        # A second login replaces the runtime: stop the old one first, or its socket and its slot stay held.
        self._quote_runtime.stop()
        self._quote_runtime = FubonQuoteRuntime(sdk=sdk)
        if self._on_order_cb is not None and self._on_deal_cb is not None:
            try:
                self._wire_execution_callbacks()
            except FubonCallbackRegistrationError as exc:
                logger.error("fubon_login_failed", reason="execution callbacks", error=str(exc))
                self._discard_sdk(sdk)
                self._api = None
                self._session = None
                return False
        self._logged_in = True
        logger.info("fubon_login_ok", sdk_target="test" if sdk_url else "vendor_default")
        return True

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect by closing the current session and logging in again."""
        logger.info("fubon_reconnect", reason=reason, force=force)
        self.close(logout=True)
        return self.login()

    def close(self, logout: bool = False) -> None:
        """Stop quote runtime, optionally log out, and mark the session closed."""
        self._quote_runtime.stop()
        if logout and self._session is not None:
            self._session.logout()
        self._logged_in = False
        logger.info("fubon_close", logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        """Graceful shutdown — delegates to close()."""
        self.close(logout=logout)

    # ------------------------------------------------------------------ #
    # Quote / subscription
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Register *cb* as both tick and bidask handler, then subscribe."""
        self._quote_runtime.register_quote_callbacks(cb, cb)
        if self._symbols:
            self._quote_runtime.subscribe(self._symbols)

    def fetch_snapshots(self) -> list[Any]:
        """Fetch intraday snapshots for the stored symbols via ``FubonSnapshotFetcher``.

        Returns ``[]`` before login (there is no SDK to ask) or when no symbols
        are stored; per-symbol failures are isolated by the fetcher.
        """
        if self._api is None:
            return []
        return FubonSnapshotFetcher(self._api).fetch_snapshots(self._symbols)

    def resubscribe(self) -> bool:
        """Stop current subscriptions and re-subscribe to stored symbols."""
        self._quote_runtime.stop()
        if self._symbols:
            # Re-create runtime with current SDK to get a fresh state.
            self._quote_runtime = FubonQuoteRuntime(sdk=self._api)
            self._quote_runtime.subscribe(self._symbols)
        return True

    def reload_symbols(self) -> None:
        """Reload symbol list (no-op until contracts_runtime is available)."""
        logger.warning("fubon_reload_symbols: not yet implemented")

    # ------------------------------------------------------------------ #
    # Execution callbacks
    # ------------------------------------------------------------------ #

    def _wire_execution_callbacks(self) -> None:
        """Register the stored callbacks on the live SDK (raises if it cannot)."""
        on_order = self._on_order_cb
        on_deal = self._on_deal_cb
        if on_order is None or on_deal is None:
            raise FubonCallbackRegistrationError("execution callbacks are not set")
        adapter = FubonExecutionCallbackAdapter(self._api)
        adapter.register(on_order=bridge_order_callback(on_order), on_deal=on_deal)
        self._exec_adapter = adapter

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store the platform callbacks and wire them into the SDK once logged in.

        ``on_order(state, payload)`` and ``on_deal(payload)`` are the platform's
        shapes.  Raises ``FubonCallbackRegistrationError`` when the SDK cannot
        deliver execution reports (startup must not continue without fills).
        """
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        if self._api is not None:
            self._wire_execution_callbacks()
        logger.info("fubon_execution_callbacks_set", wired=self._exec_adapter is not None)

    # ------------------------------------------------------------------ #
    # Order operations
    # ------------------------------------------------------------------ #

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate to FubonOrderGateway.place_order, translating OrderAdapter's kwargs first."""
        if "contract_code" in kwargs:
            kwargs = translate_adapter_order_kwargs(kwargs)
        return self._order_gateway.place_order(*args, **kwargs)

    def cancel_order(self, trade: Any) -> Any:
        """Delegate to FubonOrderGateway.cancel_order."""
        return self._order_gateway.cancel_order(trade)

    def update_order(
        self,
        trade: Any,
        price: int | None = None,
        qty: int | None = None,
    ) -> Any:
        """Delegate to FubonOrderGateway.update_order."""
        return self._order_gateway.update_order(trade, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account queries
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any] | None:
        """Delegate to FubonAccountGateway.get_positions (None = broker state unknown)."""
        return self._account_gateway.get_positions()

    def get_account_balance(self, account: Any = None) -> Any:
        """Delegate to FubonAccountGateway.get_account_balance."""
        return self._account_gateway.get_account_balance(account)

    def get_margin(self, account: Any = None) -> Any:
        """Delegate to FubonAccountGateway.get_margin."""
        return self._account_gateway.get_margin(account)

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        """Delegate to FubonAccountGateway.list_profit_loss."""
        return self._account_gateway.list_profit_loss(account, begin_date, end_date)

    def list_position_detail(self, account: Any = None) -> Any:
        """Delegate to FubonAccountGateway.list_position_detail."""
        return self._account_gateway.list_position_detail(account)

    # ------------------------------------------------------------------ #
    # Contract helpers
    # ------------------------------------------------------------------ #

    def get_exchange(self, symbol: str) -> str:
        """Return exchange string (placeholder until contracts_runtime)."""
        return ""

    def validate_symbols(self) -> list[str]:
        """Return empty list (placeholder until contracts_runtime)."""
        return []

    def get_contract_refresh_status(self) -> dict[str, Any]:
        """Return empty dict (placeholder until contracts_runtime)."""
        return {}
