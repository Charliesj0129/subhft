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
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

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
    ts: float  # monotonic epoch from time.time()


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
    """

    __slots__ = ("_pending_reason", "_pending_ts")

    def __init__(self) -> None:
        self._pending_reason: str | None = None
        self._pending_ts: float = 0.0

    def mark_pending(self, reason: str, current_ts: float | None = None) -> QuotePendingState:
        """Transition to pending state.

        Returns a QuotePendingState delta for the caller to apply.
        Idempotent: repeated marks with the same reason return an unchanged state.
        """
        ts = current_ts if current_ts is not None else time.time()
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
        self._pending_reason = None
        self._pending_ts = 0.0
        return QuotePendingState(pending=False, reason=None, ts=0.0)

    @property
    def is_pending(self) -> bool:
        return self._pending_reason is not None

    @property
    def current_reason(self) -> str | None:
        return self._pending_reason

    @property
    def pending_since(self) -> float:
        return self._pending_ts

    def snapshot(self) -> QuotePendingState:
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

    __slots__ = ("_client", "_event_handler")

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client
        self._event_handler = QuoteEventHandler()

    # ------------------------------------------------------------------ #
    # Pending state management via QuoteEventHandler
    # ------------------------------------------------------------------ #

    def mark_pending(self, reason: str) -> QuotePendingState:
        """Mark the quote feed as pending and return the new state delta."""
        delta = self._event_handler.mark_pending(reason)
        c = self._client
        c._pending_quote_resubscribe = delta.pending
        if delta.pending and c._pending_quote_ts == 0.0:
            c._pending_quote_ts = delta.ts
        c._pending_quote_reason = delta.reason
        return delta

    def clear_pending(self) -> QuotePendingState:
        """Clear the pending state and return the cleared state delta."""
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
        from hft_platform.feed_adapter.shioaji_client import dispatch_tick_cb

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
                c._quote_version = "v0"
                ok = _set_v0()
                if ok and c.metrics:
                    c.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                if not ok:
                    c._quote_version = "v1"
            else:
                if not supports_v1:
                    logger.warning("Quote v1 callbacks not available on this Shioaji version")
                if allow_fallback and not supports_v0:
                    logger.warning("Quote v0 callbacks not available; staying on v1")
                c._quote_version = "v1"
                ok = False
        else:
            if supports_v0:
                ok = _set_v0()
            else:
                logger.warning("Quote v0 callbacks not available on this Shioaji version")
                if supports_v1:
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
                        c._quote_version = "v0"
                        if c.metrics:
                            c.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                            try:
                                c.metrics.quote_watchdog_recovery_attempts_total.labels(
                                    action="version_downgrade"
                                ).inc()
                            except Exception:
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
                            except Exception:
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
            except Exception:
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

        now = dt.datetime.now(calendar._tz)
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
        c = self._client
        now = timebase.now_s()
        if not c._pending_quote_resubscribe or c._pending_quote_reason != reason:
            logger.warning("Quote pending", reason=reason)
        if c._quote_event_handler is not None:
            try:
                delta = c._quote_event_handler.mark_pending(reason, current_ts=now)
                c._pending_quote_resubscribe = delta.pending
                c._pending_quote_reason = delta.reason
                if delta.pending and c._pending_quote_ts == 0.0:
                    c._pending_quote_ts = delta.ts
            except Exception:
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
        if c._quote_event_handler is not None:
            try:
                c._quote_event_handler.clear_pending()
            except Exception:
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
                except Exception:
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
                    except Exception:
                        pass
            elif event_code == 4:
                if c._pending_quote_resubscribe:
                    c._clear_quote_pending()
                c._schedule_resubscribe("event_4")
                try:
                    if c.metrics:
                        c.metrics.feed_resubscribe_total.labels(result="event_4").inc()
                except Exception:
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
                remaining: list[dict[str, Any]] = []
                for sym in list(c._failed_sub_symbols):
                    if not c._sub_retry_running:
                        remaining.append(sym)
                        continue
                    if c._subscribe_symbol(sym, cb):
                        code = sym.get("code")
                        if code:
                            c.subscribed_codes.add(code)
                        c.subscribed_count = len(c.subscribed_codes)
                        logger.info("Subscription retry succeeded", code=sym.get("code"))
                    else:
                        remaining.append(sym)
                c._failed_sub_symbols = remaining
                if not c._failed_sub_symbols:
                    logger.info("All failed subscriptions resolved")
                    break
                logger.warning(
                    "Subscription retry: still pending",
                    count=len(c._failed_sub_symbols),
                    codes=[s.get("code") for s in c._failed_sub_symbols[:10]],
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
        return QuoteRuntimeSnapshot(
            pending_resubscribe=bool(getattr(self._client, "_pending_quote_resubscribe", False)),
            pending_reason=getattr(self._client, "_pending_quote_reason", None),
            pending_since=float(getattr(self._client, "_pending_quote_ts", 0.0) or 0.0),
            callbacks_registered=bool(getattr(self._client, "_callbacks_registered", False)),
        )
