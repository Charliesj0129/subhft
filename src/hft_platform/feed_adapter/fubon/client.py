"""Fubon TradeAPI client implementing BrokerProtocol (duck-typed).

Composes real sub-components (FubonOrderGateway, FubonAccountGateway,
FubonQuoteRuntime) and delegates all operations to them.  FubonClient
itself is a lightweight shim.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.account_gateway import (
    FubonAccountGateway,
)
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime

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
        "_api_key",
        "_password",
        "_on_order_cb",
        "_on_deal_cb",
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
        self._api_key: str = os.environ.get("HFT_FUBON_API_KEY", "")
        self._password: str = os.environ.get("HFT_FUBON_PASSWORD", "")
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._symbols: list[str] = []

        # Sub-components start with sdk=None (graceful degradation).
        # Re-initialised with a real SDK handle after login().
        self._order_gateway: FubonOrderGateway = FubonOrderGateway(sdk=None)
        self._account_gateway: FubonAccountGateway = FubonAccountGateway(sdk=None)
        self._quote_runtime: FubonQuoteRuntime = FubonQuoteRuntime(sdk=None)

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
        """Create FubonSDK, authenticate, and wire sub-components."""
        if FubonSDK is None:
            logger.error("fubon_neo SDK not installed; cannot login")
            return False
        try:
            sdk = FubonSDK()
            accounts = sdk.login(
                self._api_key,
                self._password,
                *args,
                **kwargs,
            )
            self._api = sdk
            self._logged_in = True

            # Re-wire gateways with the live SDK handle.
            self._order_gateway = FubonOrderGateway(sdk=sdk)
            self._account_gateway = FubonAccountGateway(sdk=sdk)
            self._quote_runtime = FubonQuoteRuntime(sdk=sdk)

            logger.info(
                "fubon_login_ok",
                accounts=len(accounts) if accounts else 0,
            )
            return True
        except Exception as exc:
            logger.error("fubon_login_failed", error=str(exc))
            return False

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect by closing the current session and logging in again."""
        logger.info("fubon_reconnect", reason=reason, force=force)
        self.close(logout=True)
        return self.login()

    def close(self, logout: bool = False) -> None:
        """Stop quote runtime and mark session as closed."""
        self._quote_runtime.stop()
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
        """Return snapshot data (placeholder until snapshot API is wired)."""
        return []

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

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store order and deal callbacks for execution events."""
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        logger.info("fubon_execution_callbacks_set")

    # ------------------------------------------------------------------ #
    # Order operations
    # ------------------------------------------------------------------ #

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        """Delegate to FubonOrderGateway.place_order."""
        return self._order_gateway.place_order(*args, **kwargs)

    def cancel_order(self, trade: Any) -> Any:
        """Delegate to FubonOrderGateway.cancel_order."""
        return self._order_gateway.cancel_order(trade)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any:
        """Delegate to FubonOrderGateway.update_order."""
        return self._order_gateway.update_order(trade, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account queries
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        """Delegate to FubonAccountGateway.get_inventories."""
        return self._account_gateway.get_inventories()

    def get_account_balance(self, account: Any = None) -> Any:
        """Delegate to FubonAccountGateway.get_accounting."""
        return self._account_gateway.get_accounting()

    def get_margin(self, account: Any = None) -> Any:
        """Delegate to FubonAccountGateway.get_margin."""
        return self._account_gateway.get_margin()

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        """Return empty list (placeholder until profit/loss API is wired)."""
        return []

    def list_position_detail(self, account: Any = None) -> Any:
        """Return empty list (placeholder until position detail API is wired)."""
        return []

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
