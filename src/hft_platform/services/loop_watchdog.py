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

Killing the process is necessary but, on its own, not diagnostic. On
2026-09-15 at 06:33:46Z THESHOW's engine killed itself with
``event_loop_stall_kill stalled_s=66`` after a 30.6 s stall two minutes
earlier. Only the engine scrape went ``up=0`` and ClickHouse stayed up, so it
was blocking work on the engine's own loop -- but *which* call is unknowable,
because the watchdog recorded the duration and nothing else. The 30.6 s stall
left no trace at all: it never reached the kill threshold, so it was never
logged.

So the watchdog also dumps the stalled loop thread's stack, at
``warn_stall_s`` and again at the kill::

    t=0s    loop stops beating
    t=15s   event_loop_stall_warning  + stack   <- new; survives a recovery
    t=60s   event_loop_stall_kill     + stack   <- new; survives the exit
            os._exit(70) -> container restarts

The dump runs on the watchdog thread, reading ``sys._current_frames()``, so it
adds nothing to the hot path and needs no cooperation from the starved loop.
It names the loop thread exactly, because :meth:`beat` records the ident of
whichever thread calls it.

Configuration (read by the engine when constructing the watchdog):
  - ``HFT_LOOP_STALL_KILL_S``  stall threshold in seconds (default 60;
    ``<= 0`` disables the watchdog entirely).
  - ``HFT_LOOP_STALL_CHECK_S`` poll interval in seconds (default 5).
  - ``HFT_LOOP_STALL_WARN_S``  stack-dump threshold in seconds (default 15;
    ``<= 0`` disables the early dump, leaving only the one at the kill).

The default 60 s threshold is ~5 orders of magnitude above normal loop lag
(sub-millisecond) and well above any legitimate transient blocking, so it
never trips on GC pauses or ordinary jitter.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from collections.abc import Callable
from types import FrameType

import structlog

logger = structlog.get_logger("service.loop_watchdog")

# Distinct, non-zero exit code so an operator (and `restart: always`) can tell a
# stall-kill apart from a normal shutdown or an unrelated crash.
STALL_KILL_EXIT_CODE = 70

#: Innermost frames kept per stalled thread. Deep enough to cross an SDK
#: boundary and name the blocking call, short enough that the dump still fits
#: in one container-log line.
DEFAULT_STACK_DEPTH = 40


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
        warn_stall_s: float = 15.0,
        stack_depth: int = DEFAULT_STACK_DEPTH,
        clock: Callable[[], float] = time.monotonic,
        on_stall: Callable[[float], None] | None = None,
    ) -> None:
        self._stall_kill_s = float(stall_kill_s)
        self._check_interval_s = max(0.1, float(check_interval_s))
        self._warn_stall_s = float(warn_stall_s)
        self._stack_depth = max(1, int(stack_depth))
        self._clock = clock
        self._on_stall: Callable[[float], None] = on_stall or (lambda _elapsed: _hard_exit(STALL_KILL_EXIT_CODE))
        self._enabled = self._stall_kill_s > 0.0
        self._last_beat = self._clock()
        self._fired = False
        self._warned = False
        # Set by beat(), so the dump names the loop's own thread rather than
        # guessing at the main thread. None until the loop has beaten once.
        self._loop_thread_ident: int | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def beat(self) -> None:
        """Record event-loop liveness. Cheap; called from the loop each tick.

        Also re-arms the warning dump, so a loop that stalls, is dumped, and
        then recovers gets dumped again on its next stall instead of once per
        process lifetime.
        """
        self._last_beat = self._clock()
        self._loop_thread_ident = threading.get_ident()
        self._warned = False

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
        if self._warn_stall_s > 0.0 and elapsed >= self._warn_stall_s and not self._warned:
            # Fires even when the loop later recovers: a 30 s stall that never
            # reaches the kill threshold is exactly the evidence that was
            # missing on 2026-09-15.
            self._warned = True
            self._emit_stack_dump("event_loop_stall_warning", elapsed)
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
            self._emit_stack_dump("event_loop_stall_kill_stack", elapsed)
            self._on_stall(elapsed)
        return True

    def capture_stacks(self) -> str:
        """Render the stalled loop thread's stack, plus a line per other thread.

        Reads ``sys._current_frames()`` from the watchdog thread, so it costs
        the starved loop nothing and does not need it to cooperate. The loop
        thread is reported in full depth because it holds the blocking call;
        every other thread contributes only its innermost frame, which keeps
        the dump bounded while still showing an SDK thread that is spinning.
        """
        try:
            frames = sys._current_frames()
        except Exception as exc:  # pragma: no cover - defensive
            return f"<stack unavailable: {exc}>"
        names = {t.ident: t.name for t in threading.enumerate() if t.ident is not None}
        loop_ident = self._loop_thread_ident
        parts: list[str] = []
        if loop_ident is not None and loop_ident in frames:
            parts.append(f"loop thread {loop_ident} ({names.get(loop_ident, '?')}):")
            parts.append(self._format_frame(frames[loop_ident], self._stack_depth))
        elif loop_ident is None:
            # The loop never beat, so there is nothing to single out. Dump every
            # thread shallowly rather than claiming to know which one is stuck.
            parts.append("loop thread unknown (never beat); innermost frame per thread:")
        else:
            parts.append(f"loop thread {loop_ident} has no frame (exited?); innermost frame per thread:")
        for ident, frame in frames.items():
            if ident == loop_ident:
                continue
            parts.append(f"thread {ident} ({names.get(ident, '?')}): {self._format_frame(frame, 1).strip()}")
        return "\n".join(parts)

    @staticmethod
    def _format_frame(frame: FrameType, depth: int) -> str:
        try:
            return "".join(traceback.format_stack(frame, limit=depth))
        except Exception as exc:  # pragma: no cover - defensive
            return f"<frame unavailable: {exc}>"

    def _emit_stack_dump(self, event: str, elapsed: float) -> None:
        """Write the dump to structlog AND raw stderr.

        Both, for the same reason the kill message uses both: a dump that only
        reaches a starved logging path is a dump that does not exist when it
        matters.
        """
        try:
            stacks = self.capture_stacks()
        except Exception as exc:  # pragma: no cover - the dump must never kill the watchdog
            stacks = f"<capture failed: {exc}>"
        try:
            logger.warning(
                event,
                stalled_s=round(elapsed, 1),
                warn_s=self._warn_stall_s,
                threshold_s=self._stall_kill_s,
                stacks=stacks,
            )
        except Exception:  # pragma: no cover
            pass
        try:
            sys.stderr.write(f"[loop-stall-watchdog] {event} stalled_s={elapsed:.1f}\n{stacks}\n")
            sys.stderr.flush()
        except Exception:  # pragma: no cover
            pass

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
