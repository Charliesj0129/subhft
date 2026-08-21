"""The scheduled resubscribe path must record what it achieved.

``QuoteRuntime.schedule_resubscribe`` runs the resubscribe on a daemon thread.
Before this contract existed the thread recorded no metric at all: only the
explicit ``SubscriptionManager.resubscribe()`` entry point moved
``feed_resubscribe_total``, so production on 2026-08-20 read

    feed_resubscribe_total{result="event_4"} 1433
    feed_resubscribe_total{result="ok"}         5

and there was no way to tell whether the 1433 triggers had been serviced --
``event_4`` is a *trigger* label, ``ok``/``error``/``skip`` are *outcomes*.
An exception inside the thread reached only ``threading.excepthook``.
"""

from __future__ import annotations

import threading
import unittest.mock as mock

import pytest


def _make_runtime(monkeypatch, *, resubscribe_raises: bool = False, tick_callback: object = None):
    prom = pytest.importorskip("prometheus_client")
    assert prom is not None
    from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime
    from hft_platform.observability.metrics import MetricsRegistry

    metrics = MetricsRegistry.get()
    client = mock.MagicMock()
    client.metrics = metrics
    client._resubscribe_scheduled = False
    client._resubscribe_delay_s = 0.0
    client.tick_callback = tick_callback
    client._ensure_callbacks = mock.MagicMock()
    if resubscribe_raises:
        client._resubscribe_all = mock.MagicMock(side_effect=RuntimeError("broker said no"))
    else:
        client._resubscribe_all = mock.MagicMock()

    runtime = QuoteRuntime.__new__(QuoteRuntime)
    object.__setattr__(runtime, "_client", client)
    return runtime, client, metrics


def _counter_value(metrics, result: str) -> float:
    counter = metrics.feed_resubscribe_total
    for sample in counter.collect()[0].samples:
        if sample.labels.get("result") == result and (sample.name.endswith("_total") or sample.name == counter._name):
            return float(sample.value)
    return 0.0


def _run_and_join(runtime, client, reason: str) -> None:
    runtime.schedule_resubscribe(reason)
    thread = client._resubscribe_thread
    assert isinstance(thread, threading.Thread)
    thread.join(timeout=5.0)
    assert not thread.is_alive(), "resubscribe thread did not finish"


def test_scheduled_resubscribe_records_ok_when_it_completes(monkeypatch) -> None:
    runtime, client, metrics = _make_runtime(monkeypatch, tick_callback=lambda *a, **k: None)
    before = _counter_value(metrics, "ok")

    _run_and_join(runtime, client, "event_4")

    assert client._resubscribe_all.call_count == 1
    assert _counter_value(metrics, "ok") == before + 1
    assert client._resubscribe_scheduled is False


def test_scheduled_resubscribe_records_error_instead_of_dying_silently(monkeypatch) -> None:
    runtime, client, metrics = _make_runtime(monkeypatch, resubscribe_raises=True, tick_callback=lambda *a, **k: None)
    before_error = _counter_value(metrics, "error")
    before_ok = _counter_value(metrics, "ok")

    _run_and_join(runtime, client, "event_4")

    assert _counter_value(metrics, "error") == before_error + 1
    assert _counter_value(metrics, "ok") == before_ok, "a raising resubscribe must not count as ok"
    # The scheduling latch must still clear, or no further resubscribe is possible.
    assert client._resubscribe_scheduled is False


def test_scheduled_resubscribe_records_skip_when_there_is_no_tick_callback(monkeypatch) -> None:
    runtime, client, metrics = _make_runtime(monkeypatch, tick_callback=None)
    before_skip = _counter_value(metrics, "skip")
    before_ok = _counter_value(metrics, "ok")

    _run_and_join(runtime, client, "event_13")

    assert client._resubscribe_all.call_count == 0
    assert _counter_value(metrics, "skip") == before_skip + 1
    # Doing nothing used to log "Resubscribe completed" and count as nothing.
    assert _counter_value(metrics, "ok") == before_ok
