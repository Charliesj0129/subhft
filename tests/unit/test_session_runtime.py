from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.feed_adapter.shioaji.session_runtime import (
    SessionRuntime,
    SessionStateSnapshot,
)

# ------------------------------------------------------------------ #
# Phase-1 delegation tests (existing)
# ------------------------------------------------------------------ #


def test_session_runtime_request_reconnect_delegates():
    client = MagicMock()
    client.reconnect.return_value = True

    runtime = SessionRuntime(client)
    ok = runtime.request_reconnect(reason="unit-test", force=True)

    assert ok is True
    client.reconnect.assert_called_once_with(reason="unit-test", force=True)


def test_session_runtime_is_logged_in_reads_client_state():
    client = MagicMock()
    client.logged_in = True
    runtime = SessionRuntime(client)

    assert runtime.is_logged_in() is True


# ------------------------------------------------------------------ #
# Phase-2: login_with_retry tests
# ------------------------------------------------------------------ #


def test_login_with_retry_no_api_key_returns_false():
    """Without SHIOAJI_API_KEY in env and no args, returns False."""
    client = MagicMock()
    client.api = MagicMock()  # SDK present but no creds

    runtime = SessionRuntime(client)
    with patch.dict("os.environ", {}, clear=False):
        # Ensure keys are not set
        import os

        for k in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"):
            os.environ.pop(k, None)
        result = runtime.login_with_retry()

    assert result is False


def test_login_with_retry_no_sdk_no_key_returns_false():
    """Without SDK and no creds, logs warning and returns False."""
    client = MagicMock()
    client.api = None

    runtime = SessionRuntime(client)
    import os

    for k in ("SHIOAJI_API_KEY", "SHIOAJI_SECRET_KEY"):
        os.environ.pop(k, None)

    result = runtime.login_with_retry()
    assert result is False


def test_login_with_retry_sdk_unavailable_with_key_returns_false():
    """With credentials but api=None, returns False."""
    client = MagicMock()
    client.api = None

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")
    assert result is False
    # Should NOT call _ensure_session_lock since api is None
    client._ensure_session_lock.assert_not_called()


def test_login_with_retry_success_sets_logged_in():
    """Successful login sets client.logged_in = True."""
    client = MagicMock()
    client.api = MagicMock()
    client._login_retry_max = 0
    client._login_timeout_s = 5.0
    client.fetch_contract = True
    client.subscribe_trade = True
    client.activate_ca = False
    client._last_login_error = None
    client._safe_call_with_timeout.return_value = (True, None, None, False)

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")

    assert result is True
    assert client.logged_in is True


def test_login_with_retry_failure_records_metric():
    """Failed login (all retries exhausted) records shioaji_login_fail_total metric."""
    client = MagicMock()
    client.api = MagicMock()
    client._login_retry_max = 0
    client._login_timeout_s = 5.0
    client.fetch_contract = False
    client.subscribe_trade = True
    client.activate_ca = False
    client._safe_call_with_timeout.return_value = (False, None, "timeout", True)
    client._sanitize_metric_label.return_value = "timeout"
    client.metrics = MagicMock()
    client.metrics.shioaji_login_fail_total = MagicMock()
    client.metrics.shioaji_login_fail_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)
    result = runtime.login_with_retry(api_key="key", secret_key="secret")

    assert result is False
    client.metrics.shioaji_login_fail_total.labels.assert_called_once()
    client._release_session_lock.assert_called_once()


def test_login_delegates_to_login_with_retry():
    """SessionRuntime.login() is an alias for login_with_retry()."""
    client = MagicMock()
    runtime = SessionRuntime(client)

    with patch.object(SessionRuntime, "login_with_retry", return_value=True) as mock_lwr:
        result = runtime.login(api_key="k", secret_key="s")

    assert result is True
    # patch.object replaces the class attribute; instance call does NOT bind self.
    mock_lwr.assert_called_once_with(api_key="k", secret_key="s")


# ------------------------------------------------------------------ #
# Phase-2: do_session_refresh tests
# ------------------------------------------------------------------ #


def test_do_session_refresh_no_api_returns_false():
    client = MagicMock()
    client.api = None
    runtime = SessionRuntime(client)

    assert runtime.do_session_refresh() is False


