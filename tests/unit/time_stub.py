"""A private ``time`` module for one module under test.

``patch("some.module.time.sleep")`` is a trap. ``module.time`` *is* the stdlib
``time`` module object, so that patch replaces ``time.sleep`` for the **whole
process**: every other test running in the same xdist worker that sleeps lands
in the mock, and any background thread an earlier test left running keeps
calling it.

The failures it produces look like defects in the code under test and pass in
isolation every time:

    2026-09-09  test_fubon_session_lifecycle.py::...::test_success_second_attempt
                E  AssertionError: Expected 'sleep' to be called once.
                                   Called 722 times.

    (earlier)   test_session_runtime.py::_run_loop -- a backoff ladder that
                reached 3600 s locally stopped at 1920 s under xdist, because
                strangers' short sleeps burned the loop's wake-up budget.

Rebinding the module's ``time`` *name* to a stub keeps the fake inside the
module under test. Everything except ``sleep`` passes through to the real
clock, so ``perf_counter_ns`` and friends still measure real time.

Usage, as a context manager::

    with patch.object(session_runtime, "time", sleepless_time()):
        ...

or as a decorator, which needs a fresh stub per test::

    @patch.object(session_runtime, "time", new_callable=sleepless_time)
    def test_x(self, stub_time):
        stub_time.sleep.assert_called_once_with(0.5)
"""

from __future__ import annotations

import time as _real_time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

__all__ = ["sleepless_time"]

_PASSTHROUGH = tuple(name for name in dir(_real_time) if not name.startswith("_") and name != "sleep")


def sleepless_time(**sleep_kwargs: Any) -> SimpleNamespace:
    """Return a stand-in for ``time`` whose ``sleep`` is a ``MagicMock``.

    ``sleep_kwargs`` are handed to the mock, so ``side_effect=`` works the way
    it did with the old spelling.
    """
    stub = SimpleNamespace(**{name: getattr(_real_time, name) for name in _PASSTHROUGH})
    stub.sleep = MagicMock(name="time.sleep", **sleep_kwargs)
    return stub
