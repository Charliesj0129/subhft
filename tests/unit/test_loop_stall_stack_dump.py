"""A stall must name its own cause.

On 2026-09-15 at 06:33:46Z THESHOW's engine killed itself with
``event_loop_stall_kill stalled_s=66``, two minutes after a 30.6 s stall.
Only the engine scrape went ``up=0`` and ClickHouse stayed up, so it was
blocking work on the engine's own event loop -- but which call it was is
unknowable, because the watchdog recorded a duration and nothing else. The
earlier 30.6 s stall left no trace at all: it never reached the kill
threshold, so it was never logged.

    t=0s    loop stops beating
    t=15s   event_loop_stall_warning  + stack   <- survives a recovery
    t=60s   event_loop_stall_kill     + stack   <- survives the exit
            os._exit(70)

These tests pin that both dumps happen, that they name the loop's own thread,
that a recovery re-arms the early one, and that a failure to capture a stack
never stops the kill -- the kill is the safety property; the dump is evidence.
"""

from __future__ import annotations

import threading

import pytest

from hft_platform.services.loop_watchdog import LoopStallWatchdog

_WARN_HEADER = "[loop-stall-watchdog] event_loop_stall_warning"
_KILL_HEADER = "[loop-stall-watchdog] event_loop_stall_kill_stack"


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make(clock: _Clock, *, kill: float = 60.0, warn: float = 15.0, depth: int = 40):
    fired: list[float] = []
    wd = LoopStallWatchdog(
        stall_kill_s=kill,
        check_interval_s=1.0,
        warn_stall_s=warn,
        stack_depth=depth,
        clock=clock,
        on_stall=fired.append,
    )
    return wd, fired


