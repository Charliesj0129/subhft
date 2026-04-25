from __future__ import annotations

import datetime as dt
import os
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from structlog import get_logger

from hft_platform.core import timebase

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

logger = get_logger("feed_adapter.quote_runtime")


@dataclass(frozen=True)
class QuotePendingState:
    """Immutable snapshot of the quote pending state at a point in time.

    Produced by QuoteEventHandler and applied atomically to the client.
    Using a frozen dataclass enforces immutability (Coding Style rule)
    and enables safe cross-thread reads without explicit locking.
    """

    pending: bool
    reason: str | None
    ts: float  # wall-clock epoch from timebase.now_s()


@dataclass(frozen=True)
class QuoteRuntimeSnapshot:
    pending_resubscribe: bool
    pending_reason: str | None
    pending_since: float
    callbacks_registered: bool


class QuoteEventHandler:
    """Centralises quote pending state transitions.

    The pending state tracks whether the quote feed is in a degraded/recovering
    state. Previously this logic was scattered across _mark_quote_pending(),
    _clear_quote_pending(), and _on_quote_event() in ShioajiClient. By
    centralising it here we can:
      1. Test state transitions in isolation.
      2. Enforce that only QuoteEventHandler writes pending state.
      3. Make the state machine explicit and auditable.

    ShioajiClient delegates to this class and applies the returned
    QuotePendingState atomically.

    Thread safety (P0-D2, 2026-04-24)
    ---------------------------------
    ``mark_pending``/``clear_pending`` are invoked from at least three
    threads: the Shioaji SDK event-callback thread (``on_quote_event``),
    the quote watchdog thread, and the TickDispatcher worker thread (via
    ``_process_tick`` -> ``_clear_quote_pending``). The (_reason, _ts)
    pair is a compound state — a reader observing ``reason is None and
    ts > 0`` is a torn read that the watchdog then misinterprets as a
    stuck pending (operators saw this as "stuck CH/Redis gauges").
    The internal ``_pending_lock`` ensures the two fields are written and
    sampled atomically. The returned ``QuotePendingState`` is an immutable
    frozen dataclass so callers can mirror it to ``ShioajiClient`` fields
    under the same lock without re-reading handler state.
    """

    __slots__ = ("_pending_reason", "_pending_ts", "_pending_lock")

    def __init__(self) -> None:
        self._pending_reason: str | None = None
        self._pending_ts: float = 0.0
        # Guards the (_pending_reason, _pending_ts) pair so readers never
        # observe a half-updated handler. Also used by QuoteRuntime to mirror
        # handler state onto ShioajiClient fields atomically — hence RLock:
        # QuoteRuntime.mark_pending acquires the lock and then calls
        # handler.mark_pending which would re-acquire it on a plain Lock.
        self._pending_lock: threading.RLock = threading.RLock()

    @property
    def pending_lock(self) -> threading.RLock:
        """Expose the internal lock so QuoteRuntime can extend the critical
        section across the handler update and the client-side mirror."""
        return self._pending_lock

    def mark_pending(self, reason: str, current_ts: float | None = None) -> QuotePendingState:
        """Transition to pending state.

        Returns a QuotePendingState delta for the caller to apply.
        Idempotent: repeated marks with the same reason return an unchanged state.
        """
        ts = current_ts if current_ts is not None else timebase.now_s()
        with self._pending_lock:
            if self._pending_reason == reason:
                # Already pending for the same reason — no-op
                return QuotePendingState(pending=True, reason=reason, ts=self._pending_ts)
            self._pending_reason = reason
            self._pending_ts = ts
            return QuotePendingState(pending=True, reason=reason, ts=ts)

    def clear_pending(self) -> QuotePendingState:
        """Transition out of pending state.

        Returns a QuotePendingState with pending=False for the caller to apply.
        """
        with self._pending_lock:
            self._pending_reason = None
            self._pending_ts = 0.0
        return QuotePendingState(pending=False, reason=None, ts=0.0)

    @property
    def is_pending(self) -> bool:
        # Single attribute read — GIL-atomic, no lock needed.
        return self._pending_reason is not None

    @property
    def current_reason(self) -> str | None:
        return self._pending_reason

    @property
    def pending_since(self) -> float:
        return self._pending_ts

    def snapshot(self) -> QuotePendingState:
        """Return a consistent snapshot of the (_reason, _ts) pair."""
        with self._pending_lock:
            if self._pending_reason is None:
                return QuotePendingState(pending=False, reason=None, ts=0.0)
            return QuotePendingState(pending=True, reason=self._pending_reason, ts=self._pending_ts)


