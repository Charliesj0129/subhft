"""Tests for FubonClientFacade: sim-safe SDK construction, login wiring and delegation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.unit.fubon_mock_helper import (
    SdkFactory,
    fail,
    install_fubon_neo_mock,
    make_account,
    make_fubon_sdk_mock,
    ok,
)

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon import facade as facade_module
from hft_platform.feed_adapter.fubon.execution_callbacks import REQUIRED_SDK_SETTERS, FubonCallbackRegistrationError
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade

_TEST_URL = "wss://fubon-test.example.invalid/ws"
_BROKER_CONFIG = {"broker": "fubon"}

_ENV_NAMES = (
    "HFT_ORDER_MODE",
    "HFT_FUBON_TEST_URL",
    "HFT_FUBON_PERSONAL_ID",
    "HFT_FUBON_API_KEY",
    "HFT_FUBON_PASSWORD",
    "HFT_FUBON_CERT_PATH",
    "HFT_FUBON_CERT_PASS",
    "HFT_FUBON_ACCOUNT",
)


class _LoginFactory(SdkFactory):
    """SdkFactory whose SDKs answer login with *login_result* (never connects)."""

    def __init__(self, login_result: Any = None) -> None:
        super().__init__()
        self.login_result = login_result if login_result is not None else ok([make_account()])

    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        sdk = super().__call__(*args, **kwargs)
        sdk.login.return_value = self.login_result
        sdk.apikey_login.return_value = self.login_result
        return sdk


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
    monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
    monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")


@pytest.fixture()
def live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_ORDER_MODE", "live")


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> _LoginFactory:
    factory = _LoginFactory()
    monkeypatch.setattr(facade_module, "FubonSDK", factory)
    return factory


def _facade() -> FubonClientFacade:
    return FubonClientFacade(symbols_path=None, broker_config=_BROKER_CONFIG)


def _subscribe_basket(facade: FubonClientFacade, monkeypatch: pytest.MonkeyPatch) -> None:
    # The connect sequence subscribes after login; a reconnect then restores that basket.
    monkeypatch.setenv("HFT_SYMBOLS", "TXFL6")
    facade._quote_runtime = MagicMock(name="quote_runtime")
    facade.subscribe_basket(lambda *args, **kwargs: None)


class TestSdkConstruction:
    def test_live_mode_builds_the_sdk_with_the_vendor_default_target(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()

        assert factory.calls == [((), {})]
        assert facade._sdk is factory.instances[0]

    def test_non_live_without_a_test_url_never_builds_the_sdk(self, factory: _LoginFactory) -> None:
        facade = _facade()

        assert factory.calls == []
        assert facade._sdk is None
        assert facade.login() is False
        assert facade.logged_in is False

    def test_non_live_builds_the_sdk_only_against_the_test_url(
        self, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _TEST_URL)

        facade = _facade()

        assert factory.calls == [((), {"url": _TEST_URL})]
        assert facade.login() is True

    def test_non_live_refuses_the_production_host(
        self, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_TEST_URL", "wss://neoapi.fbs.com.tw/TASP/XCPXWS")

        facade = _facade()

        assert factory.calls == []
        assert facade._sdk is None

    def test_without_broker_config_no_sdk_is_built(self, live: None, factory: _LoginFactory) -> None:
        facade = FubonClientFacade(symbols_path=None, broker_config=None)

        assert factory.calls == []
        assert facade._sdk is None

    def test_sdk_not_installed_means_login_fails(self, live: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(facade_module, "FubonSDK", None)

        facade = _facade()

        assert facade.login() is False
        assert facade.logged_in is False


class TestLogin:
    def test_login_authenticates_and_binds_the_account_to_both_gateways(
        self, live: None, factory: _LoginFactory
    ) -> None:
        facade = _facade()

        assert facade.login() is True

        account = factory.login_result.data[0]
        assert facade.logged_in is True
        assert facade._order_gateway._account is account
        assert facade._account_gateway._account is account
        factory.instances[0].login.assert_called_once_with("A123456789", "pwd", "/c.pfx", None)

    def test_login_is_a_single_attempt(self, live: None, factory: _LoginFactory) -> None:
        factory.login_result = fail("bad credentials")
        facade = _facade()

        assert facade.login() is False

        assert factory.instances[0].login.call_count == 1
        assert facade.logged_in is False

    def test_failed_login_leaves_gateways_without_an_account(self, live: None, factory: _LoginFactory) -> None:
        factory.login_result = fail("bad credentials")
        facade = _facade()
        facade.login()

        with pytest.raises(RuntimeError, match="no trading account"):
            facade.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

    def test_login_passes_arguments_to_the_session(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()

        assert facade.login(api_key="KEY", personal_id="A9", cert_path="/x.pfx") is True

        factory.instances[0].apikey_login.assert_called_once_with("A9", "KEY", "/x.pfx", None)

    def test_reconnect_logs_out_logs_in_and_rebinds_the_account(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        facade = _facade()
        facade.login()
        _subscribe_basket(facade, monkeypatch)
        new_account = make_account("7654321")
        factory.instances[0].login.return_value = ok([new_account])

        assert facade.reconnect(reason="drop", force=True) is True

        factory.instances[0].logout.assert_called_once_with()
        assert facade._order_gateway._account is new_account
        assert facade._account_gateway._account is new_account
        facade.close()

    def test_failed_reconnect_does_not_report_success(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        facade = _facade()
        facade.login()
        _subscribe_basket(facade, monkeypatch)
        factory.instances[0].login.return_value = fail("gateway timeout")

        assert facade.reconnect(reason="drop", force=True) is False

        assert facade.logged_in is False

    def test_close_with_logout_logs_out(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade.login()

        facade.close(logout=True)

        factory.instances[0].logout.assert_called_once_with()
        assert facade.logged_in is False

    def test_close_without_logout_keeps_the_session(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade.login()

        facade.close()

        factory.instances[0].logout.assert_not_called()

    def test_shutdown_delegates_to_close(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade.login()

        facade.shutdown(logout=True)

        factory.instances[0].logout.assert_called_once_with()


class TestExecutionCallbacks:
    def test_set_execution_callbacks_registers_the_real_setters(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()

        facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())

        for name in REQUIRED_SDK_SETTERS:
            getattr(factory.instances[0], name).assert_called_once()

    def test_set_execution_callbacks_without_an_sdk_raises(self) -> None:
        facade = FubonClientFacade(symbols_path=None, broker_config=None)

        with pytest.raises(FubonCallbackRegistrationError):
            facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())

    def test_set_execution_callbacks_on_an_sdk_missing_a_setter_raises(
        self, live: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def build(*args: Any, **kwargs: Any) -> MagicMock:
            sdk = make_fubon_sdk_mock()
            del sdk.set_on_order_futopt_changed
            return sdk

        monkeypatch.setattr(facade_module, "FubonSDK", build)
        facade = _facade()

        with pytest.raises(FubonCallbackRegistrationError, match="set_on_order_futopt_changed"):
            facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())

    def test_callbacks_are_re_registered_after_a_reconnect(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        facade = _facade()
        facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())
        facade.login()
        _subscribe_basket(facade, monkeypatch)
        sdk = factory.instances[0]
        before = sdk.set_on_futopt_filled.call_count

        assert facade.reconnect(reason="drop", force=True) is True

        assert sdk.set_on_futopt_filled.call_count == before + 1
        facade.close()

    def test_callbacks_are_wired_on_login(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())
        sdk = factory.instances[0]
        sdk.set_on_futopt_filled.reset_mock()

        facade.login()

        sdk.set_on_futopt_filled.assert_called_once()

    def test_wired_order_callback_receives_state_and_payload(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        calls: list[tuple[Any, Any]] = []
        facade.set_execution_callbacks(
            on_order=lambda state, payload: calls.append((state, payload)), on_deal=MagicMock()
        )

        handler = factory.instances[0].set_on_futopt_order.call_args.args[0]
        handler(
            "0",
            SimpleNamespace(order_no="l1", symbol="FITX", expiry_date="202612", buy_sell="Sell", lot=1, price=1.0),
        )

        assert calls[0][0] == "order"
        assert calls[0][1]["action"] == "Sell"
        assert calls[0][1]["symbol"] == "TXFL6"

    def test_session_is_logged_out_when_callbacks_cannot_be_rewired_on_login(
        self, live: None, factory: _LoginFactory
    ) -> None:
        facade = _facade()
        facade.set_execution_callbacks(on_order=MagicMock(), on_deal=MagicMock())
        sdk = factory.instances[0]
        del sdk.set_on_event

        assert facade.login() is False

        sdk.logout.assert_called_once_with()
        assert facade.logged_in is False


class TestDefaultAccountId:
    """The id fills are booked under is the account chosen at login, not only what the environment names."""

    def test_default_account_id_is_the_account_selected_at_login(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()

        assert facade.login() is True

        assert facade.get_default_account_id() == "1234567"

    def test_default_account_id_follows_the_environment_choice_among_several_accounts(
        self, live: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        accounts = [
            make_account("1111111", account_type="stock"),
            make_account("2222222"),
            make_account("3333333"),
        ]
        monkeypatch.setattr(facade_module, "FubonSDK", _LoginFactory(ok(accounts)))
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "3333333")
        facade = _facade()

        assert facade.login() is True

        assert facade.get_default_account_id() == "3333333"

    def test_default_account_id_is_never_the_stock_account(self, live: None, monkeypatch: pytest.MonkeyPatch) -> None:
        accounts = [make_account("1111111", account_type="stock"), make_account("2222222")]
        monkeypatch.setattr(facade_module, "FubonSDK", _LoginFactory(ok(accounts)))
        facade = _facade()

        assert facade.login() is True

        assert facade.get_default_account_id() == "2222222"

    def test_default_account_id_matches_the_account_the_gateways_trade_under(
        self, live: None, factory: _LoginFactory
    ) -> None:
        facade = _facade()
        facade.login()

        assert facade.get_default_account_id() == facade._session_runtime.account.account

    def test_default_account_id_before_login_falls_back_to_the_environment(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "5555555")

        assert _facade().get_default_account_id() == "5555555"

    def test_default_account_id_is_empty_before_login_with_nothing_configured(
        self, live: None, factory: _LoginFactory
    ) -> None:
        assert _facade().get_default_account_id() == ""


class TestDelegation:
    def test_place_order_delegates_to_the_gateway(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade._order_gateway = MagicMock()
        facade._order_gateway.place_order.return_value = "placed"

        assert facade.place_order(symbol="TMFK6", price=1, qty=1, side="Buy") == "placed"

        facade._order_gateway.place_order.assert_called_once_with(symbol="TMFK6", price=1, qty=1, side="Buy")

    def test_cancel_and_update_delegate_to_the_gateway(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade._order_gateway = MagicMock()

        facade.cancel_order("t")
        facade.update_order("t", price=5, qty=2)

        facade._order_gateway.cancel_order.assert_called_once_with("t")
        facade._order_gateway.update_order.assert_called_once_with("t", price=5, qty=2)

    def test_place_order_after_login_sends_through_the_real_futopt_api(
        self, live: None, factory: _LoginFactory
    ) -> None:
        facade = _facade()
        facade.login()
        sdk = factory.instances[0]
        sdk.futopt.place_order.return_value = ok(SimpleNamespace(order_no="l1", seq_no="1"))

        result = facade.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

        sdk.futopt.place_order.assert_called_once()
        assert sdk.futopt.place_order.call_args.args[0] is factory.login_result.data[0]
        assert result.order_no == "l1"

    def test_positions_before_login_are_unknown_not_flat(self, live: None, factory: _LoginFactory) -> None:
        assert _facade().get_positions() is None

    def test_positions_after_login_use_the_real_accounting_api(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade.login()
        sdk = factory.instances[0]
        sdk.futopt_accounting.query_single_position.return_value = ok([])

        assert facade.get_positions() == []

        sdk.futopt_accounting.query_single_position.assert_called_once_with(factory.login_result.data[0])

    def test_margin_balance_detail_and_profit_loss_delegate(self, live: None, factory: _LoginFactory) -> None:
        facade = _facade()
        facade._account_gateway = MagicMock()

        facade.get_margin()
        facade.get_account_balance()
        facade.list_position_detail()
        facade.list_profit_loss(begin_date="20260101", end_date="20260301")

        facade._account_gateway.get_margin.assert_called_once_with(None)
        facade._account_gateway.get_account_balance.assert_called_once_with(None)
        facade._account_gateway.list_position_detail.assert_called_once_with(None)
        facade._account_gateway.list_profit_loss.assert_called_once_with(None, "20260101", "20260301")


class TestSessionAdapterWithoutSdk:
    def test_every_session_operation_fails_closed(self) -> None:
        facade = FubonClientFacade(symbols_path=None, broker_config=None)

        assert facade.login() is False
        assert facade.reconnect(reason="x", force=True) is False
        facade.close(logout=True)
        assert facade.logged_in is False


class TestMisc:
    def test_default_account_id_comes_from_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_ACCOUNT", "1234567")

        assert FubonClientFacade().get_default_account_id() == "1234567"

    def test_default_account_id_is_empty_when_unset(self) -> None:
        assert FubonClientFacade().get_default_account_id() == ""

    def test_snapshots_are_not_fetched_by_the_facade_itself(self) -> None:
        assert FubonClientFacade().fetch_snapshots() == []
