"""Tests for QuoteConnectionPool and related changes."""

import os
import unittest.mock as mock

import pytest


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
