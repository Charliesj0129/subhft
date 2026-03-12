"""Tests for Fubon TradeAPI client skeleton and deprecation warnings."""

from __future__ import annotations

import warnings

import pytest

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.client import FubonClient
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime


class TestFubonClientInit:
    def test_fubon_client_init(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = FubonClient()
        assert client.logged_in is False
        assert client.api is None

    def test_fubon_client_has_slots(self) -> None:
        assert hasattr(FubonClient, "__slots__")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = FubonClient()
        with pytest.raises(AttributeError):
            client.nonexistent_attr = 42  # type: ignore[attr-defined]

    def test_fubon_client_logged_in_property(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = FubonClient()
        assert client.logged_in is False

    def test_fubon_client_deprecation_warning(self) -> None:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            FubonClient()
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) >= 1
        assert "FubonClient is deprecated" in str(deprecation_warnings[0].message)


class TestFubonClientMethodsRaise:
    """Every stub method must raise NotImplementedError."""

    METHODS_NO_ARGS = (
        "fetch_snapshots",
        "resubscribe",
        "reload_symbols",
        "get_positions",
    )

    def test_fubon_client_login_raises_not_implemented(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            client = FubonClient()
        with pytest.raises(NotImplementedError, match="login"):
            client.login()

    def test_fubon_client_all_methods_raise(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
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
        runtime = FubonQuoteRuntime(sdk=None)
        assert runtime._sdk is None

    def test_fubon_quote_runtime_has_slots(self) -> None:
        assert hasattr(FubonQuoteRuntime, "__slots__")

    def test_fubon_quote_runtime_register_callbacks(self) -> None:
        runtime = FubonQuoteRuntime(sdk=None)

        def on_tick(d: object) -> None:
            pass

        def on_bidask(d: object) -> None:
            pass

        runtime.register_quote_callbacks(on_tick, on_bidask)
        assert runtime._on_tick is on_tick
        assert runtime._on_bidask is on_bidask

    def test_fubon_quote_deprecation_warning_on_old_import(self) -> None:
        """Importing from quote.py (old path) emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            # Force reimport to trigger module-level warning
            import importlib

            import hft_platform.feed_adapter.fubon.quote as _quote_mod

            importlib.reload(_quote_mod)
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) >= 1
        assert "quote_runtime" in str(deprecation_warnings[0].message)


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
        gw = FubonAccountGateway(sdk=None)
        assert gw._sdk is None

    def test_fubon_account_gateway_has_slots(self) -> None:
        assert hasattr(FubonAccountGateway, "__slots__")

    def test_fubon_account_gateway_has_methods(self) -> None:
        """Real FubonAccountGateway has get_inventories, get_accounting, etc."""
        gw = FubonAccountGateway(sdk=None)
        assert hasattr(gw, "get_inventories")
        assert hasattr(gw, "get_accounting")
        assert hasattr(gw, "get_margin")
        assert hasattr(gw, "get_settlements")

    def test_fubon_account_deprecation_warning_on_old_import(self) -> None:
        """Importing from account.py (old path) emits DeprecationWarning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import importlib

            import hft_platform.feed_adapter.fubon.account as _account_mod

            importlib.reload(_account_mod)
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(deprecation_warnings) >= 1
        assert "account_gateway" in str(deprecation_warnings[0].message)
