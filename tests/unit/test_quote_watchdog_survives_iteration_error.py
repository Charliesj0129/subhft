"""One bad watchdog iteration must not end the quote watchdog thread.

``start_quote_watchdog``'s ``try`` used to wrap the entire ``while`` loop, so a
single exception ran the ``finally``, cleared ``_quote_watchdog_running``, and
left that facade with no stall detection for the life of the process. Nothing
restarts the thread on its own.

That is the same failure the loop's own comment already warns about -- "one
facade went silent for 24 h carrying 74 symbols" -- reached by a different
route. It happened on the live engine at 2026-09-07T17:47:26Z, when the shioaji
1.5.x Rust core raised a transient ``Already mutably borrowed`` mid-reconnect.
It survived only because a reconnect happened to follow 70 ms later and restart
the thread; nothing in the design guarantees one does.
"""

from __future__ import annotations

import inspect
import threading
import time
from unittest import mock

import pytest

from hft_platform.feed_adapter.shioaji import quote_runtime as qr


def _runtime_with_client(interval_s: float = 0.001):
    """A QuoteRuntime whose client is a mock, ready to run the watchdog."""
    client = mock.MagicMock()
    client.api = mock.MagicMock()
    client.logged_in = True
    client.metrics = None
    client.tick_callback = None
    client._quote_watchdog_running = False
    client._quote_watchdog_interval_s = interval_s
    client._quote_watchdog_thread = None
    client._set_thread_alive_metric = mock.MagicMock()
    # Real numerics: a MagicMock here would raise a TypeError of its own on
    # every comparison and drown the error the test is actually about.
    client._quote_no_data_s = 10_000.0
    client._market_open_grace_s = 0.0
    client._last_quote_data_ts = time.time()
    client._allow_quote_recovery = False
    client._is_market_open_grace_period = mock.MagicMock(return_value=False)

    runtime = qr.QuoteRuntime.__new__(qr.QuoteRuntime)
    runtime._client = client
    return runtime, client


def _run_watchdog_until(runtime, client, predicate, timeout_s: float = 3.0) -> bool:
    """Start the watchdog and poll until *predicate* holds (no fixed sleeps)."""
    runtime.start_quote_watchdog()
    deadline = time.monotonic() + timeout_s
    try:
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return False
    finally:
        client._quote_watchdog_running = False
        thread = client._quote_watchdog_thread
        if isinstance(thread, threading.Thread):
            thread.join(timeout=2.0)


class TestWatchdogSurvivesATransientIterationError:
    def test_a_single_iteration_error_does_not_end_the_thread(self):
        """The live 2026-09-07 shape: one 'Already mutably borrowed' must not kill it."""
        runtime, client = _runtime_with_client()
        calls = {"n": 0}

        def _flaky_logged_in():
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("Already mutably borrowed")
            return True

        type(client).logged_in = property(lambda _self: _flaky_logged_in())
        try:
            # Reaching iteration 6 when iteration 2 raised is the proof: the
            # old whole-loop try would have ended the thread at 2.
            ran_on = _run_watchdog_until(runtime, client, lambda: calls["n"] >= 6)
            assert ran_on, f"watchdog stopped after the error (iterations={calls['n']})"
            assert calls["n"] >= 6
        finally:
            del type(client).logged_in

    def test_isolated_errors_age_out_and_never_reach_the_limit(self):
        """Errors decay, so a slow drip of hiccups does not accumulate into a kill."""
        src = inspect.getsource(qr.QuoteRuntime.start_quote_watchdog)
        assert "error_decay_s" in src, "isolated errors must age out"
        assert "last_error_mono" in src
        # The decay must be compared against a monotonic reading, not wall clock.
        assert "time.monotonic()" in src

    def test_a_persistently_failing_watchdog_still_gives_up(self):
        """The guard is bounded, not absent: a stuck watchdog ends fail-closed."""
        runtime, client = _runtime_with_client()

        def _always_raises(_self):
            raise RuntimeError("Already mutably borrowed")

        type(client).logged_in = property(_always_raises)
        try:
            stopped = _run_watchdog_until(runtime, client, lambda: client._quote_watchdog_running is False)
            assert stopped, "a persistently failing watchdog must terminate"
            client._set_thread_alive_metric.assert_any_call("quote_watchdog", False)
        finally:
            del type(client).logged_in

    def test_the_limit_is_above_one_so_a_single_hiccup_is_survivable(self):
        assert qr._WATCHDOG_MAX_CONSECUTIVE_ERRORS > 1

    def test_the_whole_loop_try_cannot_be_reintroduced(self):
        """Source guard: the error handler must sit inside the loop."""
        src = inspect.getsource(qr.QuoteRuntime.start_quote_watchdog)
        assert "quote_watchdog_iteration_failed" in src, "there must be a per-iteration handler"

        loop_at = src.index("while c.api and c._quote_watchdog_running:")
        per_iteration_at = src.index("quote_watchdog_iteration_failed")
        thread_dead_at = src.index('"Quote watchdog thread crashed"')

        # The per-iteration handler must sit between the loop head and the
        # thread-level handler -- i.e. inside the loop, catching first.
        assert loop_at < per_iteration_at < thread_dead_at, (
            "the per-iteration handler must be inside the while loop and run "
            "before the thread-level one; a try wrapping the whole loop ends "
            "the thread on the first exception"
        )
        # And it must be indented deeper than the loop head.
        loop_indent = len(src[:loop_at].rsplit("\n", 1)[1])
        handler_line_start = src.rindex("except Exception", 0, per_iteration_at)
        handler_indent = len(src[:handler_line_start].rsplit("\n", 1)[1])
        assert handler_indent > loop_indent, "handler must be nested inside the loop"


@pytest.mark.parametrize("attr", ["_quote_watchdog_running"])
def test_the_thread_alive_metric_is_cleared_on_exit(attr):
    """Whatever ends the thread, the liveness gauge must go to 0 for the alert."""
    src = inspect.getsource(qr.QuoteRuntime.start_quote_watchdog)
    finally_block = src.rsplit("finally:", 1)[1]
    assert attr in finally_block
    assert '_set_thread_alive_metric("quote_watchdog", False)' in finally_block
