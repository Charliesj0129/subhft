"""The SDK's session-down notification must reach structlog and Prometheus.

On 2026-09-07 the Solace C library wrote 57 stderr lines about six broker
sessions failing to connect (48 ``Connect attempt ... timed out``, 2
``Protocol or communication error when attempting to login``). Over the same
window the engine's structured logs held **zero** error or critical events, and
the quote-event callback delivered nothing at all -- so neither a log-level
error tally nor an alert rule could see the outage. The SDK has a dedicated
``set_session_down_callback`` for exactly this and the platform never
registered it.

These tests pin that the callback is registered, that firing it is visible at
error level and on a counter, and that it stays observational -- a failure to
register it must never gate the quote callbacks.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import structlog

from hft_platform.feed_adapter.shioaji.client import ShioajiClient
from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime


def _registrar(api: object | None) -> SimpleNamespace:
    """Minimal stand-in for the client, enough for the unbound registrar."""
    return SimpleNamespace(
        api=api,
        _session_down_callback_fn=lambda: None,
        _record_crash_signature=mock.MagicMock(),
    )


class TestSessionDownCallbackRegistration:
    def test_session_down_callback_is_registered_on_the_sdk(self) -> None:
        api = mock.MagicMock()
        client = _registrar(api)

        ok = ShioajiClient._register_session_down_callback(client)

        assert ok is True
        api.set_session_down_callback.assert_called_once_with(client._session_down_callback_fn)

    def test_registration_reports_false_when_the_sdk_lacks_the_setter(self) -> None:
        # A future SDK could drop it. Degrade to a warning; never raise.
        api = SimpleNamespace()
        client = _registrar(api)

        with structlog.testing.capture_logs() as logs:
            ok = ShioajiClient._register_session_down_callback(client)

        assert ok is False
        assert any(entry["log_level"] == "warning" for entry in logs)

    def test_registration_reports_false_when_the_setter_raises(self) -> None:
        api = mock.MagicMock()
        api.set_session_down_callback.side_effect = RuntimeError("sdk busy")
        client = _registrar(api)

        ok = ShioajiClient._register_session_down_callback(client)

        assert ok is False
        client._record_crash_signature.assert_called_once()

    def test_registration_reports_false_without_an_api(self) -> None:
        assert ShioajiClient._register_session_down_callback(_registrar(None)) is False


class TestSessionDownIsVisible:
    @staticmethod
    def _runtime(metrics: object | None) -> SimpleNamespace:
        return SimpleNamespace(_client=SimpleNamespace(metrics=metrics))

    def test_session_down_logs_at_error_level(self) -> None:
        runtime = self._runtime(None)

        with structlog.testing.capture_logs() as logs:
            QuoteRuntime.on_session_down(runtime)

        # Error level is the whole point: the outage was invisible precisely
        # because every SDK-adjacent event was logged at info.
        assert [e for e in logs if e["event"] == "shioaji_session_down" and e["log_level"] == "error"]

    def test_session_down_increments_its_counter(self) -> None:
        metrics = mock.MagicMock()
        runtime = self._runtime(metrics)

        QuoteRuntime.on_session_down(runtime)

        metrics.shioaji_session_down_total.inc.assert_called_once_with()

    def test_session_down_never_raises_into_the_sdk(self) -> None:
        # Runs on a broker thread inside a C caller: an escape is a crash.
        metrics = mock.MagicMock()
        metrics.shioaji_session_down_total.inc.side_effect = RuntimeError("registry gone")
        runtime = self._runtime(metrics)

        with structlog.testing.capture_logs() as logs:
            QuoteRuntime.on_session_down(runtime)  # must not raise

        # Reaching the counter proves the raise came from inside the handler
        # and was swallowed there, rather than the handler never running.
        metrics.shioaji_session_down_total.inc.assert_called_once_with()
        assert [e for e in logs if e["event"] == "shioaji_session_down"]

    def test_session_down_does_not_trigger_a_reconnect(self) -> None:
        """Observational by design -- recovery stays with the feed-gap watchdog.

        A second, faster reconnect trigger on a broker-thread callback is how
        this platform produced its documented reconnect storms.
        """
        client = mock.MagicMock()
        client.metrics = None
        QuoteRuntime.on_session_down(SimpleNamespace(_client=client))

        for forbidden in ("_mark_quote_pending", "_schedule_resubscribe", "_resubscribe_all", "_ensure_callbacks"):
            assert not getattr(client, forbidden).called, f"{forbidden} must not run from a session-down callback"


class TestRegistrationStaysObservational:
    def test_session_down_failure_does_not_gate_the_quote_callbacks(self) -> None:
        """A lost session-down callback must not stop quotes registering."""
        import inspect

        src = inspect.getsource(ShioajiClient._register_callbacks)
        assert "_register_session_down_callback()" in src

        # It must land in its own attribute, never folded into the two flags
        # that drive the retry ladder.
        assert "self._session_down_callback_registered = self._register_session_down_callback()" in src
        for gating in (
            "ok_quote = self._register_session_down_callback",
            "ok_event = self._register_session_down_callback",
        ):
            assert gating not in src
