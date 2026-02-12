"""Pipeline health tracker (EC-5).

Aggregates degradation signals into unified state:
  HEALTHY(0) -> DEGRADED(1) -> CRITICAL(2) -> DATA_LOSS(3)

Emits gauge metric + structured logs on state transitions.
"""

import os
import threading
import time
from collections import deque
from enum import IntEnum
from typing import Any

from structlog import get_logger

logger = get_logger("recorder.health")


class PipelineState(IntEnum):
    HEALTHY = 0
    DEGRADED = 1
    CRITICAL = 2
    DATA_LOSS = 3


class PipelineHealthTracker:
    """Aggregates degradation signals from batcher, writer, and WAL.

    Events are stored in a sliding window deque with fixed memory.

    State rules:
      DEGRADED = WAL fallback or drops in window
      CRITICAL = CH down > critical_disconnect_s + WAL size warning
      DATA_LOSS = CH + WAL both failed (any data_loss event in window)
    """

    def __init__(self) -> None:
        self._window_s = float(os.getenv("HFT_HEALTH_WINDOW_S", "60"))
        self._critical_disconnect_s = float(os.getenv("HFT_HEALTH_CRITICAL_DISCONNECT_S", "60"))
        self._events: deque[tuple[float, str, dict[str, Any]]] = deque(maxlen=1000)
        self._lock = threading.Lock()
        self._state = PipelineState.HEALTHY
        self._last_ch_ok_ts = time.monotonic()
        self._ch_connected = True

        # Metrics
        self._metrics: Any = None
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            self._metrics = MetricsRegistry.get()
        except Exception:
            pass

    @property
    def state(self) -> PipelineState:
        return self._state

    def record_event(self, event_type: str, **kwargs: Any) -> None:
        """Record a degradation event.

        event_type: "wal_fallback", "drop", "ch_timeout", "ch_error",
                    "data_loss", "ch_connected", "ch_disconnected"
        """
        now = time.monotonic()
        with self._lock:
            self._events.append((now, event_type, kwargs))

            # Track CH connectivity
            if event_type == "ch_connected":
                self._ch_connected = True
                self._last_ch_ok_ts = now
            elif event_type in ("ch_timeout", "ch_error", "ch_disconnected"):
                self._ch_connected = False

            # Recompute state
            old_state = self._state
            self._state = self._compute_state(now)

            if self._state != old_state:
                logger.warning(
                    "Pipeline health state transition",
                    old_state=old_state.name,
                    new_state=self._state.name,
                    event_type=event_type,
                    **kwargs,
                )
                if self._metrics:
                    try:
                        self._metrics.pipeline_health_state.set(int(self._state))
                        self._metrics.pipeline_degradation_events_total.inc()
                    except Exception:
                        pass

    def _compute_state(self, now: float) -> PipelineState:
        """Compute current state from event window."""
        cutoff = now - self._window_s

        has_data_loss = False
        has_wal_fallback = False
        has_drops = False
        has_ch_warn = False

        for ts, etype, _meta in self._events:
            if ts < cutoff:
                continue
            if etype == "data_loss":
                has_data_loss = True
            elif etype == "wal_fallback":
                has_wal_fallback = True
            elif etype == "drop":
                has_drops = True
            elif etype in ("ch_timeout", "ch_error"):
                has_ch_warn = True

        if has_data_loss:
            return PipelineState.DATA_LOSS

        ch_down_duration = now - self._last_ch_ok_ts if not self._ch_connected else 0
        if ch_down_duration > self._critical_disconnect_s and (has_wal_fallback or has_ch_warn):
            return PipelineState.CRITICAL

        if has_wal_fallback or has_drops:
            return PipelineState.DEGRADED

        return PipelineState.HEALTHY

    def get_health(self) -> dict[str, Any]:
        """Return current health status for status API."""
        now = time.monotonic()
        cutoff = now - self._window_s

        with self._lock:
            window_events = [(ts, et, m) for ts, et, m in self._events if ts >= cutoff]
            event_counts: dict[str, int] = {}
            for _ts, et, _m in window_events:
                event_counts[et] = event_counts.get(et, 0) + 1

            return {
                "state": self._state.name,
                "state_value": int(self._state),
                "ch_connected": self._ch_connected,
                "window_s": self._window_s,
                "events_in_window": len(window_events),
                "event_counts": event_counts,
            }

    def prune(self) -> None:
        """Remove events outside the window (called periodically)."""
        now = time.monotonic()
        cutoff = now - self._window_s
        with self._lock:
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()
