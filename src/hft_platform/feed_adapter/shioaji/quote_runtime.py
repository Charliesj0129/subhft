from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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
                c.api.quote.set_on_tick_stk_v1_callback(dispatch_tick_cb)
                c.api.quote.set_on_bidask_stk_v1_callback(dispatch_tick_cb)
                c.api.quote.set_on_tick_fop_v1_callback(dispatch_tick_cb)
                c.api.quote.set_on_bidask_fop_v1_callback(dispatch_tick_cb)
                return True
            except Exception as exc:
                logger.warning("Quote v1 callback registration failed", error=str(exc))
                ok = False
                return False

        def _set_v0() -> bool:
            nonlocal ok
            if not hasattr(c.api.quote, "set_on_tick_stk_callback"):
                logger.warning("Quote v0 callbacks not available on this Shioaji version")
                ok = False
                return False
            try:
                c.api.quote.set_on_tick_stk_callback(dispatch_tick_cb)
                c.api.quote.set_on_bidask_stk_callback(dispatch_tick_cb)
                if hasattr(c.api.quote, "set_on_tick_fop_callback"):
                    c.api.quote.set_on_tick_fop_callback(dispatch_tick_cb)
                if hasattr(c.api.quote, "set_on_bidask_fop_callback"):
                    c.api.quote.set_on_bidask_fop_callback(dispatch_tick_cb)
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
