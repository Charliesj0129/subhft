"""Tests for hft_platform.feed_adapter.shioaji._solace_env observability.

Surfaces the Shioaji SDK's Solace reconnect parameters at startup. shioaji
Shioaji 1.3.3 exposed these values through ``shioaji.config``. Shioaji 1.5.x
moved messaging into its native core and no longer ships that module, so both
legacy-present and current-unavailable behavior are exercised explicitly.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
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
    def test_returns_all_five_legacy_params_as_ints(self, monkeypatch: pytest.MonkeyPatch) -> None:
        legacy_config = SimpleNamespace(
            SOL_CONNECT_TIMEOUT_MS=3000,
            SOL_RECONNECT_RETRIES=10,
            SOL_RECONNECT_RETRY_WAIT=3000,
            SOL_KEEP_ALIVE_MS=3000,
            SOL_KEEP_ALIVE_LIMIT=3,
        )
        monkeypatch.setattr(importlib, "import_module", lambda name: legacy_config)

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
    @pytest.fixture(autouse=True)
    def _legacy_params_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        params = dict.fromkeys(SOLACE_RECONNECT_PARAMS, 1)
        monkeypatch.setattr(_solace_env, "read_solace_reconnect_params", lambda: params)

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
