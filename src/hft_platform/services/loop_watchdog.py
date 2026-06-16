"""In-process event-loop stall watchdog.

A spinning or blocked asyncio event loop stops servicing health checks,
metrics, market-data ingestion, risk gates, and order cancels — yet the
*process* stays alive. Docker's ``restart: always`` only fires on process
*exit*, so a starved-but-alive engine is never recovered automatically.

The 2026-06-15 THESHOW incident hung the live engine for ~18h for exactly
this reason: a Shioaji ``451 Too Many Connections`` reconnect path pegged one
core (CPU spin) and starved the event loop; the file-based heartbeat watchdog
that should have caught it was non-functional on that host (the heartbeat file
was unwritable by the container uid, and the cron watchdog used ``systemctl``
on a docker-compose host). Nothing exited, so nothing restarted.

This watchdog closes that gap *regardless of where the spin originates*
(our code or the broker SDK's C extension): it runs on a dedicated OS thread
and force-exits the process when the event loop has not "beaten" within
``stall_kill_s`` seconds. The container then restarts the engine in seconds.

Why a separate OS thread can still act while the loop is starved: a pure-Python
spin loop releases the GIL every ``sys.getswitchinterval`` (~5 ms), and
``threading.Event.wait`` / ``time.sleep`` always release it, so the watchdog
thread is still scheduled. ``os._exit`` terminates the process via libc and
needs no cooperation from the starved main thread.

A frozen trading engine is strictly more dangerous than a restarting one — it
cannot cancel resting orders, honor a risk HALT, or record fills — so
self-termination on a *sustained* stall is the safe failure mode. The recorder
WAL and gateway dedup are crash-safe (fsync+rename, idempotent replay), so a
hard exit is recoverable.

Configuration (read by the engine when constructing the watchdog):
  - ``HFT_LOOP_STALL_KILL_S``  stall threshold in seconds (default 60;
    ``<= 0`` disables the watchdog entirely).
  - ``HFT_LOOP_STALL_CHECK_S`` poll interval in seconds (default 5).

The default 60 s threshold is ~5 orders of magnitude above normal loop lag
(sub-millisecond) and well above any legitimate transient blocking, so it
never trips on GC pauses or ordinary jitter.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections.abc import Callable

import structlog

logger = structlog.get_logger("service.loop_watchdog")

# Distinct, non-zero exit code so an operator (and `restart: always`) can tell a
# stall-kill apart from a normal shutdown or an unrelated crash.
STALL_KILL_EXIT_CODE = 70


def _hard_exit(code: int) -> None:  # pragma: no cover - terminates the process
    os._exit(code)


class LoopStallWatchdog:
    """Force-exit the process when the event loop stops beating.

    The event loop calls :meth:`beat` on every supervisor tick (1 Hz). A
    dedicated daemon thread polls :meth:`check_once`; when the time since the
    last beat reaches ``stall_kill_s`` it dispatches ``on_stall`` exactly once.
    The production ``on_stall`` hard-exits the process; tests inject a recorder.
    """

    def __init__(
        self,
        *,
        stall_kill_s: float,
        check_interval_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
        on_stall: Callable[[float], None] | None = None,
    ) -> None:
        self._stall_kill_s = float(stall_kill_s)
        self._check_interval_s = max(0.1, float(check_interval_s))
        self._clock = clock
        self._on_stall: Callable[[float], None] = on_stall or (lambda _elapsed: _hard_exit(STALL_KILL_EXIT_CODE))
        self._enabled = self._stall_kill_s > 0.0
        self._last_beat = self._clock()
        self._fired = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def beat(self) -> None:
        """Record event-loop liveness. Cheap; called from the loop each tick."""
        self._last_beat = self._clock()

    def stale_for(self) -> float:
        """Seconds since the last beat (never negative)."""
        return max(0.0, self._clock() - self._last_beat)

    def check_once(self) -> bool:
        """Return ``True`` if the loop has stalled past the threshold.

        Dispatches ``on_stall(elapsed)`` exactly once across the watchdog's
        lifetime. Always returns the current stalled state so callers/tests can
        observe persistence after firing.
        """
        if not self._enabled:
            return False
        elapsed = self.stale_for()
        if elapsed < self._stall_kill_s:
            return False
        if not self._fired:
            self._fired = True
            # Emit to BOTH structlog and raw stderr: the structlog/logging path
            # may itself be starved or blocked behind the spin, but the direct
            # write is guaranteed to reach the container log before we exit.
            try:
                logger.critical(
                    "event_loop_stall_kill",
                    stalled_s=round(elapsed, 1),
                    threshold_s=self._stall_kill_s,
                    hint="event loop starved; force-exiting so the container restarts the engine",
                )
            except Exception:  # pragma: no cover - logging must never block the kill
                pass
            try:
                sys.stderr.write(
                    f"[loop-stall-watchdog] event loop starved {elapsed:.1f}s "
                    f">= {self._stall_kill_s:.1f}s threshold; force-exiting "
                    f"(exit {STALL_KILL_EXIT_CODE}) for container restart\n"
                )
                sys.stderr.flush()
            except Exception:  # pragma: no cover
                pass
            self._on_stall(elapsed)
        return True

    def start(self) -> None:
        """Spawn the watchdog thread. No-op when disabled or already running."""
        if not self._enabled or self._thread is not None:
            return
        self._last_beat = self._clock()
        self._fired = False
        self._stop.clear()
        t = threading.Thread(
            target=self._run,
            name="loop-stall-watchdog",
            daemon=True,
        )
        self._thread = t
        t.start()
        logger.info(
            "loop_stall_watchdog_started",
            stall_kill_s=self._stall_kill_s,
            check_interval_s=self._check_interval_s,
        )

    def stop(self) -> None:
        """Signal the watchdog thread to exit and join it. Safe to call twice."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=self._check_interval_s + 1.0)
            self._thread = None

    def _run(self) -> None:  # pragma: no cover - thread loop; logic tested via check_once
        # ``Event.wait`` returns True only when stop() is set, so the loop exits
        # promptly on shutdown and otherwise polls every check_interval_s.
        while not self._stop.wait(self._check_interval_s):
            try:
                self.check_once()
            except Exception:
                # The watchdog must never die silently; if its own check raised,
                # treat the loop as unobservable and trigger the safe failure.
                try:
                    self._on_stall(self.stale_for())
                except Exception:
                    pass
                return
