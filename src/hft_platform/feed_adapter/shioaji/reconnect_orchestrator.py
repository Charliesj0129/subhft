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

if TYPE_CHECKING:
    from hft_platform.feed_adapter.shioaji_client import ShioajiClient

logger = get_logger("feed_adapter.reconnect_orchestrator")


class ReconnectOrchestrator:
    """Coordinates reconnect across session, quote, and subscription layers.

    Constructor takes a reference to the underlying ShioajiClient so it can
    read/write shared state (logged_in, subscribed_codes, etc.) and delegate
    to existing helpers (_safe_call_with_timeout, _ensure_callbacks, …).

    All methods preserve the exact reconnect sequence from the original
    ShioajiClient — this is a pure code-movement extraction.
    """

    __slots__ = ("_client",)

    def __init__(self, client: "ShioajiClient") -> None:
        self._client = client

    # ------------------------------------------------------------------ #
    # Reconnect sequence (RESILIENCE-CRITICAL)
    # ------------------------------------------------------------------ #

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Full reconnect: logout → login → callbacks → subscribe.

        Respects backoff, cooldown, and lock guards. Returns True only when
        the full sequence (login + subscribe) succeeds.

        Extracted verbatim from ShioajiClient.reconnect().
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
            c.subscribed_codes = set()
            c.subscribed_count = 0
            c._refresh_quote_routes()

            login_ok = bool(c.login())
            if not login_ok or not c.logged_in:
                c._last_reconnect_error = c._last_login_error or "login_failed"
                if c.metrics:
                    c.metrics.feed_reconnect_total.labels(result="fail").inc()
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
                                except Exception:
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
                return True

            c._reconnect_backoff_s = min(c._reconnect_backoff_s * 2.0, c._reconnect_backoff_max_s)
            return False
        except Exception as exc:
            c._last_reconnect_error = str(exc)
            logger.error("Reconnect failed unexpectedly", reason=reason, error=str(exc))
            if c.metrics:
                c.metrics.feed_reconnect_total.labels(result="exception").inc()
                try:
                    c.metrics.feed_reconnect_exception_total.labels(
                        reason=reason or "unknown",
                        exception_type=type(exc).__name__,
                    ).inc()
                except Exception:
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
            except Exception:
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
        """Return True if currently within TWSE trading hours."""
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.now(calendar._tz)
            return calendar.is_trading_hours(now_dt)
        except Exception:
            # Conservative fallback: weekdays 09:00-13:30 Asia/Taipei.
            now_dt = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            if now_dt.weekday() >= 5:
                return False
            minute = now_dt.hour * 60 + now_dt.minute
            return (9 * 60) <= minute <= (13 * 60 + 30)

    # ------------------------------------------------------------------ #
    # Quote version helpers
    # ------------------------------------------------------------------ #

    def get_quote_version(self) -> Any:
        """Return the appropriate QuoteVersion enum value, or None."""
        try:
            import shioaji as sj
        except Exception:
            return None
        if not sj or not hasattr(sj.constant, "QuoteVersion"):
            return None
        c = self._client
        if c._quote_version == "v0" and not c._supports_quote_v0():
            if c._supports_quote_v1():
                return sj.constant.QuoteVersion.v1
            return None
        return sj.constant.QuoteVersion.v0 if c._quote_version == "v0" else sj.constant.QuoteVersion.v1

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
        except Exception:
            pass
        if c._quote_schema_mismatch_count % c._quote_schema_mismatch_log_every == 1:
            logger.error(
                "Quote schema guard rejected callback payload",
                expected_version="v1",
                reason=reason,
                arg0_type=(type(args[0]).__name__ if args else None),
                kwargs_keys=sorted(kwargs.keys())[:8],
            )
