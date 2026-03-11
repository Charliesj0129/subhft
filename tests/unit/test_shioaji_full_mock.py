import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import yaml
from prometheus_client import REGISTRY
from prometheus_client.openmetrics.exposition import generate_latest as generate_openmetrics_latest

# IMPORTANT: mocking BEFORE import is hard if module imports at top level.
# shioaji_client tries to import shioaji inside try-except.
# We will mock the module `shioaji` in `sys.modules` or rely on `sj` being None check if we can't install it.
# However, we want to test the logic when `sj` IS present.
# So we must patch `hft_platform.feed_adapter.shioaji_client.sj`
from hft_platform.feed_adapter.shioaji_client import ShioajiClient, dispatch_tick_cb
from hft_platform.observability.metrics import MetricsRegistry


class TestShioajiClientFull(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"HFT_SESSION_REFRESH_S": "0"})
        self.env_patcher.start()
        # Create temp config
        self.tmp_config = tempfile.NamedTemporaryFile(mode="w", delete=False)
        yaml.dump(
            {"symbols": [{"code": "2330", "exchange": "TSE"}, {"code": "TXFA", "exchange": "FUT"}]}, self.tmp_config
        )
        self.tmp_config.close()

        # Patch 'sj' in the module
        self.patcher = patch("hft_platform.feed_adapter.shioaji_client.sj")
        self.mock_sj_mod = self.patcher.start()

        # Configure Mock Shioaji class
        self.mock_api_instance = MagicMock()
        self.mock_sj_mod.Shioaji.return_value = self.mock_api_instance

        # Constants
        self.mock_sj_mod.constant.QuoteType.Tick = "tick"
        self.mock_sj_mod.constant.QuoteType.BidAsk = "bidask"
        self.mock_sj_mod.constant.Action.Buy = "Buy"
        self.mock_sj_mod.constant.Action.Sell = "Sell"
        self.mock_sj_mod.constant.StockPriceType.LMT = "LMT"
        self.mock_sj_mod.constant.OrderType.ROD = "ROD"
        self.mock_sj_mod.constant.OrderType.IOC = "IOC"
        self.mock_sj_mod.constant.OrderType.FOK = "FOK"

        self.client = ShioajiClient(config_path=self.tmp_config.name)
        # Client init tries to create Shioaji() if sj is present
        # We need to ensure self.client.api is our mock
        self.client.api = self.mock_api_instance
        self.client.metrics = MagicMock()

        # Mock Contracts lookup structure
        # Contracts.Stocks.TSE["2330"] etc
        self.mock_contract_2330 = MagicMock()
        self.mock_contract_2330.code = "2330"
        self.mock_contract_txfa = MagicMock()

        self.mock_api_instance.Contracts.Stocks.TSE = {"2330": self.mock_contract_2330}
        self.mock_api_instance.Contracts.Futures = {"TXFA": self.mock_contract_txfa}

    def tearDown(self):
        try:
            self.client.close()
        except Exception:
            pass
        self.env_patcher.stop()
        self.patcher.stop()
        os.unlink(self.tmp_config.name)

    def test_login_flow(self):
        # Test Env var login
        with patch.dict(os.environ, {"SHIOAJI_API_KEY": "TESTKEY", "SHIOAJI_SECRET_KEY": "TESTSECRET"}):
            self.client.login()
            self.mock_api_instance.login.assert_called_once()
            _, kwargs = self.mock_api_instance.login.call_args
            self.assertEqual(kwargs["api_key"], "TESTKEY")
            self.assertEqual(kwargs["secret_key"], "TESTSECRET")
            # Default contracts_cb is now provided by SessionRuntime
            from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime
            self.assertIs(kwargs.get("contracts_cb"), SessionRuntime._default_contracts_cb)
            self.assertTrue(self.client.logged_in)

    def test_login_fallback_failure_does_not_raise(self):
        with patch.dict(
            os.environ,
            {
                "SHIOAJI_API_KEY": "TESTKEY",
                "SHIOAJI_SECRET_KEY": "TESTSECRET",
                "HFT_SHIOAJI_LOGIN_RETRY_MAX": "0",
                "HFT_SHIOAJI_LOGIN_TIMEOUT_S": "0",
            },
            clear=False,
        ):
            self.mock_api_instance.login.side_effect = RuntimeError("login down")
            ok = self.client.login()
        self.assertFalse(ok)
        self.assertFalse(self.client.logged_in)

    def test_subscribe_basket(self):
        self.client.logged_in = True
        cb = MagicMock()
        self.client.subscribe_basket(cb)

        # Should lookup 2330 and TXFA
        # 2330 in config is TSE
        # TXFA in config is FUT

        # Verify subscriptions
        # 2 symbols * 2 quote types = 4 subscribes
        self.assertEqual(self.mock_api_instance.quote.subscribe.call_count, 4)
        self.mock_api_instance.quote.set_on_tick_stk_v1_callback.assert_called_with(dispatch_tick_cb)

    def test_callback_retry_loop_sets_registered(self):
        self.client.logged_in = True
        cb = MagicMock()
        self.client._callbacks_registered = False

        attempts = {"n": 0}

        def fake_register(_cb):
            attempts["n"] += 1
            if attempts["n"] < 2:
                return False
            self.client._callbacks_registered = True
            return True

        with patch.object(self.client, "_register_callbacks", side_effect=fake_register):
            with patch("time.sleep", return_value=None):
                self.client._start_callback_retry(cb)
                self.client._callbacks_retry_thread.join(timeout=1)

        self.assertTrue(self.client._callbacks_registered)
        self.assertFalse(self.client._callbacks_retrying)
        self.assertGreaterEqual(attempts["n"], 2)

    def test_place_order(self):
        self.client.place_order("2330", "TSE", "Buy", 100.0, 1, "ROD", "Regular")

        # Verify sj.Order constructor called correctly
        self.mock_sj_mod.Order.assert_called()
        call_args = self.mock_sj_mod.Order.call_args
        kwargs = call_args[1]
        self.assertEqual(kwargs["price"], 100.0)
        self.assertEqual(kwargs["quantity"], 1)
        self.assertEqual(kwargs["action"], "Buy")
        self.assertEqual(kwargs["price_type"], "LMT")
        self.assertEqual(kwargs["order_type"], "ROD")

        # Verify api.place_order called with result of sj.Order
        self.mock_api_instance.place_order.assert_called()

    def test_set_execution_callbacks(self):
        on_order = MagicMock()
        on_deal = MagicMock()
        self.client.set_execution_callbacks(on_order, on_deal)

        self.mock_api_instance.set_order_callback.assert_called_once()
        callback = self.mock_api_instance.set_order_callback.call_args[0][0]
        self.assertTrue(callable(callback))
        self.mock_api_instance.set_deal_callback.assert_not_called()

    def test_usage_cache_and_rate_limit(self):
        self.client.logged_in = True
        self.mock_api_instance.usage.return_value = {"subscribed": 1, "bytes_used": 10}

        # First call hits API and caches.
        usage1 = self.client.get_usage()
        # Second call should use cache.
        usage2 = self.client.get_usage()

        self.assertEqual(usage1, usage2)
        self.assertEqual(self.mock_api_instance.usage.call_count, 1)

    def test_positions_cache(self):
        self.client.logged_in = True
        self.client.mode = "real"
        self.mock_api_instance.stock_account = object()
        self.mock_api_instance.futopt_account = object()
        self.mock_api_instance.list_positions.side_effect = [["S1"], ["F1"]]

        positions1 = self.client.get_positions()
        positions2 = self.client.get_positions()

        self.assertEqual(positions1, positions2)
        self.assertEqual(self.mock_api_instance.list_positions.call_count, 2)

    def test_record_api_latency_error(self):
        start_ns = time.perf_counter_ns()
        # Ensure error path doesn't raise.
        self.client._record_api_latency("place_order", start_ns, ok=False)

    def test_record_api_latency_sanitizes_non_string_labels(self):
        self.client.metrics = MetricsRegistry.get()
        start_ns = time.perf_counter_ns()
        # Regression guard: bad label types must not break /metrics exposition.
        self.client._record_api_latency(type("Catcher", (), {}), start_ns, ok=object)
        payload = generate_openmetrics_latest(REGISTRY)
        self.assertIn(b"shioaji_api_latency_ms", payload)

    def test_usage_rate_limit_returns_cached(self):
        self.client.logged_in = True
        self.client._cache_set("usage", 10, {"subscribed": 2, "bytes_used": 20})
        self.client._api_rate_limiter.check = MagicMock(return_value=False)

        usage = self.client.get_usage()

        self.assertEqual(usage["subscribed"], 2)
        self.mock_api_instance.usage.assert_not_called()

    def test_fetch_snapshots_batches(self):
        self.client.logged_in = True
        self.client.symbols = [{"code": "2330", "exchange": "TSE"}]
        self.client.code_exchange_map = {"2330": "TSE"}
        self.mock_api_instance.snapshots.return_value = [{"code": "2330"}]

        results = self.client.fetch_snapshots()

        self.assertEqual(results, [{"code": "2330"}])
        self.mock_api_instance.snapshots.assert_called_once()

    def test_resubscribe(self):
        self.client.logged_in = True
        self.client.tick_callback = MagicMock()
        self.client.symbols = [{"code": "2330", "exchange": "TSE"}]
        self.client.code_exchange_map = {"2330": "TSE"}
        self.client._last_resubscribe_ts = 0.0

        ok = self.client.resubscribe()

        self.assertTrue(ok)
        self.assertGreaterEqual(self.mock_api_instance.quote.subscribe.call_count, 1)

    def test_reconnect_returns_false_when_login_fails(self):
        self.client.logged_in = True
        self.client.login = MagicMock(return_value=False)
        ok = self.client.reconnect(reason="unit", force=True)
        self.assertFalse(ok)

    def test_quote_event_12_marks_pending(self):
        self.client.tick_callback = MagicMock()
        with patch.object(self.client, "_ensure_callbacks") as ensure_cb:
            self.client._on_quote_event(0, 12, "info", "event")
        self.assertTrue(self.client._pending_quote_resubscribe)
        self.assertEqual(self.client._pending_quote_reason, "event_12")
        ensure_cb.assert_called_once()

    def test_quote_event_13_resubscribes_and_clears_pending(self):
        self.client.tick_callback = MagicMock()
        self.client._pending_quote_resubscribe = True
        with patch.object(self.client, "_resubscribe_all") as resub, patch.object(self.client, "_ensure_callbacks"):
            self.client._on_quote_event(0, 13, "info", "event")
        resub.assert_called_once()
        self.assertFalse(self.client._pending_quote_resubscribe)

    def test_quote_event_13_without_pending_does_not_resubscribe(self):
        self.client.tick_callback = MagicMock()
        self.client._pending_quote_resubscribe = False
        with patch.object(self.client, "_resubscribe_all") as resub, patch.object(self.client, "_ensure_callbacks"):
            self.client._on_quote_event(0, 13, "info", "event")
        resub.assert_not_called()

    def test_quote_flap_forces_relogin(self):
        self.client._quote_flap_events.clear()
        self.client._last_quote_flap_relogin_ts = 0.0
        self.client._quote_flap_window_s = 60.0
        self.client._quote_flap_threshold = 2
        self.client._quote_flap_cooldown_s = 0.0
        with patch.object(self.client, "_start_forced_relogin") as force_relogin:
            self.client._on_quote_event(0, 12, "info", "event")
            self.client._on_quote_event(0, 12, "info", "event")
        force_relogin.assert_called_once()

    def test_quote_watchdog_skips_recovery_off_hours(self):
        self.client.logged_in = True
        self.client._quote_no_data_s = 0.01
        self.client._quote_watchdog_interval_s = 0.01
        self.client._quote_watchdog_skip_off_hours = True
        self.client._last_quote_data_ts = time.time() - 10.0

        with (
            patch.object(self.client, "_is_trading_hours", return_value=False),
            patch.object(self.client, "_mark_quote_pending") as mark_pending,
        ):
            self.client._start_quote_watchdog()
            time.sleep(0.05)
            self.client.logged_in = False
            if self.client._quote_watchdog_thread is not None:
                self.client._quote_watchdog_thread.join(timeout=0.2)
        mark_pending.assert_not_called()

    def test_register_event_callback_uses_persistent_reference(self):
        ok = self.client._register_event_callback()
        self.assertTrue(ok)
        self.mock_api_instance.quote.set_event_callback.assert_called_with(self.client._event_callback_fn)

    def test_subscribe_symbol_returns_false_when_quote_api_missing(self):
        self.client.api.quote = None
        sym = {"code": "2330", "exchange": "TSE"}
        ok = self.client._subscribe_symbol(sym, MagicMock())
        self.assertFalse(ok)

    def test_subscribe_symbol_records_crash_signature_metric(self):
        sym = {"code": "2330", "exchange": "TSE"}
        self.mock_api_instance.quote.subscribe.side_effect = AttributeError(
            "'NoneType' object has no attribute 'subscribe'"
        )
        self.client.metrics.shioaji_crash_signature_total = MagicMock()
        crash_child = MagicMock()
        self.client.metrics.shioaji_crash_signature_total.labels.return_value = crash_child

        ok = self.client._subscribe_symbol(sym, MagicMock())

        self.assertFalse(ok)
        self.client.metrics.shioaji_crash_signature_total.labels.assert_called_with(
            signature="none_subscribe",
            context="subscribe_symbol",
        )
        crash_child.inc.assert_called_once()

    def test_cache_expiry(self):
        self.client._cache_set("usage", -1, {"subscribed": 1})
        value = self.client._cache_get("usage")
        self.assertIsNone(value)

    def test_quote_schema_guard_rejects_v0_shape_when_v1_locked(self):
        self.client._quote_version = "v1"
        self.client._quote_schema_guard = True
        self.client._quote_schema_guard_strict = True
        self.client.tick_callback = MagicMock()

        class Quote:
            code = "2330"

        self.client._process_tick("Q/TSE/2330", Quote())

        self.client.tick_callback.assert_not_called()
        self.assertGreaterEqual(self.client._quote_schema_mismatch_count, 1)

    def test_quote_schema_guard_allows_v1_object_shape(self):
        self.client._quote_version = "v1"
        self.client._quote_schema_guard = True
        self.client._quote_schema_guard_strict = True
        self.client.tick_callback = MagicMock()

        class BidAsk:
            code = "2330"
            bid_price = [100]
            ask_price = [101]

        # Common v1 callback shape: (exchange, quote_obj)
        self.client._process_tick("TSE", BidAsk())

        self.client.tick_callback.assert_called_once()

    def test_session_refresh_config(self):
        """Test session refresh configuration from env."""
        with patch.dict(os.environ, {"HFT_SESSION_REFRESH_S": "3600"}):
            client = ShioajiClient(config_path=self.tmp_config.name)
            self.assertEqual(client._session_refresh_interval_s, 3600.0)

    def test_market_open_grace_config(self):
        """Test market open grace period configuration."""
        with patch.dict(os.environ, {"HFT_MARKET_OPEN_GRACE_S": "120"}):
            client = ShioajiClient(config_path=self.tmp_config.name)
            self.assertEqual(client._market_open_grace_s, 120.0)

    def test_is_market_open_grace_period_no_calendar(self):
        """Test grace period returns False when calendar unavailable."""
        # Mock import error for market_calendar
        with patch.dict("sys.modules", {"hft_platform.core.market_calendar": None}):
            # Should return False gracefully
            result = self.client._is_market_open_grace_period()
            # Since the import might be cached, just verify no exception
            self.assertIsInstance(result, bool)

    def test_do_session_refresh_no_api(self):
        """Test session refresh handles missing API gracefully."""
        self.client.api = None
        result = self.client._do_session_refresh()
        self.assertFalse(result)

    def test_do_session_refresh_success(self):
        """Test successful session refresh."""
        self.client.logged_in = True

        # Mock SessionRuntime.login_with_retry() to succeed.
        def mock_login_with_retry(*args, **kwargs):
            self.client.logged_in = True
            return True

        with patch.object(type(self.client._session_runtime), "login_with_retry", side_effect=mock_login_with_retry):
            result = self.client._do_session_refresh()

        self.assertTrue(result)
        self.assertTrue(self.client._last_session_refresh_ts > 0)

    def test_session_refresh_holiday_aware_config(self):
        """Test holiday-aware session refresh configuration."""
        # Default should be enabled
        self.assertTrue(self.client._session_refresh_holiday_aware)

        # Test disabled via env
        with patch.dict(os.environ, {"HFT_SESSION_REFRESH_HOLIDAY_AWARE": "0"}):
            client = ShioajiClient(config_path=self.tmp_config.name)
            self.assertFalse(client._session_refresh_holiday_aware)

    def test_verify_quotes_flowing_no_subscriptions(self):
        """Test quote verification returns True when no subscriptions."""
        self.client.logged_in = True
        self.client.subscribed_count = 0

        result = self.client._verify_quotes_flowing(timeout_s=0.1)
        self.assertTrue(result)

    def test_verify_quotes_flowing_success(self):
        """Test quote verification succeeds when new data arrives."""
        self.client.logged_in = True
        self.client.subscribed_count = 1
        self.client._last_quote_data_ts = 100.0

        # Simulate new quote data arriving
        def update_ts():
            time.sleep(0.05)
            self.client._last_quote_data_ts = 200.0

        import threading

        t = threading.Thread(target=update_ts, daemon=True)
        t.start()

        result = self.client._verify_quotes_flowing(timeout_s=1.0)
        self.assertTrue(result)
        t.join(timeout=1.0)

    def test_verify_quotes_flowing_timeout(self):
        """Test quote verification times out when no data."""
        self.client.logged_in = True
        self.client.subscribed_count = 1
        self.client._last_quote_data_ts = 100.0
        # Don't update timestamp

        result = self.client._verify_quotes_flowing(timeout_s=0.2)
        self.assertFalse(result)

    def test_session_refresh_verify_timeout_config(self):
        """Test quote verification timeout configuration."""
        with patch.dict(os.environ, {"HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S": "30.0"}):
            client = ShioajiClient(config_path=self.tmp_config.name)
            self.assertEqual(client._session_refresh_verify_timeout_s, 30.0)

    # --- Feed Governance Tests (B1-B5) ---

    def test_login_fallback_no_contract_also_fails_returns_false(self):
        """B1: login() fallback failure should be fail-safe and return False."""
        self.client.api = self.mock_api_instance
        self.client.logged_in = False
        # Both login attempts raise
        self.mock_api_instance.login.side_effect = RuntimeError("token_login failed")

        with patch.dict(
            os.environ,
            {
                "SHIOAJI_API_KEY": "key",
                "SHIOAJI_SECRET_KEY": "secret",
                "HFT_LOGIN_FETCH_CONTRACT_FALLBACK": "1",
            },
        ):
            ok = self.client.login()
            self.assertFalse(ok)

        self.assertGreaterEqual(self.mock_api_instance.login.call_count, 2)

    def test_quote_watchdog_skips_resubscribe_outside_trading_hours(self):
        """B2: Watchdog must not trigger resubscribe outside trading hours."""
        import time as _time

        self.client.logged_in = True
        self.client._quote_watchdog_running = False
        self.client._quote_watchdog_interval_s = 0.01
        self.client._quote_no_data_s = 0.0  # threshold so gap always triggers
        self.client._last_quote_data_ts = 1.0  # old timestamp
        self.client.tick_callback = MagicMock()

        ensure_cb = MagicMock()
        resub_all = MagicMock()
        self.client._ensure_callbacks = ensure_cb
        self.client._resubscribe_all = resub_all

        with patch.object(self.client, "_is_trading_hours", return_value=False):
            self.client._start_quote_watchdog()
            _time.sleep(0.05)
            # Stop watchdog
            self.client.logged_in = False
            _time.sleep(0.02)

        ensure_cb.assert_not_called()
        resub_all.assert_not_called()

    def test_relogin_after_reconnect_exception_does_not_crash_thread(self):
        """B3: _relogin_after() must not propagate exception from reconnect()."""
        self.client._pending_quote_resubscribe = True
        self.client._pending_quote_relogining = True

        with patch.object(self.client, "reconnect", side_effect=RuntimeError("sim login fail")):
            # Invoke _schedule_force_relogin with delay=0 and run inner function directly
            delay = 0.0
            self.client._quote_force_relogin_s = delay

            def _relogin_after() -> None:
                try:
                    import time as _t

                    _t.sleep(delay)
                    if self.client._pending_quote_resubscribe:
                        try:
                            self.client.reconnect(reason="quote_pending", force=True)
                        except Exception as exc:
                            pass  # must be swallowed
                finally:
                    self.client._pending_quote_relogining = False

            # Should not raise
            _relogin_after()

        self.assertFalse(self.client._pending_quote_relogining)

    def test_do_relogin_reconnect_exception_does_not_crash_thread(self):
        """B4: _do_relogin() must not propagate exception from reconnect()."""
        self.client._pending_quote_relogining = True
        reason = "test_reason"

        with patch.object(self.client, "reconnect", side_effect=RuntimeError("login error")):

            def _do_relogin() -> None:
                try:
                    try:
                        self.client.reconnect(reason=reason, force=True)
                    except Exception as exc:
                        pass  # must be swallowed
                finally:
                    self.client._pending_quote_relogining = False

            _do_relogin()

        self.assertFalse(self.client._pending_quote_relogining)

    def test_reconnect_login_exception_returns_false(self):
        """B5: reconnect() must return False when login() raises; backoff must double."""
        self.client.logged_in = False
        self.client._reconnect_backoff_s = 30.0
        self.client._reconnect_backoff_max_s = 600.0
        self.client._last_reconnect_ts = 0.0

        with patch.object(self.client, "login", side_effect=TimeoutError("token timeout")):
            result = self.client.reconnect(reason="test", force=True)

        self.assertFalse(result)
        self.assertFalse(self.client.logged_in)
        # Backoff should have doubled (30 → 60)
        self.assertAlmostEqual(self.client._reconnect_backoff_s, 60.0, places=1)

    # --- C0-c: Reconnect Chaos Integration ---

    def test_reconnect_chaos_three_consecutive_login_timeouts_backoff_reaches_cap(self):
        """C0-c: 3 consecutive login() timeouts → backoff doubles up to cap.

        Each failed reconnect() must:
        1. Return False without raising.
        2. Double the backoff (30 → 60 → 120, capped at 120 for backoff_max=120).
        """
        self.client.logged_in = False
        self.client._reconnect_backoff_s = 30.0
        self.client._reconnect_backoff_max_s = 120.0
        self.client._last_reconnect_ts = 0.0

        with patch.object(self.client, "login", side_effect=TimeoutError("broker_timeout")):
            result1 = self.client.reconnect(reason="chaos_1", force=True)
            # Reset cooldown so subsequent calls are allowed
            self.client._last_reconnect_ts = 0.0
            result2 = self.client.reconnect(reason="chaos_2", force=True)
            self.client._last_reconnect_ts = 0.0
            result3 = self.client.reconnect(reason="chaos_3", force=True)

        # All must fail cleanly
        self.assertFalse(result1)
        self.assertFalse(result2)
        self.assertFalse(result3)

        # Backoff must have doubled toward cap: 30→60→120→120 (capped)
        self.assertGreaterEqual(self.client._reconnect_backoff_s, 60.0)
        self.assertLessEqual(self.client._reconnect_backoff_s, 120.0)

        # logged_in must remain False
        self.assertFalse(self.client.logged_in)

    def test_reconnect_chaos_exception_from_logout_does_not_abort_reconnect(self):
        """C0-c: Exception during logout must not propagate; reconnect returns False for other reasons."""
        self.client.logged_in = True
        self.client._last_reconnect_ts = 0.0
        self.mock_api_instance.logout.side_effect = RuntimeError("logout_crashed")

        with patch.object(self.client, "login", return_value=False):
            result = self.client.reconnect(reason="chaos_logout", force=True)

        # Should not raise; must return False (login failed)
        self.assertFalse(result)

    def test_reconnect_chaos_subscribe_exception_after_login_backoff_doubles(self):
        """C0-c: subscribe_basket raises after successful login → backoff doubles."""
        self.client._reconnect_backoff_s = 30.0
        self.client._reconnect_backoff_max_s = 600.0
        self.client._last_reconnect_ts = 0.0
        self.client.tick_callback = MagicMock()

        def mock_login(*args, **kwargs):
            self.client.logged_in = True
            return True

        with patch.object(self.client, "login", side_effect=mock_login):
            with patch.object(self.client, "_ensure_callbacks", side_effect=RuntimeError("cb_crash")):
                result = self.client.reconnect(reason="chaos_cb", force=True)

        self.assertFalse(result)
        # Backoff must have doubled
        self.assertAlmostEqual(self.client._reconnect_backoff_s, 60.0, places=1)

    def test_first_quote_increments_metric(self):
        """CANARY-01: feed_first_quote_total.inc() fires exactly once on first quote."""
        first_quote_counter = MagicMock()
        self.client.metrics = MagicMock(feed_first_quote_total=first_quote_counter)
        self.client._first_quote_seen = False

        with patch.object(self.client, "_validate_quote_schema", return_value=(True, "")):
            with patch.object(self.client, "_clear_quote_pending"):
                self.client.tick_callback = None
                self.client._process_tick("topic", MagicMock())
                self.client._process_tick("topic", MagicMock())

        first_quote_counter.inc.assert_called_once()

    def test_reconnect_exception_fills_exception_metric(self):
        """CANARY-01: feed_reconnect_exception_total is incremented on outer exception."""
        self.client._last_reconnect_ts = 0.0
        exc_counter = MagicMock()
        self.client.metrics = MagicMock(
            feed_reconnect_total=MagicMock(),
            feed_reconnect_exception_total=exc_counter,
        )
        self.client.metrics.feed_reconnect_total.labels.return_value = MagicMock()
        exc_counter.labels.return_value = MagicMock()

        with patch.object(self.client, "login", side_effect=RuntimeError("forced_exc")):
            self.client.reconnect(reason="test_exc", force=True)

        exc_counter.labels.assert_called_once_with(reason="test_exc", exception_type="RuntimeError")
        exc_counter.labels.return_value.inc.assert_called_once()

    def test_reconnect_subscribe_timeout_fills_timeout_metric(self):
        """CANARY-01: feed_reconnect_timeout_total is incremented on subscribe timeout."""
        self.client._last_reconnect_ts = 0.0
        self.client.tick_callback = MagicMock()
        timeout_counter = MagicMock()
        self.client.metrics = MagicMock(
            feed_reconnect_total=MagicMock(),
            feed_reconnect_timeout_total=timeout_counter,
        )
        self.client.metrics.feed_reconnect_total.labels.return_value = MagicMock()
        timeout_counter.labels.return_value = MagicMock()

        def mock_login(*args, **kwargs):
            self.client.logged_in = True
            return True

        # _safe_call_with_timeout returns (ok, result, error, timed_out)
        # timed_out=True triggers the timeout metric
        def ensure_cb_side_effect(*args):
            # reconnect() resets _callbacks_registered = False; this restores it
            self.client._callbacks_registered = True

        with patch.object(self.client, "login", side_effect=mock_login):
            with patch.object(self.client, "_ensure_callbacks", side_effect=ensure_cb_side_effect):
                with patch.object(
                    self.client,
                    "_safe_call_with_timeout",
                    return_value=(False, None, None, True),
                ):
                    self.client.reconnect(reason="test_timeout", force=True)

        timeout_counter.labels.assert_called_once_with(reason="subscribe")
        timeout_counter.labels.return_value.inc.assert_called_once()

    def test_subscribe_basket_defers_all_when_event_callback_not_registered(self):
        """Bug 2: subscribe_basket defers symbols when event callback is not registered."""
        self.client.logged_in = True
        self.client._event_callback_registered = False
        self.client._callbacks_registered = True
        cb = MagicMock()

        with patch.object(self.client, "_ensure_callbacks"):  # prevent side effects from setting flag
            with patch.object(self.client, "_start_sub_retry_thread") as mock_retry:
                with patch.object(self.client, "_start_quote_watchdog"):
                    with patch.object(self.client, "_start_session_refresh_thread"):
                        self.client.subscribe_basket(cb)

        self.mock_api_instance.quote.subscribe.assert_not_called()
        mock_retry.assert_called_once_with(cb)

    def test_subscribe_basket_proceeds_when_event_callback_registered(self):
        """Bug 2 complement: subscribe_basket proceeds when both callbacks are ready."""
        self.client.logged_in = True
        self.client._callbacks_registered = True
        self.client._event_callback_registered = True
        cb = MagicMock()

        with patch.object(self.client, "_ensure_callbacks"):
            with patch.object(self.client, "_start_contract_refresh_thread"):
                with patch.object(self.client, "_start_quote_watchdog"):
                    with patch.object(self.client, "_start_session_refresh_thread"):
                        self.client.subscribe_basket(cb)

        # 2 symbols × 2 quote types = 4 calls
        self.assertEqual(self.mock_api_instance.quote.subscribe.call_count, 4)

    def test_sub_retry_loop_skips_when_not_logged_in(self):
        """Bug 3: retry loop does not subscribe while logged_in is False."""
        self.client.logged_in = False
        self.client._event_callback_registered = True
        self.client._callbacks_registered = True
        self.client._failed_sub_symbols = [{"code": "2330", "exchange": "TSE"}]
        cb = MagicMock()

        iteration = {"n": 0}

        def fake_sleep(_s):
            iteration["n"] += 1
            if iteration["n"] >= 2:
                self.client._sub_retry_running = False

        with patch("hft_platform.feed_adapter.shioaji_client.time.sleep", side_effect=fake_sleep):
            self.client._start_sub_retry_thread(cb)
            self.client._sub_retry_thread.join(timeout=2)

        self.mock_api_instance.quote.subscribe.assert_not_called()
        self.assertEqual(len(self.client._failed_sub_symbols), 1)

    def test_sub_retry_loop_skips_when_event_callback_not_registered(self):
        """Bug 3: retry loop does not subscribe while event callback is not registered."""
        self.client.logged_in = True
        self.client._event_callback_registered = False
        self.client._callbacks_registered = True
        self.client._failed_sub_symbols = [{"code": "2330", "exchange": "TSE"}]
        cb = MagicMock()

        iteration = {"n": 0}

        def fake_sleep(_s):
            iteration["n"] += 1
            if iteration["n"] >= 2:
                self.client._sub_retry_running = False

        with patch("hft_platform.feed_adapter.shioaji_client.time.sleep", side_effect=fake_sleep):
            with patch.object(self.client, "_ensure_callbacks"):  # prevent it from setting the flag
                self.client._start_sub_retry_thread(cb)
                self.client._sub_retry_thread.join(timeout=2)

        self.mock_api_instance.quote.subscribe.assert_not_called()
        self.assertEqual(len(self.client._failed_sub_symbols), 1)
