from __future__ import annotations

from typing import Any

from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway
from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime
from hft_platform.feed_adapter.shioaji.historical_gateway import HistoricalGateway
from hft_platform.feed_adapter.shioaji.market_info_gateway import MarketInfoGateway
from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
from hft_platform.feed_adapter.shioaji.scanner_gateway import ScannerGateway
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
        "contracts_runtime",
        "order_gateway",
        "account_gateway",
        "historical_gateway",
        "scanner_gateway",
        "market_info_gateway",
        "subscription_manager",
    )

    def __init__(self, config_path: str | None = None, shioaji_config: dict[str, Any] | None = None) -> None:
        client = ShioajiClient(config_path=config_path, shioaji_config=shioaji_config)
        self._client = client
        # Reuse the runtime instances already created by ShioajiClient.__init__
        # so the Facade, the client, and the runtimes all share the same objects.
        self.session_runtime = client._session_runtime
        self.quote_runtime = client._quote_runtime
        self.contracts_runtime = ContractsRuntime(client)
        self.order_gateway = OrderGateway(client)
        self.account_gateway = AccountGateway(client)
        self.historical_gateway = HistoricalGateway(client)
        self.scanner_gateway = ScannerGateway(client)
        self.market_info_gateway = MarketInfoGateway(client)
        self.subscription_manager = client._subscription_manager
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
        self.subscription_manager.subscribe_basket(cb)

    def fetch_snapshots(self) -> list[Any]:
        return self._client.fetch_snapshots()

    def reload_symbols(self) -> None:
        self.contracts_runtime.reload_symbols()

    def resubscribe(self) -> bool:
        return self.subscription_manager.resubscribe()

    def set_execution_callbacks(self, on_order, on_deal) -> None:
        self.subscription_manager.set_execution_callbacks(on_order=on_order, on_deal=on_deal)

    def place_order(self, *args, **kwargs) -> Any:
        return self.order_gateway.place_order(*args, **kwargs)

    def get_exchange(self, symbol: str) -> str:
        return self._client.get_exchange(symbol) or ""

    def cancel_order(
        self,
        trade: Any,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        return self.order_gateway.cancel_order(trade, timeout=timeout, cb=cb)

    def update_order(
        self,
        trade: Any,
        price: float | None = None,
        qty: int | None = None,
        timeout: int = 5000,
        cb: Any | None = None,
    ) -> Any:
        return self.order_gateway.update_order(trade, price=price, qty=qty, timeout=timeout, cb=cb)

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

    def get_trading_limits(self, account: Any = None) -> Any:
        return self.account_gateway.get_trading_limits(account=account)

    def get_settlements(self, account: Any = None) -> Any:
        return self.account_gateway.get_settlements(account=account)

    def list_profit_loss_summary(
        self, account: Any = None, begin_date: str | None = None, end_date: str | None = None
    ) -> Any:
        return self.account_gateway.list_profit_loss_summary(account=account, begin_date=begin_date, end_date=end_date)

    def list_profit_loss_detail(self, account: Any = None, detail_id: int = 0, unit: str | None = None) -> Any:
        return self.account_gateway.list_profit_loss_detail(account=account, detail_id=detail_id, unit=unit)

    def validate_symbols(self) -> list[str]:
        return self.contracts_runtime.validate_symbols()

    def get_contract_refresh_status(self) -> dict[str, object]:
        return self.contracts_runtime.refresh_status()

    def get_ticks(self, *args: Any, **kwargs: Any) -> Any:
        return self.historical_gateway.get_ticks(*args, **kwargs)

    def get_kbars(self, *args: Any, **kwargs: Any) -> Any:
        return self.historical_gateway.get_kbars(*args, **kwargs)

    def scan(
        self,
        scanner_type: str,
        ascending: bool = False,
        count: int = 100,
        date: str | None = None,
        timeout: int = 30000,
    ) -> list[Any]:
        return self.scanner_gateway.scan(
            scanner_type=scanner_type,
            ascending=ascending,
            count=count,
            date=date,
            timeout=timeout,
        )

    def get_credit_enquires(
        self,
        contract_codes: list[str],
        exchange: str,
        timeout: int = 30000,
        product_type: str | None = None,
    ) -> list[Any]:
        return self.market_info_gateway.get_credit_enquires(
            contract_codes,
            exchange,
            timeout=timeout,
            product_type=product_type,
        )

    def get_short_stock_sources(
        self,
        contract_codes: list[str],
        exchange: str,
        timeout: int = 5000,
        product_type: str | None = None,
    ) -> list[Any]:
        return self.market_info_gateway.get_short_stock_sources(
            contract_codes,
            exchange,
            timeout=timeout,
            product_type=product_type,
        )

    def get_punish_stocks(self, timeout: int = 5000) -> Any:
        return self.market_info_gateway.get_punish_stocks(timeout=timeout)

    def get_notice_stocks(self, timeout: int = 5000) -> Any:
        return self.market_info_gateway.get_notice_stocks(timeout=timeout)

    def close(self, logout: bool = False) -> None:
        self._client.close(logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        self._client.shutdown(logout=logout)
