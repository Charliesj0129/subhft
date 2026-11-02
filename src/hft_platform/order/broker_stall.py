"""How long the broker order path has been silent, measured from our own calls.

THESHOW 2026-10-29..11-02: the order client's broker answered some calls in
3-30 s while the quote feed on the same connection stayed at p99 ~160 ms, and
calls sent while one was stuck came back only after it did (61 calls, median
+0.10 s after the stuck one). So while one call is waiting, every order we
send joins the same queue at the broker; the useful question is not "how slow
was the last answer" but "is anything waiting right now, and for how long".

``silence_ns`` answers that. A waiting call counts if it was sent AFTER the
newest call the broker has answered: the broker has not yet got past it. Its
silence is measured from the later of its send time and the last answer's
arrival, so a call queued behind a stuck one starts counting only when the
stuck one comes back. A waiting call sent BEFORE an answered one is not
silence: the broker answered something sent later, so the path is moving and
that one reply was lost or is slow on its own.

Calls older than ``max_age_ns`` are dropped on every read as well as on every
send, so a call whose completion is never delivered stops counting after the
bound even when nothing new is sent (back-pressure itself stops new sends).

Single-threaded by contract: every mutation happens on the event loop
(``OrderAdapter._run_blocking_call`` registers before it starts the worker
thread, and the completion callbacks arrive through ``call_soon_threadsafe``).
Readers on the loop see a consistent dict; ``silence_ns`` iterates without
copying (it runs once per order intent), and a reader off the loop that races a
mutation reads a partial answer instead of raising.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from structlog import get_logger

logger = get_logger("order.broker_stall")

# A broker call that has been waiting this long is no longer tracked. The SDK's
# own ``place_order`` timeout is 30 s (1.7.9), so a real call ends well before
# this; the bound exists so a callback that is never delivered (the loop closed
# under a worker thread) cannot hold a silence forever.
_DEFAULT_MAX_AGE_NS = 300 * 1_000_000_000
_DEFAULT_MAX_ENTRIES = 256


class BrokerStallTracker:
    """Outstanding broker order calls and the time of the last answer."""

    __slots__ = (
        "_outstanding",
        "_last_answer_ns",
        "_last_answered_send_ns",
        "_next_token",
        "_max_entries",
        "_max_age_ns",
        "_on_episode",
        "_clock",
    )

    def __init__(
        self,
        *,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
        max_age_ns: int = _DEFAULT_MAX_AGE_NS,
        on_episode: Callable[[int], None] | None = None,
        clock: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        # token -> send time. Insertion order is send order because tokens are
        # issued in time order on one thread, so the first matching value in
        # iteration order is the oldest.
        self._outstanding: dict[int, int] = {}
        self._last_answer_ns: int = 0
        # Send time of the newest call the broker has answered. Calls sent at
        # or before it and still waiting were overtaken, not stalled.
        self._last_answered_send_ns: int = 0
        self._next_token: int = 0
        self._max_entries = max(1, int(max_entries))
        self._max_age_ns = max(1, int(max_age_ns))
        # Called with the length of a silence that just ended (an answer
        # arrived while a call sent after the previous answer was waiting).
        self._on_episode = on_episode
        self._clock = clock

    def sent(self) -> int:
        """Register a call about to be handed to the broker; return its token."""
        now = self._clock()
        self._evict(now)
        token = self._next_token
        self._next_token += 1
        self._outstanding[token] = now
        return token

    def answered(self, token: int) -> None:
        """The broker answered ``token`` (a result, possibly after our timeout)."""
        sent_ns = self._outstanding.pop(token, None)
        now = self._clock()
        if sent_ns is not None:
            start = self._silence_start_ns(include=sent_ns)
            if start is not None and self._on_episode is not None:
                try:
                    self._on_episode(max(0, now - start))
                except Exception:  # noqa: BLE001 - observation must never disturb the order path
                    logger.debug("broker_silence_episode_observer_failed", exc_info=True)
            if sent_ns > self._last_answered_send_ns:
                self._last_answered_send_ns = sent_ns
        # An answer for a token already evicted still proves the broker moved.
        if now > self._last_answer_ns:
            self._last_answer_ns = now

    def abandoned(self, token: int) -> None:
        """The call ended without a broker answer (the SDK raised, or never sent).

        It stops waiting, but it is not an answer: it says nothing about the
        broker having moved, so ``_last_answer_ns`` is left where it was.
        """
        self._outstanding.pop(token, None)

    def silence_ns(self) -> int:
        """How long the broker has left a call it has not got past waiting."""
        now = self._clock()
        self._evict(now)
        start = self._silence_start_ns()
        if start is None:
            return 0
        return max(0, now - start)

    def inflight(self) -> int:
        """Number of broker calls handed off and not yet ended."""
        return len(self._outstanding)

    def _silence_start_ns(self, include: int | None = None) -> int | None:
        """When the current silence began, or None if nothing is waiting on it.

        ``include`` is the send time of a call being answered right now, so the
        episode it ends is measured as if it were still waiting.
        """
        floor = self._last_answered_send_ns
        oldest = include if include is not None and include > floor else None
        try:
            for sent_ns in self._outstanding.values():
                if sent_ns > floor:
                    if oldest is None or sent_ns < oldest:
                        oldest = sent_ns
                    break  # values are in send order; the first match is the oldest
        except RuntimeError:
            # Mutated mid-iteration: a reader off the loop thread, which the
            # contract excludes. Use what was found so far rather than raise
            # into the risk gate.
            pass
        if oldest is None:
            return None
        # A call queued behind a stuck one cannot be served before the stuck
        # one is answered; its own wait starts there.
        return max(oldest, self._last_answer_ns)

    def _evict(self, now: int) -> None:
        outstanding = self._outstanding
        cutoff = now - self._max_age_ns
        while outstanding:
            token, sent_ns = next(iter(outstanding.items()))
            if sent_ns >= cutoff and len(outstanding) < self._max_entries:
                break
            del outstanding[token]
