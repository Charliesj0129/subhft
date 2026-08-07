"""Per-facade health checks for the QuoteConnectionPool.

Provides two public functions:

- ``get_healthy_feed_gap_s``: returns the maximum feed gap across CONNECTED
  facades.  Returns ``float("inf")`` when no facade is CONNECTED so callers
  can treat this as a HALT-triggering condition.

- ``check_facade_health``: examines each slot and drives state transitions
  (CONNECTED → DEGRADED → reconnect trigger, DISCONNECTED backoff trigger).
  RECOVERING slots are never touched because a reconnect is already in flight.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import structlog

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


def get_healthy_feed_gap_s(slots: list[FacadeSlot]) -> float:
    """Return the maximum feed gap (seconds) across all CONNECTED facades.

    A CONNECTED facade with a large gap indicates a stalled feed even though
    the connection FSM has not yet moved to DEGRADED.  Callers (e.g. StormGuard)
    should compare this value against a halt threshold.

    Returns ``float("inf")`` when:
    - ``slots`` is empty, or
    - no slot is in CONNECTED state.

    This ensures callers that trigger HALT on large gaps will do so safely
    when every connection is unhealthy.
    """
    max_gap: float = float("-inf")
    found_connected = False

    for slot in slots:
        if slot.state is not FacadeState.CONNECTED:
            continue
        found_connected = True
        gap = slot.feed_gap_s()
        if gap > max_gap:
            max_gap = gap

    if not found_connected:
        return float("inf")
    return max_gap


# Heartbeat interval for the "reconnect suppressed" log. Long enough that an
# overnight shutdown costs a few hundred lines rather than a few hundred
# thousand, short enough that suppression is still visibly *active* rather than
# a single line logged hours ago.
_SUPPRESS_LOG_INTERVAL_S = 300.0


def _should_log_suppression(slot: FacadeSlot, now: float) -> bool:
    """Return True at most once per ``_SUPPRESS_LOG_INTERVAL_S`` per slot."""
    last = slot._suppress_logged_mono
    if last != 0.0 and (now - last) < _SUPPRESS_LOG_INTERVAL_S:
        return False
    slot._suppress_logged_mono = now
    return True


def check_facade_health(
    slots: list[FacadeSlot],
    *,
    degraded_threshold_s: float = 3.0,
    reconnect_trigger_s: float = 10.0,
    schedule_fn: Callable[[str], None],
    suppress_reconnect: bool = False,
    suppress_gap_reconnect: bool = False,
) -> None:
    """Evaluate each slot's health and drive FSM transitions.

    State machine transitions
    -------------------------
    CONNECTED:
        gap > degraded_threshold_s → DEGRADED (sets degraded_since_mono)
    DEGRADED:
        gap < degraded_threshold_s → CONNECTED (feed recovered)
        gap > degraded_threshold_s AND degraded_since > reconnect_trigger_s
            → calls schedule_fn(conn_id)  (skipped when suppress_reconnect=True
            **or** suppress_gap_reconnect=True)
    DISCONNECTED:
        time since last_reconnect_mono > backoff_s() → calls schedule_fn(conn_id)
            (skipped when suppress_reconnect=True)
        last_reconnect_mono == 0.0 (never reconnected) → calls schedule_fn(conn_id)
            (skipped when suppress_reconnect=True)
    RECOVERING:
        Not touched — a reconnect coroutine is already running.

    Parameters
    ----------
    slots:
        List of FacadeSlot objects to evaluate.
    degraded_threshold_s:
        Feed gap in seconds that triggers CONNECTED → DEGRADED transition.
    reconnect_trigger_s:
        Duration in DEGRADED state that triggers a reconnect schedule.
    schedule_fn:
        Callable that accepts a ``conn_id`` (str) and schedules a reconnect
        coroutine for that connection.  Must be non-blocking.
    suppress_reconnect:
        When True, state transitions (CONNECTED→DEGRADED, DEGRADED→CONNECTED)
        still occur for observability, but ``schedule_fn`` is never called for
        *any* state.  Use outside the reconnect window to prevent futile
        reconnect attempts.
    suppress_gap_reconnect:
        When True, only the DEGRADED (feed-gap driven) trigger is held back;
        a genuinely DISCONNECTED slot is still recovered.

        A feed gap is evidence of a broken connection **only while the session
        is open**.  Outside one there is by definition no data, so the trigger
        is guaranteed true and the remedy guaranteed ineffective: the reconnect
        succeeds, ``last_data_mono`` is refreshed, no tick arrives,
        ``degraded_threshold_s`` elapses, ``reconnect_trigger_s`` elapses, and
        the slot relogs — every ~13 s until the market itself supplies data.
        On 2026-08-07 the four pooled facades entered that loop the instant the
        pre-open lead opened the gate (four ``facade_reconnect_triggered`` in
        one tick at 14:45:00.652–.654) and cost 52 reconnect schedules, 35
        logins, 35 full 54,727-contract loads, 34 basket resubscribes and 9
        broker ``451`` rejections in the 20 minutes before the night open —
        on four connections that were logged in and fully subscribed the whole
        time.  The same shape ran at the 08:30 day pre-open (7 × ``451``).

        Callers pass ``suppress_gap_reconnect=not session_open`` and
        ``suppress_reconnect=not reconnect_window_open`` so the pre-open lead
        keeps doing the one job it is for — reviving a genuinely dead session
        before the bell — without dismantling live ones.
    """
    now = time.monotonic()

    for slot in slots:
        state = slot.state

        if state is FacadeState.RECOVERING:
            # Reconnect already in flight — do not interfere.
            continue

        if state is FacadeState.CONNECTED:
            gap = slot.feed_gap_s()
            if gap > degraded_threshold_s:
                slot.state = FacadeState.DEGRADED
                slot.degraded_since_mono = now
                log.warning(
                    "facade_degraded",
                    conn_id=slot.conn_id,
                    feed_gap_s=round(gap, 3),
                )

        elif state is FacadeState.DEGRADED:
            gap = slot.feed_gap_s()
            if gap < degraded_threshold_s:
                # Feed recovered.
                slot.state = FacadeState.CONNECTED
                slot.degraded_since_mono = None
                slot._suppress_logged_mono = 0.0
                log.info("facade_recovered", conn_id=slot.conn_id, feed_gap_s=round(gap, 3))
            else:
                # Still degraded — check whether we have been degraded long enough
                # to trigger a reconnect.
                degraded_since = slot.degraded_since_mono
                if degraded_since is not None:
                    degraded_duration = now - degraded_since
                    if degraded_duration > reconnect_trigger_s:
                        if suppress_reconnect or suppress_gap_reconnect:
                            if _should_log_suppression(slot, now):
                                log.info(
                                    "facade_reconnect_suppressed",
                                    conn_id=slot.conn_id,
                                    degraded_duration_s=round(degraded_duration, 3),
                                    # Which gate held, so the pre-open window is
                                    # distinguishable from the closed market.
                                    reason=("outside_reconnect_window" if suppress_reconnect else "session_closed"),
                                )
                        else:
                            slot._suppress_logged_mono = 0.0
                            log.warning(
                                "facade_reconnect_triggered",
                                conn_id=slot.conn_id,
                                degraded_duration_s=round(degraded_duration, 3),
                            )
                            schedule_fn(slot.conn_id)

        elif state is FacadeState.DISCONNECTED:
            if suppress_reconnect:
                continue
            last_reconnect = slot.last_reconnect_mono
            if last_reconnect == 0.0:
                # Never attempted a reconnect yet — trigger immediately.
                log.info("facade_initial_reconnect", conn_id=slot.conn_id)
                schedule_fn(slot.conn_id)
            else:
                elapsed = now - last_reconnect
                backoff = slot.backoff_s()
                if elapsed >= backoff:
                    log.info(
                        "facade_backoff_elapsed",
                        conn_id=slot.conn_id,
                        elapsed_s=round(elapsed, 3),
                        backoff_s=backoff,
                    )
                    schedule_fn(slot.conn_id)
