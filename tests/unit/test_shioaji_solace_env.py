"""Tests for hft_platform.feed_adapter.shioaji._solace_env observability.

Surfaces the Shioaji SDK's Solace reconnect parameters at startup. shioaji
1.3.3 is installed in this environment, so the real-import path returns the
five configured values; the unavailable path is exercised by mocking the
import.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from hft_platform.feed_adapter.shioaji import _solace_env
from hft_platform.feed_adapter.shioaji._solace_env import (
    SOLACE_RECONNECT_PARAMS,
    log_solace_reconnect_params,
    read_solace_reconnect_params,
    reset_solace_reconnect_log,
)


@pytest.fixture(autouse=True)
def _reset_log_guard() -> Any:
    reset_solace_reconnect_log()
    yield
    reset_solace_reconnect_log()


class TestReadSolaceReconnectParams:
    def test_returns_all_five_params_as_ints(self) -> None:
        params = read_solace_reconnect_params()
        assert params is not None
        assert set(params.keys()) == set(SOLACE_RECONNECT_PARAMS)
        # shioaji.config coerces each with int(...) at import time.
        for name in SOLACE_RECONNECT_PARAMS:
            assert isinstance(params[name], int), name

    def test_returns_none_when_config_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(name: str) -> Any:
            raise ImportError(name)

        monkeypatch.setattr(importlib, "import_module", _boom)
        assert read_solace_reconnect_params() is None


class TestLogSolaceReconnectParams:
    def test_logs_once_then_suppresses(self) -> None:
        first = log_solace_reconnect_params()
        assert first is not None
        assert set(first.keys()) == set(SOLACE_RECONNECT_PARAMS)
        # Second call is a no-op (already logged this process).
        assert log_solace_reconnect_params() is None

    def test_force_relogs(self) -> None:
        assert log_solace_reconnect_params() is not None
        assert log_solace_reconnect_params() is None
        assert log_solace_reconnect_params(force=True) is not None

    def test_reset_allows_relog(self) -> None:
        assert log_solace_reconnect_params() is not None
        reset_solace_reconnect_log()
        assert log_solace_reconnect_params() is not None

    def test_returns_none_when_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(_solace_env, "read_solace_reconnect_params", lambda: None)
        assert log_solace_reconnect_params() is None
