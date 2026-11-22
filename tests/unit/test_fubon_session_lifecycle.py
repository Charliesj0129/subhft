"""Tests for FubonSessionRuntime retry, logout, reconnect and refresh lifecycle."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.fubon import session_runtime
from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime as SessionWrapper
from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime
from tests.unit.fubon_mock_helper import fail, make_account, make_fubon_sdk_mock, ok
from tests.unit.time_stub import sleepless_time

_CREDS = {"password": "pwd", "personal_id": "A123456789", "cert_path": "/c.pfx"}

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
def live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lifecycle mechanics run in live mode; non-live refusal lives in test_fubon_sdk_safety."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HFT_ORDER_MODE", "live")


@pytest.fixture()
def env_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
    monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
    monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")


@pytest.fixture()
def account() -> SimpleNamespace:
    return make_account()


@pytest.fixture()
def sdk(account: SimpleNamespace) -> MagicMock:
    sdk = make_fubon_sdk_mock()
    sdk.login.return_value = ok([account])
    return sdk


class TestLoginWithRetry:
    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_success_on_first_attempt_does_not_sleep(self, stub_time: SimpleNamespace, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login_with_retry(**_CREDS) is True

        sdk.login.assert_called_once()
        stub_time.sleep.assert_not_called()

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_success_on_second_attempt_sleeps_once(
        self, stub_time: SimpleNamespace, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        sdk.login.side_effect = [fail("busy"), ok([account])]
        rt = FubonSessionRuntime(sdk)

        assert rt.login_with_retry(max_retries=3, backoff_base_s=0.5, **_CREDS) is True

        assert sdk.login.call_count == 2
        stub_time.sleep.assert_called_once_with(0.5)

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_exhausted_retries_return_false_with_backoff_ladder(
        self, stub_time: SimpleNamespace, sdk: MagicMock
    ) -> None:
        sdk.login.side_effect = RuntimeError("down")
        rt = FubonSessionRuntime(sdk)

        assert rt.login_with_retry(max_retries=4, backoff_base_s=1.0, **_CREDS) is False

        assert sdk.login.call_count == 4
        assert [c.args[0] for c in stub_time.sleep.call_args_list] == [1.0, 2.0, 4.0]
        assert rt.is_logged_in is False

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_missing_credentials_are_retried_without_touching_the_sdk(
        self, stub_time: SimpleNamespace, sdk: MagicMock
    ) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login_with_retry(max_retries=2, backoff_base_s=0.1) is False

        sdk.login.assert_not_called()
        sdk.apikey_login.assert_not_called()

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_credentials_are_forwarded_to_every_attempt(
        self, stub_time: SimpleNamespace, sdk: MagicMock, account: SimpleNamespace
    ) -> None:
        sdk.apikey_login.side_effect = [fail("busy"), ok([account])]
        rt = FubonSessionRuntime(sdk)

        rt.login_with_retry(max_retries=2, api_key="KEY", personal_id="A123456789", cert_path="/c.pfx", cert_pass="cp")

        assert [c.args for c in sdk.apikey_login.call_args_list] == [("A123456789", "KEY", "/c.pfx", "cp")] * 2


class TestLogout:
    def test_logout_clears_logged_in_state(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(**_CREDS)

        rt.logout()

        sdk.logout.assert_called_once_with()
        assert rt.is_logged_in is False

    def test_logout_error_still_clears_state(self, sdk: MagicMock) -> None:
        sdk.logout.side_effect = RuntimeError("socket closed")
        rt = FubonSessionRuntime(sdk)
        rt.login(**_CREDS)

        rt.logout()

        assert rt.is_logged_in is False


class TestReconnect:
    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_reconnect_logs_out_then_in_again(
        self, stub_time: SimpleNamespace, sdk: MagicMock, env_credentials: None
    ) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.reconnect(reason="test") is True

        sdk.logout.assert_called_once_with()
        sdk.login.assert_called_once_with("A123456789", "pwd", "/c.pfx", None)
        assert rt.is_logged_in is True

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_cooldown_blocks_a_second_reconnect(
        self, stub_time: SimpleNamespace, sdk: MagicMock, env_credentials: None
    ) -> None:
        rt = FubonSessionRuntime(sdk, config={"reconnect_cooldown_s": 999})

        assert rt.reconnect(reason="first") is True
        assert rt.reconnect(reason="second") is False

        assert sdk.logout.call_count == 1

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_force_bypasses_the_cooldown(
        self, stub_time: SimpleNamespace, sdk: MagicMock, env_credentials: None
    ) -> None:
        rt = FubonSessionRuntime(sdk, config={"reconnect_cooldown_s": 999})

        assert rt.reconnect(reason="first") is True
        assert rt.reconnect(reason="forced", force=True) is True

        assert sdk.logout.call_count == 2

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_failed_reconnect_leaves_the_session_logged_out(
        self, stub_time: SimpleNamespace, sdk: MagicMock, env_credentials: None
    ) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(**_CREDS)
        sdk.login.return_value = fail("gateway timeout")

        assert rt.reconnect(reason="drop", force=True) is False

        assert rt.is_logged_in is False
        assert "gateway timeout" in (rt.last_login_error or "")


class TestRefreshToken:
    def test_refresh_token_logs_out_then_in_again(self, sdk: MagicMock, env_credentials: None) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(**_CREDS)
        sdk.reset_mock()
        sdk.login.return_value = ok([make_account()])

        assert rt.refresh_token() is True

        sdk.logout.assert_called_once_with()
        sdk.login.assert_called_once()
        assert rt.is_logged_in is True

    def test_refresh_token_without_credentials_stays_logged_out(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)
        rt.login(**_CREDS)

        assert rt.refresh_token() is False

        assert rt.is_logged_in is False
        assert "missing credentials" in (rt.last_login_error or "")


class TestInitialState:
    def test_runtime_starts_logged_out_with_no_account(self, sdk: MagicMock) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.is_logged_in is False
        assert rt.account is None
        assert rt.last_login_error is None

    def test_default_cooldown_is_five_seconds(self, sdk: MagicMock) -> None:
        assert FubonSessionRuntime(sdk).snapshot()["reconnect_cooldown_s"] == 5.0

    def test_cooldown_is_configurable(self, sdk: MagicMock) -> None:
        assert FubonSessionRuntime(sdk, config={"reconnect_cooldown_s": 12}).snapshot()["reconnect_cooldown_s"] == 12.0

    def test_login_logout_cycle_updates_state(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        rt = FubonSessionRuntime(sdk)

        assert rt.login(**_CREDS) is True
        assert (rt.is_logged_in, rt.account) == (True, account)
        rt.logout()
        assert rt.is_logged_in is False


class TestSessionWrapper:
    def test_wrapper_delegates_login_and_exposes_state(self, sdk: MagicMock, account: SimpleNamespace) -> None:
        wrapper = SessionWrapper(sdk)

        assert wrapper.login(**_CREDS) is True

        assert wrapper.is_logged_in is True
        assert wrapper.account is account
        sdk.login.assert_called_once_with("A123456789", "pwd", "/c.pfx", None)

    def test_wrapper_forwards_config_to_the_runtime(self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        wrapper = SessionWrapper(sdk, {"sdk_url": "wss://test.example.invalid/ws"})

        assert wrapper.login(**_CREDS) is True

        sdk.login.assert_called_once()

    def test_wrapper_without_sdk_url_refuses_outside_live(
        self, sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_ORDER_MODE", "sim")
        wrapper = SessionWrapper(sdk)

        assert wrapper.login(**_CREDS) is False

        sdk.login.assert_not_called()

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_wrapper_delegates_retry_reconnect_refresh_and_logout(
        self, stub_time: SimpleNamespace, sdk: MagicMock, env_credentials: None
    ) -> None:
        wrapper = SessionWrapper(sdk)

        assert wrapper.login_with_retry(max_retries=1) is True
        assert wrapper.reconnect(reason="t", force=True) is True
        assert wrapper.refresh_token() is True
        wrapper.logout()

        assert wrapper.is_logged_in is False
        assert sdk.logout.call_count == 3
