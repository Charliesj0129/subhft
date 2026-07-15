"""Reconnect orchestrator for Shioaji session recovery.

Phase-6 decoupling: extracts reconnect sequence, quote verification,
trading-hours guards, and quote-version helpers from ShioajiClient into
a focused module. The orchestrator coordinates session_runtime and
quote_runtime during reconnect without owning either.

RESILIENCE-CRITICAL: This module coordinates multi-system reconnect.
Do NOT change reconnect behaviour — pure extraction only.
"""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import TYPE_CHECKING, Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji._compat import resolve_quote_enum

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji.client import ShioajiClient

logger = get_logger("feed_adapter.reconnect_orchestrator")


class ReconnectOrchestrator:
    """Coordinates reconnect across session, quote, and subscription layers.

    Constructor takes a reference to the underlying ShioajiClient so it can
    read/write shared state (logged_in, subscribed_codes, etc.) and delegate
    to existing helpers (_safe_call_with_timeout, _ensure_callbacks, …).

    All methods preserve the exact reconnect sequence from the original
    ShioajiClient — this is a pure code-movement extraction.
    """

    __slots__ = ("_client", "_consecutive_failures")

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client
        self._consecutive_failures: int = 0

    # ------------------------------------------------------------------ #
    # Reconnect sequence (RESILIENCE-CRITICAL)
    # ------------------------------------------------------------------ #

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Full reconnect: logout → login → callbacks → subscribe.

        Respects backoff, cooldown, and lock guards. Returns True only when
        the full sequence (login + subscribe) succeeds.

        After HARD_RECONNECT_THRESHOLD consecutive failures, recreates the
        sj.Shioaji() instance to recover from stale SDK state (e.g., weekend
        session expiry).
        """
        c = self._client
        if not c.api:
            return False
        now = timebase.now_s()
        cooldown = float(os.getenv("HFT_RECONNECT_COOLDOWN", "30"))
        if not force and now - c._last_reconnect_ts < max(cooldown, c._reconnect_backoff_s):
            return False
        if not c._reconnect_lock.acquire(blocking=False):
            return False
        try:
            c._last_reconnect_ts = now
            c._last_reconnect_error = None

            # Hard reconnect: recreate API object after repeated failures
            hard_threshold = int(os.getenv("HFT_HARD_RECONNECT_THRESHOLD", "3"))
            if self._consecutive_failures >= hard_threshold:
                logger.warning(
                    "hard_reconnect_triggered",
                    reason=reason,
                    consecutive_failures=self._consecutive_failures,
                )
                if c.recreate_api():
                    self._consecutive_failures = 0
                else:
                    c._last_reconnect_error = "recreate_api_failed"
                    return False
            else:
                logger.warning("Reconnecting Shioaji", reason=reason, force=force)
                ok_logout, _, err_logout, _ = c._safe_call_with_timeout(
                    "logout",
                    lambda: c.api.logout(),
                    c._reconnect_timeout_s,
                )
                if not ok_logout:
                    logger.warning("Logout failed during reconnect", error=str(err_logout))

                c.logged_in = False
                c._callbacks_registered = False
                c._clear_quote_pending()
                # D2: in-place clear (don't rebind — preserves identity).
                c.subscribed_codes.clear()
                c.subscribed_count = 0
                c._refresh_quote_routes()

            login_ok = bool(c.login())
            if not login_ok or not c.logged_in:
                c._last_reconnect_error = c._last_login_error or "login_failed"
                if c.metrics:
                    c.metrics.feed_reconnect_total.labels(result="fail").inc()
                self._consecutive_failures += 1
                c._reconnect_backoff_s = min(c._reconnect_backoff_s * 2.0, c._reconnect_backoff_max_s)
                return False

            subscribe_ok = True
            callback = c.tick_callback
            if callback is not None:
                try:
                    c._ensure_callbacks(callback)
                    if not c._callbacks_registered:
                        subscribe_ok = False
                        c._last_reconnect_error = "callbacks_not_registered"
                    else:
                        ok_sub, _, err_sub, timed_out_sub = c._safe_call_with_timeout(
                            "subscribe_basket",
                            lambda: c.subscribe_basket(callback),
                            c._reconnect_subscribe_timeout_s,
                        )
                        if not ok_sub:
                            subscribe_ok = False
                            c._last_reconnect_error = str(err_sub) if err_sub is not None else "subscribe_failed"
                            if c.metrics and timed_out_sub:
                                try:
                                    c.metrics.feed_reconnect_timeout_total.labels(reason="subscribe").inc()
                                except Exception as exc:
                                    logger.debug("operation_fallback", error=str(exc))
                                    pass
                            logger.error(
                                "Subscribe basket failed after reconnect",
                                timeout=timed_out_sub,
                                error=c._last_reconnect_error,
                            )
                except Exception as exc:
                    subscribe_ok = False
                    c._last_reconnect_error = str(exc)
                    logger.error("Callback/subscribe failed after reconnect login", error=str(exc))

            ok = c.logged_in and subscribe_ok
            if c.metrics:
                c.metrics.feed_reconnect_total.labels(result="ok" if ok else "fail").inc()
            if ok:
                c._reconnect_backoff_s = float(os.getenv("HFT_RECONNECT_BACKOFF_S", "30"))
                self._consecutive_failures = 0
                return True

            self._consecutive_failures += 1
            c._reconnect_backoff_s = min(c._reconnect_backoff_s * 2.0, c._reconnect_backoff_max_s)
            return False
        except Exception as exc:
            self._consecutive_failures += 1
            c._last_reconnect_error = str(exc)
            logger.error("Reconnect failed unexpectedly", reason=reason, error=str(exc))
            if c.metrics:
                c.metrics.feed_reconnect_total.labels(result="exception").inc()
                try:
                    from hft_platform.observability.metrics import cap_exception_type  # noqa: PLC0415

                    c.metrics.feed_reconnect_exception_total.labels(
                        reason=reason or "unknown",
                        exception_type=cap_exception_type(exc),
                    ).inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
            c._reconnect_backoff_s = min(c._reconnect_backoff_s * 2.0, c._reconnect_backoff_max_s)
            return False
        finally:
            c._reconnect_lock.release()

    # ------------------------------------------------------------------ #
    # SessionPolicy routing
    # ------------------------------------------------------------------ #

    def request_reconnect_via_policy(self, reason: str, force: bool = True) -> bool:
        """Route a reconnect intent through the SessionPolicy interface.

        Falls back to direct self.reconnect() when policy is not yet
        initialized (e.g., in unit tests that construct ShioajiClient directly).
        """
        c = self._client
        if c._session_policy is not None:
            try:
                return bool(c._session_policy.request_reconnect(reason=reason, force=force))
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                return False
        # Fallback: direct call (only in legacy/test contexts)
        return bool(self.reconnect(reason=reason, force=force))

    # ------------------------------------------------------------------ #
    # Quote health verification
    # ------------------------------------------------------------------ #

    def verify_quotes_flowing(self, timeout_s: float | None = None) -> bool:
        """Verify quotes are flowing after refresh (O5).

        Waits for new quote data to arrive within timeout period.

        Args:
            timeout_s: Timeout in seconds (default: HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S)

        Returns:
            True if new quote data received within timeout
        """
        c = self._client
        if not c.logged_in or not c.subscribed_count:
            return True

        if timeout_s is None:
            timeout_s = c._session_refresh_verify_timeout_s

        start_ts = c._last_quote_data_ts
        deadline = timebase.now_s() + timeout_s

        logger.debug(
            "Verifying quotes flowing",
            timeout_s=timeout_s,
            subscribed_count=c.subscribed_count,
        )

        while timebase.now_s() < deadline:
            if c._last_quote_data_ts > start_ts:
                logger.debug(
                    "Quotes flowing verified",
                    elapsed_s=round(timebase.now_s() - (deadline - timeout_s), 2),
                )
                return True
            time.sleep(0.5)

        logger.warning(
            "Quote verification timeout",
            timeout_s=timeout_s,
            subscribed_count=c.subscribed_count,
        )
        return False

    # ------------------------------------------------------------------ #
    # Trading hours / market calendar guards
    # ------------------------------------------------------------------ #

    def is_trading_hours(self) -> bool:
        """Return True if currently within TAIFEX futures/options trading hours.

        Covers day session (08:45-13:45) and night session (15:00-05:00).
        """
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)
            return calendar.is_trading_hours(now_dt, product_type="future")
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            # Conservative fallback: weekdays TAIFEX hours Asia/Taipei.
            now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=dt.timezone(dt.timedelta(hours=8)))
            if now_dt.weekday() >= 5:
                return False
            minute = now_dt.hour * 60 + now_dt.minute
            # Day: 08:45-13:45, Night: 15:00-05:00 (next day)
            day_session = (8 * 60 + 45) <= minute <= (13 * 60 + 45)
            night_session = minute >= (15 * 60) or minute <= (5 * 60)
            return day_session or night_session

    # ------------------------------------------------------------------ #
    # Quote version helpers
    # ------------------------------------------------------------------ #

    def get_quote_version(self) -> Any:
        """Return the appropriate QuoteVersion enum value, or None."""
        try:
            import shioaji as sj
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return None
        if not sj:
            return None
        try:
            quote_version = resolve_quote_enum(sj, "QuoteVersion")
        except AttributeError:
            return None
        c = self._client
        # H13: snapshot under the dedicated lock so the watchdog thread
        # cannot flip _quote_version between the two comparisons below.
        lock = getattr(c, "_quote_version_lock", None)
        if lock is not None:
            with lock:
                current = c._quote_version
        else:
            current = c._quote_version
        if current == "v0":
            v0 = getattr(quote_version, "v0", None)
            if v0 is not None and c._supports_quote_v0():
                return v0
            if c._supports_quote_v1():
                return getattr(quote_version, "v1", None)
            return None
        return getattr(quote_version, "v1", None)

    def handle_quote_schema_mismatch(self, reason: str, *args: Any, **kwargs: Any) -> None:
        """Record and log quote schema mismatches."""
        c = self._client
        c._quote_schema_mismatch_count += 1
        try:
            if c.metrics and hasattr(c.metrics, "quote_schema_mismatch_total"):
                key = ("v1", reason)
                child = c._quote_schema_mismatch_metric_cache.get(key)
                if child is None:
                    child = c.metrics.quote_schema_mismatch_total.labels(expected="v1", reason=reason)
                    c._quote_schema_mismatch_metric_cache[key] = child
                child.inc()
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass
        if c._quote_schema_mismatch_count % c._quote_schema_mismatch_log_every == 1:
            logger.error(
                "Quote schema guard rejected callback payload",
                expected_version="v1",
                reason=reason,
                arg0_type=(type(args[0]).__name__ if args else None),
                kwargs_keys=sorted(kwargs.keys())[:8],
            )
