"""Tests for Fubon client facade, broker factory, and broker registry."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _FakeLoginResult:
    """Mimics fubon_neo SDK login result with .data attribute."""

    def __init__(self, accounts: list[Any] | None = None) -> None:
        self.data = accounts or [MagicMock(name="account_0")]


@pytest.fixture()
def _patch_fubon_sdk():
    """Patch _get_sdk_class so tests don't require fubon-neo installed."""
    fake_sdk_instance = MagicMock()
    fake_sdk_instance.login.return_value = _FakeLoginResult()

    fake_sdk_cls = MagicMock(return_value=fake_sdk_instance)

    with patch(
        "hft_platform.feed_adapter.fubon.session_runtime._get_sdk_class",
        return_value=fake_sdk_cls,
    ):
        yield fake_sdk_instance


@pytest.fixture()
def facade(_patch_fubon_sdk: Any) -> Any:
    """Return a FubonClientFacade with mocked SDK."""
    from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

    return FubonClientFacade(broker_config={"fubon": {"user_id": "test", "password": "pw"}})


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestFubonConfig:
    def test_load_defaults(self) -> None:
        from hft_platform.feed_adapter.fubon._config import load_fubon_config

        cfg = load_fubon_config()
        assert cfg.user_id == ""
        assert cfg.simulation is True

    def test_load_from_settings(self) -> None:
        from hft_platform.feed_adapter.fubon._config import load_fubon_config

        cfg = load_fubon_config({"fubon": {"user_id": "u1", "password": "p1"}})
        assert cfg.user_id == "u1"
        assert cfg.password == "p1"

    def test_env_overrides_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hft_platform.feed_adapter.fubon._config import load_fubon_config

        monkeypatch.setenv("FUBON_ID", "env_user")
        cfg = load_fubon_config({"fubon": {"user_id": "yaml_user"}})
        assert cfg.user_id == "env_user"

    def test_frozen(self) -> None:
        from hft_platform.feed_adapter.fubon._config import FubonClientConfig

        cfg = FubonClientConfig()
        with pytest.raises(AttributeError):
            cfg.user_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Session runtime
# ---------------------------------------------------------------------------


class TestSessionRuntime:
    def test_login_success(self, _patch_fubon_sdk: Any) -> None:
        from hft_platform.feed_adapter.fubon._config import FubonClientConfig
        from hft_platform.feed_adapter.fubon.session_runtime import (
            FubonSessionRuntime,
        )

        rt = FubonSessionRuntime(FubonClientConfig(user_id="u", password="p"))
        assert not rt.logged_in
        rt.login()
        assert rt.logged_in
        assert rt.sdk is not None
        assert rt.account is not None

    def test_login_missing_creds(self) -> None:
        from hft_platform.feed_adapter.fubon._config import FubonClientConfig
        from hft_platform.feed_adapter.fubon.session_runtime import (
            FubonSessionRuntime,
        )

        rt = FubonSessionRuntime(FubonClientConfig())
        with pytest.raises(ValueError, match="user_id and password"):
            rt.login()

    def test_shutdown_clears_refs(self, _patch_fubon_sdk: Any) -> None:
        from hft_platform.feed_adapter.fubon._config import FubonClientConfig
        from hft_platform.feed_adapter.fubon.session_runtime import (
            FubonSessionRuntime,
        )

        rt = FubonSessionRuntime(FubonClientConfig(user_id="u", password="p"))
        rt.login()
        rt.shutdown()
        assert not rt.logged_in
        assert rt.sdk is None
        assert rt.account is None


# ---------------------------------------------------------------------------
# Facade — init
# ---------------------------------------------------------------------------


class TestFacadeInit:
    def test_init_no_config_path(self, _patch_fubon_sdk: Any) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        f = FubonClientFacade(broker_config={"fubon": {"user_id": "t", "password": "p"}})
        assert not f.logged_in
        assert f._market_data is None

    def test_init_with_bad_config_path(self, _patch_fubon_sdk: Any) -> None:
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        f = FubonClientFacade(
            config_path="/nonexistent/symbols.yaml",
            broker_config={"fubon": {"user_id": "t", "password": "p"}},
        )
        assert f._symbols == []


# ---------------------------------------------------------------------------
# Facade — BrokerSession delegation
# ---------------------------------------------------------------------------


class TestFacadeBrokerSession:
    def test_login_creates_gateways(self, facade: Any) -> None:
        assert facade._market_data is None
        facade.login()
        assert facade.logged_in
        assert facade._market_data is not None
        assert facade._order_gateway is not None
        assert facade._account_gateway is not None

    def test_close(self, facade: Any) -> None:
        facade.login()
        facade.close()
        assert not facade.logged_in

    def test_shutdown(self, facade: Any) -> None:
        facade.login()
        facade.shutdown()
        assert not facade.logged_in

    def test_reconnect_success(self, facade: Any) -> None:
        facade.login()
        ok = facade.reconnect(reason="test")
        assert ok is True
        assert facade.logged_in


# ---------------------------------------------------------------------------
# Facade — MarketDataProvider delegation
# ---------------------------------------------------------------------------


