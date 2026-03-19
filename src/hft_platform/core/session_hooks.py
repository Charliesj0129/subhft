"""Pre/Post market session hooks (WU-11).

Polls market hours to detect session transitions and fires registered
callbacks at pre-market and post-market boundaries.

Disabled by default: ``HFT_SESSION_HOOKS_ENABLED=0``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from enum import Enum
from typing import Any, Awaitable, Callable

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("core.session_hooks")

# Type alias for hook callbacks: sync or async callables with no args.
HookCallback = Callable[[], Any | Awaitable[Any]]

_DEFAULT_POLL_INTERVAL_S = 30.0
_DEFAULT_HOOK_TIMEOUT_S = 30.0


class SessionPhase(Enum):
    """Current market session phase."""

    PRE_MARKET = "pre_market"
    MARKET_OPEN = "market_open"
    POST_MARKET = "post_market"


class SessionHookManager:
    """Manages pre/post market session hooks.

    Polls the :class:`MarketCalendar` at a configurable interval to detect
    session transitions.  When a transition is detected the registered
    callbacks are fired sequentially with a per-hook timeout.

    Config env vars:
        ``HFT_SESSION_HOOKS_ENABLED`` (default ``0``): ``1`` to enable.
        ``HFT_SESSION_HOOKS_POLL_S`` (default ``30``): poll interval seconds.
        ``HFT_SESSION_HOOKS_TIMEOUT_S`` (default ``30``): per-hook timeout.
    """

    __slots__ = (
        "_enabled",
        "_poll_interval_s",
        "_hook_timeout_s",
        "_pre_market_hooks",
        "_post_market_hooks",
        "_phase",
        "_running",
        "_calendar",
    )

    def __init__(self) -> None:
        self._enabled = os.getenv("HFT_SESSION_HOOKS_ENABLED", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._poll_interval_s = float(os.getenv("HFT_SESSION_HOOKS_POLL_S", str(_DEFAULT_POLL_INTERVAL_S)))
        self._hook_timeout_s = float(os.getenv("HFT_SESSION_HOOKS_TIMEOUT_S", str(_DEFAULT_HOOK_TIMEOUT_S)))
        self._pre_market_hooks: list[tuple[str, HookCallback]] = []
        self._post_market_hooks: list[tuple[str, HookCallback]] = []
        self._phase: SessionPhase | None = None
        self._running = False
        self._calendar: Any = None  # Lazy-loaded MarketCalendar

    # -- Registration API ---------------------------------------------------

    def register_pre_market(self, name: str, callback: HookCallback) -> None:
        """Register a callback to run before market opens."""
        self._pre_market_hooks.append((name, callback))
        logger.info("pre_market_hook_registered", hook=name)

    def register_post_market(self, name: str, callback: HookCallback) -> None:
        """Register a callback to run after market closes."""
        self._post_market_hooks.append((name, callback))
        logger.info("post_market_hook_registered", hook=name)

    # -- Phase detection ----------------------------------------------------

    @property
    def phase(self) -> SessionPhase | None:
        return self._phase

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _get_calendar(self) -> Any:
        """Lazy-load the MarketCalendar singleton."""
        if self._calendar is None:
            from hft_platform.core.market_calendar import get_calendar

            self._calendar = get_calendar()
        return self._calendar

    def _detect_phase(self) -> SessionPhase:
        """Determine the current session phase from the market calendar."""
        cal = self._get_calendar()
        try:
            from zoneinfo import ZoneInfo

            tz_name = os.getenv("HFT_TS_TZ", "Asia/Taipei")
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = dt.timezone(dt.timedelta(hours=8))

        now = dt.datetime.now(tz)

        if cal.is_trading_hours(now):
            return SessionPhase.MARKET_OPEN

        # Check if today is a trading day and we are past market close.
        if cal.is_trading_day(now.date()):
            close = cal.get_session_close(now.date())
            if close is not None:
                # Normalize both to offset-aware for comparison.
                if close.tzinfo is None:
                    close = close.replace(tzinfo=tz)
                if now >= close:
                    return SessionPhase.POST_MARKET

        return SessionPhase.PRE_MARKET

    # -- Hook execution -----------------------------------------------------

    async def _fire_hooks(self, hooks: list[tuple[str, HookCallback]], label: str) -> None:
        """Run a list of hooks sequentially with timeout."""
        for name, cb in hooks:
            t0 = timebase.now_ns()
            try:
                result = cb()
                if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                    await asyncio.wait_for(result, timeout=self._hook_timeout_s)
                duration_ms = (timebase.now_ns() - t0) / 1_000_000
                logger.info(
                    "session_hook_executed",
                    phase=label,
                    hook=name,
                    duration_ms=round(duration_ms, 2),
                )
            except asyncio.TimeoutError:
                logger.error(
                    "session_hook_timeout",
                    phase=label,
                    hook=name,
                    timeout_s=self._hook_timeout_s,
                )
            except Exception as exc:
                logger.error(
                    "session_hook_error",
                    phase=label,
                    hook=name,
                    error=str(exc),
                    exc_info=True,
                )

    # -- Main loop ----------------------------------------------------------

    async def run(self) -> None:
        """Main polling loop.  Should be started as a background task."""
        if not self._enabled:
            logger.info("session_hooks_disabled")
            return

        self._running = True
        # Determine initial phase without firing hooks.
        self._phase = self._detect_phase()
        logger.info(
            "session_hooks_started",
            initial_phase=self._phase.value,
            poll_interval_s=self._poll_interval_s,
            pre_hooks=len(self._pre_market_hooks),
            post_hooks=len(self._post_market_hooks),
        )

        while self._running:
            await asyncio.sleep(self._poll_interval_s)
            if not self._running:
                break

            new_phase = self._detect_phase()
            if new_phase == self._phase:
                continue

            old_phase = self._phase
            self._phase = new_phase
            logger.info(
                "session_phase_transition",
                old_phase=old_phase.value if old_phase else "unknown",
                new_phase=new_phase.value,
            )

            # Fire hooks on relevant transitions.
            if new_phase == SessionPhase.MARKET_OPEN and old_phase == SessionPhase.PRE_MARKET:
                await self._fire_hooks(self._pre_market_hooks, "pre_market")
            elif new_phase == SessionPhase.POST_MARKET and old_phase == SessionPhase.MARKET_OPEN:
                await self._fire_hooks(self._post_market_hooks, "post_market")

        logger.info("session_hooks_stopped")

    def stop(self) -> None:
        """Signal the run loop to exit."""
        self._running = False
