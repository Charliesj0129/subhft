"""Tests for the in-process event-loop stall watchdog.

The watchdog force-exits the process when the asyncio event loop stops
ticking (a spin/starvation) so the container's ``restart: always`` policy
recovers the engine in seconds instead of hanging indefinitely — the
2026-06-15 THESHOW incident (Shioaji 451 reconnect CPU-spin) hung the live
engine ~18h precisely because a starved-but-alive process never exits.
"""

from __future__ import annotations

from hft_platform.services.heartbeat import heartbeat_writable
from hft_platform.services.loop_watchdog import LoopStallWatchdog


class _Clock:
    """Deterministic, manually-advanced monotonic clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class TestLoopStallWatchdog:
    def _make(self, clock: _Clock, *, threshold: float = 60.0):
        fired: list[float] = []
        wd = LoopStallWatchdog(
            stall_kill_s=threshold,
            check_interval_s=1.0,
            clock=clock,
            on_stall=fired.append,
        )
        return wd, fired

    def test_no_kill_when_beats_are_fresh(self) -> None:
        clock = _Clock()
        wd, fired = self._make(clock)
        for _ in range(10):
            clock.advance(1.0)
            wd.beat()
            assert wd.check_once() is False
        assert fired == []

    def test_kills_when_loop_stalls_past_threshold(self) -> None:
        clock = _Clock()
        wd, fired = self._make(clock, threshold=60.0)
        wd.beat()
        clock.advance(59.0)
        assert wd.check_once() is False
        assert fired == []
        clock.advance(2.0)  # now 61s since last beat
        assert wd.check_once() is True
        assert len(fired) == 1
        assert fired[0] >= 60.0

    def test_fires_only_once(self) -> None:
        clock = _Clock()
        wd, fired = self._make(clock, threshold=10.0)
        wd.beat()
        clock.advance(100.0)
        assert wd.check_once() is True
        assert wd.check_once() is True  # still stalled...
        assert len(fired) == 1  # ...but only one kill is dispatched

    def test_beat_resets_stale_timer(self) -> None:
        clock = _Clock()
        wd, fired = self._make(clock, threshold=30.0)
        wd.beat()
        clock.advance(29.0)
        wd.beat()  # liveness restored just in time
        clock.advance(29.0)
        assert wd.check_once() is False
        assert fired == []

    def test_disabled_when_threshold_non_positive(self) -> None:
        clock = _Clock()
        fired: list[float] = []
        wd = LoopStallWatchdog(stall_kill_s=0.0, clock=clock, on_stall=fired.append)
        assert wd.enabled is False
        wd.start()  # no-op when disabled
        clock.advance(10_000.0)
        assert wd.check_once() is False
        assert fired == []

    def test_stale_for_reports_elapsed_since_last_beat(self) -> None:
        clock = _Clock()
        wd, _ = self._make(clock)
        wd.beat()
        clock.advance(12.5)
        assert wd.stale_for() == 12.5

    def test_start_is_idempotent_and_stop_is_safe(self) -> None:
        clock = _Clock()
        wd, _ = self._make(clock, threshold=60.0)
        wd.start()
        wd.start()  # second start must not spawn a second thread or raise
        wd.stop()
        wd.stop()  # stop after stop is safe


class TestHeartbeatWritable:
    def test_writable_true_for_existing_writable_dir(self, tmp_path) -> None:
        ok, reason = heartbeat_writable(str(tmp_path / "heartbeat"))
        assert ok is True
        assert reason is None

    def test_writable_false_when_dir_missing_or_unwritable(self, tmp_path) -> None:
        # Parent directory does not exist -> not writable.
        ok, reason = heartbeat_writable(str(tmp_path / "nope" / "deeper" / "heartbeat"))
        assert ok is False
        assert reason  # a human-readable reason is returned

    def test_writable_does_not_leave_probe_file(self, tmp_path) -> None:
        path = tmp_path / "heartbeat"
        heartbeat_writable(str(path))
        # The probe must not clobber/leave a stale heartbeat that a real write
        # would otherwise own; the directory probe is the contract.
        assert not (tmp_path / ".hb_probe").exists()
