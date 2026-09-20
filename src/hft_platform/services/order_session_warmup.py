"""Warm the broker's order session before a trading session opens.

The order facade and the quote facades are separate broker connections. The
quote facades carry market data continuously, so their transport session stays
up; the order facade sends nothing between the close and the next open, and its
Solace session lapses. The first order of the day then pays the handshake:

    THESHOW, 2026-09-16
    00:45:00.131  R47 places its first order of the session
    00:45:00.132  place_order -> SubCode(SessionNotEstablished)
                  "Unable to wait for session '(c4,s1)_sinopac' to be established"
    00:45:00.216  retries exhausted (10 ms + 20 ms) -> DLQ, breaker +1
       ...        37 such failures
    00:45:27.267  last SessionNotEstablished -- the session is finally up

The handshake took 27 s against a 30 ms retry budget, and each exhausted retry
recorded a circuit-breaker failure. Once the breaker opened, every intent that
followed was dead-lettered without ever reaching the broker: across
2026-08-28..09-16 the order DLQ took 334 ``connection_error`` entries and 1,733
``circuit_breaker`` entries, and the 00:45 open minute is the top minute on
every affected day.

Nothing here makes an order wait for the handshake. It is run ahead of the open
instead, on a cheap read-only call, so that by the time a strategy has an
intent the session is already established:

    ... CLOSED .................|<-- lead -->|  OPEN ...............
                                ^            ^
                          warm_once()   first intent lands on a live session
                          (retries here)

Fail-open by construction: a warm-up that never succeeds changes nothing about
how orders are placed. It only removes the handshake from the first order's
critical path.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("services.order_session_warmup")

__all__ = [
    "OrderSessionWarmup",
    "WarmupClock",
    "WarmupPlan",
]

DEFAULT_LEAD_S = 300.0
DEFAULT_TICK_S = 30.0
DEFAULT_ATTEMPTS = 10
DEFAULT_RETRY_INTERVAL_S = 3.0


class _WarmableClient(Protocol):
    def warm_order_session(self) -> bool: ...


@dataclass(frozen=True)
class WarmupPlan:
    """What the clock decided on one tick, and why."""

    warm: bool
    reason: str


class WarmupClock:
    """Decide, tick by tick, whether the order session should be warmed now.

    The window is ``[next_open - lead, next_open)``, expressed the way
    ``QuoteConnectionPool.reconnect_allowed`` expresses it: "closed now, but
    open ``lead`` seconds from now". That phrasing borrows the calendar's
    holiday and half-day handling instead of restating a wall-clock range, and
    it covers both TAIFEX opens (08:45 day, 15:00 night) without naming either.

    One success per window is enough, so the clock latches. It re-arms as soon
    as the window is behind it, which is also what makes a missed window
    self-correcting: the next one warms again from scratch.
    """

    __slots__ = ("_lead_s", "_warmed")

    def __init__(self, lead_s: float = DEFAULT_LEAD_S) -> None:
        self._lead_s = float(lead_s)
        self._warmed = False

    @property
    def lead_s(self) -> float:
        return self._lead_s

    @property
    def warmed(self) -> bool:
        return self._warmed

    def decide(self, *, trading_now: bool, trading_after_lead: bool) -> WarmupPlan:
        """Return whether to warm on this tick.

        ``trading_now`` and ``trading_after_lead`` are the calendar's answers
        for now and for ``now + lead``. Taking them as arguments keeps the
        decision testable without a clock or a calendar.
        """
        if trading_now:
            # Already inside a session: the order path is the warm-up now, and
            # anything this method did would only add a call under load.
            self._warmed = False
            return WarmupPlan(warm=False, reason="session_open")
        if not trading_after_lead:
            self._warmed = False
            return WarmupPlan(warm=False, reason="no_open_ahead")
        if self._warmed:
            return WarmupPlan(warm=False, reason="already_warm")
        return WarmupPlan(warm=True, reason="pre_open")

    def mark(self, ok: bool) -> None:
        """Record the outcome of a warm-up attempted in this window.

        Only a success latches. A failure leaves the clock armed so the
        remaining ticks in the window try again -- the handshake is exactly the
        thing that needs more than one attempt.
        """
        if ok:
            self._warmed = True


class OrderSessionWarmup:
    """Runs :class:`WarmupClock` against the calendar and the order client."""

    __slots__ = (
        "_client",
        "_clock",
        "_tick_s",
        "_attempts",
        "_retry_interval_s",
        "_product_type",
        "_metrics",
        "_window_source",
    )

    def __init__(
        self,
        client: _WarmableClient,
        *,
        lead_s: float | None = None,
        tick_s: float | None = None,
        attempts: int | None = None,
        retry_interval_s: float | None = None,
        product_type: str = "future",
        metrics: Any | None = None,
        window_source: Callable[[], tuple[bool, bool] | None] | None = None,
    ) -> None:
        self._client = client
        self._clock = WarmupClock(_pick(lead_s, "HFT_ORDER_WARMUP_LEAD_S", DEFAULT_LEAD_S))
        # Injected so the loop can be driven across a whole trading day without
        # a clock; production leaves it None and reads the calendar.
        self._window_source = window_source if window_source is not None else self._calendar_window
        self._tick_s = _pick(tick_s, "HFT_ORDER_WARMUP_TICK_S", DEFAULT_TICK_S)
        self._attempts = int(_pick(attempts, "HFT_ORDER_WARMUP_ATTEMPTS", DEFAULT_ATTEMPTS))
        self._retry_interval_s = _pick(retry_interval_s, "HFT_ORDER_WARMUP_RETRY_S", DEFAULT_RETRY_INTERVAL_S)
        self._product_type = product_type
        self._metrics = metrics

    @property
    def clock(self) -> WarmupClock:
        return self._clock

    def _calendar_window(self) -> tuple[bool, bool] | None:
        """Return (trading_now, trading_after_lead), or None if unknown.

        Unknown is not "closed": a calendar that cannot answer must not be read
        as an open one lead away, or the warm-up would fire on every tick.
        """
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.fromtimestamp(timebase.now_s(), tz=calendar._tz)
            ahead = now_dt + dt.timedelta(seconds=self._clock.lead_s)
            return (
                bool(calendar.is_trading_hours(now_dt, product_type=self._product_type)),
                bool(calendar.is_trading_hours(ahead, product_type=self._product_type)),
            )
        except Exception as exc:  # noqa: BLE001 — a calendar failure must not kill the clock
            logger.debug("order_warmup_calendar_unavailable", error=str(exc))
            return None

    async def warm_once(self) -> bool:
        """Probe the order session until it answers, or the budget runs out.

        Every probe is a cheap read-only broker call run off the event loop.
        Repetition is the point: the SDK gives up waiting for the session long
        before it is established, but each attempt keeps the handshake moving --
        which is how the 2026-09-16 open eventually recovered, after 37 tries.
        """
        loop = asyncio.get_running_loop()
        for attempt in range(1, max(1, self._attempts) + 1):
            try:
                ok = await loop.run_in_executor(None, self._client.warm_order_session)
            except Exception as exc:  # noqa: BLE001 — broker SDK
                logger.warning("order_session_warmup_error", attempt=attempt, error=str(exc))
                ok = False
            if ok:
                self._bump("ok")
                logger.info("order_session_warmed", attempts=attempt)
                return True
            if attempt < max(1, self._attempts):
                await asyncio.sleep(self._retry_interval_s)
        self._bump("failed")
        logger.warning(
            "order_session_warmup_exhausted",
            attempts=max(1, self._attempts),
            note="orders will pay the handshake on the first intent",
        )
        return False

    async def run(self) -> None:
        """Tick forever, warming the order session ahead of each open."""
        logger.info(
            "order_session_warmup_started",
            lead_s=self._clock.lead_s,
            tick_s=self._tick_s,
            attempts=self._attempts,
        )
        while True:
            try:
                window = self._window_source()
                if window is None:
                    self._bump("unknown")
                else:
                    trading_now, trading_after_lead = window
                    plan = self._clock.decide(
                        trading_now=trading_now,
                        trading_after_lead=trading_after_lead,
                    )
                    if plan.warm:
                        logger.info("order_session_warmup_window", reason=plan.reason, lead_s=self._clock.lead_s)
                        self._clock.mark(await self.warm_once())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — one bad tick must not end the clock
                logger.warning("order_session_warmup_tick_failed", error=str(exc))
            await asyncio.sleep(self._tick_s)

    def _bump(self, result: str) -> None:
        metrics = self._metrics
        if metrics is None:
            try:
                from hft_platform.observability.metrics import MetricsRegistry

                metrics = MetricsRegistry.get()
            except Exception as exc:  # noqa: BLE001
                logger.debug("operation_fallback", error=str(exc))
                return
        counter = getattr(metrics, "order_session_warmup_total", None)
        if counter is None:
            return
        try:
            counter.labels(result=result).inc()
        except Exception as exc:  # noqa: BLE001 — metrics must never block the clock
            logger.debug("operation_fallback", error=str(exc))


def _pick(explicit: float | int | None, env_var: str, default: float) -> float:
    """An explicit argument wins, then the env var, then the default."""
    if explicit is not None:
        return float(explicit)
    return _env_float(env_var, default)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("order_warmup_env_invalid", var=name, value=raw, using=default)
        return default
