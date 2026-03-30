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

    def test_rejects_group_exceeding_200(self, tmp_path):
        from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool
        symbols = [{"code": f"SYM{i}", "exchange": "TSE", "group": 0} for i in range(201)]
        sym_path = self._make_symbols_yaml(symbols, tmp_path)
        with pytest.raises(ValueError, match="Group 0 has 201 symbols"):
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
        with mock.patch(
            "hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade"
        ) as MockFacade:
            MockFacade.return_value = mock.MagicMock()
            pool.create_facades()
            assert MockFacade.call_count == 2
            assert len(pool._clients) == 2

    def test_create_facades_injects_lock_suffix(self, tmp_path):
        symbols = [{"code": "TXFC0", "exchange": "TAIFEX", "group": 0}]
        pool = self._make_pool_with_symbols(tmp_path, symbols, 1)
        with mock.patch(
            "hft_platform.feed_adapter.shioaji.quote_connection_pool.ShioajiClientFacade"
        ) as MockFacade:
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
