"""Extended tests for SessionRuntime and SessionPolicy."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji.session_runtime import (
    SessionPolicy,
    SessionRuntime,
    SessionStateSnapshot,
)


def make_mock_client(**overrides):
    """Build a MagicMock that quacks like ShioajiClient for SessionRuntime."""
    c = MagicMock()
    c.api = MagicMock()
    c.logged_in = False
    c.ca_active = False
    c.activate_ca = False
    c.ca_path = ""
    c.fetch_contract = True
    c.contracts_timeout = 30
    c.subscribe_trade = True
    c._login_retry_max = 2
    c._login_timeout_s = 30.0
    c._last_login_error = None
    c._last_reconnect_error = None
    c._reconnect_backoff_s = 30.0
    c._last_session_refresh_ts = 0.0
    c._session_refresh_running = False
    c._session_refresh_interval_s = 3600
    c._session_refresh_check_interval_s = 60
    c._session_refresh_holiday_aware = False
    c.tick_callback = None
    c.metrics = MagicMock()
    c._safe_call_with_timeout = MagicMock(return_value=(True, None, None, False))
    c._record_api_latency = MagicMock()
    c._ensure_session_lock = MagicMock()
    c._release_session_lock = MagicMock()
    c._ensure_contracts = MagicMock()
    c._sanitize_metric_label = MagicMock(return_value="unknown")
    c.reconnect = MagicMock(return_value=True)
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


class TestSessionPolicyProtocol:
    def test_session_policy_is_runtime_checkable(self):
        assert (
            hasattr(SessionPolicy, "__protocol_attrs__")
            or hasattr(SessionPolicy, "__abstractmethods__")
            or issubclass(SessionPolicy, type) is False
        )
        rt = SessionRuntime(make_mock_client())
        assert isinstance(rt, SessionPolicy)

    def test_session_runtime_implements_session_policy(self):
        rt = SessionRuntime(make_mock_client())
        assert isinstance(rt, SessionPolicy)
        assert callable(getattr(rt, "request_reconnect", None))
        assert callable(getattr(rt, "is_logged_in", None))


class TestRequestReconnect:
    def test_delegates_to_client_reconnect(self):
        client = make_mock_client()
        client.reconnect.return_value = True
        rt = SessionRuntime(client)
        result = rt.request_reconnect("quote_timeout", force=True)
        assert result is True
        client.reconnect.assert_called_once_with(reason="quote_timeout", force=True)

    def test_returns_false_on_exception(self):
        client = make_mock_client()
        client.reconnect.side_effect = RuntimeError("lock busy")
        rt = SessionRuntime(client)
        result = rt.request_reconnect("crash")
        assert result is False


class TestIsLoggedIn:
    def test_reads_client_logged_in(self):
        client = make_mock_client(logged_in=True)
        rt = SessionRuntime(client)
        assert rt.is_logged_in() is True
        client.logged_in = False
        assert rt.is_logged_in() is False


class TestLogin:
    def test_login_calls_login_with_retry(self):
        client = make_mock_client()
        rt = SessionRuntime(client)
        with patch.object(SessionRuntime, "login_with_retry", return_value=True) as mock_lwr:
            result = rt.login(api_key="k", secret_key="s")
            assert result is True
            mock_lwr.assert_called_once_with(api_key="k", secret_key="s")


class TestLoginWithRetry:
    def test_no_api_key_returns_false(self, monkeypatch):
        monkeypatch.delenv("SHIOAJI_API_KEY", raising=False)
        monkeypatch.delenv("SHIOAJI_SECRET_KEY", raising=False)
        client = make_mock_client()
        rt = SessionRuntime(client)
        result = rt.login_with_retry()
        assert result is False

    def test_success_path_with_env_vars(self, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "test_key")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "test_secret")
        client = make_mock_client()
        client._safe_call_with_timeout.return_value = (True, None, None, False)
        rt = SessionRuntime(client)
        result = rt.login_with_retry()
        assert result is True
        assert client.logged_in is True
        assert client.ca_active is False
        client._ensure_session_lock.assert_called_once()

    def test_ca_activation_on_success(self, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "k")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
        monkeypatch.setenv("SHIOAJI_PERSON_ID", "A123456789")
        monkeypatch.setenv("SHIOAJI_CA_PASSWORD", "capass")
        client = make_mock_client(activate_ca=True, ca_path="/certs/ca.p12")
        client._safe_call_with_timeout.return_value = (True, None, None, False)
        rt = SessionRuntime(client)
        result = rt.login_with_retry()
        assert result is True
        assert client.ca_active is True
        client.api.activate_ca.assert_called_once_with(ca_path="/certs/ca.p12", ca_passwd="capass")


class TestSnapshot:
    def test_snapshot_returns_correct_values(self):
        client = make_mock_client(
            logged_in=True,
            _reconnect_backoff_s=15.0,
            _last_login_error="timeout",
            _last_reconnect_error="refused",
        )
        rt = SessionRuntime(client)
        snap = rt.snapshot()
        assert isinstance(snap, SessionStateSnapshot)
        assert snap.logged_in is True
        assert snap.reconnect_backoff_s == 15.0
        assert snap.last_login_error == "timeout"
        assert snap.last_reconnect_error == "refused"


class TestSessionStateSnapshotFrozen:
    def test_frozen_raises_on_mutation(self):
        snap = SessionStateSnapshot(
            logged_in=True,
            reconnect_backoff_s=1.0,
            last_login_error=None,
            last_reconnect_error=None,
        )
        with pytest.raises(FrozenInstanceError):
            snap.logged_in = False  # type: ignore[misc]


class TestReconnect:
    def test_reconnect_delegates_to_request_reconnect(self):
        client = make_mock_client()
        client.reconnect.return_value = True
        rt = SessionRuntime(client)
        result = rt.reconnect(reason="manual", force=False)
        assert result is True
        client.reconnect.assert_called_once_with(reason="manual", force=False)


class TestFallbackLoginPreservesFetchContract:
    """Regression: fallback login must NOT permanently set c.fetch_contract=False.

    Bug: session_runtime used to write c.fetch_contract = False when the
    no-contract fallback succeeded.  This killed the post-login
    _ensure_contracts() call (guarded by `if not login_fetch_contract and
    c.fetch_contract`) so contracts_ready stayed False and every order was
    blocked.
    """

    def test_fetch_contract_not_mutated_on_fallback(self, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "k")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
        monkeypatch.delenv("SHIOAJI_CA_PASSWORD", raising=False)
        monkeypatch.setenv("HFT_LOGIN_FETCH_CONTRACT_FALLBACK", "1")

        client = make_mock_client(fetch_contract=True)
        # First attempt (with contracts) fails; fallback (without contracts) succeeds.
        client._safe_call_with_timeout.side_effect = [
            (False, None, "timeout", True),   # initial login timed out
            (True, None, None, False),         # fallback without contracts succeeds
        ]
        client.contracts_ready = True

        rt = SessionRuntime(client)
        result = rt.login_with_retry()

        assert result is True, "login_with_retry should succeed via fallback"
        # c.fetch_contract must remain True — do NOT mutate it during fallback.
        assert client.fetch_contract is True, (
            "c.fetch_contract was permanently set to False by the fallback path — "
            "_ensure_contracts() will never run and orders will be blocked"
        )

    def test_ensure_contracts_called_after_fallback(self, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "k")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "s")
        monkeypatch.delenv("SHIOAJI_CA_PASSWORD", raising=False)
        monkeypatch.setenv("HFT_LOGIN_FETCH_CONTRACT_FALLBACK", "1")

        client = make_mock_client(fetch_contract=True)
        client._safe_call_with_timeout.side_effect = [
            (False, None, "connect error", False),
            (True, None, None, False),
        ]
        client.contracts_ready = True

        rt = SessionRuntime(client)
        rt.login_with_retry()

        # After fallback, _ensure_contracts() must have been called to recover
        # the contracts that were skipped during the fallback login.
        client._ensure_contracts.assert_called_once()
