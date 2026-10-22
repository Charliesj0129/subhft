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


class _Unprintable(Exception):
    def __str__(self) -> str:
        raise RuntimeError("no text for this exception")


def _failing_logger() -> MagicMock:
    """A sink that fails on the logout report only: ``recreate_api``'s own success log is not under test."""
    logger = MagicMock()

    def warning(event, *_args, **_kwargs):
        if event == "shioaji_logout_failed_on_recreate":
            raise OSError("log sink gone")

    logger.warning.side_effect = warning
    return logger


def _failing_registry() -> MagicMock:
    registry = MagicMock()
    registry.shioaji_logout_failed_total.labels.side_effect = RuntimeError("registry gone")
    return registry


@pytest.mark.parametrize(
    "break_the_report",
    [
        pytest.param(lambda c, m: m.setattr(client_module, "logger", _failing_logger()), id="log_sink_raises"),
        pytest.param(lambda c, m: setattr(c, "metrics", _failing_registry()), id="counter_raises"),
    ],
)
def test_a_report_that_fails_does_not_stop_the_new_api_being_built(client, monkeypatch, break_the_report):
    """The report is a diagnostic about a handle that is being thrown away; it may not block the replacement."""
    old_api = MagicMock(name="old_api")
    old_api.logout.side_effect = RuntimeError("boom")
    client.api = old_api
    break_the_report(client, monkeypatch)

    assert client.recreate_api() is True

    assert client.api is not old_api


def test_a_logout_error_with_unprintable_text_still_replaces_the_handle(client, monkeypatch):
    monkeypatch.setattr(client_module, "logger", MagicMock())
    old_api = MagicMock(name="old_api")
    old_api.logout.side_effect = _Unprintable()
    client.api = old_api

    assert client.recreate_api() is True

    assert client.api is not old_api


def test_the_logged_logout_error_has_identity_and_credential_text_redacted(client, monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(client_module, "logger", logger)
    old_api = MagicMock(name="old_api")
    old_api.logout.side_effect = RuntimeError(
        "logout refused person_id=A123456789 api_key=SYNTHETICKEY99 from 10.1.2.3"
    )
    client.api = old_api

    assert client.recreate_api() is True

    warning = [c for c in logger.warning.call_args_list if c.args[0] == "shioaji_logout_failed_on_recreate"]
    text = warning[0].kwargs["error"]
    assert "A123456789" not in text
    assert "SYNTHETICKEY99" not in text
    assert "10.1.2.3" not in text
    assert "logout refused" in text
