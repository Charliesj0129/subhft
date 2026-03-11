"""Fubon client facade — unified entrypoint for all Fubon broker operations."""

from __future__ import annotations

from typing import Any, Callable

from structlog import get_logger

from hft_platform.feed_adapter.fubon._config import load_fubon_config
from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.market_data import FubonMarketDataProvider
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

logger = get_logger("fubon.facade")


class FubonClientFacade:
    """Unified facade over Fubon session, market data, order, and account modules.

    Mirrors the ShioajiClientFacade API surface so both brokers satisfy the same
    protocol interfaces (BrokerSession, MarketDataProvider, OrderExecutor,
    AccountProvider).
    """

    __slots__ = (
        "_config",
        "_session",
        "_market_data",
        "_order_gateway",
        "_account_gateway",
        "_symbols",
    )

    def __init__(
        self,
        config_path: str | None = None,
        broker_config: dict[str, Any] | None = None,
    ) -> None:
        self._config = load_fubon_config(broker_config)
        self._session = FubonSessionRuntime(self._config)
        self._symbols: list[str] = []
        if config_path:
            self._symbols = self._load_symbols(config_path)
        self._market_data: FubonMarketDataProvider | None = None
        self._order_gateway: FubonOrderGateway | None = None
        self._account_gateway: FubonAccountGateway | None = None

    @staticmethod
    def _load_symbols(config_path: str) -> list[str]:
        """Load symbol list from YAML config."""
        try:
            import yaml  # type: ignore[import-untyped]

            with open(config_path) as f:
                data = yaml.safe_load(f) or {}
            symbols = data.get("symbols", [])
            return [s.get("code", s) if isinstance(s, dict) else str(s) for s in symbols]
        except Exception:
            logger.warning("fubon_symbols_load_failed", path=config_path)
            return []

    def _ensure_gateways(self) -> None:
        """Lazily create gateway objects once SDK/account are available."""
        if self._market_data is None:
            sdk = self._session.sdk
            acc = self._session.account
            self._market_data = FubonMarketDataProvider(sdk, acc, self._symbols)
            self._order_gateway = FubonOrderGateway(sdk, acc)
            self._account_gateway = FubonAccountGateway(sdk, acc)

    # -- BrokerSession --------------------------------------------------------

    @property
    def logged_in(self) -> bool:
        return self._session.logged_in

    def login(self, **kwargs: Any) -> Any:
        result = self._session.login(**kwargs)
        self._ensure_gateways()
        return result

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        # Invalidate stale gateways before reconnect so _ensure_gateways
        # rebuilds them with the fresh SDK/account references.
        self._market_data = None
        self._order_gateway = None
        self._account_gateway = None
        ok = self._session.reconnect(reason=reason, force=force)
        if ok:
            self._ensure_gateways()
        return ok

    def close(self, logout: bool = False) -> None:
        self._session.close(logout=logout)

    def shutdown(self, logout: bool = False) -> None:
        self._session.shutdown(logout=logout)

    # -- MarketDataProvider ---------------------------------------------------

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        self._ensure_gateways()
        assert self._market_data is not None
        self._market_data.subscribe_basket(cb)

    def fetch_snapshots(self) -> list[Any]:
        if not self._market_data:
            return []
        return self._market_data.fetch_snapshots()

    def resubscribe(self) -> bool:
        if not self._market_data:
            return False
        return self._market_data.resubscribe()

    def reload_symbols(self) -> None:
        if self._market_data:
            self._market_data.reload_symbols()

    def validate_symbols(self) -> list[str]:
        if not self._market_data:
            return []
        return self._market_data.validate_symbols()

    # -- OrderExecutor --------------------------------------------------------

    def place_order(self, *args: Any, **kwargs: Any) -> Any:
        self._ensure_gateways()
        assert self._order_gateway is not None
        return self._order_gateway.place_order(*args, **kwargs)

    def cancel_order(self, trade: Any) -> Any:
        if not self._order_gateway:
            return None
        return self._order_gateway.cancel_order(trade)

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> Any:
        if not self._order_gateway:
            return None
        return self._order_gateway.update_order(trade, price=price, qty=qty)

    def get_exchange(self, symbol: str) -> str:
        if not self._order_gateway:
            return "TSE"
        return self._order_gateway.get_exchange(symbol)

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]) -> None:
        self._ensure_gateways()
        assert self._order_gateway is not None
        self._order_gateway.set_execution_callbacks(on_order, on_deal)

    # -- AccountProvider ------------------------------------------------------

    def get_positions(self) -> list[Any]:
        if not self._account_gateway:
            return []
        return self._account_gateway.get_positions()

    def get_account_balance(self, account: Any = None) -> Any:
        if not self._account_gateway:
            return None
        return self._account_gateway.get_account_balance(account)

    def get_margin(self, account: Any = None) -> Any:
        if not self._account_gateway:
            return None
        return self._account_gateway.get_margin(account)

    def list_position_detail(self, account: Any = None) -> list[Any]:
        if not self._account_gateway:
            return []
        return self._account_gateway.list_position_detail(account)

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        if not self._account_gateway:
            return []
        return self._account_gateway.list_profit_loss(account, begin_date, end_date)
