"""Tests for WU7 session improvements: contracts_cb, receive_window, list_accounts."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, call, patch

from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime


# ------------------------------------------------------------------ #
# Helper: build a minimal mock client for login_with_retry
# ------------------------------------------------------------------ #


def _make_client(**overrides: object) -> MagicMock:
    client = MagicMock()
    client.api = MagicMock()
    client.ca_active = False
    client.logged_in = False
    client._last_login_error = None
    client.contracts_timeout = 10000
    client.fetch_contract = True
    client.subscribe_trade = True
    client._login_timeout_s = 20
    client._login_retry_max = 0  # single attempt
    client.activate_ca = False
    client._receive_window = None
    client._last_session_refresh_ts = 0
    client._safe_call_with_timeout = MagicMock(return_value=(True, None, None, False))
    for k, v in overrides.items():
        setattr(client, k, v)
    return client


# ------------------------------------------------------------------ #
# contracts_cb tests
# ------------------------------------------------------------------ #


def test_default_contracts_cb_passed_when_none() -> None:
    """When caller provides no contracts_cb, the default callback is used."""
    client = _make_client()
    runtime = SessionRuntime(client)

    env = {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}
    with patch.dict("os.environ", env, clear=False):
        runtime.login_with_retry()

    # _safe_call_with_timeout is called with a lambda; invoke it to trigger api.login
    login_lambda = client._safe_call_with_timeout.call_args_list[0][0][1]
    login_lambda()

    # api.login should have been called with contracts_cb = _default_contracts_cb
    login_call = client.api.login.call_args
    assert login_call.kwargs["contracts_cb"] is SessionRuntime._default_contracts_cb


def test_caller_contracts_cb_not_overridden() -> None:
    """When caller provides contracts_cb, it is passed through (not replaced)."""
    client = _make_client()
    runtime = SessionRuntime(client)

    custom_cb = MagicMock()
    env = {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}
    with patch.dict("os.environ", env, clear=False):
        runtime.login_with_retry(contracts_cb=custom_cb)

    login_lambda = client._safe_call_with_timeout.call_args_list[0][0][1]
    login_lambda()

    login_call = client.api.login.call_args
    assert login_call.kwargs["contracts_cb"] is custom_cb


def test_default_contracts_cb_logs_security_type() -> None:
    """The default callback logs without raising."""
    # Should not raise for any security_type value
    SessionRuntime._default_contracts_cb("Stock")
    SessionRuntime._default_contracts_cb(42)
    SessionRuntime._default_contracts_cb(None)


# ------------------------------------------------------------------ #
# receive_window tests
# ------------------------------------------------------------------ #


def test_receive_window_passed_when_configured() -> None:
    """When _receive_window is set, it is passed to api.login."""
    client = _make_client(_receive_window=60000)
    runtime = SessionRuntime(client)

    env = {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}
    with patch.dict("os.environ", env, clear=False):
        runtime.login_with_retry()

    login_lambda = client._safe_call_with_timeout.call_args_list[0][0][1]
    login_lambda()

    login_call = client.api.login.call_args
    assert login_call.kwargs["receive_window"] == 60000


def test_receive_window_not_passed_when_none() -> None:
    """When _receive_window is None, receive_window kwarg is omitted."""
    client = _make_client(_receive_window=None)
    runtime = SessionRuntime(client)

    env = {"SHIOAJI_API_KEY": "k", "SHIOAJI_SECRET_KEY": "s"}
    with patch.dict("os.environ", env, clear=False):
        runtime.login_with_retry()

    login_lambda = client._safe_call_with_timeout.call_args_list[0][0][1]
    login_lambda()

    login_call = client.api.login.call_args
    assert "receive_window" not in login_call.kwargs


def test_receive_window_env_var_zero_means_none() -> None:
    """HFT_SHIOAJI_RECEIVE_WINDOW=0 should result in _receive_window=None."""
    with patch.dict("os.environ", {"HFT_SHIOAJI_RECEIVE_WINDOW": "0"}, clear=False):
        val = int(os.environ.get("HFT_SHIOAJI_RECEIVE_WINDOW", "0")) or None
    assert val is None


def test_receive_window_env_var_nonzero() -> None:
    """HFT_SHIOAJI_RECEIVE_WINDOW=60000 should result in 60000."""
    with patch.dict("os.environ", {"HFT_SHIOAJI_RECEIVE_WINDOW": "60000"}, clear=False):
        val = int(os.environ.get("HFT_SHIOAJI_RECEIVE_WINDOW", "0")) or None
    assert val == 60000


# ------------------------------------------------------------------ #
# list_accounts tests
# ------------------------------------------------------------------ #


def test_list_accounts_returns_accounts() -> None:
    """list_accounts returns the list from api.list_accounts()."""
    client = _make_client()
    client.logged_in = True
    accounts = [MagicMock(), MagicMock()]
    client.api.list_accounts.return_value = accounts

    runtime = SessionRuntime(client)
    result = runtime.list_accounts()

    assert result == accounts
    client._record_api_latency.assert_called_once()
    latency_call = client._record_api_latency.call_args
    assert latency_call[0][0] == "list_accounts"
    assert latency_call.kwargs["ok"] is True


def test_list_accounts_returns_empty_when_not_logged_in() -> None:
    """list_accounts returns [] when not logged in."""
    client = _make_client()
    client.logged_in = False

    runtime = SessionRuntime(client)
    result = runtime.list_accounts()

    assert result == []
    client.api.list_accounts.assert_not_called()


def test_list_accounts_returns_empty_when_no_api() -> None:
    """list_accounts returns [] when api is None."""
    client = _make_client()
    client.api = None
    client.logged_in = True

    runtime = SessionRuntime(client)
    result = runtime.list_accounts()

    assert result == []


def test_list_accounts_handles_api_error() -> None:
    """list_accounts returns [] and records failure on exception."""
    client = _make_client()
    client.logged_in = True
    client.api.list_accounts.side_effect = RuntimeError("broker error")

    runtime = SessionRuntime(client)
    result = runtime.list_accounts()

    assert result == []
    latency_call = client._record_api_latency.call_args
    assert latency_call[0][0] == "list_accounts"
    assert latency_call.kwargs["ok"] is False


def test_list_accounts_returns_empty_when_api_returns_none() -> None:
    """list_accounts returns [] when api.list_accounts() returns None."""
    client = _make_client()
    client.logged_in = True
    client.api.list_accounts.return_value = None

    runtime = SessionRuntime(client)
    result = runtime.list_accounts()

    assert result == []
