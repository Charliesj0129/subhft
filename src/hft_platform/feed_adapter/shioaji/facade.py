from __future__ import annotations

from typing import Any

from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway
from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime
from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
from hft_platform.feed_adapter.shioaji_client import ShioajiClient


class ShioajiClientFacade:
    """Explicit facade over legacy ShioajiClient modules.

    The facade exposes stable entrypoints while routing calls to dedicated
    runtime/gateway modules. It intentionally avoids ``__getattr__`` passthrough
    to keep responsibilities explicit and reviewable.
    """

    __slots__ = (
        "_client",
        "session_runtime",
        "quote_runtime",
        "reconnect_orchestrator",
        "contracts_runtime",
        "order_gateway",
        "account_gateway",
    )

    def __init__(self, config_path: str | None = None, shioaji_config: dict[str, Any] | None = None) -> None:
        client = ShioajiClient(config_path=config_path, shioaji_config=shioaji_config)
        self._client = client
        # Reuse the runtime instances already created by ShioajiClient.__init__
        # so the Facade, the client, and the runtimes all share the same objects.
        self.session_runtime = client._session_runtime
        self.quote_runtime = client._quote_runtime
        self.reconnect_orchestrator = client._reconnect_orchestrator
        self.contracts_runtime = ContractsRuntime(client)
        self.order_gateway = OrderGateway(client)
        self.account_gateway = AccountGateway(client)
        # Wire decoupled interfaces (already set in __init__, but kept explicit here).
        client._session_policy = self.session_runtime
        client._quote_event_handler = self.quote_runtime._event_handler

    @property
    def api(self) -> Any:
        return self._client.api

    @api.setter
    def api(self, value: Any) -> None:
        self._client.api = value

    @property
    def logged_in(self) -> bool:
        return bool(self._client.logged_in)

    @logged_in.setter
    def logged_in(self, value: bool) -> None:
        self._client.logged_in = bool(value)

    @property
    def tick_callback(self) -> Any:
        return self._client.tick_callback

    @tick_callback.setter
    def tick_callback(self, cb: Any) -> None:
        self._client.tick_callback = cb

    def login(self, *args, **kwargs) -> bool:
        return self.session_runtime.login(*args, **kwargs)

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        return self.session_runtime.request_reconnect(reason=reason, force=force)

    def subscribe_basket(self, cb) -> None:
        self._client.subscribe_basket(cb)

    def fetch_snapshots(self) -> list[Any]:
        return self._client.fetch_snapshots()

    def reload_symbols(self) -> None:
        self.contracts_runtime.reload_symbols()

    def resubscribe(self) -> bool:
        return self.quote_runtime.resubscribe()

    def set_execution_callbacks(self, on_order, on_deal) -> None:
        self._client.set_execution_callbacks(on_order=on_order, on_deal=on_deal)

    def place_order(self, *args, **kwargs) -> Any:
        return self.order_gateway.place_order(*args, **kwargs)

    def get_exchange(self, symbol: str) -> str:
        return self._client.get_exchange(symbol) or ""

    def cancel_order(self, trade: Any) -> Any:
        return self.order_gateway.cancel_order(trade)

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any:
        return self.order_gateway.update_order(trade, price=price, qty=qty)

    def get_positions(self) -> list[Any]:
        return self.account_gateway.get_positions()

    def get_account_balance(self, account: Any = None) -> Any:
        return self.account_gateway.get_account_balance(account=account)

    def get_margin(self, account: Any = None) -> Any:
        return self.account_gateway.get_margin(account=account)

    def list_position_detail(self, account: Any = None) -> Any:
        return self.account_gateway.list_position_detail(account=account)

    def list_profit_loss(self, account: Any = None, begin_date: str | None = None, end_date: str | None = None) -> Any:
        return self.account_gateway.list_profit_loss(account=account, begin_date=begin_date, end_date=end_date)

    def validate_symbols(self) -> list[str]:
        return self.contracts_runtime.validate_symbols()

    def get_contract_refresh_status(self) -> dict[str, object]:
        return self.contracts_runtime.refresh_status()

    def close(self, logout: bool = False) -> None:
        self._client.close(logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        self._client.shutdown(logout=logout)