class QuoteRuntime:
    """Manages quote callbacks, schema validation, and watchdog.

    Phase-2 decoupling: owns validate_quote_schema(), register_quote_callbacks(),
    and start_quote_watchdog() logic. ShioajiClient stubs delegate here.
    Phase-3 target (C3): own the quote event FSM via QuoteEventHandler,
    returning QuotePendingState deltas that ShioajiClient applies atomically.
    """

    __slots__ = (
        "_client",
        "_event_handler",
        # D1 (2026-04-25): per-symbol retry state with exponential backoff +
        # max-attempts cap. Replaces the pre-fix unbounded 60 s retry loop
        # that burned 22 TXO codes for 24 h+ in production.
        "_retry_attempts",
        "_retry_next_ts",
        "_permanently_failed",
        "_retry_state_lock",
    )

    # D1: backoff schedule in seconds. Beyond the schedule, the cap (3600 s)
    # repeats indefinitely until max-attempts trips.
    _RETRY_BACKOFF_SCHEDULE_S: tuple[float, ...] = (60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client
        self._event_handler = QuoteEventHandler()
        # D1: per-symbol retry state.
        self._retry_attempts: dict[str, int] = {}
        self._retry_next_ts: dict[str, float] = {}
        self._permanently_failed: set[str] = set()
        self._retry_state_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # D1: subscription retry state helpers
    # ------------------------------------------------------------------ #

    def _retry_max_attempts(self) -> int:
        return int(os.getenv("HFT_SUB_RETRY_MAX_ATTEMPTS", "10"))

    def _backoff_for_attempt(self, attempt_count: int) -> float:
        """Return the backoff (seconds) for the Nth failure (1-indexed)."""
        idx = max(0, attempt_count - 1)
        if idx >= len(self._RETRY_BACKOFF_SCHEDULE_S):
            return self._RETRY_BACKOFF_SCHEDULE_S[-1]
        return self._RETRY_BACKOFF_SCHEDULE_S[idx]

    def _should_attempt_subscription(self, code: str, now: float) -> tuple[bool, str]:
        """Return (allowed, reason) for whether ``code`` may be retried now.

        Reasons:
        - ``ok``: never seen, or backoff window elapsed → allow.
        - ``skip_backoff``: still inside backoff window.
        - ``skip_permanent``: code is in ``_permanently_failed`` and will
          never be retried again until restart.
        """
        with self._retry_state_lock:
            if code in self._permanently_failed:
                reason = "skip_permanent"
            else:
                next_ts = self._retry_next_ts.get(code, 0.0)
                if next_ts == 0.0 or now >= next_ts:
                    reason = "ok"
                else:
                    reason = "skip_backoff"
        # Metric outside lock to keep critical section minimal.
        self._bump_retry_metric(code, reason)
        return reason == "ok", reason

    def _record_subscription_failure(self, code: str, now: float) -> bool:
        """Record a failed subscribe; return True iff symbol just became permanent.

        Updates ``_retry_attempts`` and ``_retry_next_ts``. After
        ``HFT_SUB_RETRY_MAX_ATTEMPTS`` failures, moves to
        ``_permanently_failed`` and emits the permanent-failures metric.
        """
        max_attempts = self._retry_max_attempts()
        became_permanent = False
        attempts: int = 0
        with self._retry_state_lock:
            if code in self._permanently_failed:
                # Defensive — should not be retried after permanent.
                attempts = self._retry_attempts.get(code, max_attempts)
            else:
                attempts = self._retry_attempts.get(code, 0) + 1
                self._retry_attempts[code] = attempts
                self._retry_next_ts[code] = now + self._backoff_for_attempt(attempts)
                if attempts >= max_attempts:
                    self._permanently_failed.add(code)
                    became_permanent = True
        # Metric updates outside lock.
        self._set_attempts_gauge(code, attempts)
        if became_permanent:
            self._bump_permanent_metric(code)
            logger.warning(
                "subscription_permanently_failed",
                code=code,
                attempts=attempts,
                max_attempts=max_attempts,
            )
        return became_permanent

    def _record_subscription_success(self, code: str) -> None:
        """Reset all retry state for ``code`` after a successful subscribe."""
        with self._retry_state_lock:
            self._retry_attempts.pop(code, None)
            self._retry_next_ts.pop(code, None)
            self._permanently_failed.discard(code)
        self._set_attempts_gauge(code, 0)

    # ------------------------------------------------------------------ #
    # D1: metric helpers (defensive — metrics may be absent on test stubs)
    # ------------------------------------------------------------------ #

    def _bump_retry_metric(self, code: str, result: str) -> None:
        metrics = getattr(self._client, "metrics", None)
        counter = getattr(metrics, "feed_subscription_retry_total", None) if metrics else None
        if counter is None:
            return
        try:
            sym = metrics.cap_symbol(code) if hasattr(metrics, "cap_symbol") else code
            counter.labels(symbol=sym, result=result).inc()
        except Exception:  # noqa: BLE001
            pass

    def _bump_permanent_metric(self, code: str) -> None:
        metrics = getattr(self._client, "metrics", None)
        counter = getattr(metrics, "feed_subscription_permanent_failures_total", None) if metrics else None
        if counter is None:
            return
        try:
            sym = metrics.cap_symbol(code) if hasattr(metrics, "cap_symbol") else code
            counter.labels(symbol=sym).inc()
        except Exception:  # noqa: BLE001
            pass

    def _set_attempts_gauge(self, code: str, attempts: int) -> None:
        metrics = getattr(self._client, "metrics", None)
        gauge = getattr(metrics, "feed_subscription_retry_attempts", None) if metrics else None
        if gauge is None:
            return
        try:
            sym = metrics.cap_symbol(code) if hasattr(metrics, "cap_symbol") else code
            gauge.labels(symbol=sym).set(attempts)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # Pending state management via QuoteEventHandler
    # ------------------------------------------------------------------ #

    def mark_pending(self, reason: str) -> QuotePendingState:
        """Mark the quote feed as pending and return the new state delta.

        The handler mutation and the mirror to ShioajiClient fields run under
        a single critical section so concurrent readers of
        ``_pending_quote_resubscribe`` / ``_pending_quote_reason`` /
        ``_pending_quote_ts`` never see a torn triple (P0-D2).
        """
        # Compute ts outside the lock to minimise its duration; handler
        # takes the lock again internally but that's a cheap reentrant-free
        # sequence of ordinary attribute writes.
        with self._event_handler.pending_lock:
            delta = self._event_handler.mark_pending(reason)
            c = self._client
            c._pending_quote_resubscribe = delta.pending
            if delta.pending and c._pending_quote_ts == 0.0:
                c._pending_quote_ts = delta.ts
            c._pending_quote_reason = delta.reason
        return delta

    def clear_pending(self) -> QuotePendingState:
        """Clear the pending state and return the cleared state delta."""
        with self._event_handler.pending_lock:
            delta = self._event_handler.clear_pending()
            c = self._client
            c._pending_quote_resubscribe = False
            c._pending_quote_reason = None
            c._pending_quote_ts = 0.0
        return delta

    @property
    def is_pending(self) -> bool:
        return self._event_handler.is_pending

    # ------------------------------------------------------------------ #
    # Quote schema validation (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def validate_quote_schema(self, *args: Any, **kwargs: Any) -> tuple[bool, str]:
        """Best-effort schema guard for quote callbacks.

        CE2-11 intent: when quote_version is locked to v1, reject obvious v0-style
        callback payloads (topic string first arg) and malformed payloads.

        Extracted from ShioajiClient._validate_quote_schema().
        """
        c = self._client
        if not c._quote_schema_guard:
            return True, "disabled"
        if c._quote_version != "v1":
            return True, "not_v1"

        if not args and not kwargs:
            return False, "empty"

        if args:
            first = args[0]
            # v0-style callbacks typically begin with a topic string.
            if isinstance(first, str) and ("/" in first or ":" in first):
                return False, "topic_string_arg_v0_shape"

        payload = None
        if len(args) >= 2:
            payload = args[1]
        elif len(args) == 1:
            payload = args[0]
        else:
            for key in ("quote", "bidask", "tick", "msg", "data"):
                if key in kwargs:
                    payload = kwargs.get(key)
                    break
            if payload is None and kwargs:
                payload = next(iter(kwargs.values()))

        if payload is None:
            return False, "missing_payload"

        if isinstance(payload, dict):
            if any(k in payload for k in ("code", "Code", "bid_price", "ask_price", "close")):
                return True, "dict_payload"
            if c._quote_schema_guard_strict:
                return False, "dict_payload_unrecognized"
            return True, "dict_payload_relaxed"

        # v1 callbacks commonly pass typed objects with code/bid/ask/tick fields
        if hasattr(payload, "code") or hasattr(payload, "Code"):
            return True, "object_with_code"
        if hasattr(payload, "bid_price") or hasattr(payload, "ask_price"):
            return True, "object_bidask"
        if hasattr(payload, "close") or hasattr(payload, "volume"):
            return True, "object_tick"

        if c._quote_schema_guard_strict:
            return False, "object_unrecognized"
        return True, "object_relaxed"

    # ------------------------------------------------------------------ #
    # Quote callback registration (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def register_quote_callbacks(self) -> bool:
        """Register quote callbacks based on active quote version.

        Extracted from ShioajiClient._register_quote_callbacks().
        Uses a local import of dispatch_tick_cb to avoid circular imports
        at module load time.
        """
        c = self._client
        if not c.api:
            return False
        quote_api = c._quote_api()
        if quote_api is None:
            logger.warning("Quote API unavailable; callback registration deferred")
            return False

        # Local import avoids circular dependency at module load time;
        # by call time both modules are fully initialised.
        from hft_platform.feed_adapter.shioaji.client import dispatch_tick_cb

        supports_v1 = c._supports_quote_v1()
        supports_v0 = c._supports_quote_v0()
        logger.info(
            "Registering quote callbacks",
            quote_version=c._quote_version,
            quote_version_mode=c._quote_version_mode,
        )
        ok = True
        version = c._quote_version

        def _set_v1() -> bool:
            nonlocal ok
            try:
                quote_api.set_on_tick_stk_v1_callback(dispatch_tick_cb)
                quote_api.set_on_bidask_stk_v1_callback(dispatch_tick_cb)
                quote_api.set_on_tick_fop_v1_callback(dispatch_tick_cb)
                quote_api.set_on_bidask_fop_v1_callback(dispatch_tick_cb)
                return True
            except Exception as exc:
                logger.warning("Quote v1 callback registration failed", error=str(exc))
                ok = False
                return False

        def _set_v0() -> bool:
            nonlocal ok
            if not hasattr(quote_api, "set_on_tick_stk_callback"):
                logger.warning("Quote v0 callbacks not available on this Shioaji version")
                ok = False
                return False
            try:
                quote_api.set_on_tick_stk_callback(dispatch_tick_cb)
                quote_api.set_on_bidask_stk_callback(dispatch_tick_cb)
                if hasattr(quote_api, "set_on_tick_fop_callback"):
                    quote_api.set_on_tick_fop_callback(dispatch_tick_cb)
                if hasattr(quote_api, "set_on_bidask_fop_callback"):
                    quote_api.set_on_bidask_fop_callback(dispatch_tick_cb)
                return True
            except Exception as exc:
                logger.warning("Quote v0 callback registration failed", error=str(exc))
                ok = False
                return False

        if version == "v1":
            if supports_v1 and _set_v1():
                return ok
            allow_fallback = c._quote_version_mode == "auto" or (
                c._quote_version_mode == "v1" and not c._quote_version_strict
            )
            if allow_fallback and supports_v0:
                logger.warning("Falling back to quote v0 callbacks")
                with c._quote_version_lock:
                    c._quote_version = "v0"
                ok = _set_v0()
                if ok and c.metrics:
                    c.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                if not ok:
                    with c._quote_version_lock:
                        c._quote_version = "v1"
            else:
                if not supports_v1:
                    logger.warning("Quote v1 callbacks not available on this Shioaji version")
                if allow_fallback and not supports_v0:
                    logger.warning("Quote v0 callbacks not available; staying on v1")
                with c._quote_version_lock:
                    c._quote_version = "v1"
                ok = False
        else:
            if supports_v0:
                ok = _set_v0()
            else:
                logger.warning("Quote v0 callbacks not available on this Shioaji version")
                if supports_v1:
                    with c._quote_version_lock:
                        c._quote_version = "v1"
                ok = False

        return ok

    # ------------------------------------------------------------------ #
    # Quote watchdog (Phase-2: owned here, not in ShioajiClient)
    # ------------------------------------------------------------------ #

    def start_quote_watchdog(self) -> None:
        """Start background watchdog that detects quote feed stalls.

        Extracted from ShioajiClient._start_quote_watchdog().
        """
        c = self._client
        if c._quote_watchdog_running:
            return
        c._quote_watchdog_running = True
        c._set_thread_alive_metric("quote_watchdog", True)
        logger.info(
            "Starting quote watchdog",
            interval_s=c._quote_watchdog_interval_s,
            no_data_s=c._quote_no_data_s,
        )

        def _watch() -> None:
            try:
                while c.api and c.logged_in:
                    time.sleep(c._quote_watchdog_interval_s)
                    c._update_quote_pending_metrics()
                    last = c._last_quote_data_ts
                    if last <= 0:
                        continue
                    gap = timebase.now_s() - last
                    # Use relaxed threshold during market open grace period (C4)
                    threshold = c._quote_no_data_s
                    if c._is_market_open_grace_period():
                        threshold = max(threshold, c._market_open_grace_s)
                    if gap < threshold:
                        continue
                    if not c._allow_quote_recovery("watchdog_no_data"):
                        continue
                    c._mark_quote_pending("no_data")
                    downgrade_allowed = c._quote_version_mode == "auto" or (
                        c._quote_version_mode == "v1" and not c._quote_version_strict
                    )
                    if downgrade_allowed and c._quote_version == "v1" and c._supports_quote_v0():
                        logger.warning(
                            "No quote data; switching quote version",
                            gap_s=round(gap, 3),
                            to_version="v0",
                        )
                        with c._quote_version_lock:
                            c._quote_version = "v0"
                        if c.metrics:
                            c.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                            try:
                                c.metrics.quote_watchdog_recovery_attempts_total.labels(
                                    action="version_downgrade"
                                ).inc()
                            except Exception as exc:
                                logger.debug("operation_fallback", error=str(exc))
                                pass
                    else:
                        if downgrade_allowed and c._quote_version == "v1" and not c._supports_quote_v0():
                            logger.warning("Quote v0 callbacks unavailable; staying on v1")
                        logger.warning(
                            "No quote data; re-registering callbacks",
                            gap_s=round(gap, 3),
                            quote_version=c._quote_version,
                        )
                        if c.metrics:
                            try:
                                c.metrics.quote_watchdog_recovery_attempts_total.labels(
                                    action="callback_reregister"
                                ).inc()
                            except Exception as exc:
                                logger.debug("operation_fallback", error=str(exc))
                                pass
                    if c.tick_callback:
                        c._callbacks_registered = False
                        c._ensure_callbacks(c.tick_callback)
                        c._resubscribe_all()
                    c._last_quote_data_ts = timebase.now_s()
            except Exception as exc:
                logger.error("Quote watchdog thread crashed", error=str(exc))
            finally:
                c._quote_watchdog_running = False
                c._set_thread_alive_metric("quote_watchdog", False)

        c._quote_watchdog_thread = threading.Thread(
            target=_watch,
            name="shioaji-quote-watchdog",
            daemon=True,
        )
        c._quote_watchdog_thread.start()

    # ------------------------------------------------------------------ #
    # Quote recovery / retry lifecycle (Phase-5: moved from client)
    # ------------------------------------------------------------------ #

    def allow_quote_recovery(self, reason: str) -> bool:
        c = self._client
        if not c._quote_watchdog_skip_off_hours:
            return True
        if c._is_trading_hours():
            return True
        now = timebase.now_s()
        if now - c._last_quote_off_hours_log_ts >= c._quote_off_hours_log_interval_s:
            logger.info("Skipping quote recovery outside trading hours", reason=reason)
            c._last_quote_off_hours_log_ts = now
        if c.metrics and hasattr(c.metrics, "quote_watchdog_recovery_attempts_total"):
            try:
                c.metrics.quote_watchdog_recovery_attempts_total.labels(action="skip_off_hours").inc()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                pass
        return False

    def is_market_open_grace_period(self) -> bool:
        c = self._client
        if c._market_open_grace_s <= 0:
            return False

        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
        except ImportError:
            return False

        now = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)
        if not calendar.is_trading_day(now.date()):
            return False

        open_time = calendar.get_session_open(now.date())
        if open_time is None:
            return False

        elapsed = (now - open_time).total_seconds()
        in_grace = 0 <= elapsed <= c._market_open_grace_s
        if c.metrics and in_grace != c._market_open_grace_active:
            c.metrics.market_open_grace_active.set(1 if in_grace else 0)
        c._market_open_grace_active = in_grace
        return in_grace

    def start_callback_retry(self, cb: Callable[..., Any]) -> None:
        c = self._client
        if c._callbacks_retrying:
            return
        c._callbacks_retrying = True
        c._set_thread_alive_metric("callback_retry", True)
        logger.warning("Starting quote callback retry loop")

        def _retry_loop() -> None:
            interval = float(os.getenv("HFT_QUOTE_CB_RETRY_S", "5"))
            try:
                while c.api and not c._callbacks_registered:
                    ok = c._register_callbacks(cb)
                    if ok:
                        logger.info("Quote callbacks registered after retry")
                        break
                    logger.warning("Quote callback registration retrying", interval_s=interval)
                    time.sleep(interval)
            except Exception as exc:
                logger.error("Quote callback retry loop crashed", error=str(exc))
            finally:
                c._callbacks_retrying = False
                c._set_thread_alive_metric("callback_retry", False)

        c._callbacks_retry_thread = threading.Thread(
            target=_retry_loop,
            name="shioaji-callback-retry",
            daemon=True,
        )
        c._callbacks_retry_thread.start()

    def start_event_callback_retry(self) -> None:
        c = self._client
        if c._event_callback_retrying:
            return
        c._event_callback_retrying = True
        c._set_thread_alive_metric("event_callback_retry", True)
        logger.warning("Starting quote event callback retry loop")

        def _retry_loop() -> None:
            interval = c._event_callback_retry_s
            try:
                while c.api and not c._event_callback_registered:
                    with c._callback_register_lock:
                        ok = c._register_event_callback()
                    if ok:
                        c._event_callback_registered = True
                        logger.info("Quote event callback registered after retry")
                        break
                    logger.warning("Quote event callback registration retrying", interval_s=interval)
                    time.sleep(interval)
            except Exception as exc:
                logger.error("Quote event callback retry loop crashed", error=str(exc))
            finally:
                c._event_callback_retrying = False
                c._set_thread_alive_metric("event_callback_retry", False)

        c._event_callback_retry_thread = threading.Thread(
            target=_retry_loop,
            name="shioaji-event-callback-retry",
            daemon=True,
        )
        c._event_callback_retry_thread.start()

    def schedule_force_relogin(self) -> None:
        c = self._client
        delay = c._quote_force_relogin_s
        if delay <= 0:
            return
        # Use _reconnect_lock (not bool flag) to prevent concurrent relogin.
        # Bool check-then-set is not atomic — two threads can both read False
        # and enter login simultaneously, crashing Shioaji SDK (2026-04-15).
        if not c._reconnect_lock.acquire(blocking=False):
            return
        c._reconnect_lock.release()  # release immediately; re-acquire inside thread
        if c._pending_quote_relogining:
            return
        c._pending_quote_relogining = True
        c._set_thread_alive_metric("quote_relogin", True)

        def _relogin_after() -> None:
            try:
                time.sleep(delay)
                if c._pending_quote_resubscribe:
                    if not c._allow_quote_recovery("quote_pending_timeout"):
                        return
                    logger.warning("Quote pending too long; forcing reconnect", delay_s=delay)
                    try:
                        ok, _, err, timed_out = c._safe_call_with_timeout(
                            "reconnect_quote_pending",
                            lambda: c._request_reconnect_via_policy("quote_pending", force=True),
                            c._reconnect_timeout_s,
                        )
                        if not ok:
                            logger.error(
                                "Force reconnect (quote_pending) failed",
                                timeout=timed_out,
                                error=str(err),
                            )
                    except Exception as exc:
                        logger.error("Force reconnect (quote_pending) failed", error=str(exc))
            finally:
                c._pending_quote_relogining = False
                c._set_thread_alive_metric("quote_relogin", False)

        c._pending_quote_relogin_thread = threading.Thread(
            target=_relogin_after,
            name="shioaji-quote-relogin",
            daemon=True,
        )
        c._pending_quote_relogin_thread.start()

    def start_forced_relogin(self, reason: str) -> None:
        c = self._client
        # Use _reconnect_lock to prevent concurrent relogin (same fix as above).
        if not c._reconnect_lock.acquire(blocking=False):
            return
        c._reconnect_lock.release()
        if c._pending_quote_relogining:
            return
        if not c._allow_quote_recovery(reason):
            return
        c._pending_quote_relogining = True
        c._set_thread_alive_metric("force_relogin", True)

        def _do_relogin() -> None:
            try:
                try:
                    ok, _, err, timed_out = c._safe_call_with_timeout(
                        "reconnect_force",
                        lambda: c._request_reconnect_via_policy(reason, force=True),
                        c._reconnect_timeout_s,
                    )
                    if not ok:
                        logger.error(
                            "Force reconnect failed",
                            reason=reason,
                            timeout=timed_out,
                            error=str(err),
                        )
                except Exception as exc:
                    logger.error("Force reconnect failed", reason=reason, error=str(exc))
            finally:
                c._pending_quote_relogining = False
                c._set_thread_alive_metric("force_relogin", False)

        threading.Thread(
            target=_do_relogin,
            name="shioaji-force-relogin",
            daemon=True,
        ).start()

    def note_quote_flap(self, now: float) -> None:
        c = self._client
        if c._quote_flap_window_s <= 0 or c._quote_flap_threshold <= 0:
            return
        c._quote_flap_events.append(now)
        while c._quote_flap_events and now - c._quote_flap_events[0] > c._quote_flap_window_s:
            c._quote_flap_events.popleft()
        if len(c._quote_flap_events) < c._quote_flap_threshold:
            return
        if now - c._last_quote_flap_relogin_ts < c._quote_flap_cooldown_s:
            return
        c._last_quote_flap_relogin_ts = now
        logger.warning(
            "Quote session flapping; forcing relogin",
            count=len(c._quote_flap_events),
            window_s=c._quote_flap_window_s,
        )
        c._start_forced_relogin("quote_flap")

    def supports_quote_v0(self) -> bool:
        c = self._client
        quote_api = c._quote_api()
        if quote_api is None:
            return False
        return hasattr(quote_api, "set_on_tick_stk_callback")

    def supports_quote_v1(self) -> bool:
        c = self._client
        quote_api = c._quote_api()
        if quote_api is None:
            return False
        return hasattr(quote_api, "set_on_tick_stk_v1_callback")

    def mark_quote_pending(self, reason: str) -> None:
        """Public entry for marking pending state.

        Unlike ``mark_pending`` above, this variant is invoked from the
        ShioajiClient stub (_mark_quote_pending) and covers the fallback
        path when ``c._quote_event_handler`` is not yet wired. The
        handler's ``pending_lock`` guards the (_reason, _ts, _resubscribe)
        triple on both the handler and the client mirror (P0-D2).
        """
        c = self._client
        now = timebase.now_s()
        # Use our local handler's lock — always available via QuoteRuntime.
        lock = self._event_handler.pending_lock
        with lock:
            if not c._pending_quote_resubscribe or c._pending_quote_reason != reason:
                logger.warning("Quote pending", reason=reason)
            if c._quote_event_handler is not None:
                try:
                    delta = c._quote_event_handler.mark_pending(reason, current_ts=now)
                    c._pending_quote_resubscribe = delta.pending
                    c._pending_quote_reason = delta.reason
                    if delta.pending and c._pending_quote_ts == 0.0:
                        c._pending_quote_ts = delta.ts
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    c._pending_quote_resubscribe = True
                    c._pending_quote_reason = reason
                    c._pending_quote_ts = now
            else:
                c._pending_quote_resubscribe = True
                c._pending_quote_reason = reason
                c._pending_quote_ts = now
            c._quote_pending_stall_reported = False
        c._update_quote_pending_metrics()
        c._schedule_force_relogin()

    def clear_quote_pending(self) -> None:
        c = self._client
        lock = self._event_handler.pending_lock
        with lock:
            if c._quote_event_handler is not None:
                try:
                    c._quote_event_handler.clear_pending()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
            c._pending_quote_resubscribe = False
            c._pending_quote_reason = None
            c._pending_quote_ts = 0.0
            c._quote_pending_stall_reported = False
        c._update_quote_pending_metrics()
        logger.info("Quote data resumed; clearing pending")

    def schedule_resubscribe(self, reason: str) -> None:
        c = self._client
        if c._resubscribe_scheduled:
            return
        c._resubscribe_scheduled = True
        delay = max(0.0, c._resubscribe_delay_s)

        def _do_resubscribe() -> None:
            try:
                if delay > 0:
                    time.sleep(delay)
                if c.tick_callback:
                    c._callbacks_registered = False
                    c._ensure_callbacks(c.tick_callback)
                    c._resubscribe_all()
                logger.info("Resubscribe completed", reason=reason)
            finally:
                c._resubscribe_scheduled = False

        c._resubscribe_thread = threading.Thread(
            target=_do_resubscribe,
            name="shioaji-resubscribe",
            daemon=True,
        )
        c._resubscribe_thread.start()

    def on_quote_event(self, resp_code: int, event_code: int, info: str, event: str) -> None:
        c = self._client
        try:
            now = timebase.now_s()
            c._last_quote_event_ts = now
            c._event_callback_registered = True
            if event_code in (1, 2, 3, 4, 12, 13):
                logger.info("Quote event", resp_code=resp_code, event_code=event_code, info=info, event_name=event)
            if event_code == 12:
                c._note_quote_flap(now)
                try:
                    if c.metrics:
                        c.metrics.shioaji_keepalive_failures_total.inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
                c._mark_quote_pending("event_12")
                if c.tick_callback:
                    c._callbacks_registered = False
                    c._ensure_callbacks(c.tick_callback)
            elif event_code == 13:
                if c._pending_quote_resubscribe:
                    c._clear_quote_pending()
                    if c.tick_callback:
                        c._callbacks_registered = False
                        c._ensure_callbacks(c.tick_callback)
                        c._resubscribe_all()
                    else:
                        c._schedule_resubscribe("event_13")
                    try:
                        if c.metrics:
                            c.metrics.feed_resubscribe_total.labels(result="event_13").inc()
                    except Exception as exc:
                        logger.debug("operation_fallback", error=str(exc))
                        pass
            elif event_code == 4:
                if c._pending_quote_resubscribe:
                    c._clear_quote_pending()
                c._schedule_resubscribe("event_4")
                try:
                    if c.metrics:
                        c.metrics.feed_resubscribe_total.labels(result="event_4").inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
        except Exception as exc:
            c._record_crash_signature(str(exc), context="quote_event")
            logger.error(
                "Quote event handler failed",
                resp_code=resp_code,
                event_code=event_code,
                info=info,
                event_name=event,
                error=str(exc),
            )

    def start_sub_retry_thread(self, cb: Callable[..., Any]) -> None:
        c = self._client
        if c._sub_retry_running:
            return
        c._sub_retry_running = True
        c._set_thread_alive_metric("sub_retry", True)
        logger.info("Starting subscription retry thread", failed=len(c._failed_sub_symbols))

        def _retry_loop() -> None:
            interval = c._contract_retry_s
            while c._sub_retry_running and c._failed_sub_symbols:
                time.sleep(interval)
                if not c._sub_retry_running:
                    break
                if not c.logged_in:
                    continue
                if not (c._callbacks_registered and c._event_callback_registered):
                    if c.tick_callback:
                        c._ensure_callbacks(c.tick_callback)
                    if not (c._callbacks_registered and c._event_callback_registered):
                        logger.warning("Subscription retry waiting for quote callbacks to register")
                        continue
                quote_api = c._quote_api()
                if quote_api is None or not hasattr(quote_api, "subscribe"):
                    logger.warning("Subscription retry waiting for quote API availability")
                    continue
                # Check capacity before retrying — if at limit, stop the loop
                # to avoid infinite retries against a permanent broker limit.
                if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                    logger.warning(
                        "Subscription retry aborted: at capacity",
                        subscribed=c.subscribed_count,
                        limit=c.MAX_SUBSCRIPTIONS,
                        dropped=len(c._failed_sub_symbols),
                        codes=[s.get("code") for s in list(c._failed_sub_symbols)[:10]],
                    )
                    break
                # L2: drain in place via ``popleft`` and conditionally
                # re-``append`` on failure. The previous pattern built a
                # ``remaining`` list and reassigned ``c._failed_sub_symbols
                # = remaining``, which lost any concurrent ``append`` from
                # ``subscription_manager.py:112`` (event loop) that landed
                # on the OLD list between snapshot and reassign. Drain +
                # append-back keeps everything on the same deque object,
                # and each individual deque op is GIL-atomic.
                #
                # Bound the work per pass to the size at entry so a
                # peer-thread append during this pass does not livelock
                # the loop here — those new entries are picked up on the
                # next interval tick.
                pending = len(c._failed_sub_symbols)
                now = timebase.now_s()
                for _ in range(pending):
                    if not c._sub_retry_running:
                        break
                    try:
                        sym = c._failed_sub_symbols.popleft()
                    except IndexError:
                        break
                    if not c._sub_retry_running:
                        c._failed_sub_symbols.append(sym)
                        continue
                    if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                        c._failed_sub_symbols.append(sym)
                        continue
                    # D1: per-symbol backoff + max-attempts gate.
                    code = sym.get("code") or ""
                    allowed, reason = self._should_attempt_subscription(code, now)
                    if not allowed:
                        if reason == "skip_permanent":
                            # Drop permanently — never re-append.
                            continue
                        # skip_backoff: retain in queue for next interval.
                        c._failed_sub_symbols.append(sym)
                        continue
                    if c._subscribe_symbol(sym, cb):
                        if code:
                            c.subscribed_codes.add(code)
                        c.subscribed_count = len(c.subscribed_codes)
                        # D1: success clears retry state for this symbol.
                        self._record_subscription_success(code)
                        logger.info("Subscription retry succeeded", code=code)
                        # Bug 12: trigger alias propagation so strategies re-resolve
                        # symbols when subscribe succeeds after the initial
                        # connect sequence finished.
                        _cb_alias = getattr(c, "on_alias_map_updated", None)
                        if _cb_alias is not None:
                            try:
                                _cb_alias()
                            except Exception as exc:
                                logger.warning("on_alias_map_updated_failed", error=str(exc))
                    else:
                        # D1: record failure → may move to _permanently_failed
                        # after HFT_SUB_RETRY_MAX_ATTEMPTS. Permanent symbols
                        # are NOT re-appended to _failed_sub_symbols.
                        permanent = self._record_subscription_failure(code, now)
                        if not permanent:
                            c._failed_sub_symbols.append(sym)
                if not c._failed_sub_symbols:
                    logger.info("All failed subscriptions resolved")
                    break
                # If all remaining failures are due to capacity, stop retrying
                if c.subscribed_count >= c.MAX_SUBSCRIPTIONS:
                    logger.warning(
                        "Subscription retry stopped: capacity reached",
                        subscribed=c.subscribed_count,
                        limit=c.MAX_SUBSCRIPTIONS,
                        remaining=len(c._failed_sub_symbols),
                    )
                    break
                logger.warning(
                    "Subscription retry: still pending",
                    count=len(c._failed_sub_symbols),
                    codes=[s.get("code") for s in list(c._failed_sub_symbols)[:10]],
                )
            c._sub_retry_running = False
            c._set_thread_alive_metric("sub_retry", False)

        c._sub_retry_thread = threading.Thread(
            target=_retry_loop,
            name="shioaji-sub-retry",
            daemon=True,
        )
        c._sub_retry_thread.start()

    # ------------------------------------------------------------------ #
    # Legacy pass-through helpers
    # ------------------------------------------------------------------ #

    def resubscribe(self) -> bool:
        return bool(self._client.resubscribe())

    def allow_recovery(self, reason: str) -> bool:
        return bool(self._client._allow_quote_recovery(reason))

    def snapshot(self) -> QuoteRuntimeSnapshot:
        # Read the (_resubscribe, _reason, _ts) triple under the pending_lock
        # so the returned snapshot is internally consistent (P0-D2). The
        # callbacks_registered flag is a single bool (GIL-atomic) and not
        # part of the triple, so reading it outside the lock is acceptable.
        c = self._client
        with self._event_handler.pending_lock:
            resubscribe = bool(getattr(c, "_pending_quote_resubscribe", False))
            reason = getattr(c, "_pending_quote_reason", None)
            since = float(getattr(c, "_pending_quote_ts", 0.0) or 0.0)
        return QuoteRuntimeSnapshot(
            pending_resubscribe=resubscribe,
            pending_reason=reason,
            pending_since=since,
            callbacks_registered=bool(getattr(c, "_callbacks_registered", False)),
        )
