"""Tests for Fubon TradeAPI client skeleton."""

from __future__ import annotations

import pytest

from hft_platform.feed_adapter.fubon.account import FubonAccountGateway
from hft_platform.feed_adapter.fubon.client import FubonClient
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime


class TestFubonClientInit:
    def test_fubon_client_init(self) -> None:
        client = FubonClient()
        assert client.logged_in is False
        assert client.api is None

    def test_fubon_client_has_slots(self) -> None:
        assert hasattr(FubonClient, "__slots__")
        client = FubonClient()
        with pytest.raises(AttributeError):
            client.nonexistent_attr = 42  # type: ignore[attr-defined]

    def test_fubon_client_logged_in_property(self) -> None:
        client = FubonClient()
        assert client.logged_in is False


class TestFubonClientMethodsRaise:
    """Every stub method must raise NotImplementedError."""

    METHODS_NO_ARGS = (
        "fetch_snapshots",
        "resubscribe",
        "reload_symbols",
        "get_positions",
    )

    def test_fubon_client_login_raises_not_implemented(self) -> None:
        client = FubonClient()
        with pytest.raises(NotImplementedError, match="login"):
            client.login()

    def test_fubon_client_all_methods_raise(self) -> None:
        client = FubonClient()
        for method_name in self.METHODS_NO_ARGS:
            with pytest.raises(NotImplementedError, match=method_name):
                getattr(client, method_name)()

        with pytest.raises(NotImplementedError):
            client.reconnect()
        with pytest.raises(NotImplementedError):
            client.close()
        with pytest.raises(NotImplementedError):
            client.shutdown()
        with pytest.raises(NotImplementedError):
            client.subscribe_basket(lambda: None)
        with pytest.raises(NotImplementedError):
            client.set_execution_callbacks(lambda: None, lambda: None)
        with pytest.raises(NotImplementedError):
            client.place_order()
        with pytest.raises(NotImplementedError):
            client.cancel_order(None)
        with pytest.raises(NotImplementedError):
            client.update_order(None)
        with pytest.raises(NotImplementedError):
            client.get_account_balance()
        with pytest.raises(NotImplementedError):
            client.get_margin()
        with pytest.raises(NotImplementedError):
            client.list_profit_loss()
        with pytest.raises(NotImplementedError):
            client.list_position_detail()
        with pytest.raises(NotImplementedError):
            client.get_exchange("2330")
        with pytest.raises(NotImplementedError):
            client.validate_symbols()


class TestFubonSessionRuntime:
    def test_fubon_session_runtime_init(self) -> None:
        runtime = FubonSessionRuntime(client=None)
        assert runtime._client is None

    def test_fubon_session_runtime_has_slots(self) -> None:
        assert hasattr(FubonSessionRuntime, "__slots__")

    def test_fubon_session_methods_raise(self) -> None:
        runtime = FubonSessionRuntime(client=None)
        with pytest.raises(NotImplementedError):
            runtime.login()
        with pytest.raises(NotImplementedError):
            runtime.refresh_token()
        with pytest.raises(NotImplementedError):
            runtime.logout()


class TestFubonQuoteRuntime:
    def test_fubon_quote_runtime_init(self) -> None:
        runtime = FubonQuoteRuntime(client=None)
        assert runtime._client is None

    def test_fubon_quote_runtime_has_slots(self) -> None:
        assert hasattr(FubonQuoteRuntime, "__slots__")

    def test_fubon_quote_methods_raise(self) -> None:
        runtime = FubonQuoteRuntime(client=None)
        with pytest.raises(NotImplementedError):
            runtime.subscribe("2330")
        with pytest.raises(NotImplementedError):
            runtime.unsubscribe("2330")
        with pytest.raises(NotImplementedError):
            runtime.on_quote_callback(lambda: None)


class TestFubonOrderGateway:
    def test_fubon_order_gateway_init(self) -> None:
        gw = FubonOrderGateway(client=None)
        assert gw._client is None

    def test_fubon_order_gateway_has_slots(self) -> None:
        assert hasattr(FubonOrderGateway, "__slots__")

    def test_fubon_order_methods_raise(self) -> None:
        gw = FubonOrderGateway(client=None)
        with pytest.raises(NotImplementedError):
            gw.place_order()
        with pytest.raises(NotImplementedError):
            gw.cancel_order(None)
        with pytest.raises(NotImplementedError):
            gw.update_order(None)
        with pytest.raises(NotImplementedError):
            gw.batch_place_orders([])


class TestFubonAccountGateway:
    def test_fubon_account_gateway_init(self) -> None:
        gw = FubonAccountGateway(client=None)
        assert gw._client is None

    def test_fubon_account_gateway_has_slots(self) -> None:
        assert hasattr(FubonAccountGateway, "__slots__")

    def test_fubon_account_methods_raise(self) -> None:
        gw = FubonAccountGateway(client=None)
        with pytest.raises(NotImplementedError):
            gw.get_positions()
        with pytest.raises(NotImplementedError):
            gw.get_balance()
        with pytest.raises(NotImplementedError):
            gw.get_margin()
        with pytest.raises(NotImplementedError):
            gw.list_profit_loss()
