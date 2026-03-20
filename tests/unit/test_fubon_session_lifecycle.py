"""Tests for FubonSessionRuntime login, logout, reconnect, and retry lifecycle."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime


def make_mock_sdk(
    login_result: list | None = None,
    login_error: Exception | None = None,
) -> MagicMock:
    """Create a mock Fubon SDK with configurable login behaviour."""
    sdk = MagicMock()
    if login_error:
        sdk.login.side_effect = login_error
    else:
        result = MagicMock()
        result.data = [MagicMock()] if login_result is None else login_result
        sdk.login.return_value = result
    return sdk


def _make_runtime(
    sdk: MagicMock | None = None,
    config: dict | None = None,
) -> FubonSessionRuntime:
    return FubonSessionRuntime(sdk or make_mock_sdk(), config)


class TestLoginSuccess:
    def test_login_returns_true_and_sets_state(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        assert rt.login(api_key="key", password="pwd") is True
        assert rt.is_logged_in is True
        assert rt.account is not None
        sdk.login.assert_called_once_with("key", "pwd", "")

    def test_login_passes_cert_path(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p", cert_path="/cert")
        sdk.login.assert_called_once_with("k", "p", "/cert")


class TestLoginMissingCredentials:
    def test_missing_key_returns_false(self) -> None:
        rt = _make_runtime()
        assert rt.login(api_key="", password="pwd") is False
        assert rt.is_logged_in is False

    def test_missing_password_returns_false(self) -> None:
        rt = _make_runtime()
        assert rt.login(api_key="key", password="") is False

    def test_both_missing_returns_false(self) -> None:
        rt = _make_runtime()
        assert rt.login() is False


class TestLoginSDKException:
    def test_sdk_raises_returns_false(self) -> None:
        sdk = make_mock_sdk(login_error=RuntimeError("connection refused"))
        rt = _make_runtime(sdk)
        assert rt.login(api_key="k", password="p") is False
        assert rt.is_logged_in is False
        assert rt._last_login_error == "connection refused"


class TestLoginNoAccounts:
    def test_empty_data_returns_false(self) -> None:
        sdk = make_mock_sdk(login_result=[])
        rt = _make_runtime(sdk)
        assert rt.login(api_key="k", password="p") is False
        assert rt.is_logged_in is False

    def test_none_result_returns_false(self) -> None:
        sdk = MagicMock()
        sdk.login.return_value = None
        rt = _make_runtime(sdk)
        assert rt.login(api_key="k", password="p") is False


class TestLoginEnvVarFallback:
    def test_falls_back_to_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "env_key")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "env_pwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/env/cert")
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        assert rt.login() is True
        sdk.login.assert_called_once_with("env_key", "env_pwd", "/env/cert")


class TestLoginWithRetryFirstAttempt:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_success_first_attempt(self, mock_sleep: MagicMock) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        assert rt.login_with_retry(api_key="k", password="p") is True
        sdk.login.assert_called_once()
        mock_sleep.assert_not_called()


class TestLoginWithRetrySecondAttempt:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_success_second_attempt(self, mock_sleep: MagicMock) -> None:
        sdk = MagicMock()
        fail_result = MagicMock()
        fail_result.data = []
        ok_result = MagicMock()
        ok_result.data = [MagicMock()]
        sdk.login.side_effect = [fail_result, ok_result]
        rt = _make_runtime(sdk)
        assert rt.login_with_retry(max_retries=3, backoff_base_s=0.5, api_key="k", password="p") is True
        assert sdk.login.call_count == 2
        mock_sleep.assert_called_once_with(0.5)


class TestLoginWithRetryExhausted:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_all_retries_fail(self, mock_sleep: MagicMock) -> None:
        sdk = make_mock_sdk(login_error=RuntimeError("down"))
        rt = _make_runtime(sdk)
        assert rt.login_with_retry(max_retries=3, backoff_base_s=1.0, api_key="k", password="p") is False
        assert sdk.login.call_count == 3
        assert mock_sleep.call_count == 2


class TestLogoutSuccess:
    def test_logout_clears_state(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p")
        assert rt.is_logged_in is True
        rt.logout()
        assert rt.is_logged_in is False
        sdk.logout.assert_called_once()


class TestLogoutException:
    def test_logout_exception_still_clears_state(self) -> None:
        sdk = make_mock_sdk()
        sdk.logout.side_effect = RuntimeError("disconnect error")
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p")
        rt.logout()
        assert rt.is_logged_in is False


class TestReconnectSuccess:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_reconnect_calls_logout_then_login(self, mock_sleep: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        assert rt.reconnect(reason="test") is True
        sdk.logout.assert_called_once()
        assert rt.is_logged_in is True


class TestReconnectCooldown:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_cooldown_blocks_reconnect(self, mock_sleep: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk, config={"reconnect_cooldown_s": 999})
        assert rt.reconnect(reason="first") is True
        assert rt.reconnect(reason="second") is False


class TestReconnectForce:
    @patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep")
    def test_force_bypasses_cooldown(self, mock_sleep: MagicMock, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk, config={"reconnect_cooldown_s": 999})
        assert rt.reconnect(reason="first") is True
        assert rt.reconnect(reason="forced", force=True) is True


class TestRefreshToken:
    def test_refresh_token_does_logout_then_login(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p")
        sdk.reset_mock()
        ok_result = MagicMock()
        ok_result.data = [MagicMock()]
        sdk.login.return_value = ok_result
        assert rt.refresh_token() is False
        sdk.logout.assert_called_once()

    def test_refresh_token_succeeds_with_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        assert rt.refresh_token() is True
        sdk.logout.assert_called_once()
        assert rt.is_logged_in is True


class TestSnapshot:
    def test_snapshot_before_login(self) -> None:
        rt = _make_runtime()
        snap = rt.snapshot()
        assert snap["logged_in"] is False
        assert snap["account"] is None
        assert snap["last_login_error"] is None
        assert snap["last_reconnect_ns"] == 0
        assert isinstance(snap["reconnect_cooldown_s"], float)
        assert snap["login_latency_ns"] == 0

    def test_snapshot_after_login(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p")
        snap = rt.snapshot()
        assert snap["logged_in"] is True
        assert snap["account"] is not None
        assert snap["last_login_error"] is None
        assert snap["login_latency_ns"] > 0


class TestProperties:
    def test_is_logged_in_default(self) -> None:
        rt = _make_runtime()
        assert rt.is_logged_in is False

    def test_account_default(self) -> None:
        rt = _make_runtime()
        assert rt.account is None

    def test_account_set_after_login(self) -> None:
        sdk = make_mock_sdk()
        rt = _make_runtime(sdk)
        rt.login(api_key="k", password="p")
        assert rt.account is not None
