"""Simulation safety: outside live HFT_ORDER_MODE the Fubon SDK may only reach a test endpoint.

fubon_neo's constructor connects the trading websocket and defaults to the
production endpoint, so the guard must run before the class is called, and
login must refuse a session that was not bound to a test endpoint.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from hft_platform.feed_adapter.fubon.session_runtime import (
    PROD_TRADING_WS_URL,
    FubonSessionRuntime,
    FubonSimSafetyError,
    build_fubon_sdk,
    is_live_order_mode,
    resolve_sdk_url,
    validate_test_url,
)
from tests.unit.fubon_mock_helper import SdkFactory, make_account, make_fubon_sdk_mock, ok

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


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def factory() -> SdkFactory:
    return SdkFactory()


class TestOrderModeClassification:
    @pytest.mark.parametrize("mode", ["live", "LIVE", " live ", "real", "Real"])
    def test_live_spellings_are_live(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", mode)

        assert is_live_order_mode() is True

    @pytest.mark.parametrize("mode", ["", "sim", "disabled", "paper", "livee", "0", "true"])
    def test_everything_else_is_not_live(self, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", mode)

        assert is_live_order_mode() is False

    def test_unset_mode_is_not_live(self) -> None:
        assert is_live_order_mode() is False


class TestValidateTestUrl:
    @pytest.mark.parametrize("url", [_TEST_URL, "ws://localhost:9000/ws", "wss://sandbox.fubon.example/TASP/XCPXWS"])
    def test_non_production_ws_urls_are_accepted(self, url: str) -> None:
        assert validate_test_url(url) == url

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert validate_test_url(f"  {_TEST_URL}  ") == _TEST_URL

    @pytest.mark.parametrize("url", [None, "", "   "])
    def test_missing_url_is_refused(self, url: Any) -> None:
        with pytest.raises(FubonSimSafetyError, match="HFT_FUBON_TEST_URL is not set"):
            validate_test_url(url)

    @pytest.mark.parametrize(
        "url", ["https://test.example/ws", "http://test.example", "ftp://x", "test.example/ws", "wss://"]
    )
    def test_non_websocket_or_hostless_urls_are_refused(self, url: str) -> None:
        with pytest.raises(FubonSimSafetyError, match="ws:// or wss://"):
            validate_test_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            PROD_TRADING_WS_URL,
            "wss://NEOAPI.FBS.COM.TW/TASP/XCPXWS",
            "wss://neoapi.fbs.com.tw./TASP/XCPXWS",
            "wss://neoapi.fbs.com.tw:443/TASP/XCPXWS",
            "ws://neoapi.fbs.com.tw/x",
        ],
    )
    def test_production_host_is_refused_in_every_spelling(self, url: str) -> None:
        with pytest.raises(FubonSimSafetyError, match="production trading host"):
            validate_test_url(url)

    def test_malformed_url_is_refused(self) -> None:
        with pytest.raises(FubonSimSafetyError):
            validate_test_url("wss://[::1")


class TestResolveSdkUrl:
    def test_live_mode_uses_the_vendor_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "live")

        assert resolve_sdk_url() is None

    def test_live_mode_ignores_a_configured_test_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _TEST_URL)

        assert resolve_sdk_url() is None

    def test_sim_mode_uses_the_test_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _TEST_URL)

        assert resolve_sdk_url() == _TEST_URL

    def test_default_mode_without_a_url_refuses(self) -> None:
        with pytest.raises(FubonSimSafetyError, match="refusing to construct the SDK"):
            resolve_sdk_url()

    def test_sim_mode_with_the_production_url_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        monkeypatch.setenv("HFT_FUBON_TEST_URL", PROD_TRADING_WS_URL)

        with pytest.raises(FubonSimSafetyError, match="production trading host"):
            resolve_sdk_url()


class TestBuildFubonSdk:
    def test_the_sdk_class_is_never_called_when_refused(self, factory: SdkFactory) -> None:
        with pytest.raises(FubonSimSafetyError):
            build_fubon_sdk(factory)

        assert factory.calls == []

    def test_the_sdk_class_is_never_called_for_the_production_url(
        self, factory: SdkFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_TEST_URL", PROD_TRADING_WS_URL)

        with pytest.raises(FubonSimSafetyError):
            build_fubon_sdk(factory)

        assert factory.calls == []

    def test_non_live_passes_the_test_url_as_the_real_keyword(
        self, factory: SdkFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_TEST_URL", _TEST_URL)

        sdk, url = build_fubon_sdk(factory)

        assert factory.calls == [((), {"url": _TEST_URL})]
        assert url == _TEST_URL
        assert sdk is factory.instances[0]

    def test_live_mode_constructs_with_no_arguments(self, factory: SdkFactory, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "live")

        sdk, url = build_fubon_sdk(factory)

        assert factory.calls == [((), {})]
        assert url is None
        assert sdk is factory.instances[0]

    def test_factory_rejects_a_keyword_the_real_constructor_does_not_have(self, factory: SdkFactory) -> None:
        with pytest.raises(TypeError):
            factory(ws_url=_TEST_URL)


class TestLoginTargetGuard:
    @pytest.fixture()
    def creds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")

    @pytest.fixture()
    def sdk(self) -> Any:
        sdk = make_fubon_sdk_mock()
        sdk.login.return_value = ok([make_account()])
        sdk.apikey_login.return_value = ok([make_account()])
        return sdk

    def test_login_is_refused_when_not_live_and_not_bound_to_a_test_url(self, creds: None, sdk: Any) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login() is False

        sdk.login.assert_not_called()
        sdk.apikey_login.assert_not_called()
        assert "refusing to log in" in (rt.last_login_error or "")
        assert rt.is_logged_in is False

    def test_login_is_refused_when_bound_to_the_production_url(self, creds: None, sdk: Any) -> None:
        rt = FubonSessionRuntime(sdk, {"sdk_url": PROD_TRADING_WS_URL})

        assert rt.login() is False

        sdk.login.assert_not_called()

    def test_login_with_api_key_is_also_refused(self, creds: None, sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "KEY")
        rt = FubonSessionRuntime(sdk)

        assert rt.login() is False

        sdk.apikey_login.assert_not_called()

    def test_login_succeeds_when_bound_to_a_test_url(self, creds: None, sdk: Any) -> None:
        rt = FubonSessionRuntime(sdk, {"sdk_url": _TEST_URL})

        assert rt.login() is True

        sdk.login.assert_called_once()
        assert rt.snapshot()["sdk_target"] == "test"

    def test_login_succeeds_in_live_mode_without_a_test_url(
        self, creds: None, sdk: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "live")
        rt = FubonSessionRuntime(sdk)

        assert rt.login() is True

        assert rt.snapshot()["sdk_target"] == "vendor_default"

    def test_a_refusal_is_not_retried(self, creds: None, sdk: Any) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login_with_retry(max_retries=5, backoff_base_s=60.0) is False

        sdk.login.assert_not_called()

    def test_refused_login_logs_no_credentials(self, sdk: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "PID-SECRET-1")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "PWD-SECRET-2")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")
        monkeypatch.setenv("HFT_FUBON_CERT_PASS", "CERT-SECRET-3")
        rt = FubonSessionRuntime(sdk)

        with structlog.testing.capture_logs() as logs:
            rt.login()

        assert logs
        rendered = repr(logs) + (rt.last_login_error or "")
        for secret in ("PID-SECRET-1", "PWD-SECRET-2", "CERT-SECRET-3"):
            assert secret not in rendered

    def test_a_session_restored_to_live_mode_can_log_in_after_a_refusal(
        self, creds: None, sdk: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rt = FubonSessionRuntime(sdk)
        assert rt.login() is False
        monkeypatch.setenv("HFT_ORDER_MODE", "live")

        assert rt.login() is True


class TestMocksMatchTheRealSdk:
    """A wrong method name or argument list in the code under test must fail a test."""

    @pytest.mark.parametrize(
        "name",
        ["set_order_callback", "set_on_dealt", "set_on_order_dealt", "futopt_login", "connect"],
    )
    def test_invented_sdk_attribute_raises(self, name: str) -> None:
        with pytest.raises(AttributeError):
            getattr(make_fubon_sdk_mock(), name)

    @pytest.mark.parametrize("name", ["cancel", "modify_price_obj", "place_futopt_order", "update_order"])
    def test_invented_futopt_attribute_raises(self, name: str) -> None:
        with pytest.raises(AttributeError):
            getattr(make_fubon_sdk_mock().futopt, name)

    def test_login_with_the_wrong_arity_raises(self) -> None:
        sdk = make_fubon_sdk_mock()

        with pytest.raises(TypeError):
            sdk.login("only-one-argument")

    def test_place_order_without_an_account_raises(self) -> None:
        sdk = make_fubon_sdk_mock()

        with pytest.raises(TypeError):
            sdk.futopt.place_order()