class TestTheEarlyDump:
    def test_a_stall_short_of_the_kill_still_dumps(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The 30.6 s stall that recovered is exactly the missing evidence."""
        clock = _Clock()
        wd, fired = _make(clock)
        wd.beat()

        clock.advance(31.0)
        assert wd.check_once() is False  # short of the kill threshold
        assert fired == []

        err = capsys.readouterr().err
        assert _WARN_HEADER in err
        assert "stalled_s=31.0" in err

    def test_the_dump_does_not_repeat_within_one_stall(self, capsys: pytest.CaptureFixture[str]) -> None:
        clock = _Clock()
        wd, _ = _make(clock)
        wd.beat()

        clock.advance(20.0)
        wd.check_once()
        clock.advance(5.0)
        wd.check_once()

        # Count the stderr header: the dumped stack quotes the very source
        # line that emits it, so the bare token appears inside the dump too.
        assert capsys.readouterr().err.count(_WARN_HEADER) == 1

    def test_a_recovery_rearms_the_dump(self, capsys: pytest.CaptureFixture[str]) -> None:
        """A loop that stalls, recovers, and stalls again must dump twice."""
        clock = _Clock()
        wd, _ = _make(clock)
        wd.beat()

        clock.advance(20.0)
        wd.check_once()
        wd.beat()  # recovered
        clock.advance(20.0)
        wd.check_once()

        assert capsys.readouterr().err.count(_WARN_HEADER) == 2

    def test_no_dump_before_the_warn_threshold(self, capsys: pytest.CaptureFixture[str]) -> None:
        clock = _Clock()
        wd, _ = _make(clock)
        wd.beat()

        clock.advance(14.9)
        wd.check_once()

        assert _WARN_HEADER not in capsys.readouterr().err

    def test_a_non_positive_warn_threshold_disables_only_the_early_dump(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        clock = _Clock()
        wd, fired = _make(clock, warn=0.0)
        wd.beat()

        clock.advance(61.0)
        assert wd.check_once() is True
        assert len(fired) == 1

        err = capsys.readouterr().err
        assert _WARN_HEADER not in err
        assert _KILL_HEADER in err


class TestTheKillDump:
    def test_the_kill_carries_a_stack(self, capsys: pytest.CaptureFixture[str]) -> None:
        clock = _Clock()
        wd, fired = _make(clock)
        wd.beat()

        clock.advance(66.0)
        assert wd.check_once() is True
        assert len(fired) == 1

        err = capsys.readouterr().err
        assert _KILL_HEADER in err
        assert "stalled_s=66.0" in err

    def test_the_stack_is_dumped_before_the_process_exits(self) -> None:
        """Ordering is the whole point: os._exit() never returns."""
        clock = _Clock()
        order: list[str] = []
        wd = LoopStallWatchdog(
            stall_kill_s=60.0,
            check_interval_s=1.0,
            warn_stall_s=0.0,
            clock=clock,
            on_stall=lambda _e: order.append("exit"),
        )
        wd.capture_stacks = lambda: order.append("dump") or "stack"  # type: ignore[method-assign]
        wd.beat()

        clock.advance(61.0)
        wd.check_once()

        assert order == ["dump", "exit"]

    def test_a_capture_failure_never_blocks_the_kill(self) -> None:
        """The kill is the safety property; the dump is only evidence."""
        clock = _Clock()
        wd, fired = _make(clock, warn=0.0)

        def _boom() -> str:
            raise RuntimeError("frames unavailable")

        wd.capture_stacks = _boom  # type: ignore[method-assign]
        wd.beat()

        clock.advance(61.0)
        assert wd.check_once() is True
        assert len(fired) == 1


class TestCaptureStacks:
    def test_the_loop_thread_is_named_from_its_own_beat(self) -> None:
        clock = _Clock()
        wd, _ = _make(clock)
        wd.beat()  # beat() runs on this thread, so this thread is "the loop"

        dump = wd.capture_stacks()

        assert f"loop thread {threading.get_ident()}" in dump
        assert "test_the_loop_thread_is_named_from_its_own_beat" in dump

    def test_a_loop_that_never_beat_is_not_guessed_at(self) -> None:
        clock = _Clock()
        wd, _ = _make(clock)

        dump = wd.capture_stacks()

        assert "loop thread unknown (never beat)" in dump

    def test_other_threads_contribute_one_line_each(self) -> None:
        clock = _Clock()
        wd, _ = _make(clock)
        wd.beat()

        started = threading.Event()
        release = threading.Event()

        def _park() -> None:
            started.set()
            release.wait(5.0)

        helper = threading.Thread(target=_park, name="parked-helper", daemon=True)
        helper.start()
        try:
            assert started.wait(2.0)
            dump = wd.capture_stacks()
        finally:
            release.set()
            helper.join(timeout=2.0)

        assert f"thread {helper.ident} (parked-helper):" in dump
        # One line for the helper, not a full stack.
        helper_lines = [ln for ln in dump.split("\n") if f"thread {helper.ident} (parked-helper):" in ln]
        assert len(helper_lines) == 1

    def test_the_loop_stack_is_bounded(self) -> None:
        """A dump that does not fit in a log line is a dump nobody reads."""
        clock = _Clock()
        wd, _ = _make(clock, depth=2)

        def _deep(n: int) -> str:
            if n:
                return _deep(n - 1)
            wd.beat()
            return wd.capture_stacks()

        dump = _deep(30)

        loop_section = dump.split("\nthread ")[0]
        assert loop_section.count('  File "') <= 2

    def test_the_innermost_frames_are_kept_not_the_outermost(self) -> None:
        """The blocking call is at the bottom of the stack, not the top.

        ``traceback.format_stack(frame, limit=N)`` keeps the N frames nearest
        the frame it is given, which is the end that names what the thread is
        actually doing. Truncating from the other end would leave only the
        supervisor's entry point -- true of every stall, and useless.
        """
        clock = _Clock()
        wd, _ = _make(clock, depth=3)

        def _innermost() -> str:
            wd.beat()
            return wd.capture_stacks()

        def _middle() -> str:
            return _innermost()

        def _outermost() -> str:
            return _middle()

        loop_section = _outermost().split("\nthread ")[0]

        assert "_innermost" in loop_section
        assert "_outermost" not in loop_section