class TestFacadeMarketData:
    def test_subscribe_basket(self, facade: Any) -> None:
        facade.login()
        cb = MagicMock()
        facade.subscribe_basket(cb)
        assert facade._market_data is not None
        assert facade._market_data._callback is cb

    def test_fetch_snapshots_before_login(self, facade: Any) -> None:
        assert facade.fetch_snapshots() == []

    def test_resubscribe_before_login(self, facade: Any) -> None:
        assert facade.resubscribe() is False

    def test_validate_symbols_before_login(self, facade: Any) -> None:
        assert facade.validate_symbols() == []

    def test_reload_symbols_noop(self, facade: Any) -> None:
        facade.reload_symbols()  # should not raise


# ---------------------------------------------------------------------------
# Facade — OrderExecutor delegation
# ---------------------------------------------------------------------------


class TestFacadeOrderExecutor:
    def test_place_order(self, facade: Any) -> None:
        facade.login()
        result = facade.place_order("2330", "TSE", "Buy", 100, 1)
        assert result is None  # stub returns None

    def test_cancel_order_before_login(self, facade: Any) -> None:
        assert facade.cancel_order(MagicMock()) is None

    def test_update_order_before_login(self, facade: Any) -> None:
        assert facade.update_order(MagicMock(), price=100.0) is None

    def test_get_exchange_default(self, facade: Any) -> None:
        assert facade.get_exchange("2330") == "TSE"

    def test_set_execution_callbacks(self, facade: Any) -> None:
        facade.login()
        on_order, on_deal = MagicMock(), MagicMock()
        facade.set_execution_callbacks(on_order, on_deal)
        assert facade._order_gateway is not None
        assert facade._order_gateway._on_order_cb is on_order
        assert facade._order_gateway._on_deal_cb is on_deal


# ---------------------------------------------------------------------------
# Facade — AccountProvider delegation
# ---------------------------------------------------------------------------


class TestFacadeAccountProvider:
    def test_get_positions_before_login(self, facade: Any) -> None:
        assert facade.get_positions() == []

    def test_get_account_balance_before_login(self, facade: Any) -> None:
        assert facade.get_account_balance() is None

    def test_get_margin_before_login(self, facade: Any) -> None:
        assert facade.get_margin() is None

    def test_list_position_detail_before_login(self, facade: Any) -> None:
        assert facade.list_position_detail() == []

    def test_list_profit_loss_before_login(self, facade: Any) -> None:
        assert facade.list_profit_loss() == []

    def test_account_methods_after_login(self, facade: Any) -> None:
        facade.login()
        assert facade.get_positions() == []
        assert facade.get_account_balance() is None
        assert facade.get_margin() is None
        assert facade.list_position_detail() == []
        assert facade.list_profit_loss() == []


# ---------------------------------------------------------------------------
# Broker factory
# ---------------------------------------------------------------------------


class TestFubonBrokerFactory:
    def test_create_clients(self, _patch_fubon_sdk: Any) -> None:
        from hft_platform.feed_adapter.fubon.factory import FubonBrokerFactory

        factory = FubonBrokerFactory()
        md, order = factory.create_clients("", {"fubon": {"user_id": "u", "password": "p"}})
        from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

        assert isinstance(md, FubonClientFacade)
        assert isinstance(order, FubonClientFacade)
        assert md is not order


# ---------------------------------------------------------------------------
# Broker registry
# ---------------------------------------------------------------------------


class TestBrokerRegistry:
    def test_register_and_get(self) -> None:
        from hft_platform.feed_adapter.broker_registry import (
            BrokerFactory,
            get_broker_factory,
            register_broker,
        )

        class _DummyFactory:
            __slots__ = ()

            def create_clients(self, symbols_path: str, broker_config: dict[str, Any]) -> tuple[Any, Any]:
                return (None, None)

        assert isinstance(_DummyFactory(), BrokerFactory)
        register_broker("dummy_test", _DummyFactory())
        f = get_broker_factory("dummy_test")
        assert f is not None

    def test_unknown_broker_raises(self) -> None:
        from hft_platform.feed_adapter.broker_registry import get_broker_factory

        with pytest.raises(ValueError, match="Unknown broker"):
            get_broker_factory("nonexistent_broker_xyz")

    def test_fubon_auto_registration(self) -> None:
        import hft_platform.feed_adapter.fubon  # noqa: F401
        from hft_platform.feed_adapter.broker_registry import get_broker_factory

        f = get_broker_factory("fubon")
        assert f is not None

    def test_list_brokers(self) -> None:
        import hft_platform.feed_adapter.fubon  # noqa: F401
        from hft_platform.feed_adapter.broker_registry import list_brokers

        brokers = list_brokers()
        assert "fubon" in brokers


# ---------------------------------------------------------------------------
# Protocol conformance (structural typing)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_broker_factory_protocol(self) -> None:
        from hft_platform.feed_adapter.broker_registry import BrokerFactory
        from hft_platform.feed_adapter.fubon.factory import FubonBrokerFactory

        assert isinstance(FubonBrokerFactory(), BrokerFactory)
