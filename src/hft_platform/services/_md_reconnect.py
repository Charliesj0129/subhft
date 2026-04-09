"""Market data reconnection, rollover, and watchdog logic.

Private module — imported only by ``market_data.py``.
All ``datetime.now()`` calls replaced with ``timebase``-based equivalents.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase

from ._md_ingestion import FeedState

logger = get_logger("service.market_data")


class MarketDataReconnectMixin:
    """Reconnection / rollover / watchdog methods for ``MarketDataService``."""

    _last_rollover_reconnect_date: dt.date | None
    _last_rollover_seen_date: dt.date | None
    _pending_reconnect_reason: str | None
    _pending_reconnect_gap: float
    _pending_reconnect_since: float | None
    _on_reconnect_callbacks: list[Any]

    # -- rollover / window checks -------------------------------------------

    def _should_rollover_reconnect(self: Any) -> bool:
        tz = getattr(self, "_reconnect_tzinfo", dt.timezone.utc)
        now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=tz)
        last_event_dt = dt.datetime.fromtimestamp(
            getattr(self, "last_event_ts", 0.0),
            tz=tz,
        )
        if last_event_dt.date() == now_dt.date():
            return False
        if getattr(self, "_last_rollover_seen_date", None) == now_dt.date():
            return False
        self._last_rollover_seen_date = now_dt.date()
        return True

    def _within_reconnect_window(self: Any) -> bool:
        reconnect_days: set[str] = getattr(self, "reconnect_days", set())
        reconnect_hours: str = getattr(self, "reconnect_hours", "")
        reconnect_hours_2: str = getattr(self, "reconnect_hours_2", "")
        if not reconnect_days and not reconnect_hours and not reconnect_hours_2:
            return True
        tz = getattr(self, "_reconnect_tzinfo", dt.timezone.utc)
        now = dt.datetime.fromtimestamp(timebase.now_s(), tz=tz)
        if os.getenv("HFT_RECONNECT_USE_CALENDAR", "1").lower() not in {"0", "false", "no", "off"}:
            try:
                from hft_platform.core.market_calendar import get_calendar

                calendar = get_calendar()
                if calendar.available and calendar.days_until_trading(now.date()) > 1:
                    return False
            except Exception:
                pass
        weekday = now.strftime("%a").lower()
        if reconnect_days and weekday not in reconnect_days:
            return False

        windows = [w for w in (reconnect_hours, reconnect_hours_2) if w]
        if not windows:
            return True
        for window in windows:
            try:
                start_str, end_str = window.split("-", 1)
                start = dt.time.fromisoformat(start_str)
                end = dt.time.fromisoformat(end_str)
                now_t = now.timetz().replace(tzinfo=None)
                if start <= end:
                    if start <= now_t <= end:
                        return True
                else:
                    if now_t >= start or now_t <= end:
                        return True
            except Exception:
                continue
        return False

    # -- reconnect / resubscribe actions ------------------------------------

    async def _attempt_resubscribe(self: Any, gap: float, reason: str = "heartbeat_gap") -> None:
        if not self._within_reconnect_window():
            return
        now = timebase.now_s()
        if now - getattr(self, "_last_resubscribe_ts", 0.0) < getattr(self, "resubscribe_cooldown_s", 15.0):
            return
        metrics_registry = getattr(self, "metrics_registry", None)
        if metrics_registry:
            if reason == "heartbeat_gap":
                if getattr(self, "_feed_reconnect_gap_metric_child", None) is None:
                    self._feed_reconnect_gap_metric_child = metrics_registry.feed_reconnect_total.labels(result="gap")
                gap_metric_child = self._feed_reconnect_gap_metric_child
                if gap_metric_child is not None:
                    gap_metric_child.inc()
            elif reason == "symbol_gap":
                metrics_registry.feed_reconnect_total.labels(result="symbol_gap").inc()
        self._last_resubscribe_ts = now
        client = getattr(self, "client", None)
        ok = await asyncio.to_thread(client.resubscribe) if client else False
        if ok:
            self._resubscribe_attempts = 0
        else:
            self._resubscribe_attempts += 1
        logger.info("Resubscribe attempt", gap=gap, reason=reason, ok=ok, attempts=self._resubscribe_attempts)

    async def _request_reconnect(self: Any, gap: float, reason: str | None = None) -> None:
        if self._within_reconnect_window():
            await self._trigger_reconnect(gap, reason=reason)
            return
        self._mark_pending_reconnect(gap, reason=reason)

    async def _trigger_reconnect(self: Any, gap: float, reason: str | None = None) -> bool:
        now = timebase.now_s()
        if now - getattr(self, "_last_reconnect_ts", 0.0) < getattr(self, "reconnect_cooldown_s", 60.0):
            return False
        if not self._within_reconnect_window():
            return False
        self._last_reconnect_ts = now
        reason_label = reason or "heartbeat_gap"
        logger.warning("Triggering reconnect", gap=gap, reason=reason_label)
        self._set_state(FeedState.RECOVERING)
        force_login = reason_label == "session_rollover"
        reconnect_timeout_s = getattr(self, "reconnect_timeout_s", 30.0)
        client = getattr(self, "client", None)
        if client is None:
            self._set_state(FeedState.DISCONNECTED)
            return False
        metrics_registry = getattr(self, "metrics_registry", None)
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(client.reconnect, f"{reason_label} {gap:.1f}s", force_login),
                timeout=max(0.1, float(reconnect_timeout_s)),
            )
        except asyncio.TimeoutError:
            logger.error("Reconnect timed out", reason=reason_label, timeout_s=reconnect_timeout_s)
            if metrics_registry and hasattr(metrics_registry, "feed_reconnect_timeout_total"):
                metrics_registry.feed_reconnect_timeout_total.labels(reason=reason_label).inc()
            self._set_state(FeedState.DISCONNECTED)
            return False
        except Exception as exc:
            logger.error("Reconnect raised exception", reason=reason_label, error=str(exc))
            if metrics_registry and hasattr(metrics_registry, "feed_reconnect_exception_total"):
                from hft_platform.observability.metrics import cap_exception_type  # noqa: PLC0415

                metrics_registry.feed_reconnect_exception_total.labels(
                    reason=reason_label,
                    exception_type=cap_exception_type(exc),
                ).inc()
            self._set_state(FeedState.DISCONNECTED)
            return False
        # Per-facade LOB/Feature reset is deferred via _pending_warmup_reset.
        # Apply it now on the event loop thread for thread-safe dict mutation.
        client = getattr(self, "client", None)
        if hasattr(client, "_apply_pending_resets"):
            client._apply_pending_resets()
        elif not hasattr(client, "get_healthy_feed_gap_s"):
            # Single-client mode: global reset on event loop (already safe).
            lob = getattr(self, "lob", None)
            if lob is not None and hasattr(lob, "reset_books"):
                lob.reset_books()
            fe = getattr(self, "feature_engine", None)
            if fe is not None and hasattr(fe, "reset_all"):
                fe.reset_all()
        if ok:
            self._set_state(FeedState.CONNECTED)
            self.last_event_ts = timebase.now_s()
            self.last_event_mono = time.monotonic()
            self._resubscribe_attempts = 0
            # Fire post-reconnect callbacks (e.g. invalidate stale live orders)
            for cb in getattr(self, "_on_reconnect_callbacks", []):
                try:
                    result = cb(reason_label)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as cb_exc:
                    logger.warning("on_reconnect_callback_error", error=str(cb_exc))
        else:
            self._set_state(FeedState.DISCONNECTED)
        return ok

    def _mark_pending_reconnect(self: Any, gap: float, reason: str | None = None) -> None:
        reason_label = reason or "heartbeat_gap"
        if getattr(self, "_pending_reconnect_reason", None) != reason_label:
            logger.warning("Reconnect pending (outside window)", gap=gap, reason=reason_label)
        self._pending_reconnect_reason = reason_label
        self._pending_reconnect_gap = gap
        if getattr(self, "_pending_reconnect_since", None) is None:
            self._pending_reconnect_since = timebase.now_s()

    # -- public helpers ------------------------------------------------------

    def register_on_reconnect(self: Any, callback: Any) -> None:
        """Register a callback to invoke after successful reconnect.

        Callbacks receive a single ``reason: str`` argument.  Async
        callables (coroutines) are awaited automatically.
        """
        cbs: list[Any] = getattr(self, "_on_reconnect_callbacks", [])
        if not hasattr(self, "_on_reconnect_callbacks"):
            self._on_reconnect_callbacks = cbs
        cbs.append(callback)

    def within_reconnect_window(self: Any) -> bool:
        """Public hook for supervisors."""
        return self._within_reconnect_window()

    # -- trading hours / grace period ----------------------------------------

    def _is_trading_hours(self: Any) -> bool:
        product_type = os.getenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)
            return calendar.is_trading_hours(now_dt, product_type=product_type)
        except Exception:
            now_dt = dt.datetime.fromtimestamp(
                timebase.now_s(),
                tz=dt.timezone(dt.timedelta(hours=8)),
            )
            if now_dt.weekday() >= 5:
                return False
            minute = now_dt.hour * 60 + now_dt.minute
            return (8 * 60 + 45) <= minute <= (13 * 60 + 45)

    def _is_market_open_grace_period(self: Any) -> bool:
        """Check if within grace period after market open (C4)."""
        grace_s = getattr(self, "_market_open_grace_s", 0.0)
        if grace_s <= 0:
            return False
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
        except ImportError:
            return False
        try:
            now = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)
            if not calendar.is_trading_day(now.date()):
                return False
            open_time = calendar.get_session_open(now.date())
            if open_time is None:
                return False
            elapsed = (now - open_time).total_seconds()
            in_grace = 0 <= elapsed <= grace_s
            metrics_registry = getattr(self, "metrics_registry", None)
            if metrics_registry and hasattr(metrics_registry, "market_open_grace_active"):
                metrics_registry.market_open_grace_active.set(1 if in_grace else 0)
            return in_grace
        except Exception:
            return False

    # -- watchdog loop -------------------------------------------------------

    async def _watchdog_loop(self: Any) -> None:
        """Per-symbol feed gap watchdog."""
        while getattr(self, "running", False):
            await asyncio.sleep(getattr(self, "_watchdog_interval_s", 1.0))

            if getattr(self, "state", None) != FeedState.CONNECTED:
                continue

            if getattr(self, "_symbol_gap_skip_off_hours", True) and not self._is_trading_hours():
                self._symbol_gap_consecutive_hits = 0
                now_s = timebase.now_s()
                interval = getattr(self, "_symbol_gap_off_hours_log_interval_s", 300.0)
                if now_s - getattr(self, "_last_symbol_gap_off_hours_log_ts", 0.0) >= interval:
                    logger.info("Skipping symbol gap watchdog outside trading hours")
                    self._last_symbol_gap_off_hours_log_ts = now_s
                continue

            symbol_last_tick: dict[str, float] = getattr(self, "_symbol_last_tick", {})
            if not symbol_last_tick:
                self._symbol_gap_consecutive_hits = 0
                continue
            try:
                tick_snapshot = dict(symbol_last_tick)
            except RuntimeError:
                continue

            now = time.monotonic()
            lookback = getattr(self, "_symbol_gap_active_lookback_s", 90.0)
            if lookback > 0:
                active_snapshot = {
                    symbol: last_ts for symbol, last_ts in tick_snapshot.items() if (now - last_ts) <= lookback
                }
            else:
                active_snapshot = tick_snapshot
            min_active = getattr(self, "_symbol_gap_min_active_symbols", 24)
            if len(active_snapshot) < min_active:
                self._symbol_gap_consecutive_hits = 0
                continue
            stale_symbols: list[tuple[str, float]] = []

            threshold = getattr(self, "_symbol_gap_threshold_s", 6.0)
            if self._is_market_open_grace_period():
                threshold = max(threshold, getattr(self, "_market_open_grace_gap_threshold_s", 30.0))

            for symbol, last_ts in active_snapshot.items():
                gap = now - last_ts
                if gap > threshold:
                    stale_symbols.append((symbol, gap))

            if stale_symbols:
                self._symbol_gap_consecutive_hits += 1
                symbols_str = ", ".join(f"{s}({g:.1f}s)" for s, g in stale_symbols[:5])
                active_count = len(active_snapshot)
                stale_ratio = (len(stale_symbols) / active_count) if active_count > 0 else 0.0
                max_stale_gap = max(g for _, g in stale_symbols)
                hits = self._symbol_gap_consecutive_hits
                if hits == 1 or hits % 10 == 0:
                    logger.warning(
                        "Feed gap detected for symbols",
                        stale_count=len(stale_symbols),
                        active_count=active_count,
                        stale_ratio=round(stale_ratio, 3),
                        symbols=symbols_str,
                        threshold_s=getattr(self, "_symbol_gap_threshold_s", 6.0),
                        consecutive_cycles=hits,
                    )

                min_stale = getattr(self, "_symbol_gap_min_stale_count", 5)
                ratio_threshold = getattr(self, "_symbol_gap_stale_ratio_threshold", 0.85)
                severe_gap = getattr(self, "_symbol_gap_severe_gap_s", 30.0)
                consec_cycles = getattr(self, "_symbol_gap_consecutive_cycles", 5)
                cooldown = getattr(self, "_symbol_gap_resubscribe_cooldown_s", 120.0)
                if (
                    len(stale_symbols) >= min_stale
                    and stale_ratio >= ratio_threshold
                    and max_stale_gap >= max(severe_gap, threshold)
                    and hits >= consec_cycles
                    and (timebase.now_s() - getattr(self, "_last_symbol_gap_resubscribe_ts", 0.0)) >= cooldown
                ):
                    self._last_symbol_gap_resubscribe_ts = timebase.now_s()
                    await self._attempt_resubscribe(max_stale_gap, reason="symbol_gap")
            else:
                self._symbol_gap_consecutive_hits = 0

    # -- monitor loop (reconnect portion) ------------------------------------

    async def _run_monitor_reconnect_checks(self: Any, gap: float) -> None:
        """Reconnection checks called from the monitor loop each cycle."""
        tz = getattr(self, "_reconnect_tzinfo", dt.timezone.utc)
        if getattr(self, "_pending_reconnect_reason", None) and self._within_reconnect_window():
            ok = await self._trigger_reconnect(
                getattr(self, "_pending_reconnect_gap", 0.0),
                reason=getattr(self, "_pending_reconnect_reason", None),
            )
            if ok:
                if getattr(self, "_pending_reconnect_reason", None) == "session_rollover":
                    self._last_rollover_reconnect_date = dt.datetime.fromtimestamp(
                        timebase.now_s(),
                        tz=tz,
                    ).date()
                self._pending_reconnect_reason = None
                self._pending_reconnect_gap = 0.0
                self._pending_reconnect_since = None

        state = getattr(self, "state", None)
        if state == FeedState.CONNECTED:
            heartbeat_threshold_s = getattr(self, "heartbeat_threshold_s", 5.0)
            if gap > heartbeat_threshold_s:
                logger.warning("Heartbeat missing", gap=gap)
            resubscribe_gap_s = getattr(self, "resubscribe_gap_s", 15.0)
            if gap > resubscribe_gap_s:
                await self._attempt_resubscribe(gap, reason="heartbeat_gap")
            force_gap = getattr(self, "force_reconnect_gap_s", 300.0)
            reconnect_gap = getattr(self, "reconnect_gap_s", 60.0)
            if gap > force_gap or (gap > reconnect_gap and getattr(self, "_resubscribe_attempts", 0) > 2):
                await self._request_reconnect(gap, reason="heartbeat_gap")
            if self._should_rollover_reconnect():
                await self._request_reconnect(gap, reason="session_rollover")

        if state in {FeedState.DISCONNECTED, FeedState.RECOVERING}:
            await self._request_reconnect(gap, reason="recovering")
