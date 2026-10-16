"""Condition waits for tests: poll a predicate against a deadline instead of sleeping a fixed time.

A fixed ``time.sleep(0.3)`` is both too long (every run pays it) and too short (a loaded CI runner needs more). These
helpers return as soon as the condition holds and fail with a message naming it when the deadline passes.

    from tests.waiting import wait_until, await_until

    wait_until(lambda: gauge.get() > 0, message="gauge was never set")
    await await_until(lambda: sink.write.called, message="the sink never received a write")
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

__all__ = ["DEFAULT_TIMEOUT_S", "POLL_S", "await_until", "wait_until"]

DEFAULT_TIMEOUT_S = 5.0
# Short enough that a satisfied condition costs almost nothing; long enough not to spin a core.
POLL_S = 0.005


def wait_until(
    predicate: Callable[[], object],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = POLL_S,
    message: str = "condition",
) -> None:
    """Block until ``predicate()`` is truthy; raise ``AssertionError`` if ``timeout_s`` passes first.

    The predicate is checked once more after the deadline so a condition that became true during the last poll is not
    reported as a timeout. An exception from the predicate propagates (a broken probe is not a timeout).
    """
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            if predicate():
                return
            raise AssertionError(f"timed out after {timeout_s}s waiting for: {message}")
        time.sleep(poll_s)


async def await_until(
    predicate: Callable[[], object],
    *,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poll_s: float = POLL_S,
    message: str = "condition",
) -> None:
    """``wait_until`` for coroutines: yields to the event loop between checks so the code under test can run."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            if predicate():
                return
            raise AssertionError(f"timed out after {timeout_s}s waiting for: {message}")
        await asyncio.sleep(poll_s)