def test_do_session_refresh_login_fails_returns_false():
    """If login fails after logout, do_session_refresh returns False."""
    client = MagicMock()
    client.api = MagicMock()
    client.logged_in = False  # After logout
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)

    with patch.object(SessionRuntime, "login_with_retry", return_value=False) as mock_lwr:
        result = runtime.do_session_refresh()

    assert result is False
    mock_lwr.assert_called_once()
    client.metrics.session_refresh_total.labels.assert_called_with(result="error")


def test_do_session_refresh_success_no_subscriptions():
    """Successful refresh with no tick_callback records ok metric."""
    client = MagicMock()
    client.api = MagicMock()
    client.tick_callback = None
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()

    runtime = SessionRuntime(client)

    def _set_logged_in(*args, **kwargs):
        client.logged_in = True
        return True

    with patch.object(SessionRuntime, "login_with_retry", side_effect=_set_logged_in):
        result = runtime.do_session_refresh()

    assert result is True
    client.metrics.session_refresh_total.labels.assert_called_with(result="ok")


def test_do_session_refresh_success_with_subscriptions():
    """Successful refresh with tick_callback triggers resubscription and watchdog."""
    client = MagicMock()
    client.api = MagicMock()
    client.tick_callback = MagicMock()
    client.metrics = MagicMock()
    client.metrics.session_refresh_total = MagicMock()
    client.metrics.session_refresh_total.labels.return_value = MagicMock()
    client._verify_quotes_flowing.return_value = True

    runtime = SessionRuntime(client)

    def _set_logged_in(*args, **kwargs):
        client.logged_in = True
        return True

    with patch.object(SessionRuntime, "login_with_retry", side_effect=_set_logged_in):
        result = runtime.do_session_refresh()

    assert result is True
    client._ensure_callbacks.assert_called_once_with(client.tick_callback)
    client._resubscribe_all.assert_called_once()
    client._start_quote_watchdog.assert_called_once()
    client.metrics.session_refresh_total.labels.assert_called_with(result="ok")


# ------------------------------------------------------------------ #
# Phase-2: start_session_refresh_thread tests
# ------------------------------------------------------------------ #


def test_start_session_refresh_thread_noop_if_already_running():
    client = MagicMock()
    client._session_refresh_running = True

    runtime = SessionRuntime(client)
    runtime.start_session_refresh_thread()

    # Thread should NOT be started
    client._set_thread_alive_metric.assert_not_called()


def test_start_session_refresh_thread_noop_if_interval_zero():
    client = MagicMock()
    client._session_refresh_running = False
    client._session_refresh_interval_s = 0

    runtime = SessionRuntime(client)
    runtime.start_session_refresh_thread()

    client._set_thread_alive_metric.assert_not_called()


def test_start_session_refresh_thread_starts_thread():
    client = MagicMock()
    client._session_refresh_running = False
    client._session_refresh_interval_s = 86400
    client._session_refresh_check_interval_s = 3600
    client._session_refresh_holiday_aware = False
    client._set_thread_alive_metric = MagicMock()

    runtime = SessionRuntime(client)

    with patch("hft_platform.feed_adapter.shioaji.session_runtime.threading.Thread") as mock_thread_cls:
        mock_thread = MagicMock()
        mock_thread_cls.return_value = mock_thread

        with patch(
            "hft_platform.feed_adapter.shioaji.session_runtime.get_calendar",
            create=True,
        ):
            runtime.start_session_refresh_thread()

    assert client._session_refresh_running is True
    mock_thread_cls.assert_called_once()
    mock_thread.start.assert_called_once()
    assert client._session_refresh_thread is mock_thread


# ------------------------------------------------------------------ #
# Snapshot helper
# ------------------------------------------------------------------ #


def test_snapshot_returns_correct_fields():
    client = MagicMock()
    client.logged_in = True
    client._reconnect_backoff_s = 30.0
    client._last_login_error = "timeout"
    client._last_reconnect_error = None

    runtime = SessionRuntime(client)
    snap = runtime.snapshot()

    assert isinstance(snap, SessionStateSnapshot)
    assert snap.logged_in is True
    assert snap.reconnect_backoff_s == 30.0
    assert snap.last_login_error == "timeout"
    assert snap.last_reconnect_error is None
