"""Tests for ReconnectOrchestrator — Phase-6 Shioaji decoupling (WU-04).

Verifies that the extracted reconnect logic behaves identically to the
original ShioajiClient methods it replaces.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.feed_adapter.shioaji.reconnect_orchestrator import ReconnectOrchestrator


def _make_mock_login(client_ref):
    """Return a login callable that sets client.logged_in = True."""

    def _mock_login():
        client_ref.logged_in = True
        return True

    return _mock_login


@pytest.fixture()
def client():
    """Create a minimal ShioajiClient with mocked Shioaji SDK."""
    tmp = tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".yaml")
    yaml.dump({"symbols": [{"code": "2330", "exchange": "TSE"}]}, tmp)
    tmp.close()

    with patch.dict(os.environ, {"HFT_SESSION_REFRESH_S": "0"}):
        with patch("hft_platform.feed_adapter.shioaji_client.sj") as mock_sj:
            mock_api = MagicMock()
            mock_sj.Shioaji.return_value = mock_api
            mock_sj.constant.QuoteType.Tick = "tick"
            mock_sj.constant.QuoteType.BidAsk = "bidask"

            from hft_platform.feed_adapter.shioaji_client import ShioajiClient

            c = ShioajiClient(config_path=tmp.name)
            c.api = mock_api
            c.metrics = MagicMock()
            yield c
            try:
                c.close()
            except Exception:
                pass

    os.unlink(tmp.name)


class TestReconnectSequence:
    """Test the full reconnect sequence: login -> subscribe -> verify."""

    def test_reconnect_no_api_returns_false(self, client):
        orch = ReconnectOrchestrator(client)
        client.api = None
        assert orch.reconnect(reason="test") is False

    def test_reconnect_cooldown_gates_out(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = time.time() + 9999
        assert orch.reconnect(reason="test", force=False) is False

    def test_reconnect_force_bypasses_cooldown(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = time.time() + 9999
        client.login = _make_mock_login(client)
        client.tick_callback = None
        result = orch.reconnect(reason="test", force=True)
        assert result is True

    def test_reconnect_lock_contention_returns_false(self, client):
        orch = ReconnectOrchestrator(client)
        # Acquire the lock externally
        client._reconnect_lock.acquire()
        try:
            assert orch.reconnect(reason="test", force=True) is False
        finally:
            client._reconnect_lock.release()

    def test_reconnect_login_failure_increments_backoff(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = 0.0
        client.login = MagicMock(return_value=False)
        client.logged_in = False
        initial_backoff = client._reconnect_backoff_s
        result = orch.reconnect(reason="login_fail", force=True)
        assert result is False
        assert client._reconnect_backoff_s > initial_backoff

    def test_reconnect_success_resets_backoff(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = 0.0
        client._reconnect_backoff_s = 120.0
        client.login = _make_mock_login(client)
        client.tick_callback = None
        result = orch.reconnect(reason="recovery", force=True)
        assert result is True
        assert client._reconnect_backoff_s == 30.0  # reset to default

    def test_reconnect_with_subscribe(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = 0.0

        def _mock_ensure_callbacks(cb):
            client._callbacks_registered = True

        client.login = _make_mock_login(client)
        client.tick_callback = MagicMock()
        client._ensure_callbacks = _mock_ensure_callbacks
        client.subscribe_basket = MagicMock()
        result = orch.reconnect(reason="resub", force=True)
        assert result is True

    def test_reconnect_exception_records_metric(self, client):
        orch = ReconnectOrchestrator(client)
        client._last_reconnect_ts = 0.0
        # Make logout raise to trigger exception path
        client.api.logout.side_effect = RuntimeError("boom")
        client.login = MagicMock(side_effect=RuntimeError("double boom"))
        result = orch.reconnect(reason="crash", force=True)
        assert result is False
        client.metrics.feed_reconnect_total.labels.assert_called()


class TestQuoteEventFSM:
    """Test quote event FSM transitions via delegation stubs."""

    def test_on_quote_event_code_12_marks_pending(self, client):
        """Event code 12 (keepalive failure) should mark quotes pending."""
        client._pending_quote_resubscribe = False
        client._on_quote_event(0, 12, "keepalive", "disconnect")
        assert client._pending_quote_resubscribe is True

    def test_on_quote_event_code_13_clears_pending(self, client):
        """Event code 13 (reconnect success) should clear pending state."""
        client._pending_quote_resubscribe = True
        client._pending_quote_reason = "event_12"
        client.tick_callback = MagicMock()
        client._callbacks_registered = True
        client._event_callback_registered = True
        client._ensure_callbacks = MagicMock()
        client._resubscribe_all = MagicMock()
        client._on_quote_event(0, 13, "reconnected", "connect")
        assert client._pending_quote_resubscribe is False

    def test_on_quote_event_code_4_schedules_resubscribe(self, client):
        """Event code 4 should schedule a resubscribe."""
        client._resubscribe_scheduled = False
        client._on_quote_event(0, 4, "reset", "quote_reset")
        # schedule_resubscribe sets _resubscribe_scheduled
        assert client._resubscribe_scheduled is True


class TestTradingHoursGuard:
    """Test trading hours and market calendar guards."""

    def test_is_trading_hours_fallback(self, client):
        """When market calendar is unavailable, uses fallback logic."""
        orch = ReconnectOrchestrator(client)
        # The fallback uses weekday + time check; result depends on current time
        # Just verify it returns a bool without error
        result = orch.is_trading_hours()
        assert isinstance(result, bool)

    def test_is_trading_hours_with_calendar(self, client):
        orch = ReconnectOrchestrator(client)
        mock_cal = MagicMock()
        mock_cal.is_trading_hours.return_value = True
        mock_module = MagicMock(get_calendar=MagicMock(return_value=mock_cal))
        with patch.dict("sys.modules", {"hft_platform.core.market_calendar": mock_module}):
            result = orch.is_trading_hours()
            assert result is True


class TestQuoteSchemaMismatch:
    """Test quote schema mismatch handling."""

    def test_handle_mismatch_increments_count(self, client):
        orch = ReconnectOrchestrator(client)
        client._quote_schema_mismatch_count = 0
        orch.handle_quote_schema_mismatch("dict_payload_unrecognized", {"bad": True})
        assert client._quote_schema_mismatch_count == 1

    def test_handle_mismatch_records_metric(self, client):
        orch = ReconnectOrchestrator(client)
        client._quote_schema_mismatch_count = 0
        mock_child = MagicMock()
        client.metrics.quote_schema_mismatch_total.labels.return_value = mock_child
        orch.handle_quote_schema_mismatch("topic_string_arg_v0_shape", "TSE/2330")
        mock_child.inc.assert_called_once()

    def test_handle_mismatch_logs_on_first(self, client):
        """First mismatch (count % log_every == 1) should log."""
        orch = ReconnectOrchestrator(client)
        client._quote_schema_mismatch_count = 0
        client._quote_schema_mismatch_log_every = 100
        # count becomes 1, 1 % 100 == 1 -> should log
        orch.handle_quote_schema_mismatch("empty")
        assert client._quote_schema_mismatch_count == 1


class TestVerifyQuotesFlowing:
    """Test quote health verification."""

    def test_no_subscriptions_returns_true(self, client):
        orch = ReconnectOrchestrator(client)
        client.logged_in = True
        client.subscribed_count = 0
        assert orch.verify_quotes_flowing() is True

    def test_not_logged_in_returns_true(self, client):
        orch = ReconnectOrchestrator(client)
        client.logged_in = False
        client.subscribed_count = 5
        assert orch.verify_quotes_flowing() is True

    def test_quotes_arrive_within_timeout(self, client):
        orch = ReconnectOrchestrator(client)
        client.logged_in = True
        client.subscribed_count = 1
        client._last_quote_data_ts = 0.0

        # Simulate quotes arriving after a short delay
        def _bump_ts():
            time.sleep(0.1)
            client._last_quote_data_ts = time.time() + 1000

        t = threading.Thread(target=_bump_ts, daemon=True)
        t.start()
        assert orch.verify_quotes_flowing(timeout_s=2.0) is True
        t.join(timeout=1.0)

    def test_quotes_timeout(self, client):
        orch = ReconnectOrchestrator(client)
        client.logged_in = True
        client.subscribed_count = 1
        client._last_quote_data_ts = 0.0
        assert orch.verify_quotes_flowing(timeout_s=0.1) is False


class TestGetQuoteVersion:
    """Test quote version detection."""

    def test_no_shioaji_returns_none(self, client):
        """When shioaji cannot be imported, get_quote_version returns None."""
        orch = ReconnectOrchestrator(client)
        # Force the import inside get_quote_version to fail
        with patch.dict("sys.modules", {"shioaji": None}):
            result = orch.get_quote_version()
            assert result is None


class TestRequestReconnectViaPolicy:
    """Test policy-based reconnect routing."""

    def test_routes_through_policy(self, client):
        orch = ReconnectOrchestrator(client)
        mock_policy = MagicMock()
        mock_policy.request_reconnect.return_value = True
        client._session_policy = mock_policy
        assert orch.request_reconnect_via_policy("test", force=True) is True
        mock_policy.request_reconnect.assert_called_once_with(reason="test", force=True)

    def test_policy_exception_returns_false(self, client):
        orch = ReconnectOrchestrator(client)
        mock_policy = MagicMock()
        mock_policy.request_reconnect.side_effect = RuntimeError("boom")
        client._session_policy = mock_policy
        assert orch.request_reconnect_via_policy("test") is False

    def test_no_policy_falls_back_to_direct(self, client):
        orch = ReconnectOrchestrator(client)
        client._session_policy = None
        client._last_reconnect_ts = 0.0
        client.login = _make_mock_login(client)
        client.tick_callback = None
        result = orch.request_reconnect_via_policy("fallback", force=True)
        assert result is True
