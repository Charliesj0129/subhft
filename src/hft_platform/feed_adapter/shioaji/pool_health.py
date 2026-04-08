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


def check_facade_health(
    slots: list[FacadeSlot],
    *,
    degraded_threshold_s: float = 3.0,
    reconnect_trigger_s: float = 10.0,
    schedule_fn: Callable[[str], None],
) -> None:
    """Evaluate each slot's health and drive FSM transitions.

    State machine transitions
    -------------------------
    CONNECTED:
        gap > degraded_threshold_s → DEGRADED (sets degraded_since_mono)
    DEGRADED:
        gap < degraded_threshold_s → CONNECTED (feed recovered)
        gap > degraded_threshold_s AND degraded_since > reconnect_trigger_s
            → calls schedule_fn(conn_id)
    DISCONNECTED:
        time since last_reconnect_mono > backoff_s() → calls schedule_fn(conn_id)
        last_reconnect_mono == 0.0 (never reconnected) → calls schedule_fn(conn_id)
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
                log.info("facade_recovered", conn_id=slot.conn_id, feed_gap_s=round(gap, 3))
            else:
                # Still degraded — check whether we have been degraded long enough
                # to trigger a reconnect.
                degraded_since = slot.degraded_since_mono
                if degraded_since is not None:
                    degraded_duration = now - degraded_since
                    if degraded_duration > reconnect_trigger_s:
                        log.warning(
                            "facade_reconnect_triggered",
                            conn_id=slot.conn_id,
                            degraded_duration_s=round(degraded_duration, 3),
                        )
                        schedule_fn(slot.conn_id)

        elif state is FacadeState.DISCONNECTED:
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
