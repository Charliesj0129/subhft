"""FacadeState FSM and FacadeSlot per-connection data structure.

These are the foundational data structures for per-connection isolation in the
QuoteConnectionPool. Each connection slot tracks its own state, symbols, and
failure counters independently so a failure on one connection cannot cascade
to others.
"""

from __future__ import annotations

import enum
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class FacadeState(enum.IntEnum):
    """Finite-state machine states for a single quote facade connection.

    Ordered by severity: CONNECTED (healthy) < DEGRADED < RECOVERING < DISCONNECTED.
    IntEnum allows direct numeric comparisons and sorted() across states.
    """

    CONNECTED = 0
    DEGRADED = 1
    RECOVERING = 2
    DISCONNECTED = 3

    def is_healthy(self) -> bool:
        """Return True only when the connection is fully operational."""
        return self is FacadeState.CONNECTED


class FacadeSlot:
    """Per-connection slot tracking state, symbols, and reconnect metadata.

    Uses __slots__ to reduce memory overhead and improve attribute access speed
    on the hot path (HFT Cache Law compliance).

    Attributes
    ----------
    conn_id:
        Stable string identifier for this connection (e.g. ``"conn-0"``).
    facade:
        The underlying ShioajiClientFacade instance bound to this slot.
    state:
        Current FSM state (FacadeState).
    symbols:
        Set of symbol codes currently subscribed on this connection.
    last_data_mono:
        ``time.monotonic()`` timestamp of the most recent market data callback.
        Initialised to the creation time so feed_gap_s() is well-defined immediately.
    last_reconnect_mono:
        ``time.monotonic()`` timestamp of the most recent reconnect attempt.
    reconnect_failures:
        Consecutive reconnect failure count. Reset to 0 on successful reconnect.
    degraded_since_mono:
        ``time.monotonic()`` timestamp when the slot first entered DEGRADED state,
        or ``None`` if not currently degraded.
    """

    __slots__ = (
        "conn_id",
        "facade",
        "state",
        "symbols",
        "last_data_mono",
        "last_reconnect_mono",
        "reconnect_failures",
        "degraded_since_mono",
        "_pending_warmup_reset",
        "_lock",
    )

    def __init__(self, conn_id: str, facade: Any) -> None:
        self.conn_id: str = conn_id
        self.facade: Any = facade
        # Start in RECOVERING — transitions to CONNECTED after first data arrives
        # via subscribe_all(). Prevents premature DEGRADED transitions during startup.
        self.state: FacadeState = FacadeState.RECOVERING
        self.symbols: set[str] = set()
        self.last_data_mono: float = time.monotonic()
        self.last_reconnect_mono: float = time.monotonic()
        self.reconnect_failures: int = 0
        self.degraded_since_mono: float | None = None
        self._pending_warmup_reset: bool = False
        # Guards compound state transitions (state + reconnect_failures +
        # degraded_since_mono + _pending_warmup_reset) across the event-loop
        # supervisor and the daemon reconnect threads spawned in
        # QuoteConnectionPool._schedule_reconnect. Without this lock the
        # `reconnect_failures += 1` RMW on the daemon thread can lose updates
        # visible to the supervisor reading on the event loop (P1, 2026-04-24).
        self._lock: threading.Lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        """Per-slot lock for cross-thread state transitions."""
        return self._lock

    def record_reconnect_success(self) -> None:
        """Atomically transition to CONNECTED and reset failure counter."""
        with self._lock:
            self.state = FacadeState.CONNECTED
            self.reconnect_failures = 0
            self.last_data_mono = time.monotonic()
            self.degraded_since_mono = None
            self._pending_warmup_reset = True

    def record_reconnect_failure(self) -> None:
        """Atomically increment failure counter and mark DISCONNECTED."""
        with self._lock:
            self.reconnect_failures += 1
            self.state = FacadeState.DISCONNECTED

    def begin_reconnect(self) -> bool:
        """Atomically transition into RECOVERING if not already reconnecting.

        Returns True when the caller acquired the reconnect slot and should
        proceed to perform the reconnect; False when another thread is
        already reconnecting this slot.
        """
        with self._lock:
            if self.state == FacadeState.RECOVERING:
                return False
            self.state = FacadeState.RECOVERING
            self.last_reconnect_mono = time.monotonic()
            return True

    def snapshot(self) -> tuple[FacadeState, int, float | None]:
        """Return a consistent (state, reconnect_failures, degraded_since_mono) tuple."""
        with self._lock:
            return self.state, self.reconnect_failures, self.degraded_since_mono

    def feed_gap_s(self) -> float:
        """Return elapsed seconds since the last market data callback.

        A large value indicates a feed stall and may trigger DEGRADED promotion.
        """
        return time.monotonic() - self.last_data_mono

    def backoff_s(self) -> float:
        """Return exponential backoff delay in seconds, capped at 120 seconds.

        Formula: ``min(120.0, 5.0 * 2 ** reconnect_failures)``

        Examples
        --------
        - 0 failures → 5 s
        - 1 failure  → 10 s
        - 2 failures → 20 s
        - 5 failures → 120 s (capped from 160 s)
        """
        return min(120.0, 5.0 * (2**self.reconnect_failures))
