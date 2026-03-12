"""Tests for FubonSessionRuntime — login, retry, reconnect, cooldown."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.fubon.session_runtime import FubonSessionRuntime

# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #


def _make_sdk(
    login_ok: bool = True,
    login_raises: Exception | None = None,
    account_data: list[Any] | None = None,
) -> MagicMock:
    """Return a mock FubonSDK."""
    sdk = MagicMock()
    if login_raises is not None:
        sdk.login.side_effect = login_raises
    elif login_ok:
        data = account_data or ["ACCOUNT-001"]
        sdk.login.return_value = SimpleNamespace(data=data)
    else:
        sdk.login.return_value = SimpleNamespace(data=[])
    return sdk


# ------------------------------------------------------------------ #
# Login success / failure
# ------------------------------------------------------------------ #


class TestLogin:
    def test_login_success(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        result = rt.login(api_key="key", password="pwd", cert_path="/cert")

        assert result is True
        assert rt.is_logged_in is True
        assert rt.account == "ACCOUNT-001"
        sdk.login.assert_called_once_with("key", "pwd", "/cert")

    def test_login_missing_credentials(self) -> None:
        sdk = _make_sdk()
        rt = FubonSessionRuntime(sdk)

        result = rt.login()  # no creds, no env vars

        assert result is False
        assert rt.is_logged_in is False
        sdk.login.assert_not_called()

    def test_login_sdk_exception(self) -> None:
        sdk = _make_sdk(login_raises=ConnectionError("timeout"))
        rt = FubonSessionRuntime(sdk)

        result = rt.login(api_key="k", password="p")

        assert result is False
        assert rt.is_logged_in is False
        snap = rt.snapshot()
        assert snap["last_login_error"] == "timeout"

    def test_login_empty_accounts(self) -> None:
        sdk = _make_sdk(login_ok=False)
        rt = FubonSessionRuntime(sdk)

        result = rt.login(api_key="k", password="p")

        assert result is False
        assert rt.is_logged_in is False

    def test_login_none_data(self) -> None:
        sdk = MagicMock()
        sdk.login.return_value = SimpleNamespace(data=None)
        rt = FubonSessionRuntime(sdk)

        assert rt.login(api_key="k", password="p") is False

    def test_login_reads_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "env_key")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "env_pwd")
        monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/env/cert")

        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        assert rt.login() is True
        sdk.login.assert_called_once_with("env_key", "env_pwd", "/env/cert")

    def test_login_explicit_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "env_key")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "env_pwd")

        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        assert rt.login(api_key="explicit_key", password="explicit_pwd") is True
        sdk.login.assert_called_once_with("explicit_key", "explicit_pwd", "")


# ------------------------------------------------------------------ #
# Retry logic
# ------------------------------------------------------------------ #


class TestLoginWithRetry:
    def test_retry_succeeds_first_attempt(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        result = rt.login_with_retry(max_retries=3, api_key="k", password="p")

        assert result is True
        assert sdk.login.call_count == 1

    def test_retry_succeeds_on_second_attempt(self) -> None:
        sdk = MagicMock()
        # First call fails, second succeeds
        sdk.login.side_effect = [
            ConnectionError("fail"),
            SimpleNamespace(data=["ACCT"]),
        ]
        rt = FubonSessionRuntime(sdk)

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            result = rt.login_with_retry(max_retries=3, api_key="k", password="p")

        assert result is True
        assert sdk.login.call_count == 2

    def test_retry_exhausted(self) -> None:
        sdk = _make_sdk(login_raises=ConnectionError("fail"))
        rt = FubonSessionRuntime(sdk)

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            result = rt.login_with_retry(max_retries=3, api_key="k", password="p")

        assert result is False
        assert sdk.login.call_count == 3

    def test_exponential_backoff_timing(self) -> None:
        sdk = _make_sdk(login_raises=ConnectionError("fail"))
        rt = FubonSessionRuntime(sdk)

        sleep_calls: list[float] = []

        with patch(
            "hft_platform.feed_adapter.fubon.session_runtime.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ):
            rt.login_with_retry(max_retries=4, backoff_base_s=1.0, api_key="k", password="p")

        # 3 sleeps between 4 attempts: 1s, 2s, 4s
        assert sleep_calls == [1.0, 2.0, 4.0]

    def test_retry_with_custom_backoff(self) -> None:
        sdk = _make_sdk(login_raises=ConnectionError("fail"))
        rt = FubonSessionRuntime(sdk)

        sleep_calls: list[float] = []

        with patch(
            "hft_platform.feed_adapter.fubon.session_runtime.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ):
            rt.login_with_retry(max_retries=3, backoff_base_s=0.5, api_key="k", password="p")

        assert sleep_calls == [0.5, 1.0]


# ------------------------------------------------------------------ #
# Reconnect + cooldown
# ------------------------------------------------------------------ #


class TestReconnect:
    def test_reconnect_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            result = rt.reconnect(reason="test", force=True)

        assert result is True
        sdk.logout.assert_called_once()

    def test_reconnect_cooldown_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk, config={"reconnect_cooldown_s": 100.0})

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            # First reconnect succeeds
            assert rt.reconnect(reason="first", force=True) is True
            # Second reconnect blocked by cooldown
            assert rt.reconnect(reason="second") is False

    def test_reconnect_cooldown_bypass_with_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk, config={"reconnect_cooldown_s": 100.0})

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            assert rt.reconnect(reason="first", force=True) is True
            assert rt.reconnect(reason="second", force=True) is True

    def test_reconnect_first_call_no_cooldown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """First reconnect should never be blocked by cooldown."""
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep"):
            assert rt.reconnect(reason="initial") is True


# ------------------------------------------------------------------ #
# Logout
# ------------------------------------------------------------------ #


class TestLogout:
    def test_logout_sets_logged_in_false(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)
        rt.login(api_key="k", password="p")
        assert rt.is_logged_in is True

        rt.logout()

        assert rt.is_logged_in is False
        sdk.logout.assert_called_once()

    def test_logout_tolerates_sdk_error(self) -> None:
        sdk = MagicMock()
        sdk.logout.side_effect = RuntimeError("already disconnected")
        rt = FubonSessionRuntime(sdk)
        rt._logged_in = True

        rt.logout()  # should not raise

        assert rt.is_logged_in is False


# ------------------------------------------------------------------ #
# Refresh token
# ------------------------------------------------------------------ #


class TestRefreshToken:
    def test_refresh_token_relogins(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)
        rt._logged_in = True

        result = rt.refresh_token()

        assert result is False  # login() called without creds/env → fails
        sdk.logout.assert_called_once()

    def test_refresh_token_with_creds_in_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HFT_FUBON_API_KEY", "k")
        monkeypatch.setenv("HFT_FUBON_PASSWORD", "p")

        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)
        rt._logged_in = True

        result = rt.refresh_token()

        assert result is True
        sdk.logout.assert_called_once()


# ------------------------------------------------------------------ #
# State transitions
# ------------------------------------------------------------------ #


class TestStateTransitions:
    def test_initial_state(self) -> None:
        sdk = _make_sdk()
        rt = FubonSessionRuntime(sdk)

        assert rt.is_logged_in is False
        assert rt.account is None

    def test_login_logout_cycle(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)

        rt.login(api_key="k", password="p")
        assert rt.is_logged_in is True

        rt.logout()
        assert rt.is_logged_in is False


# ------------------------------------------------------------------ #
# Snapshot
# ------------------------------------------------------------------ #


class TestSnapshot:
    def test_snapshot_initial(self) -> None:
        sdk = _make_sdk()
        rt = FubonSessionRuntime(sdk)

        snap = rt.snapshot()

        assert snap["logged_in"] is False
        assert snap["account"] is None
        assert snap["last_login_error"] is None
        assert snap["reconnect_cooldown_s"] == 5.0

    def test_snapshot_after_login(self) -> None:
        sdk = _make_sdk(login_ok=True)
        rt = FubonSessionRuntime(sdk)
        rt.login(api_key="k", password="p")

        snap = rt.snapshot()

        assert snap["logged_in"] is True
        assert snap["account"] == "ACCOUNT-001"
        assert snap["login_latency_ns"] > 0

    def test_snapshot_after_failed_login(self) -> None:
        sdk = _make_sdk(login_raises=ValueError("bad"))
        rt = FubonSessionRuntime(sdk)
        rt.login(api_key="k", password="p")

        snap = rt.snapshot()

        assert snap["logged_in"] is False
        assert snap["last_login_error"] == "bad"
