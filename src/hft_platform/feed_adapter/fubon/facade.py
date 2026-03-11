"""Fubon (富邦) broker client facade.

Mirrors ``ShioajiClientFacade``'s interface pattern. Composes dedicated
runtime/gateway modules behind a single entry-point so that the rest of the
platform can interact with Fubon through the same surface as Shioaji.

SDK import is **lazy** — ``fubon_neo`` is only imported inside ``login()`` so
the module can be loaded (and tested with mocks) without the SDK installed.
"""

from __future__ import annotations

from typing import Any, Callable

import structlog

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.contracts_runtime import FubonContractsRuntime
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

log = structlog.get_logger()


class FubonClientFacade:
    """Unified facade for the Fubon broker.

    Composes:
    - :class:`FubonSessionRuntime` — login, session refresh
    - :class:`FubonQuoteRuntime` — market data subscriptions
    - :class:`FubonOrderGateway` — order execution
    - :class:`FubonAccountGateway` — account queries
    - :class:`FubonContractsRuntime` — symbol/contract lookup

    The facade intentionally avoids ``__getattr__`` passthrough to keep
    responsibilities explicit and reviewable (same rationale as
    ``ShioajiClientFacade``).
    """

    __slots__ = (
        "_sdk",
        "_config",
        "_logged_in",
        "_on_tick",
        "_on_bidask",
        "session_runtime",
        "quote_runtime",
        "contracts_runtime",
        "order_gateway",
        "account_gateway",
        "log",
    )

    def __init__(
        self,
        config_path: str | None = None,
        broker_config: dict[str, Any] | None = None,
    ) -> None:
        """Initialize Fubon facade.

        The SDK instance is *not* created here — it is lazy-loaded in
        :meth:`login` so that the facade can be constructed (and tested)
        without the ``fubon_neo`` package installed.
        """
        self._sdk: Any = None
        self._config: dict[str, Any] = broker_config or {}
        if config_path is not None:
            self._config.setdefault("config_path", config_path)
        self._logged_in: bool = False
        self._on_tick: Callable[..., Any] = _noop
        self._on_bidask: Callable[..., Any] = _noop

        # Sub-components are ``None`` until ``login()`` creates them.
        self.session_runtime: FubonSessionRuntime | None = None
        self.quote_runtime: FubonQuoteRuntime | None = None
        self.contracts_runtime: FubonContractsRuntime | None = None
        self.order_gateway: FubonOrderGateway | None = None
        self.account_gateway: FubonAccountGateway | None = None
        self.log = log.bind(broker="fubon")

    # ------------------------------------------------------------------ #
    # Properties
    # ------------------------------------------------------------------ #

    @property
    def logged_in(self) -> bool:
        return self._logged_in

    @property
    def sdk(self) -> Any:
        """Raw SDK handle (``None`` before login)."""
        return self._sdk

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _require_login(self, action: str) -> None:
        """Raise if the facade has not been logged in yet."""
        if not self._logged_in:
            raise RuntimeError(f"Cannot {action} before login")

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def login(self) -> bool:
        """Create the SDK instance, wire sub-components, and login.

        Returns ``True`` on success, ``False`` on failure.
        """
        try:
            import fubon_neo  # noqa: F811 — lazy import
        except ImportError as exc:
            self.log.error("fubon_neo SDK not installed", error=str(exc))
            return False

        try:
            sdk = fubon_neo.FubonSDK()
            self._sdk = sdk

            # Wire sub-components with the live SDK handle.
            self.session_runtime = FubonSessionRuntime(config=self._config)
            self.session_runtime._sdk = sdk
            self.quote_runtime = FubonQuoteRuntime(sdk)
            self.contracts_runtime = FubonContractsRuntime(sdk)
            self.order_gateway = FubonOrderGateway(sdk)
            self.account_gateway = FubonAccountGateway(sdk)

            # Delegate actual login to session runtime.
            result = self.session_runtime.login()
            self._logged_in = bool(result)
            self.log.info("Fubon login complete", success=self._logged_in)
            return self._logged_in
        except Exception as exc:
            self.log.error("Fubon login failed", error=str(exc))
            self._logged_in = False
            return False

    def logout(self) -> None:
        """Logout and tear down sub-components."""
        if self.quote_runtime is not None:
            self.quote_runtime.stop()
        if self.session_runtime is not None:
            self.session_runtime.logout()
        self._logged_in = False
        self.log.info("Fubon logout complete")

    # ------------------------------------------------------------------ #
    # Market data
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, symbols: list[str]) -> None:
        """Subscribe to market data for *symbols* via quote runtime."""
        self._require_login("subscribe")
        assert self.quote_runtime is not None  # guarded by _require_login
        self.quote_runtime.subscribe(symbols)

    def unsubscribe_basket(self, symbols: list[str]) -> None:
        """Unsubscribe from market data for *symbols*."""
        self._require_login("unsubscribe")
        assert self.quote_runtime is not None  # guarded by _require_login
        self.quote_runtime.unsubscribe(symbols)

    def set_on_tick(self, callback: Callable[..., Any]) -> None:
        """Register tick callback on quote runtime."""
        self._require_login("set callbacks")
        self._on_tick = callback
        assert self.quote_runtime is not None  # guarded by _require_login
        self.quote_runtime.register_quote_callbacks(
            on_tick=callback,
            on_bidask=self._on_bidask,
        )

    def set_on_bidask(self, callback: Callable[..., Any]) -> None:
        """Register bid/ask callback on quote runtime."""
        self._require_login("set callbacks")
        self._on_bidask = callback
        assert self.quote_runtime is not None  # guarded by _require_login
        self.quote_runtime.register_quote_callbacks(
            on_tick=self._on_tick,
            on_bidask=callback,
        )

    def fetch_snapshots(self) -> list[Any]:
        """Fetch latest snapshots (not yet implemented for Fubon)."""
        self.log.warning("fetch_snapshots not implemented for Fubon")
        return []

    # ------------------------------------------------------------------ #
    # Order execution
    # ------------------------------------------------------------------ #

    def place_order(self, contract: Any, order: Any) -> Any:
        """Delegate to order gateway."""
        self._require_login("place orders")
        assert self.order_gateway is not None  # guarded by _require_login
        return self.order_gateway.place_order(contract, order)

    def cancel_order(self, order_id: str) -> Any:
        """Cancel an order by ID."""
        self._require_login("cancel orders")
        assert self.order_gateway is not None  # guarded by _require_login
        return self.order_gateway.cancel_order(order_id)

    def update_order(self, order_id: str, price: int, qty: int) -> Any:
        """Update an existing order."""
        self._require_login("update orders")
        assert self.order_gateway is not None  # guarded by _require_login
        return self.order_gateway.update_order(order_id, price=price, qty=qty)

    # ------------------------------------------------------------------ #
    # Account queries
    # ------------------------------------------------------------------ #

    def get_positions(self) -> list[Any]:
        """Get current positions via account gateway."""
        self._require_login("query positions")
        assert self.account_gateway is not None  # guarded by _require_login
        return self.account_gateway.get_positions()

    def get_account_balance(self, account: Any = None) -> Any:
        """Get account balance."""
        self._require_login("query balance")
        assert self.account_gateway is not None  # guarded by _require_login
        return self.account_gateway.get_account_balance(account=account)

    def get_margin(self, account: Any = None) -> Any:
        """Get margin information."""
        self._require_login("query margin")
        assert self.account_gateway is not None  # guarded by _require_login
        return self.account_gateway.get_margin(account=account)

    # ------------------------------------------------------------------ #
    # Contracts
    # ------------------------------------------------------------------ #

    def validate_symbols(self) -> list[str]:
        """Validate configured symbols against Fubon contract list."""
        self._require_login("validate symbols")
        assert self.contracts_runtime is not None  # guarded by _require_login
        return self.contracts_runtime.validate_symbols()

    def get_contract_refresh_status(self) -> dict[str, object]:
        """Return contract refresh status."""
        self._require_login("get refresh status")
        assert self.contracts_runtime is not None  # guarded by _require_login
        return self.contracts_runtime.refresh_status()

    # ------------------------------------------------------------------ #
    # Shutdown
    # ------------------------------------------------------------------ #

    def close(self, logout: bool = False) -> None:
        """Close connections. Optionally logout."""
        if logout:
            self.logout()
        else:
            if self.quote_runtime is not None:
                self.quote_runtime.stop()
            self.log.info("Fubon facade closed")

    def shutdown(self, logout: bool = False) -> None:
        """Alias for :meth:`close`."""
        self.close(logout=logout)


def _noop(*_args: Any, **_kwargs: Any) -> None:
    """No-op placeholder callback."""
