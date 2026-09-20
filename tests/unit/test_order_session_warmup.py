"""The first order of a session must not pay the transport handshake.

Production shape these are written from (THESHOW, 2026-09-16): the order
facade had been idle since the previous close, so ``place_order`` at
00:45:00.131 returned ``SubCode(SessionNotEstablished)`` and kept returning it
for 27 s. The retry budget is 10 ms + 20 ms, so every intent in that window
exhausted its retries, recorded a circuit-breaker failure, and was
dead-lettered -- 181 DLQ entries that day, none of which ever reached the
broker.

These tests pin the two halves of the remedy: *when* the warm-up runs (once per
pre-open window, re-armed after it), and *how* it probes (repeatedly, because
one probe is never enough -- the real open took 37).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from hft_platform.services.order_session_warmup import (
    OrderSessionWarmup,
    WarmupClock,
)


class FakeClient:
    """Order client whose probe answers on the Nth attempt."""

    def __init__(self, succeed_on: int | None = 1, raises: bool = False) -> None:
        self.succeed_on = succeed_on
        self.raises = raises
        self.calls = 0

    def warm_order_session(self) -> bool:
        self.calls += 1
        if self.raises:
            raise RuntimeError("SolClient send request api/v1/auth/usage, code: NotReady")
        return self.succeed_on is not None and self.calls >= self.succeed_on


class TestWarmupClock:
    def test_closed_now_and_open_after_the_lead_warms(self) -> None:
        clock = WarmupClock(lead_s=300.0)
        plan = clock.decide(trading_now=False, trading_after_lead=True)
        assert plan.warm is True
        assert plan.reason == "pre_open"

    def test_an_open_session_does_not_warm(self) -> None:
        """Inside a session the order path is its own warm-up."""
        clock = WarmupClock()
        plan = clock.decide(trading_now=True, trading_after_lead=True)
        assert plan.warm is False
        assert plan.reason == "session_open"

    def test_a_closed_market_with_no_open_ahead_does_not_warm(self) -> None:
        clock = WarmupClock()
        plan = clock.decide(trading_now=False, trading_after_lead=False)
        assert plan.warm is False
        assert plan.reason == "no_open_ahead"

    def test_a_successful_warm_does_not_repeat_within_the_window(self) -> None:
        clock = WarmupClock()
        assert clock.decide(trading_now=False, trading_after_lead=True).warm is True
        clock.mark(True)
        plan = clock.decide(trading_now=False, trading_after_lead=True)
        assert plan.warm is False
        assert plan.reason == "already_warm"

    def test_a_failed_warm_is_retried_on_the_next_tick(self) -> None:
        """The handshake is exactly what needs more than one attempt."""
        clock = WarmupClock()
        assert clock.decide(trading_now=False, trading_after_lead=True).warm is True
        clock.mark(False)
        assert clock.decide(trading_now=False, trading_after_lead=True).warm is True

    def test_the_clock_rearms_for_the_next_session(self) -> None:
        """TAIFEX opens twice a day; warming once must not disarm the night."""
        clock = WarmupClock()
        clock.decide(trading_now=False, trading_after_lead=True)
        clock.mark(True)
        clock.decide(trading_now=True, trading_after_lead=True)  # session opened
        clock.decide(trading_now=False, trading_after_lead=False)  # session closed
        assert clock.decide(trading_now=False, trading_after_lead=True).warm is True

    def test_an_open_session_rearms_the_latch(self) -> None:
        clock = WarmupClock()
        clock.decide(trading_now=False, trading_after_lead=True)
        clock.mark(True)
        assert clock.warmed is True
        clock.decide(trading_now=True, trading_after_lead=True)
        assert clock.warmed is False


class TestWarmOnce:
    @pytest.mark.asyncio
    async def test_a_session_that_answers_immediately_probes_once(self) -> None:
        client = FakeClient(succeed_on=1)
        warmup = OrderSessionWarmup(client, attempts=10, retry_interval_s=0.0)
        assert await warmup.warm_once() is True
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_the_probe_repeats_until_the_handshake_completes(self) -> None:
        """The 2026-09-16 shape: the session came up on a later attempt."""
        client = FakeClient(succeed_on=5)
        warmup = OrderSessionWarmup(client, attempts=10, retry_interval_s=0.0)
        assert await warmup.warm_once() is True
        assert client.calls == 5

    @pytest.mark.asyncio
    async def test_an_unreachable_session_exhausts_a_bounded_budget(self) -> None:
        client = FakeClient(succeed_on=None)
        warmup = OrderSessionWarmup(client, attempts=4, retry_interval_s=0.0)
        assert await warmup.warm_once() is False
        assert client.calls == 4

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_is_a_failure_not_a_crash(self) -> None:
        client = FakeClient(raises=True)
        warmup = OrderSessionWarmup(client, attempts=3, retry_interval_s=0.0)
        assert await warmup.warm_once() is False
        assert client.calls == 3

    @pytest.mark.asyncio
    async def test_a_zero_attempt_budget_still_probes_once(self) -> None:
        """A misconfigured budget must not silently disable the warm-up."""
        client = FakeClient(succeed_on=1)
        warmup = OrderSessionWarmup(client, attempts=0, retry_interval_s=0.0)
        assert await warmup.warm_once() is True
        assert client.calls == 1


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_the_loop_warms_once_per_pre_open_window(self) -> None:
        client = FakeClient(succeed_on=1)
        # closed with an open ahead on every tick: the latch, not the window,
        # is what must stop the second warm-up.
        warmup = OrderSessionWarmup(
            client, tick_s=0.0, attempts=2, retry_interval_s=0.0, window_source=lambda: (False, True)
        )

        task = asyncio.create_task(warmup.run())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_a_calendar_that_cannot_answer_does_not_warm(self) -> None:
        """Unknown must not read as 'an open is one lead away'."""
        client = FakeClient(succeed_on=1)
        warmup = OrderSessionWarmup(client, tick_s=0.0, attempts=2, retry_interval_s=0.0, window_source=lambda: None)

        task = asyncio.create_task(warmup.run())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.calls == 0

    @pytest.mark.asyncio
    async def test_one_failing_tick_does_not_end_the_clock(self) -> None:
        """A watchdog that dies on its first bad tick protects nothing."""
        client = FakeClient(succeed_on=1)
        calls = {"n": 0}

        def _window() -> tuple[bool, bool]:
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("calendar exploded")
            return (False, True)

        warmup = OrderSessionWarmup(client, tick_s=0.0, attempts=2, retry_interval_s=0.0, window_source=_window)

        task = asyncio.create_task(warmup.run())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert calls["n"] > 1
        assert client.calls == 1

    @pytest.mark.asyncio
    async def test_the_loop_does_not_warm_during_an_open_session(self) -> None:
        client = FakeClient(succeed_on=1)
        warmup = OrderSessionWarmup(
            client, tick_s=0.0, attempts=2, retry_interval_s=0.0, window_source=lambda: (True, True)
        )

        task = asyncio.create_task(warmup.run())
        for _ in range(20):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.calls == 0


class TestCalendarWindow:
    def test_the_window_is_read_from_the_calendar_for_futures(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Both TAIFEX opens must be covered without naming either."""
        import hft_platform.core.market_calendar as market_calendar

        seen: list[str | None] = []

        class FakeCalendar:
            _tz = __import__("datetime").timezone(__import__("datetime").timedelta(hours=8))

            def is_trading_hours(self, ts: Any, product_type: str | None = None) -> bool:
                seen.append(product_type)
                return False

        monkeypatch.setattr(market_calendar, "get_calendar", lambda: FakeCalendar())
        warmup = OrderSessionWarmup(FakeClient())
        assert warmup._calendar_window() == (False, False)
        assert seen == ["future", "future"]

    def test_a_calendar_failure_reports_unknown_rather_than_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hft_platform.core.market_calendar as market_calendar

        def _boom() -> Any:
            raise RuntimeError("no calendar")

        monkeypatch.setattr(market_calendar, "get_calendar", _boom)
        warmup = OrderSessionWarmup(FakeClient())
        assert warmup._calendar_window() is None


def test_bootstrap_schedules_the_warmup_for_a_client_that_supports_it() -> None:
    """The clock is useless unless something runs it on the engine loop."""
    source = Path("src/hft_platform/services/bootstrap.py").read_text(encoding="utf-8")
    assert 'hasattr(order_client, "warm_order_session")' in source
    assert "OrderSessionWarmup(order_client).run()" in source
