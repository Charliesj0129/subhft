from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class BrokerProtocol(Protocol):
    """Structural protocol for all broker client implementations."""

    @property
    def logged_in(self) -> bool: ...

    # Session lifecycle
    def login(self, *args: Any, **kwargs: Any) -> bool: ...
    def reconnect(self, *args: Any, **kwargs: Any) -> bool: ...
    def close(self, logout: bool = False) -> None: ...
    def shutdown(self, logout: bool = False) -> None: ...

    # Market data
    def subscribe_basket(self, cb: Callable[..., Any]) -> None: ...
    def fetch_snapshots(self) -> list[Any]: ...
    def reload_symbols(self) -> None: ...
    def resubscribe(self) -> bool: ...
    def get_exchange(self, symbol: str) -> str: ...
    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]) -> None: ...

    # Orders
    def place_order(self, **kwargs: Any) -> Any: ...
    def cancel_order(self, trade: Any) -> Any: ...
    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
    ) -> Any: ...

    # Account
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

    # Symbols
    def validate_symbols(self) -> list[str]: ...
    def get_contract_refresh_status(self) -> dict[str, Any]: ...


@dataclass(slots=True, frozen=True)
class BrokerCapabilities:
    """Declares what a broker implementation supports."""

    name: str
    supports_batch_order: bool = False
    supports_smart_order: bool = False
    supports_nonblocking_order: bool = False
    supports_l2_depth: bool = True
    max_custom_field_len: int = 6
    auth_method: str = "cert"  # "cert" | "apikey"
    max_rate_per_second: int = 20


SHIOAJI_CAPABILITIES = BrokerCapabilities(
    name="shioaji",
    supports_batch_order=False,
    supports_smart_order=False,
    supports_nonblocking_order=True,
    supports_l2_depth=True,
    max_custom_field_len=6,
    auth_method="cert",
    max_rate_per_second=25,
)

FUBON_CAPABILITIES = BrokerCapabilities(
    name="fubon",
    supports_batch_order=True,
    supports_smart_order=True,
    supports_l2_depth=True,
    max_custom_field_len=32,
    auth_method="apikey",
    max_rate_per_second=15,
)
