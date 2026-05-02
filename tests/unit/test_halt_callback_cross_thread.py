"""H2 (2026-04-25): bootstrap HALT notification callback must dispatch
correctly across thread boundaries.

Regression coverage for the production bug where ``asyncio.get_event_loop()``
in a daemon-thread callback raised ``RuntimeError`` on Python 3.12+ and the
wrapping ``except: pass`` silently dropped every Telegram alert. The fix
plumbs the engine loop reference via ``StormGuard.get_loop()`` and uses
``asyncio.run_coroutine_threadsafe`` so callbacks fire-and-forget across
threads.

Three scenarios:
    1. Callback fired from a daemon thread → notification reaches dispatcher.
    2. Callback fired before the engine loop is bound → returns gracefully
       and increments ``halt_callback_no_loop_total``.
    3. Callback fired from the event-loop thread → still works (direct path).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import MagicMock

import pytest

from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.bootstrap import _make_halt_notification_callback


class _FakeDispatcher:
    """Records every ``notify_halt`` invocation; coroutine actually runs."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.event = asyncio.Event()

    async def notify_halt(self, reason: str) -> None:
        self.calls.append(reason)
        self.event.set()


@pytest.fixture()
def storm_guard_with_metrics() -> StormGuard:
    sg = StormGuard()
    sg.metrics = MagicMock()
    return sg


def test_callback_from_daemon_thread_reaches_dispatcher(storm_guard_with_metrics) -> None:
    """Daemon-thread fire MUST reach the dispatcher coroutine via threadsafe path."""
    sg = storm_guard_with_metrics

    async def _run() -> tuple[_FakeDispatcher, str]:
        loop = asyncio.get_running_loop()
        sg.bind_loop(loop)
        disp = _FakeDispatcher()
        cb = _make_halt_notification_callback(sg, disp)

        # Fire from a daemon thread — the smoking-gun production scenario.
        worker = threading.Thread(target=cb, daemon=True)
        worker.start()
        worker.join(timeout=2.0)

        # Wait for the cross-thread-scheduled coroutine to complete on the loop.
        await asyncio.wait_for(disp.event.wait(), timeout=2.0)
        return disp, "ok"

    disp, status = asyncio.run(_run())
    assert status == "ok"
    assert disp.calls == ["StormGuard HALT triggered"], f"Expected exactly one notify_halt call, got: {disp.calls}"


def test_callback_before_loop_bound_returns_gracefully_and_records_metric() -> None:
    """When ``bind_loop`` has not been called, the callback MUST NOT crash and
    MUST increment ``halt_callback_no_loop_total`` so operators see the drop."""
    sg = StormGuard()
    metric_mock = MagicMock()
    sg.metrics = metric_mock

    disp = _FakeDispatcher()
    cb = _make_halt_notification_callback(sg, disp)

    # Patch MetricsRegistry.get() so we can inspect the increment without
    # depending on global Prometheus state.
    from hft_platform.observability import metrics as metrics_mod

    captured: dict[str, int] = {"no_loop": 0}

    class _MockRegistry:
        @property
        def halt_callback_no_loop_total(self_inner):  # noqa: ANN001
            counter = MagicMock()

            def _inc(amount: float = 1.0) -> None:
                captured["no_loop"] += int(amount)

            counter.inc = _inc
            return counter

    original_get = metrics_mod.MetricsRegistry.get
    metrics_mod.MetricsRegistry.get = staticmethod(lambda: _MockRegistry())  # type: ignore[method-assign]
    try:
        cb()  # Should not raise.
    finally:
        metrics_mod.MetricsRegistry.get = original_get  # type: ignore[method-assign]

    assert disp.calls == [], "Dispatcher must NOT be called when no loop is bound"
    assert captured["no_loop"] == 1, f"halt_callback_no_loop_total must increment exactly once, got: {captured}"


def test_callback_from_loop_thread_uses_direct_path(storm_guard_with_metrics) -> None:
    """When fired from the engine loop thread, the dispatcher MUST still run."""
    sg = storm_guard_with_metrics

    async def _run() -> _FakeDispatcher:
        loop = asyncio.get_running_loop()
        sg.bind_loop(loop)
        disp = _FakeDispatcher()
        cb = _make_halt_notification_callback(sg, disp)

        # Fire directly on the loop thread.
        cb()
        # The dispatcher coroutine was scheduled via loop.create_task — wait.
        await asyncio.wait_for(disp.event.wait(), timeout=2.0)
        return disp

    disp = asyncio.run(_run())
    assert disp.calls == ["StormGuard HALT triggered"]


def test_callback_after_loop_closed_records_no_loop_metric() -> None:
    """If ``bind_loop`` was called but the loop is now closed, callback MUST
    treat it as "no loop bound" and emit the same drop metric."""
    sg = StormGuard()
    sg.metrics = MagicMock()

    # Build and bind a loop, then close it.
    loop = asyncio.new_event_loop()
    sg.bind_loop(loop)
    loop.close()

    disp = _FakeDispatcher()
    cb = _make_halt_notification_callback(sg, disp)

    from hft_platform.observability import metrics as metrics_mod

    captured: dict[str, int] = {"no_loop": 0}

    class _MockRegistry:
        @property
        def halt_callback_no_loop_total(self_inner):  # noqa: ANN001
            counter = MagicMock()

            def _inc(amount: float = 1.0) -> None:
                captured["no_loop"] += int(amount)

            counter.inc = _inc
            return counter

    original_get = metrics_mod.MetricsRegistry.get
    metrics_mod.MetricsRegistry.get = staticmethod(lambda: _MockRegistry())  # type: ignore[method-assign]
    try:
        cb()
    finally:
        metrics_mod.MetricsRegistry.get = original_get  # type: ignore[method-assign]

    assert disp.calls == []
    assert captured["no_loop"] == 1
