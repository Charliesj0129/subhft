"""Extended tests for ReconnectOrchestrator.

Covers reconnect sequencing, cooldown/backoff, lock guards,
policy routing, quote verification, trading hours, and schema mismatch handling.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji.reconnect_orchestrator import ReconnectOrchestrator


def make_mock_client():
    c = MagicMock()
    c.api = MagicMock()
    c.logged_in = True
    c._last_reconnect_ts = 0.0
    c._reconnect_backoff_s = 30.0
    c._reconnect_backoff_max_s = 300.0
    c._reconnect_lock = MagicMock()
    c._reconnect_lock.acquire.return_value = True
    c._reconnect_timeout_s = 10.0
    c._reconnect_subscribe_timeout_s = 10.0
    c._last_reconnect_error = None
    c._last_login_error = None
    c._callbacks_registered = True
    c.tick_callback = MagicMock()
    c.subscribed_codes = set()
    c.subscribed_count = 0
    c.metrics = MagicMock()
    c._safe_call_with_timeout = MagicMock(return_value=(True, None, None, False))
    c._ensure_callbacks = MagicMock()
    c._clear_quote_pending = MagicMock()
    c._refresh_quote_routes = MagicMock()
    c.login = MagicMock(return_value=True)
    c.subscribe_basket = MagicMock()
    c._session_policy = None
    c._last_quote_data_ts = 0.0
    c._session_refresh_verify_timeout_s = 5.0
    c._quote_version = "v1"
    c._quote_schema_mismatch_count = 0
    c._quote_schema_mismatch_log_every = 100
    c._quote_schema_mismatch_metric_cache = {}
    return c


@pytest.fixture()
def client():
    return make_mock_client()


@pytest.fixture()
def orch(client):
    return ReconnectOrchestrator(client)


# ------------------------------------------------------------------ #
# reconnect() tests
# ------------------------------------------------------------------ #


def test_reconnect_returns_false_when_api_is_none(client, orch):
    """reconnect returns False immediately when client.api is None."""
    client.api = None
    assert orch.reconnect(reason="test") is False


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_reconnect_returns_false_during_cooldown(mock_tb, client, orch):
    """reconnect returns False when within cooldown and not forced."""
    client._last_reconnect_ts = 100.0
    mock_tb.now_s.return_value = 110.0
    assert orch.reconnect(reason="cooldown-test", force=False) is False
    client._reconnect_lock.acquire.assert_not_called()


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_reconnect_returns_false_when_lock_not_acquired(mock_tb, client, orch):
    """reconnect returns False when the reconnect lock cannot be acquired."""
    mock_tb.now_s.return_value = 99999.0
    client._reconnect_lock.acquire.return_value = False
    assert orch.reconnect(reason="lock-test") is False


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_reconnect_happy_path(mock_tb, client, orch):
    """reconnect succeeds when login + subscribe both succeed."""
    mock_tb.now_s.return_value = 99999.0
    client.login.return_value = True

    def login_side_effect():
        client.logged_in = True
        return True

    def ensure_callbacks_side_effect(*args, **kwargs):
        client._callbacks_registered = True

    client.login.side_effect = login_side_effect
    client._ensure_callbacks.side_effect = ensure_callbacks_side_effect

    result = orch.reconnect(reason="happy")
    assert result is True
    client._clear_quote_pending.assert_called_once()
    client._refresh_quote_routes.assert_called_once()
    client.login.assert_called_once()
    client._ensure_callbacks.assert_called_once()
    client.metrics.feed_reconnect_total.labels.assert_called_with(result="ok")
    client._reconnect_lock.release.assert_called_once()


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_reconnect_login_failure_doubles_backoff(mock_tb, client, orch):
    """On login failure, backoff doubles and reconnect returns False."""
    mock_tb.now_s.return_value = 99999.0
    client.login.return_value = False
    client.logged_in = False
    initial_backoff = client._reconnect_backoff_s

    result = orch.reconnect(reason="login-fail")
    assert result is False
    assert client._reconnect_backoff_s == initial_backoff * 2
    client.metrics.feed_reconnect_total.labels.assert_called_with(result="fail")


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_reconnect_subscribe_failure(mock_tb, client, orch):
    """reconnect returns False when subscribe_basket fails."""
    mock_tb.now_s.return_value = 99999.0

    def login_side_effect():
        client.logged_in = True
        return True

    def ensure_callbacks_side_effect(*args, **kwargs):
        client._callbacks_registered = True

    client.login.side_effect = login_side_effect
    client._ensure_callbacks.side_effect = ensure_callbacks_side_effect
    # First call is logout (ok), second is subscribe (fail)
    client._safe_call_with_timeout.side_effect = [
        (True, None, None, False),  # logout ok
        (False, None, "sub_err", False),  # subscribe fail
    ]

    result = orch.reconnect(reason="sub-fail")
    assert result is False
    assert client._last_reconnect_error == "sub_err"


# ------------------------------------------------------------------ #
# request_reconnect_via_policy() tests
# ------------------------------------------------------------------ #


def test_request_reconnect_via_policy_delegates(client, orch):
    """When session_policy is set, delegates to policy.request_reconnect."""
    policy = MagicMock()
    policy.request_reconnect.return_value = True
    client._session_policy = policy

    result = orch.request_reconnect_via_policy(reason="policy-test", force=True)
    assert result is True
    policy.request_reconnect.assert_called_once_with(reason="policy-test", force=True)


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_request_reconnect_via_policy_fallback(mock_tb, client, orch):
    """Without session_policy, falls back to direct reconnect."""
    mock_tb.now_s.return_value = 99999.0
    client._session_policy = None

    def login_side_effect():
        client.logged_in = True
        return True

    def ensure_callbacks_side_effect(*args, **kwargs):
        client._callbacks_registered = True

    client.login.side_effect = login_side_effect
    client._ensure_callbacks.side_effect = ensure_callbacks_side_effect

    result = orch.request_reconnect_via_policy(reason="fallback", force=True)
    assert result is True
    client.login.assert_called_once()


# ------------------------------------------------------------------ #
# verify_quotes_flowing() tests
# ------------------------------------------------------------------ #


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_verify_quotes_flowing_returns_true_when_not_logged_in(mock_tb, client, orch):
    """verify_quotes_flowing returns True early when not logged in."""
    client.logged_in = False
    assert orch.verify_quotes_flowing() is True


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.time.sleep")
@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_verify_quotes_flowing_new_data_arrives(mock_tb, mock_sleep, client, orch):
    """verify_quotes_flowing returns True when new quote data arrives."""
    client.logged_in = True
    client.subscribed_count = 5
    client._last_quote_data_ts = 100.0

    call_count = 0

    def advancing_now_s():
        nonlocal call_count
        call_count += 1
        return 1000.0 + call_count * 0.1

    mock_tb.now_s.side_effect = advancing_now_s

    def update_ts(*_args, **_kwargs):
        client._last_quote_data_ts = 999.0

    mock_sleep.side_effect = update_ts

    assert orch.verify_quotes_flowing(timeout_s=5.0) is True


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.time.sleep")
@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_verify_quotes_flowing_timeout(mock_tb, mock_sleep, client, orch):
    """verify_quotes_flowing returns False when timeout expires."""
    client.logged_in = True
    client.subscribed_count = 3
    client._last_quote_data_ts = 100.0

    calls = iter([1000.0, 1000.0, 1006.0])
    mock_tb.now_s.side_effect = lambda: next(calls, 9999.0)

    assert orch.verify_quotes_flowing(timeout_s=0.01) is False


# ------------------------------------------------------------------ #
# is_trading_hours() tests
# ------------------------------------------------------------------ #


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekend(mock_tb, orch):
    """Fallback path returns False on weekends (Saturday)."""
    import datetime as dt

    saturday = dt.datetime(2026, 3, 21, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = saturday.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is False


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekday_day_session(mock_tb, orch):
    """Fallback path returns True on weekday during day session (08:45-13:45)."""
    import datetime as dt

    friday_10am = dt.datetime(2026, 3, 20, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = friday_10am.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is True


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekday_night_session(mock_tb, orch):
    """Fallback path returns True during night session (15:00-05:00)."""
    import datetime as dt

    # 22:00 Taiwan time on a Wednesday — firmly in night session
    wed_22 = dt.datetime(2026, 3, 18, 22, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = wed_22.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is True


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekday_night_session_early_morning(mock_tb, orch):
    """Fallback returns True at 03:00 Taiwan time (still night session)."""
    import datetime as dt

    thu_3am = dt.datetime(2026, 3, 19, 3, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = thu_3am.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is True


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekday_session_gap(mock_tb, orch):
    """Fallback returns False during 05:01-08:44 gap between sessions."""
    import datetime as dt

    # 06:30 Taiwan time — between night session close and day session open
    thu_630 = dt.datetime(2026, 3, 19, 6, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = thu_630.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is False


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_fallback_weekday_between_sessions(mock_tb, orch):
    """Fallback returns False during 13:46-14:59 gap between day and night."""
    import datetime as dt

    wed_14 = dt.datetime(2026, 3, 18, 14, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    mock_tb.now_s.return_value = wed_14.timestamp()

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        side_effect=ImportError("no calendar"),
    ):
        assert orch.is_trading_hours() is False


@patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.timebase")
def test_is_trading_hours_uses_futures_product_type(mock_tb, orch):
    """Calendar path must call is_trading_hours with product_type='future'."""
    import datetime as dt

    mock_calendar = MagicMock()
    mock_calendar._tz = dt.timezone(dt.timedelta(hours=8))
    mock_calendar.is_trading_hours.return_value = True
    mock_tb.now_s.return_value = 1742475600.0  # arbitrary

    with patch(
        "hft_platform.core.market_calendar.get_calendar",
        return_value=mock_calendar,
    ):
        result = orch.is_trading_hours()

    assert result is True
    # Verify product_type="future" was passed
    call_kwargs = mock_calendar.is_trading_hours.call_args
    assert call_kwargs[1].get("product_type") == "future" or (len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "future")


# ------------------------------------------------------------------ #
# handle_quote_schema_mismatch() tests
# ------------------------------------------------------------------ #


def test_handle_quote_schema_mismatch_increments_counter(client, orch):
    """handle_quote_schema_mismatch increments the mismatch counter."""
    assert client._quote_schema_mismatch_count == 0
    orch.handle_quote_schema_mismatch("missing_field", {"some": "data"})
    assert client._quote_schema_mismatch_count == 1
    orch.handle_quote_schema_mismatch("wrong_type")
    assert client._quote_schema_mismatch_count == 2


def test_handle_quote_schema_mismatch_logs_periodically(client, orch):
    """handle_quote_schema_mismatch logs on count % log_every == 1."""
    client._quote_schema_mismatch_log_every = 5

    with patch("hft_platform.feed_adapter.shioaji.reconnect_orchestrator.logger") as mock_logger:
        for i in range(12):
            orch.handle_quote_schema_mismatch("test_reason", f"arg_{i}")

        # Logs at count 1, 6, 11 (i.e., when count % 5 == 1)
        assert mock_logger.error.call_count == 3
        assert client._quote_schema_mismatch_count == 12
