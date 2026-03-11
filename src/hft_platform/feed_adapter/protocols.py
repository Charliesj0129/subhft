"""Broker-agnostic protocol interfaces for feed adapter consumers.

These Protocol classes define the minimal API surface that platform services
depend on, decoupling consumers from the concrete Shioaji implementation.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class MarketDataProvider(Protocol):
    """Protocol for market data subscription and snapshot retrieval.

    Consumers: ``MarketDataService`` (subscribe_basket, fetch_snapshots,
    resubscribe, reload_symbols, validate_symbols).
    """

    def subscribe_basket(self, cb: Callable[..., Any]) -> None: ...

    def fetch_snapshots(self) -> list[Any]: ...

    def resubscribe(self) -> bool: ...

    def reload_symbols(self) -> None: ...

    def validate_symbols(self) -> list[str]: ...


@runtime_checkable
class OrderExecutor(Protocol):
    """Protocol for order placement, cancellation, and modification.

    Consumers: ``OrderAdapter`` (place_order, cancel_order, update_order,
    get_exchange).
    """

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str,
        tif: str,
        **kwargs: Any,
    ) -> Any: ...

    def cancel_order(self, trade: Any) -> Any: ...

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any: ...

    def get_exchange(self, symbol: str) -> str: ...

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]) -> None: ...


@runtime_checkable
class AccountProvider(Protocol):
    """Protocol for account, position, and P&L queries.

    Consumers: ``ReconciliationService`` (get_positions),
    ``ShioajiClientFacade`` (get_account_balance, get_margin,
    list_position_detail, list_profit_loss).
    """

    def get_positions(self) -> list[Any]: ...

    def get_account_balance(self, account: Any = None) -> Any: ...

    def get_margin(self, account: Any = None) -> Any: ...

    def list_position_detail(self, account: Any = None) -> list[Any]: ...

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]: ...


@runtime_checkable
class BrokerSession(Protocol):
    """Protocol for broker session lifecycle management.

    Consumers: ``MarketDataService`` (login, reconnect),
    ``SystemBootstrapper`` (close, shutdown, logged_in).
    """

    def login(self, **kwargs: Any) -> Any: ...

    def reconnect(self, reason: str = "", force: bool = False) -> bool: ...

    def close(self, logout: bool = False) -> None: ...

    def shutdown(self, logout: bool = False) -> None: ...

    @property
    def logged_in(self) -> bool: ...
