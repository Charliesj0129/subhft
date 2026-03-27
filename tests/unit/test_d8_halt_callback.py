"""D8: StormGuard halt callback exceptions must be logged, not silently swallowed."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.fixture()
def storm_guard_with_failing_callback():
    """StormGuard whose halt callback raises RuntimeError."""
    async def _bad_callback():
        raise RuntimeError("callback boom")

    sg = StormGuard(on_halt_callback=_bad_callback)
    sg.metrics = MagicMock()
    return sg


def test_halt_callback_exception_is_logged(storm_guard_with_failing_callback, capsys):
    """When the halt callback coroutine raises, the error must appear in logs.

    Note: structlog uses PrintLoggerFactory (stdout), so we capture with capsys
    rather than caplog.
    """
    sg = storm_guard_with_failing_callback

    async def _run():
        sg.transition(StormGuardState.HALT, "test")
        # Give the scheduled task a chance to run and fail
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    captured = capsys.readouterr()
    assert "halt_callback_failed" in captured.out, (
        "Expected 'halt_callback_failed' in log output but got: " + captured.out
    )


def test_halt_callback_success_no_error_log(capsys):
    """When the halt callback coroutine succeeds, no error is logged."""
    async def _ok_callback():
        pass

    sg = StormGuard(on_halt_callback=_ok_callback)
    sg.metrics = MagicMock()

    async def _run():
        sg.transition(StormGuardState.HALT, "test")
        await asyncio.sleep(0.05)

    asyncio.run(_run())

    captured = capsys.readouterr()
    assert "halt_callback_failed" not in captured.out
