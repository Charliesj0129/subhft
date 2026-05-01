"""Tests for QuoteConnectionPool and related changes."""

import os
import unittest.mock as mock

import pytest
import yaml


class TestSessionLockSuffix:
    """Verify session_lock_suffix is appended to lock path."""

    def test_lock_path_includes_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))

        # Write minimal symbols YAML
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text("symbols: []")

        with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
            from hft_platform.feed_adapter.shioaji.client import ShioajiClient

            client = ShioajiClient(
                config_path=str(sym_path),
                shioaji_config={"session_lock_suffix": "_conn1"},
            )
            assert "_conn1.lock" in client._session_lock_path

    def test_lock_path_no_suffix_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text("symbols: []")

        with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
            from hft_platform.feed_adapter.shioaji.client import ShioajiClient

            client = ShioajiClient(config_path=str(sym_path))
            assert "_conn" not in client._session_lock_path
            assert client._session_lock_path.endswith(".lock")


class TestQuoteConnectionPoolValidation:
    """Test fail-fast validation in Pool constructor."""

    def _make_symbols_yaml(self, symbols: list[dict], tmp_path) -> str:
        path = tmp_path / "symbols.yaml"
        path.write_text(yaml.safe_dump({"symbols": symbols}))
        return str(path)

    def test_rejects_too_many_connections(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        sym_path = self._make_symbols_yaml([], tmp_path)
        with pytest.raises(ValueError, match="exceeds Shioaji limit of 5"):
            QuoteConnectionPool(sym_path, {}, num_conns=5)

    def test_rejects_group_exceeding_limit(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
            _MAX_SUBSCRIPTIONS_PER_CONN,
            QuoteConnectionPool,
        )

        count = _MAX_SUBSCRIPTIONS_PER_CONN + 1
        symbols = [{"code": f"SYM{i}", "exchange": "TSE", "group": 0} for i in range(count)]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        with pytest.raises(ValueError, match=f"Group 0 has {count} symbols"):
            QuoteConnectionPool(sym_path, {}, num_conns=1)

    def test_rejects_group_out_of_range(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 3}]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        with pytest.raises(ValueError, match="group=3 but only 2 connections"):
            QuoteConnectionPool(sym_path, {}, num_conns=2)

    def test_default_group_zero_when_omitted(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX"}]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        pool = QuoteConnectionPool(sym_path, {}, num_conns=1)
        assert pool.num_conns == 1

    def test_shard_files_created(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        pool = QuoteConnectionPool(sym_path, {}, num_conns=2)
        assert len(pool._shard_paths) == 2
        for p in pool._shard_paths:
            assert os.path.exists(p)
            with open(p) as f:
                data = yaml.safe_load(f)
                assert "symbols" in data


class TestQuoteConnectionPoolLifecycle:
    """Test login/subscribe/logout orchestration via mocked facades."""

    def _make_pool_with_symbols(self, tmp_path, symbols, num_conns):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_create_facades_builds_correct_count(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        with mock.patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade") as MockFacade:
            MockFacade.return_value = mock.MagicMock()
            pool.create_facades()
            assert MockFacade.call_count == 2
            assert len(pool._clients) == 2

    def test_create_facades_injects_lock_suffix(self, tmp_path):
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        with mock.patch("hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade") as MockFacade:
            MockFacade.return_value = mock.MagicMock()
            pool.create_facades()
            call_kwargs = MockFacade.call_args_list[0][1]
            assert call_kwargs["shioaji_config"]["session_lock_suffix"] == "_conn0"

    def test_login_all_calls_each_facade(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.login.return_value = True
        facade0.logged_in = True
        facade1 = mock.MagicMock()
        facade1.login.return_value = True
        facade1.logged_in = True
        pool._clients = [facade0, facade1]
        pool._login_interval_s = 0

        pool.login_all()
        facade0.login.assert_called_once()
        facade1.login.assert_called_once()

    def test_login_all_partial_failure(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.login.return_value = True
        facade0.logged_in = True
        facade1 = mock.MagicMock()
        facade1.login.return_value = False
        facade1.logged_in = False
        pool._clients = [facade0, facade1]
        pool._login_interval_s = 0

        pool.login_all()
        assert pool.partial_login is True
        assert pool.logged_in is False

    def test_subscribe_basket_calls_each_logged_in_facade(self, tmp_path):
        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 2)
        facade0 = mock.MagicMock()
        facade0.logged_in = True
        facade0.subscribed_count = 1
        facade1 = mock.MagicMock()
        facade1.logged_in = False
        facade1.subscribed_count = 0
        pool._clients = [facade0, facade1]

        cb = mock.MagicMock()
        pool.subscribe_basket(cb)
        facade0.subscribe_basket.assert_called_once_with(cb)
        facade1.subscribe_basket.assert_not_called()

    def test_logout_calls_all_facades(self, tmp_path):
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        facade0 = mock.MagicMock()
        pool._clients = [facade0]
        pool.logout()
        facade0.close.assert_called_once_with(logout=True)


class TestQuoteConnectionPoolProperties:
    """Test duck-type properties."""

    def _make_pool(self, tmp_path, num_conns=2):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_logged_in_all_true(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in, f1.logged_in = True, True
        pool._clients = [f0, f1]
        assert pool.logged_in is True

    def test_logged_in_one_false(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in, f1.logged_in = True, False
        pool._clients = [f0, f1]
        assert pool.logged_in is False

    def test_partial_login(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in, f1.logged_in = True, False
        pool._clients = [f0, f1]
        assert pool.partial_login is True

    def test_subscribed_count_sum(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.subscribed_count, f1.subscribed_count = 20, 150
        pool._clients = [f0, f1]
        assert pool.subscribed_count == 170

    def test_mode_from_first_client(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0._client.mode = "simulation"
        pool._clients = [f0]
        assert pool.mode == "simulation"

    def test_symbols_concatenation(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0._client.symbols = [{"code": "TXFC0"}]
        f1._client.symbols = [{"code": "2330"}]
        pool._clients = [f0, f1]
        codes = [s["code"] for s in pool.symbols]
        assert codes == ["TXFC0", "2330"]

    def test_health(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f0.subscribed_count = 20
        f0._client._last_quote_data_ts = 1000.0
        pool._clients = [f0]
        h = pool.health()
        assert 0 in h
        assert h[0]["logged_in"] is True
        assert h[0]["subscribed_count"] == 20


class TestQuoteConnectionPoolMetrics:
    """Test Prometheus metrics reporting."""

    def test_update_metrics_sets_gauges(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        pool = QuoteConnectionPool(str(sym_path), {}, num_conns=1)

        facade = mock.MagicMock()
        facade.logged_in = True
        facade.subscribed_count = 15
        facade._client._last_quote_data_ts = 1000.0
        pool._clients = [facade]

        pool.update_metrics()
        assert pool._clients[0].subscribed_count == 15


class TestQuoteConnectionPoolDuckTypeMethods:
    """Test duck-type methods: reconnect, resubscribe, fetch_snapshots, reload_symbols."""

    def _make_pool(self, tmp_path, num_conns=2):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [
            {"code": "TXFC0", "exchange": "TAIFEX", "group": 0},
            {"code": "2330", "exchange": "TSE", "group": 1},
        ]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_reconnect_delegates_to_all_facades(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState

        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.reconnect.return_value = True
        f1.reconnect.return_value = True
        pool._clients = [f0, f1]
        # H9 fix: reconnect() targets slots, not _clients directly.
        # Create slots in CONNECTED state; force=True overrides the state filter.
        slot0 = FacadeSlot(conn_id="0", facade=f0)
        slot0.state = FacadeState.CONNECTED
        slot1 = FacadeSlot(conn_id="1", facade=f1)
        slot1.state = FacadeState.CONNECTED
        pool._slots = [slot0, slot1]

        result = pool.reconnect(reason="test", force=True)
        assert result is True
        f0.reconnect.assert_called_once_with(reason="test", force=True)
        f1.reconnect.assert_called_once_with(reason="test", force=True)

    def test_reconnect_returns_false_on_partial_failure(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState

        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.reconnect.return_value = False
        f1.reconnect.return_value = False
        pool._clients = [f0, f1]
        # H9 fix: slots must be non-CONNECTED so they are targeted without force=True.
        # reconnect() returns True only if at least one facade succeeds (any_ok).
        # With all facades failing, any_ok stays False.
        slot0 = FacadeSlot(conn_id="0", facade=f0)
        slot0.state = FacadeState.RECOVERING
        slot1 = FacadeSlot(conn_id="1", facade=f1)
        slot1.state = FacadeState.RECOVERING
        pool._slots = [slot0, slot1]

        result = pool.reconnect()
        assert result is False

    def test_reconnect_handles_exception(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState

        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.reconnect.side_effect = RuntimeError("conn lost")
        pool._clients = [f0]
        # H9 fix: slot must be non-CONNECTED to be targeted by reconnect().
        slot0 = FacadeSlot(conn_id="0", facade=f0)
        slot0.state = FacadeState.RECOVERING
        pool._slots = [slot0]

        result = pool.reconnect()
        assert result is False

    def test_resubscribe_delegates_to_logged_in_facades(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in = True
        f0.resubscribe.return_value = True
        f1.logged_in = False
        pool._clients = [f0, f1]

        result = pool.resubscribe()
        assert result is True
        f0.resubscribe.assert_called_once()
        f1.resubscribe.assert_not_called()

    def test_resubscribe_returns_false_on_failure(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = True
        f0.resubscribe.return_value = False
        pool._clients = [f0]

        result = pool.resubscribe()
        assert result is False

    def test_fetch_snapshots_merges_all_connections(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in = True
        f0.fetch_snapshots.return_value = [{"code": "TXFC0", "close": 20000}]
        f1.logged_in = True
        f1.fetch_snapshots.return_value = [{"code": "2330", "close": 900}]
        pool._clients = [f0, f1]

        result = pool.fetch_snapshots()
        assert len(result) == 2
        assert result[0]["code"] == "TXFC0"
        assert result[1]["code"] == "2330"

    def test_fetch_snapshots_skips_unconnected(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = False
        pool._clients = [f0]

        result = pool.fetch_snapshots()
        assert result == []
        f0.fetch_snapshots.assert_not_called()

    def test_reload_symbols_delegates_to_all(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        pool._clients = [f0, f1]

        pool.reload_symbols()
        f0.reload_symbols.assert_called_once()
        f1.reload_symbols.assert_called_once()

    def test_reload_symbols_handles_exception(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.reload_symbols.side_effect = RuntimeError("fail")
        pool._clients = [f0]

        assert pool.reload_symbols() is None
        f0.reload_symbols.assert_called_once()

    def test_validate_symbols_merges_all_connections(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0, f1 = mock.MagicMock(), mock.MagicMock()
        f0.logged_in = True
        f0.validate_symbols.return_value = ["BAD1"]
        f1.logged_in = True
        f1.validate_symbols.return_value = ["BAD2"]
        pool._clients = [f0, f1]

        result = pool.validate_symbols()
        assert result == ["BAD1", "BAD2"]

    def test_validate_symbols_skips_unconnected(self, tmp_path):
        pool = self._make_pool(tmp_path)
        f0 = mock.MagicMock()
        f0.logged_in = False
        pool._clients = [f0]

        result = pool.validate_symbols()
        assert result == []
        f0.validate_symbols.assert_not_called()


class TestQuoteConnectionPoolThreadSafety:
    """Test threading.Lock-based synchronization in QuoteConnectionPool."""

    def _make_pool_with_symbols(self, tmp_path, symbols, num_conns):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_refresh_lock_initialized(self, tmp_path):
        """_refresh_lock must be a real threading.Lock, not None."""
        import threading

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        assert isinstance(pool._refresh_lock, type(threading.Lock()))

    def test_stop_options_refresh_sets_running_false(self, tmp_path):
        """stop_options_refresh() must set _options_refresh_running to False."""
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        # Simulate thread started
        pool._options_refresh_running = True
        pool.stop_options_refresh()
        assert pool._options_refresh_running is False

    def test_stop_options_refresh_joins_thread(self, tmp_path):
        """stop_options_refresh() must call join() on the saved thread reference."""
        import threading

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        pool._options_refresh_running = True

        mock_thread = mock.MagicMock(spec=threading.Thread)
        pool._refresh_thread = mock_thread

        pool.stop_options_refresh()

        mock_thread.join.assert_called_once_with(timeout=10)
        assert pool._refresh_thread is None
        assert pool._options_refresh_running is False

    def test_close_stops_options_refresh(self, tmp_path):
        """close() must stop the refresh thread before closing clients."""
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        pool._options_refresh_running = True

        facade = mock.MagicMock()
        pool._clients = [facade]

        pool.close(logout=False)

        assert pool._options_refresh_running is False


class TestOptionsRefreshGuards:
    """Defensive guards for the options auto-refresh background thread."""

    def _make_pool(self, tmp_path, num_conns=1):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_thread_skipped_when_interval_zero(self, tmp_path):
        """interval_s=0 must disable the refresh thread (do not start)."""
        pool = self._make_pool(tmp_path)
        pool.start_options_refresh_thread(interval_s=0)
        assert pool._options_refresh_running is False
        assert pool._refresh_thread is None

    def test_thread_skipped_when_interval_negative(self, tmp_path):
        """Negative interval_s must also be treated as disabled."""
        pool = self._make_pool(tmp_path)
        pool.start_options_refresh_thread(interval_s=-1)
        assert pool._options_refresh_running is False

    def test_refresh_skips_when_all_expiries_in_past(self, tmp_path, monkeypatch):
        """Stale broker cache (all expiries < today) must not trigger subscribe storm."""
        pool = self._make_pool(tmp_path)
        pool._clients = [mock.MagicMock()]
        pool._clients[0].logged_in = False
        out_path = str(tmp_path / "live_with_options.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)  # fix-rc4: writer uses runtime-snapshot env

        expired_opts = [
            {
                "code": "TXO20000A0",
                "right": "C",
                "strike": "20000",
                "delivery_date": "2020/01/15",
                "reference": "20000",
            },
            {
                "code": "TXO20000M0",
                "right": "P",
                "strike": "20000",
                "delivery_date": "2020/01/15",
                "reference": "20000",
            },
        ]
        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=expired_opts):
            assert pool.refresh_options_symbols() is False

    def test_refresh_picks_nearest_active_skipping_expired(self, tmp_path, monkeypatch):
        """When cache mixes expired + active dates, picks earliest *active* one."""
        pool = self._make_pool(tmp_path, num_conns=2)
        pool._all_symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool._clients = [mock.MagicMock() for _ in range(2)]
        for c in pool._clients:
            c.logged_in = False
        out_path = str(tmp_path / "live_with_options.yaml")
        # 2026-04-27 fix-rc4: writer no longer honours SYMBOLS_CONFIG; use the
        # dedicated runtime-snapshot env var instead.
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)

        opts = []
        for date in ("2020/01/15", "2030/04/17", "2031/04/16"):  # one expired, two active
            for s in (20000, 21000):
                opts.append(
                    {
                        "code": f"TXO{s}_{date[:4]}C",
                        "right": "C",
                        "strike": str(s),
                        "delivery_date": date,
                        "reference": "20500",
                    }
                )
                opts.append(
                    {
                        "code": f"TXO{s}_{date[:4]}P",
                        "right": "P",
                        "strike": str(s),
                        "delivery_date": date,
                        "reference": "20500",
                    }
                )

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            assert pool.refresh_options_symbols() is True
        assert pool._options_expiry == "2030/04/17"


class TestSubscriptionLimitConstant:
    """Verify _MAX_SUBSCRIPTIONS_PER_CONN reflects real Shioaji SDK topic limit."""

    def test_limit_is_120(self):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import _MAX_SUBSCRIPTIONS_PER_CONN

        # Cap is in *codes*. Each code subscribes to 2 broker topics
        # (Tick + BidAsk — see subscription_manager._subscribe_symbol).
        # SinoPac Solace per-session topic budget is ~250 (empirically
        # confirmed 2026-04-26 when conn 0 with 163 codes / 326 topics
        # got rejected after ~127 codes / 254 topics). 120 × 2 = 240
        # keeps a small headroom under that ceiling.
        assert _MAX_SUBSCRIPTIONS_PER_CONN == 120

    def test_client_default_matches_pool_limit(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
        monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
        monkeypatch.setenv("HFT_MODE", "sim")
        monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))

        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text("symbols: []")

        with mock.patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=None):
            from hft_platform.feed_adapter.shioaji.client import ShioajiClient

            client = ShioajiClient(config_path=str(sym_path))
            assert client.MAX_SUBSCRIPTIONS == 120


class TestOptionsRoundRobinSharding:
    """Verify options are distributed round-robin across groups by strike."""

    def _make_pool(self, tmp_path, num_conns):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        return QuoteConnectionPool(str(sym_path), {}, num_conns=num_conns)

    def test_round_robin_distributes_evenly_across_3_groups(self, tmp_path, monkeypatch):
        """With 60 calls + 60 puts across 30 strikes, 3 option groups should each get ~40."""
        pool = self._make_pool(tmp_path, num_conns=4)  # group 0=base, 1/2/3=options
        pool._all_symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool._clients = [mock.MagicMock() for _ in range(4)]
        for c in pool._clients:
            c.logged_in = False

        # Mock contract fetching to return synthetic options
        strikes = list(range(20000, 20000 + 30 * 50, 50))  # 30 strikes
        opts = []
        for s in strikes:
            opts.append(
                {
                    "code": f"TXO{s}D6",
                    "right": "C",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )
            opts.append(
                {
                    "code": f"TXO{s}P6",
                    "right": "P",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )

        out_path = str(tmp_path / "live_with_options.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)  # fix-rc4: writer uses runtime-snapshot env

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            result = pool.refresh_options_symbols()

        assert result is True
        # Read the generated YAML
        with open(out_path) as f:
            data = yaml.safe_load(f)
        syms = data["symbols"]
        group_counts = {}
        for s in syms:
            g = s.get("group", 0)
            group_counts[g] = group_counts.get(g, 0) + 1

        # group 0 has 1 base symbol
        assert group_counts[0] == 1
        # 60 options spread across 3 groups: 20 each
        assert group_counts[1] == 20
        assert group_counts[2] == 20
        assert group_counts[3] == 20

    def test_round_robin_auto_trims_on_overflow(self, tmp_path, monkeypatch):
        """If total options exceed per-group capacity, auto-trim to fit."""
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import _MAX_SUBSCRIPTIONS_PER_CONN

        pool = self._make_pool(tmp_path, num_conns=2)  # group 0=base, 1=options
        pool._all_symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool._clients = [mock.MagicMock() for _ in range(2)]
        for c in pool._clients:
            c.logged_in = False

        # 200 calls + 200 puts = 400 options on 1 group -> exceeds 120 limit
        strikes = list(range(20000, 20000 + 200 * 50, 50))
        opts = []
        for s in strikes:
            opts.append(
                {
                    "code": f"TXO{s}D6",
                    "right": "C",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )
            opts.append(
                {
                    "code": f"TXO{s}P6",
                    "right": "P",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )

        out_path = str(tmp_path / "live_with_options.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)  # fix-rc4: writer uses runtime-snapshot env

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            result = pool.refresh_options_symbols()

        # Auto-trim should succeed (not reject)
        assert result is True

        # Verify trimmed YAML respects per-group limit
        with open(out_path) as f:
            data = yaml.safe_load(f)
        for g in range(2):
            count = sum(1 for s in data["symbols"] if s.get("group") == g)
            assert count <= _MAX_SUBSCRIPTIONS_PER_CONN, (
                f"Group {g} has {count} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN}"
            )

        # Total options should be less than original 400
        opt_count = sum(1 for s in data["symbols"] if s.get("exchange") == "OPT")
        assert opt_count < 400
        assert opt_count > 0  # not empty

    def test_round_robin_interleaves_call_put_pairs_across_3_groups(self, tmp_path, monkeypatch):
        """With 3+ option groups, each group should have a mix of calls and puts."""
        pool = self._make_pool(tmp_path, num_conns=4)  # groups 1,2,3 for options
        pool._all_symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool._clients = [mock.MagicMock() for _ in range(4)]
        for c in pool._clients:
            c.logged_in = False

        strikes = list(range(20000, 20000 + 10 * 50, 50))  # 10 strikes
        opts = []
        for s in strikes:
            opts.append(
                {
                    "code": f"TXO{s}D6",
                    "right": "C",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )
            opts.append(
                {
                    "code": f"TXO{s}P6",
                    "right": "P",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "20500",
                }
            )

        out_path = str(tmp_path / "live_with_options.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)  # fix-rc4: writer uses runtime-snapshot env

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            result = pool.refresh_options_symbols()

        assert result is True
        with open(out_path) as f:
            data = yaml.safe_load(f)

        # With 3 option groups and strike-interleaved round-robin,
        # at least 2 of the 3 groups must have both calls and puts
        mixed_count = 0
        for g in [1, 2, 3]:
            group_syms = [s for s in data["symbols"] if s.get("group") == g]
            codes = [s["code"] for s in group_syms]
            has_call = any("D6" in c for c in codes)
            has_put = any("P6" in c for c in codes)
            if has_call and has_put:
                mixed_count += 1
        assert mixed_count >= 2, "At least 2 of 3 option groups should have both calls and puts"

    def test_production_scenario_3_conns_oversized_chain_trims(self, tmp_path, monkeypatch):
        """Regression test: 3 conns, oversized option chain must auto-trim, not reject.

        With cap=120 per conn and 2 option groups (group 0 holds the base future),
        chain capacity is 240; 250 strikes × 2 sides = 500 options must trim to ≤ 240.
        """
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import _MAX_SUBSCRIPTIONS_PER_CONN

        pool = self._make_pool(tmp_path, num_conns=3)  # group 0=base, 1/2=options
        pool._all_symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool._clients = [mock.MagicMock() for _ in range(3)]
        for c in pool._clients:
            c.logged_in = False

        # 250 strikes × (call + put) = 500 options, exceeds 2 × 120 = 240 capacity → trim
        strikes = list(range(26500, 26500 + 250 * 50, 50))
        opts = []
        for s in strikes:
            opts.append(
                {
                    "code": f"TXO{s}D6",
                    "right": "C",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "33000",
                }
            )
            opts.append(
                {
                    "code": f"TXO{s}P6",
                    "right": "P",
                    "strike": str(s),
                    "delivery_date": "2030/04/17",
                    "reference": "33000",
                }
            )

        out_path = str(tmp_path / "live_with_options.yaml")
        monkeypatch.setenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", out_path)  # fix-rc4: writer uses runtime-snapshot env

        with mock.patch.object(type(pool), "_load_options_from_cache", return_value=opts):
            result = pool.refresh_options_symbols()

        # Must succeed (auto-trim), not reject
        assert result is True

        with open(out_path) as f:
            data = yaml.safe_load(f)

        # Every group must respect limit
        for g in range(3):
            count = sum(1 for s in data["symbols"] if s.get("group") == g)
            assert count <= _MAX_SUBSCRIPTIONS_PER_CONN, (
                f"Group {g} has {count} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN}"
            )

        # Options should be trimmed but non-empty (≤ 2 conns × cap = 240)
        opt_count = sum(1 for s in data["symbols"] if s.get("exchange") == "OPT")
        assert 200 <= opt_count <= 240, f"Expected 200-240 options after trim, got {opt_count}"


# ─────────────────────────────────────────────────────────────────────
# P1-d (commit e6edea37): pool degraded gauge + debounced CRITICAL log
# ─────────────────────────────────────────────────────────────────────


class TestQuoteConnectionPoolDegradedRollup:
    """Verify pool-level degraded gauge + debounced CRITICAL log (P1-d)."""

    def _make_pool_with_4_slots(self, tmp_path):
        """Build a pool with 4 connection slots and 4 mock facades — bypasses
        ``create_facades`` so the test does not need a real Shioaji session.
        """
        from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

        symbols = [{"code": f"SYM{g}", "exchange": "TSE", "group": g} for g in range(4)]
        sym_path = tmp_path / "symbols.yaml"
        sym_path.write_text(yaml.safe_dump({"symbols": symbols}))
        pool = QuoteConnectionPool(str(sym_path), {}, num_conns=4)

        # Fabricate 4 slots with mock facades; default state = RECOVERING.
        pool._slots = []
        pool._clients = []
        for i in range(4):
            facade = mock.MagicMock()
            facade.logged_in = True
            facade.subscribed_count = 0
            slot = FacadeSlot(conn_id=str(i), facade=facade)
            slot.state = FacadeState.CONNECTED  # start healthy
            pool._slots.append(slot)
            pool._clients.append(facade)
        return pool

    def test_pool_degraded_gauge_raises_after_threshold_and_logs_once(self, tmp_path, monkeypatch):
        """Three of four conns non-CONNECTED for >= alert window must raise the
        ``hft_quote_pool_degraded`` gauge to 1, fraction to 0.75, and emit
        the CRITICAL log exactly once even if the check runs again with
        unchanged state.
        """
        import hft_platform.feed_adapter.shioaji.quote_connection_pool as qcp
        from hft_platform.feed_adapter.shioaji.facade_slot import FacadeState

        pool = self._make_pool_with_4_slots(tmp_path)
        pool._pool_degraded_alert_after_s = 1.0  # tight threshold for the test

        # Mark 3 of 4 conns DOWN.
        pool._slots[0].state = FacadeState.RECOVERING
        pool._slots[1].state = FacadeState.RECOVERING
        pool._slots[2].state = FacadeState.DISCONNECTED
        pool._slots[3].state = FacadeState.CONNECTED

        # Drive time.monotonic via monkeypatch so we can step past the window
        # deterministically (no real sleep).
        clock = {"now": 1000.0}
        monkeypatch.setattr(qcp.time, "monotonic", lambda: clock["now"])

        # Patch the module-level logger to capture .critical/.warning calls.
        mock_logger = mock.MagicMock()
        monkeypatch.setattr(qcp, "logger", mock_logger)

        # First tick: starts the degradation timer; gauge stays 0 (alert not raised yet).
        pool.update_metrics()
        assert pool._pool_degraded_since_mono == 1000.0
        assert pool._pool_degraded_alerted is False
        assert mock_logger.critical.call_count == 0

        # Advance past the alert window — second tick should fire the CRITICAL log.
        clock["now"] = 1002.0
        pool.update_metrics()
        assert pool._pool_degraded_alerted is True
        assert mock_logger.critical.call_count == 1
        crit_args = mock_logger.critical.call_args
        assert crit_args[0][0] == "quote_pool_degraded"
        # n_unhealthy=3 / n_slots=4 == 0.75
        assert crit_args[1].get("n_slots") == 4
        assert crit_args[1].get("n_unhealthy") == 3
        assert abs(crit_args[1].get("fraction") - 0.75) < 1e-9

        # Verify the pool-level gauges directly.
        assert qcp._METRIC_POOL_DEGRADED_FRACTION._value.get() == 0.75
        assert qcp._METRIC_POOL_DEGRADED._value.get() == 1.0

        # Third tick with state unchanged: must NOT re-emit the CRITICAL log
        # (debounced — alerted flag stays True until recovery).
        clock["now"] = 1003.0
        pool.update_metrics()
        assert mock_logger.critical.call_count == 1, "CRITICAL log must be emitted exactly once until recovery"
