"""Tests for FubonSessionRuntime session lifecycle."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from tests.unit.fubon_mock_helper import install_fubon_neo_mock

install_fubon_neo_mock()

from hft_platform.feed_adapter.fubon.session import FubonSessionRuntime


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture()
def mock_sdk() -> MagicMock:
    sdk = MagicMock()
    accounts_result = MagicMock()
    accounts_result.data = [MagicMock(name="acct-001")]
    sdk.login.return_value = accounts_result
    return sdk


@pytest.fixture()
def env_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HFT_FUBON_API_KEY", "test-api-key")
    monkeypatch.setenv("HFT_FUBON_PASSWORD", "test-password")
    monkeypatch.delenv("HFT_FUBON_CERT_PATH", raising=False)


# ------------------------------------------------------------------ #
# Login — success
# ------------------------------------------------------------------ #


class TestLoginSuccess:
    def test_login_sets_logged_in(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        assert session.login() is True
        assert session.is_logged_in is True

    def test_login_stores_first_account(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        assert session._account is not None

    def test_login_calls_sdk_with_key_and_password(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        mock_sdk.login.assert_called_once_with("test-api-key", "test-password")

    def test_login_with_cert_path(
        self,
        mock_sdk: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "key")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/tmp/cert.pem")
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        mock_sdk.login.assert_called_once_with(
            "key", "pwd", cert_path="/tmp/cert.pem"
        )

    def test_login_clears_last_error_on_success(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session._last_login_error = "previous error"
        session.login()
        assert session._last_login_error is None


# ------------------------------------------------------------------ #
# Login — failure
# ------------------------------------------------------------------ #


class TestLoginFailure:
    def test_login_missing_env_vars(self, mock_sdk: MagicMock) -> None:
        session = FubonSessionRuntime(mock_sdk)
        assert session.login() is False
        assert session.is_logged_in is False
        assert "missing" in (session._last_login_error or "")

    def test_login_missing_password(
        self, mock_sdk: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "key")
        monkeypatch.delenv("HFT_FUBON_PASSWORD", raising=False)
        session = FubonSessionRuntime(mock_sdk)
        assert session.login() is False

    def test_login_sdk_exception_retries(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        mock_sdk.login.side_effect = RuntimeError("connection refused")
        session = FubonSessionRuntime(mock_sdk)
        with patch("hft_platform.feed_adapter.fubon.session.time.sleep"):
            result = session.login()
        assert result is False
        assert mock_sdk.login.call_count == 3  # default max attempts
        assert session._last_login_error == "connection refused"

    def test_login_no_accounts_warns_but_succeeds(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        result = MagicMock()
        result.data = []
        mock_sdk.login.return_value = result
        session = FubonSessionRuntime(mock_sdk)
        assert session.login() is True
        assert session._account is None


# ------------------------------------------------------------------ #
# Retry / backoff
# ------------------------------------------------------------------ #


class TestRetryLogic:
    def test_retry_count_respects_config(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        mock_sdk.login.side_effect = RuntimeError("fail")
        session = FubonSessionRuntime(mock_sdk, config={"login_retry_max": 5})
        with patch("hft_platform.feed_adapter.fubon.session.time.sleep"):
            session.login()
        assert mock_sdk.login.call_count == 5

    def test_exponential_backoff_applied(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        mock_sdk.login.side_effect = RuntimeError("fail")
        session = FubonSessionRuntime(mock_sdk)
        with patch("hft_platform.feed_adapter.fubon.session.time.sleep") as mock_sleep:
            session.login()
        # 3 attempts, 2 sleeps: 1.0s then 2.0s
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1.0)
        mock_sleep.assert_any_call(2.0)

    def test_success_on_second_attempt(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        accounts_result = MagicMock()
        accounts_result.data = [MagicMock()]
        mock_sdk.login.side_effect = [RuntimeError("fail"), accounts_result]
        session = FubonSessionRuntime(mock_sdk)
        with patch("hft_platform.feed_adapter.fubon.session.time.sleep"):
            result = session.login()
        assert result is True
        assert session.is_logged_in is True
        assert mock_sdk.login.call_count == 2


# ------------------------------------------------------------------ #
# Logout
# ------------------------------------------------------------------ #


class TestLogout:
    def test_logout_sets_logged_in_false(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        session.logout()
        assert session.is_logged_in is False
        assert session._account is None

    def test_logout_calls_sdk(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        session.logout()
        mock_sdk.logout.assert_called_once()

    def test_logout_noop_when_not_logged_in(self, mock_sdk: MagicMock) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.logout()
        mock_sdk.logout.assert_not_called()

    def test_logout_handles_sdk_exception(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        mock_sdk.logout.side_effect = RuntimeError("cleanup error")
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        session.logout()  # should not raise
        assert session.is_logged_in is False


# ------------------------------------------------------------------ #
# Reconnect
# ------------------------------------------------------------------ #


class TestReconnect:
    def test_reconnect_performs_logout_then_login(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        mock_sdk.reset_mock()
        # Re-set login return value after reset
        accounts_result = MagicMock()
        accounts_result.data = [MagicMock()]
        mock_sdk.login.return_value = accounts_result

        result = session.request_reconnect(reason="test")
        assert result is True
        mock_sdk.logout.assert_called_once()
        mock_sdk.login.assert_called_once()

    def test_reconnect_returns_false_on_login_failure(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        mock_sdk.login.side_effect = RuntimeError("fail")
        with patch("hft_platform.feed_adapter.fubon.session.time.sleep"):
            result = session.request_reconnect(reason="test", force=True)
        assert result is False


# ------------------------------------------------------------------ #
# Refresh token
# ------------------------------------------------------------------ #


class TestRefreshToken:
    def test_refresh_token_performs_logout_login(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        mock_sdk.reset_mock()
        accounts_result = MagicMock()
        accounts_result.data = [MagicMock()]
        mock_sdk.login.return_value = accounts_result

        result = session.refresh_token()
        assert result is True
        mock_sdk.logout.assert_called_once()
        mock_sdk.login.assert_called_once()


# ------------------------------------------------------------------ #
# is_logged_in property
# ------------------------------------------------------------------ #


class TestIsLoggedIn:
    def test_initially_false(self, mock_sdk: MagicMock) -> None:
        session = FubonSessionRuntime(mock_sdk)
        assert session.is_logged_in is False

    def test_true_after_login(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        assert session.is_logged_in is True


# ------------------------------------------------------------------ #
# Snapshot
# ------------------------------------------------------------------ #


class TestSnapshot:
    def test_snapshot_structure(
        self, mock_sdk: MagicMock, env_creds: None
    ) -> None:
        session = FubonSessionRuntime(mock_sdk)
        session.login()
        snap = session.snapshot()
        assert snap["logged_in"] is True
        assert snap["account"] is not None
        assert snap["last_login_error"] is None
        assert "timestamp_ns" in snap

    def test_snapshot_when_not_logged_in(self, mock_sdk: MagicMock) -> None:
        session = FubonSessionRuntime(mock_sdk)
        snap = session.snapshot()
        assert snap["logged_in"] is False
        assert snap["account"] is None
