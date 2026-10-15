"""A failed ``logout()`` while the Shioaji handle is being replaced must be seen (N24).

The broker allows 5 sessions with no spare. ``recreate_api`` logs the old handle out and builds a new one; the
logout error used to be ``except Exception: pass``, so a session that was not released left no trace, and the next
login could be refused (451) with nothing pointing back here. The handle is still replaced: the old one is being
discarded either way, so the failure is reported, not acted on.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feed_adapter.shioaji import client as client_module
from hft_platform.observability.metrics import MetricsRegistry


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIOAJI_API_KEY", "TESTKEY123")
    monkeypatch.setenv("SHIOAJI_SECRET_KEY", "SECRET")
    monkeypatch.setenv("HFT_MODE", "sim")
    monkeypatch.setenv("HFT_SHIOAJI_SESSION_LOCK_DIR", str(tmp_path))
    sym_path = tmp_path / "symbols.yaml"
    sym_path.write_text("symbols: []")
    sdk = MagicMock()
    sdk.Shioaji.side_effect = lambda **_kw: MagicMock(name="new_api")
    with patch("hft_platform.feed_adapter.shioaji.client._sdk", return_value=sdk):
        shioaji_client = client_module.ShioajiClient(config_path=str(sym_path))
        shioaji_client.metrics = MetricsRegistry.get()
        yield shioaji_client


def _failed() -> float:
    return MetricsRegistry.get().shioaji_logout_failed_total.labels(context="recreate_api")._value.get()


def test_a_logout_that_raises_is_logged_and_counted_and_the_handle_is_still_replaced(client, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(client_module, "logger", logger)
    old_api = MagicMock(name="old_api")
    old_api.logout.side_effect = ConnectionError("session already gone")
    client.api = old_api
    before = _failed()

    assert client.recreate_api() is True

    assert _failed() == before + 1
    assert client.api is not old_api
    old_api.logout.assert_called_once()
    warning = [c for c in logger.warning.call_args_list if c.args[0] == "shioaji_logout_failed_on_recreate"]
    assert len(warning) == 1
    assert warning[0].kwargs["error_type"] == "ConnectionError"
    assert warning[0].kwargs["error"] == "session already gone"


def test_a_clean_logout_reports_nothing(client, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(client_module, "logger", logger)
    old_api = MagicMock(name="old_api")
    client.api = old_api
    before = _failed()

    assert client.recreate_api() is True

    assert _failed() == before
    assert "shioaji_logout_failed_on_recreate" not in [c.args[0] for c in logger.warning.call_args_list]
    old_api.logout.assert_called_once()


def test_without_an_old_handle_there_is_nothing_to_log_out_and_nothing_to_report(client, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(client_module, "logger", logger)
    client.api = None
    before = _failed()

    assert client.recreate_api() is True

    assert _failed() == before
    assert client.api is not None
    assert "shioaji_logout_failed_on_recreate" not in [c.args[0] for c in logger.warning.call_args_list]


def test_a_missing_metrics_registry_does_not_stop_the_log_or_the_replacement(client, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(client_module, "logger", logger)
    old_api = MagicMock(name="old_api")
    old_api.logout.side_effect = RuntimeError("boom")
    client.api = old_api
    client.metrics = None

    assert client.recreate_api() is True

    assert client.api is not old_api
    assert "shioaji_logout_failed_on_recreate" in [c.args[0] for c in logger.warning.call_args_list]
