"""Tests for the deprecated FubonClient shim: login wiring, safety refusal and delegation."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.fubon import client as client_module
from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway
from hft_platform.feed_adapter.fubon.client import FubonClient
from hft_platform.feed_adapter.fubon.execution_callbacks import REQUIRED_SDK_SETTERS, FubonCallbackRegistrationError
from hft_platform.feed_adapter.fubon.order_gateway import FubonOrderGateway
from tests.unit.fubon_mock_helper import SdkFactory, fail, make_account, make_fubon_sdk_mock, ok

_TEST_URL = "wss://fubon-test.example.invalid/ws"

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
    monkeypatch.setattr(client_module, "FubonSDK", factory)
    return factory


def _new_client() -> FubonClient:
    with pytest.warns(DeprecationWarning, match="FubonClient is deprecated"):
        return FubonClient()


class TestInit:
    def test_new_client_is_logged_out(self) -> None:
        client = _new_client()

        assert client.logged_in is False
        assert client.api is None

    def test_client_has_slots(self) -> None:
        client = _new_client()

        assert hasattr(FubonClient, "__slots__")
        with pytest.raises(AttributeError):
            client.nonexistent_attr = 42

    def test_sub_components_start_without_an_sdk(self) -> None:
        client = _new_client()

        assert client._order_gateway._sdk is None
        assert client._account_gateway._sdk is None
        assert client._quote_runtime._sdk is None

    def test_client_keeps_no_credential_attributes(self) -> None:
        for name in ("_api_key", "_password", "_cert_path", "_personal_id", "_cert_pass", "_secret_key"):
            assert name not in FubonClient.__slots__


class TestLogin:
    def test_login_in_live_mode_builds_the_sdk_with_vendor_default_target(
        self, live: None, factory: _LoginFactory
    ) -> None:
        client = _new_client()

        assert client.login() is True

        assert factory.calls == [((), {})]
        assert client.logged_in is True
        assert client.api is factory.instances[0]

    def test_login_binds_the_account_to_both_gateways(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()
        account = factory.login_result.data[0]

        assert client._order_gateway._account is account
        assert client._account_gateway._account is account
        assert client._order_gateway._sdk is factory.instances[0]
        assert client._account_gateway._sdk is factory.instances[0]

    def test_login_passes_arguments_to_the_session(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()

        assert client.login(api_key="KEY", personal_id="A9", cert_path="/x.pfx", cert_pass="cp") is True

        factory.instances[0].apikey_login.assert_called_once_with("A9", "KEY", "/x.pfx", "cp")
        factory.instances[0].login.assert_not_called()

    def test_failed_login_returns_false_and_discards_the_sdk(self, live: None, factory: _LoginFactory) -> None:
        factory.login_result = fail("bad credentials")
        client = _new_client()

        assert client.login() is False

        assert client.logged_in is False
        assert client.api is None
        factory.instances[0].shutdown.assert_called_once_with()

    def test_failed_login_survives_a_failing_shutdown(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        factory.login_result = fail("bad credentials")
        client = _new_client()
        original = factory.__call__

        def build(*args: Any, **kwargs: Any) -> MagicMock:
            sdk = original(*args, **kwargs)
            sdk.shutdown.side_effect = RuntimeError("already closed")
            return sdk

        monkeypatch.setattr(client_module, "FubonSDK", build)

        assert client.login() is False

    def test_missing_credentials_return_false(
        self, live: None, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HFT_FUBON_PERSONAL_ID")
        client = _new_client()

        assert client.login() is False

        factory.instances[0].login.assert_not_called()

    def test_sdk_not_installed_returns_false(self, live: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client_module, "FubonSDK", None)
        client = _new_client()

        assert client.login() is False

        assert client.logged_in is False

    def test_sdk_constructor_failure_returns_false(self, live: None, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError("cannot connect")

        monkeypatch.setattr(client_module, "FubonSDK", boom)
        client = _new_client()

        assert client.login() is False

        assert client.api is None


class TestSimulationSafety:
    def test_non_live_login_without_a_test_url_never_builds_the_sdk(self, factory: _LoginFactory) -> None:
        client = _new_client()

        assert client.login() is False

        assert factory.calls == []
        assert client.logged_in is False

    def test_non_live_login_builds_the_sdk_against_the_test_url(
        self, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _TEST_URL)
        client = _new_client()

        assert client.login() is True

        assert factory.calls == [((), {"url": _TEST_URL})]

    def test_non_live_login_refuses_the_production_host(
        self, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_TEST_URL", "wss://neoapi.fbs.com.tw/TASP/XCPXWS")
        client = _new_client()

        assert client.login() is False

        assert factory.calls == []

    @pytest.mark.parametrize("mode", ["", "sim", "disabled", "paper", "LIVE-ish"])
    def test_every_non_live_mode_is_guarded(
        self, factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch, mode: str
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", mode)
        client = _new_client()

        assert client.login() is False

        assert factory.calls == []


class TestReconnectAndClose:
    def test_reconnect_logs_out_then_builds_a_new_session(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()

        assert client.reconnect(reason="drop") is True

        factory.instances[0].logout.assert_called_once_with()
        assert len(factory.instances) == 2
        assert client.api is factory.instances[1]
        assert client.logged_in is True

    def test_close_with_logout_really_logs_out(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()

        client.close(logout=True)

        factory.instances[0].logout.assert_called_once_with()
        assert client.logged_in is False

    def test_close_without_logout_keeps_the_broker_session(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()

        client.close()

        factory.instances[0].logout.assert_not_called()
        assert client.logged_in is False

    def test_close_stops_the_quote_runtime(self) -> None:
        client = _new_client()
        client._quote_runtime = MagicMock()

        client.close()

        client._quote_runtime.stop.assert_called_once_with()
        assert client.logged_in is False

    def test_shutdown_delegates_to_close(self) -> None:
        client = _new_client()
        client._quote_runtime = MagicMock()

        client.shutdown(logout=True)

        client._quote_runtime.stop.assert_called_once_with()
        assert client.logged_in is False


class TestExecutionCallbacks:
    def test_callbacks_set_before_login_are_wired_on_login(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.set_execution_callbacks(MagicMock(), MagicMock())

        assert client.login() is True

        sdk = factory.instances[0]
        for name in REQUIRED_SDK_SETTERS:
            getattr(sdk, name).assert_called_once()

    def test_callbacks_set_after_login_are_wired_immediately(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()

        client.set_execution_callbacks(MagicMock(), MagicMock())

        for name in REQUIRED_SDK_SETTERS:
            getattr(factory.instances[0], name).assert_called_once()

    def test_callbacks_set_before_login_are_only_stored(self) -> None:
        client = _new_client()
        on_order, on_deal = MagicMock(), MagicMock()

        client.set_execution_callbacks(on_order, on_deal)

        assert client._on_order_cb is on_order
        assert client._on_deal_cb is on_deal
        assert client._exec_adapter is None

    def test_wired_order_callback_receives_the_platform_state_payload_shape(
        self, live: None, factory: _LoginFactory
    ) -> None:
        client = _new_client()
        calls: list[tuple[Any, Any]] = []
        client.set_execution_callbacks(lambda state, payload: calls.append((state, payload)), MagicMock())
        client.login()

        handler = factory.instances[0].set_on_futopt_order.call_args.args[0]
        handler("0", SimpleNamespace(order_no="l1", symbol="TMFK6", buy_sell="Buy", lot=1, price=1.0))

        assert [state for state, _ in calls] == ["order"]
        assert calls[0][1]["order_id"] == "l1"

    def test_login_fails_closed_when_the_sdk_cannot_deliver_fills(
        self, live: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def build(*args: Any, **kwargs: Any) -> MagicMock:
            sdk = make_fubon_sdk_mock()
            sdk.login.return_value = ok([make_account()])
            del sdk.set_on_futopt_filled
            return sdk

        monkeypatch.setattr(client_module, "FubonSDK", build)
        client = _new_client()
        client.set_execution_callbacks(MagicMock(), MagicMock())

        assert client.login() is False

        assert client.logged_in is False
        assert client.api is None

    def test_setting_callbacks_on_an_sdk_without_the_setters_raises(self) -> None:
        client = _new_client()
        client._api = SimpleNamespace()

        with pytest.raises(FubonCallbackRegistrationError):
            client.set_execution_callbacks(MagicMock(), MagicMock())


class TestOrderDelegation:
    def test_place_order_delegates(self) -> None:
        client = _new_client()
        gateway = MagicMock(spec=FubonOrderGateway)
        gateway.place_order.return_value = "placed"
        client._order_gateway = gateway

        result = client.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")

        gateway.place_order.assert_called_once_with(symbol="TMFK6", price=220000000, qty=1, side="Buy")
        assert result == "placed"

    def test_cancel_order_delegates(self) -> None:
        client = _new_client()
        gateway = MagicMock(spec=FubonOrderGateway)
        gateway.cancel_order.return_value = "cancelled"
        client._order_gateway = gateway

        assert client.cancel_order("order-123") == "cancelled"

        gateway.cancel_order.assert_called_once_with("order-123")

    def test_update_order_delegates(self) -> None:
        client = _new_client()
        gateway = MagicMock(spec=FubonOrderGateway)
        gateway.update_order.return_value = "updated"
        client._order_gateway = gateway

        assert client.update_order("order-123", price=221000000, qty=2) == "updated"

        gateway.update_order.assert_called_once_with("order-123", price=221000000, qty=2)

    def test_place_order_without_login_raises_instead_of_sending(self) -> None:
        client = _new_client()

        with pytest.raises(NotImplementedError):
            client.place_order(symbol="TMFK6", price=220000000, qty=1, side="Buy")


class TestAccountDelegation:
    @pytest.fixture()
    def gateway(self) -> MagicMock:
        return MagicMock(spec=FubonAccountGateway)

    def test_get_positions_delegates_and_keeps_unknown_distinct(self, gateway: MagicMock) -> None:
        client = _new_client()
        gateway.get_positions.return_value = None
        client._account_gateway = gateway

        assert client.get_positions() is None

        gateway.get_positions.assert_called_once_with()

    def test_get_account_balance_delegates(self, gateway: MagicMock) -> None:
        client = _new_client()
        gateway.get_account_balance.return_value = [{"currency": "NTD"}]
        client._account_gateway = gateway

        assert client.get_account_balance() == [{"currency": "NTD"}]

        gateway.get_account_balance.assert_called_once_with(None)

    def test_get_margin_delegates(self, gateway: MagicMock) -> None:
        client = _new_client()
        gateway.get_margin.return_value = [{"margin": 50}]
        client._account_gateway = gateway

        assert client.get_margin() == [{"margin": 50}]

        gateway.get_margin.assert_called_once_with(None)

    def test_list_profit_loss_delegates_with_dates(self, gateway: MagicMock) -> None:
        client = _new_client()
        gateway.list_profit_loss.return_value = ["row"]
        client._account_gateway = gateway

        assert client.list_profit_loss(begin_date="20260101", end_date="20260301") == ["row"]

        gateway.list_profit_loss.assert_called_once_with(None, "20260101", "20260301")

    def test_list_position_detail_delegates(self, gateway: MagicMock) -> None:
        client = _new_client()
        gateway.list_position_detail.return_value = ["pos"]
        client._account_gateway = gateway

        assert client.list_position_detail() == ["pos"]

        gateway.list_position_detail.assert_called_once_with(None)

    def test_positions_before_login_are_unknown_not_flat(self) -> None:
        assert _new_client().get_positions() is None

    def test_account_queries_after_login_use_the_real_accounting_api(self, live: None, factory: _LoginFactory) -> None:
        client = _new_client()
        client.login()
        sdk = factory.instances[0]
        sdk.futopt_accounting.query_single_position.return_value = ok([])

        assert client.get_positions() == []

        sdk.futopt_accounting.query_single_position.assert_called_once_with(factory.login_result.data[0])


class TestQuoteDelegation:
    def test_subscribe_basket_registers_and_subscribes(self) -> None:
        client = _new_client()
        client._quote_runtime = MagicMock()
        client._symbols = ["TMFK6", "TXFK6"]
        cb = MagicMock()

        client.subscribe_basket(cb)

        client._quote_runtime.register_quote_callbacks.assert_called_once_with(cb, cb)
        client._quote_runtime.subscribe.assert_called_once_with(["TMFK6", "TXFK6"])

    def test_subscribe_basket_without_symbols_only_registers(self) -> None:
        client = _new_client()
        client._quote_runtime = MagicMock()
        cb = MagicMock()

        client.subscribe_basket(cb)

        client._quote_runtime.register_quote_callbacks.assert_called_once_with(cb, cb)
        client._quote_runtime.subscribe.assert_not_called()

    def test_resubscribe_returns_true(self) -> None:
        assert _new_client().resubscribe() is True


class TestFetchSnapshots:
    def test_before_login_returns_empty_without_an_sdk(self) -> None:
        assert _new_client().fetch_snapshots() == []

    def test_delegates_to_the_snapshot_fetcher_with_the_stored_symbols(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetcher_cls = MagicMock()
        fetcher_cls.return_value.fetch_snapshots.return_value = [{"code": "TMFK6"}]
        monkeypatch.setattr(client_module, "FubonSnapshotFetcher", fetcher_cls)
        client = _new_client()
        client._api = sdk = object()
        client._symbols = ["TMFK6"]

        assert client.fetch_snapshots() == [{"code": "TMFK6"}]

        fetcher_cls.assert_called_once_with(sdk)
        fetcher_cls.return_value.fetch_snapshots.assert_called_once_with(["TMFK6"])

    def test_real_fetcher_reads_the_futopt_intraday_quote(self) -> None:
        quote = MagicMock(return_value={})
        client = _new_client()
        client._api = SimpleNamespace(
            marketdata=SimpleNamespace(
                rest_client=SimpleNamespace(futopt=SimpleNamespace(intraday=SimpleNamespace(quote=quote)))
            )
        )
        client._symbols = ["TMFK6"]

        snapshots = client.fetch_snapshots()

        quote.assert_called_once_with(symbol="TMFK6")
        assert [s["code"] for s in snapshots] == ["TMFK6"]


class TestPlaceholders:
    def test_get_exchange_returns_empty_string(self) -> None:
        assert _new_client().get_exchange("TMFK6") == ""

    def test_validate_symbols_returns_empty(self) -> None:
        assert _new_client().validate_symbols() == []

    def test_get_contract_refresh_status_returns_empty_dict(self) -> None:
        assert _new_client().get_contract_refresh_status() == {}
