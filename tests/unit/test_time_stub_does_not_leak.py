"""``patch("module.time.sleep")`` patches the process, not the module.

This is the trap ``tests/unit/time_stub.py`` exists to close. It is worth a
test of its own because the resulting failures name the wrong culprit: they
appear in whichever test asserts a call count, they blame the code under test,
and they never reproduce in isolation.

    2026-09-09 CI:
      FAILED test_fubon_session_lifecycle.py::...::test_success_second_attempt
      E  AssertionError: Expected 'sleep' to be called once. Called 722 times.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from hft_platform.feed_adapter.fubon import session_runtime
from tests.unit.time_stub import sleepless_time


class TestTheOldSpellingLeaks:
    """Pinned so nobody reintroduces it believing it is module-local."""

    def test_patching_module_time_sleep_replaces_the_process_wide_sleep(self) -> None:
        real_sleep = time.sleep

        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep") as leaked:
            # `session_runtime.time` IS the stdlib module, so this is `time.sleep`.
            assert time.sleep is leaked
            assert time.sleep is not real_sleep

        assert time.sleep is real_sleep

    def test_an_unrelated_sleep_lands_in_the_leaked_mock(self) -> None:
        """The 722-calls failure, in miniature."""
        with patch("hft_platform.feed_adapter.fubon.session_runtime.time.sleep") as leaked:
            time.sleep(0)  # stands in for any other test in the same worker

            assert leaked.call_count == 1


class TestTheStubStaysInsideItsModule:
    def test_the_process_wide_sleep_is_untouched(self) -> None:
        real_sleep = time.sleep

        with patch.object(session_runtime, "time", sleepless_time()) as stub:
            assert time.sleep is real_sleep
            assert stub.sleep is not real_sleep
            assert session_runtime.time.sleep is stub.sleep

        assert time.sleep is real_sleep
        assert session_runtime.time is time

    def test_an_unrelated_sleep_does_not_reach_the_stub(self) -> None:
        """The fix: a stranger's sleep can no longer inflate this count."""
        with patch.object(session_runtime, "time", sleepless_time()) as stub:
            time.sleep(0)

            assert stub.sleep.call_count == 0

    def test_the_rest_of_the_clock_still_works(self) -> None:
        """Only ``sleep`` is faked; latency math in the module must survive."""
        with patch.object(session_runtime, "time", sleepless_time()) as stub:
            first = stub.perf_counter_ns()
            second = stub.perf_counter_ns()

            assert isinstance(first, int)
            assert second >= first
