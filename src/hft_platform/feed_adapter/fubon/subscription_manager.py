"""Subscription orchestration for Fubon quote feeds.

Wraps ``FubonQuoteRuntime`` with higher-level basket subscription,
resubscribe-with-cooldown, and execution callback storage.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from structlog import get_logger

from .quote_runtime import FubonQuoteRuntime

logger = get_logger("feed_adapter.fubon.subscription_manager")

# Minimum seconds between consecutive resubscribe calls.
_RESUBSCRIBE_COOLDOWN_S: float = 10.0

# TAIFEX futures day session, Taipei time: 08:45 up to 13:45. TAIFEX runs no all-day
# session, so the platform sets Fubon's afterHours flag itself from the clock. Holidays
# are not consulted: a closed day simply gets the flag for its hour.
_TAIPEI = ZoneInfo("Asia/Taipei")
_DAY_OPEN = dt.time(8, 45)
_DAY_CLOSE = dt.time(13, 45)

# Seconds between session-flag checks. A switch lands at most this long after an edge,
# and a failed switch is retried on the next check.
_SESSION_CHECK_S: float = 5.0


def taipei_now() -> dt.datetime:
    """Current time in Taipei: the default clock for the futopt session flag."""
    return dt.datetime.now(_TAIPEI)


def _taipei_local(ts: dt.datetime) -> dt.datetime:
    if ts.tzinfo is None or ts.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return ts.astimezone(_TAIPEI)


def futopt_after_hours(ts: dt.datetime) -> bool:
    """True when the night session (afterHours) is the live one at *ts*.

    The day session runs from 08:45 up to, not including, 13:45 Taipei time.
    """
    local = _taipei_local(ts).time()
    return not (_DAY_OPEN <= local < _DAY_CLOSE)


def next_session_boundary(ts: dt.datetime) -> dt.datetime:
    """First day-session edge (08:45 or 13:45, Taipei) strictly after *ts*."""
    local = _taipei_local(ts)
    edges = [
        dt.datetime.combine(local.date() + dt.timedelta(days=day), edge, tzinfo=_TAIPEI)
        for day in (0, 1)
        for edge in (_DAY_OPEN, _DAY_CLOSE)
    ]
    return min(edge for edge in edges if edge > local)


class FubonSubscriptionManager:
    """Orchestrates WebSocket subscriptions via FubonQuoteRuntime.

    Handles basket subscribe, resubscribe with cooldown enforcement,
    and execution callback wiring.
    """

    __slots__ = (
        "_quote_runtime",
        "_symbols",
        "_subscribed_codes",
        "_on_order_cb",
        "_on_deal_cb",
        "_last_resubscribe_ts",
        "MAX_SUBSCRIPTIONS",
        "log",
        "_session_clock",
        "_after_hours",
        "_session_lock",
        "_session_stop",
        "_session_thread",
    )

    def __init__(
        self,
        quote_runtime: FubonQuoteRuntime,
        symbols: list[dict[str, Any]] | list[str],
        *,
        max_subscriptions: int = 200,
        session_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._quote_runtime = quote_runtime
        self._symbols = symbols
        self._subscribed_codes: set[str] = set()
        self._on_order_cb: Callable[..., Any] | None = None
        self._on_deal_cb: Callable[..., Any] | None = None
        self._last_resubscribe_ts: float = 0.0
        self.MAX_SUBSCRIPTIONS: int = max_subscriptions
        self.log = logger
        # None: no session clock, so the flag stays False and no loop runs.
        self._session_clock = session_clock
        self._after_hours: bool = False
        self._session_lock = threading.Lock()
        self._session_stop = threading.Event()
        self._session_thread: threading.Thread | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Subscribe to all configured symbols.

        Registers *cb* as both tick and bidask callback on the underlying
        ``FubonQuoteRuntime``, subscribes extracted symbol codes, and starts
        the quote watchdog.
        """
        codes = self._extract_codes(self._symbols)

        if len(codes) > self.MAX_SUBSCRIPTIONS:
            self.log.warning(
                "Symbol count exceeds MAX_SUBSCRIPTIONS cap",
                requested=len(codes),
                cap=self.MAX_SUBSCRIPTIONS,
            )
            codes = codes[: self.MAX_SUBSCRIPTIONS]

        with self._session_lock:
            self._after_hours = self._flag_now()
            self._quote_runtime.register_quote_callbacks(cb, cb)
            self._quote_runtime.subscribe(codes, after_hours=self._after_hours)
            self._subscribed_codes = set(codes)
        self._quote_runtime.start_quote_watchdog()
        self._start_session_loop()

        self.log.info(
            "Fubon basket subscription completed",
            subscribed=len(self._subscribed_codes),
        )

    def resubscribe(self, *, force: bool = False) -> bool:
        """Stop and re-subscribe all symbols.

        Enforces a cooldown of ``_RESUBSCRIBE_COOLDOWN_S`` seconds between
        calls.  ``force=True`` skips the cooldown: a relogin replaced the session the
        codes were subscribed on, so the earlier subscription is gone too.  Returns
        ``True`` on success, ``False`` if cooldown has not elapsed, if there are no
        codes to subscribe, or if the manager is closed.
        """
        now = time.monotonic()
        elapsed = now - self._last_resubscribe_ts
        if not force and self._last_resubscribe_ts > 0 and elapsed < _RESUBSCRIBE_COOLDOWN_S:
            self.log.warning(
                "Resubscribe rejected: cooldown active",
                elapsed_s=round(elapsed, 3),
                cooldown_s=_RESUBSCRIBE_COOLDOWN_S,
            )
            return False

        codes = self._extract_codes(self._symbols)
        if not codes:
            self.log.warning("Resubscribe skipped: no symbols configured")
            return False

        with self._session_lock:
            if self._session_stop.is_set():
                self.log.warning("Resubscribe refused: manager closed")
                return False
            self._quote_runtime.stop()
            self._after_hours = self._flag_now()
            self._quote_runtime.subscribe(codes, after_hours=self._after_hours)
            self._subscribed_codes = set(codes)
        self._quote_runtime.start_quote_watchdog()
        self._last_resubscribe_ts = time.monotonic()

        self.log.info(
            "Fubon resubscribe completed",
            subscribed=len(self._subscribed_codes),
        )
        return True

    def sync_session(self) -> bool:
        """Switch the afterHours flag if the clock has crossed a day-session edge.

        Unsubscribes the current codes and subscribes them again with the new flag.
        Returns True when a switch happened. The flag is recorded only after the
        resubscribe succeeds, so a failed switch is retried on the next check.
        """
        with self._session_lock:
            if self._session_stop.is_set():
                return False
            flag = self._flag_now()
            codes = sorted(self._subscribed_codes)
            if flag == self._after_hours or not codes:
                return False
            self._quote_runtime.unsubscribe(codes)
            self._quote_runtime.subscribe(codes, after_hours=flag)
            self._after_hours = flag
        self.log.info("Fubon session flag switched", after_hours=flag, subscribed=len(codes))
        return True

    def close(self) -> None:
        """Stop the session-switch loop. ``sync_session`` subscribes nothing after this returns."""
        with self._session_lock:
            self._session_stop.set()

    def _flag_now(self) -> bool:
        if self._session_clock is None:
            return False
        return futopt_after_hours(self._session_clock())

    def _start_session_loop(self) -> None:
        if self._session_clock is None or self._session_thread is not None:
            return
        self._session_thread = threading.Thread(target=self._session_loop, name="fubon-session-flag", daemon=True)
        self._session_thread.start()

    def _session_loop(self) -> None:
        # Event.wait returns True once close() sets the stop event.
        while not self._session_stop.wait(_SESSION_CHECK_S):
            try:
                self.sync_session()
            except Exception:
                # The flag is unchanged after a failure, so the next check retries.
                self.log.exception("Fubon session flag switch failed; retrying")

    def set_execution_callbacks(
        self,
        on_order: Callable[..., Any],
        on_deal: Callable[..., Any],
    ) -> None:
        """Store execution callbacks for later wiring."""
        self._on_order_cb = on_order
        self._on_deal_cb = on_deal
        self.log.info("Fubon execution callbacks stored")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _extract_codes(
        symbols: list[dict[str, Any]] | list[str],
    ) -> list[str]:
        """Extract symbol code strings from config.

        Handles both ``list[str]`` (codes directly) and ``list[dict]``
        (dicts with a ``"code"`` key).
        """
        codes: list[str] = []
        for sym in symbols:
            if isinstance(sym, str):
                codes.append(sym)
            elif isinstance(sym, dict):
                code = sym.get("code")
                if code is not None:
                    codes.append(str(code))
        return codes

    @property
    def subscribed_codes(self) -> set[str]:
        """Currently subscribed symbol codes (read-only view)."""
        return set(self._subscribed_codes)
