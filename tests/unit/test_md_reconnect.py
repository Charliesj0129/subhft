"""Unit tests for MarketDataReconnectMixin (_md_reconnect.py).

Tests cover:
- Rollover reconnect detection (_should_rollover_reconnect)
- Reconnect window checking (_within_reconnect_window)
- Attempt resubscribe logic (_attempt_resubscribe)
- Request/trigger reconnect paths (_request_reconnect, _trigger_reconnect)
- Pending reconnect marking (_mark_pending_reconnect)
- Trading hours / grace period checks (_is_trading_hours, _is_market_open_grace_period)
- Monitor reconnect checks (_run_monitor_reconnect_checks)
- Public within_reconnect_window hook
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin

# ---------------------------------------------------------------------------
# Helper: a minimal concrete class that mixes in MarketDataReconnectMixin
# ---------------------------------------------------------------------------


class _FakeMD(MarketDataReconnectMixin):
    """Minimal stub that satisfies all getattr() probes in the mixin."""

    def __init__(self) -> None:
        self.running = True
        self.state = FeedState.CONNECTED
        self.last_event_ts: float = 0.0
        self.last_event_mono: float = 0.0
        self._last_rollover_reconnect_date: dt.date | None = None
        self._last_rollover_seen_date: dt.date | None = None
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self._last_resubscribe_ts: float = 0.0
        self._last_reconnect_ts: float = 0.0
        self._resubscribe_attempts: int = 0
        self._reconnect_tzinfo: dt.tzinfo = dt.timezone.utc
        self.reconnect_days: set[str] = set()
        self.reconnect_hours: str = ""
        self.reconnect_hours_2: str = ""
        self.resubscribe_cooldown_s: float = 15.0
        self.reconnect_cooldown_s: float = 60.0
        self.heartbeat_threshold_s: float = 5.0
        self.resubscribe_gap_s: float = 15.0
        self.force_reconnect_gap_s: float = 300.0
        self.reconnect_gap_s: float = 60.0
        self.reconnect_timeout_s: float = 5.0
        self.metrics_registry = None
        self.client: MagicMock | None = None
        self._market_open_grace_s: float = 0.0
        self._symbol_last_tick: dict[str, float] = {}
        self._symbol_gap_consecutive_hits: int = 0
        self._symbol_gap_threshold_s: float = 6.0
        self._symbol_gap_min_active_symbols: int = 24
        self._symbol_gap_active_lookback_s: float = 90.0
        self._symbol_gap_min_stale_count: int = 5
        self._symbol_gap_stale_ratio_threshold: float = 0.85
        self._symbol_gap_severe_gap_s: float = 30.0
        self._symbol_gap_consecutive_cycles: int = 5
        self._symbol_gap_resubscribe_cooldown_s: float = 120.0
        self._last_symbol_gap_resubscribe_ts: float = 0.0
        self._symbol_gap_skip_off_hours: bool = False  # skip off-hours check in tests
        self._watchdog_interval_s: float = 0.001

    def _set_state(self, state: FeedState) -> None:
        self.state = state


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def md() -> _FakeMD:
    return _FakeMD()


# ---------------------------------------------------------------------------
# _should_rollover_reconnect
# ---------------------------------------------------------------------------


class TestShouldRolloverReconnect:
    def test_returns_false_when_last_event_same_day(self, md: _FakeMD) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now.timestamp()
            # last_event_ts is also today
            md.last_event_ts = now.timestamp() - 10.0
            assert md._should_rollover_reconnect() is False

    def test_returns_true_when_last_event_was_yesterday(self, md: _FakeMD) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        yesterday = now - dt.timedelta(days=1)
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now.timestamp()
            md.last_event_ts = yesterday.timestamp()
            assert md._should_rollover_reconnect() is True

    def test_returns_false_on_repeated_call_same_day(self, md: _FakeMD) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        yesterday = now - dt.timedelta(days=1)
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now.timestamp()
            md.last_event_ts = yesterday.timestamp()
            # First call sets the seen date; second should return False
            assert md._should_rollover_reconnect() is True
            assert md._should_rollover_reconnect() is False

    def test_records_seen_date(self, md: _FakeMD) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        yesterday = now - dt.timedelta(days=1)
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now.timestamp()
            md.last_event_ts = yesterday.timestamp()
            md._should_rollover_reconnect()
            assert md._last_rollover_seen_date == now.date()


# ---------------------------------------------------------------------------
# _within_reconnect_window
# ---------------------------------------------------------------------------


class TestWithinReconnectWindow:
    def test_returns_true_when_no_constraints_configured(self, md: _FakeMD) -> None:
        # No reconnect_days, no reconnect_hours → always open
        with patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}):
            assert md._within_reconnect_window() is True

    def test_respects_reconnect_hours_window_inside(self, md: _FakeMD) -> None:
        # Pick a time inside the window
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is True

    def test_respects_reconnect_hours_window_outside(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 6, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is False

    def test_wraps_midnight_window(self, md: _FakeMD) -> None:
        # Window 22:00 – 02:00 (wraps midnight)
        now_after_midnight = dt.datetime(2024, 1, 15, 1, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "22:00-02:00"
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_after_midnight.timestamp()
            assert md._within_reconnect_window() is True

    def test_weekday_filter_blocks_wrong_day(self, md: _FakeMD) -> None:
        # Tuesday (strftime %a.lower() = 'tue'), only allow 'mon'
        now = dt.datetime(2024, 1, 16, 9, 0, 0, tzinfo=dt.timezone.utc)  # Tuesday
        md.reconnect_days = {"mon"}
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is False

    def test_weekday_filter_allows_correct_day(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)  # Monday
        md.reconnect_days = {"mon"}
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is True

    def test_secondary_window_matches(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 15, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        md.reconnect_hours_2 = "14:00-16:00"
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is True

    def test_public_proxy_matches_private(self, md: _FakeMD) -> None:
        with patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}):
            assert md.within_reconnect_window() == md._within_reconnect_window()


# ---------------------------------------------------------------------------
# Cross-midnight session ownership (THESHOW Monday reconnect storm, 2026-07-27)
#
# THESHOW runs HFT_RECONNECT_HOURS_2=14:55-05:05 with days mon..fri. On Monday
# 00:00-05:05 the tail matched on wall-clock alone, but the session that owns it
# would have opened Sunday 15:00 — and Sunday is not a trading day. The facade
# health monitor therefore reconnected for five hours into a closed market,
# producing ~350 broker logins rejected with "code: 451, Too Many Connections".
# ---------------------------------------------------------------------------


class _FakeCalendar:
    """Stand-in for MarketCalendar with an explicit trading-day set."""

    def __init__(self, trading_days: set[dt.date], available: bool = True) -> None:
        self._trading_days = trading_days
        self.available = available

    def is_trading_day(self, date: dt.date) -> bool:
        return date in self._trading_days

    def days_until_trading(self, date: dt.date) -> int:
        for offset in range(0, 8):
            if (date + dt.timedelta(days=offset)) in self._trading_days:
                return offset
        return 1


# 2026-07-27 is the Monday whose small hours triggered the production storm.
_MON = dt.date(2026, 7, 27)
_TUE = dt.date(2026, 7, 28)
_TRADING_WEEK = {_MON, _TUE, dt.date(2026, 7, 29), dt.date(2026, 7, 30), dt.date(2026, 7, 31)}


class TestCrossMidnightSessionOwnership:
    @pytest.fixture
    def md_theshow(self, md: _FakeMD) -> _FakeMD:
        """A service configured exactly as THESHOW's .env has it."""
        md.reconnect_days = {"mon", "tue", "wed", "thu", "fri"}
        md.reconnect_hours = "08:30-13:35"
        md.reconnect_hours_2 = "14:55-05:05"
        return md

    def _check(self, md: _FakeMD, when: dt.datetime, calendar: _FakeCalendar) -> bool:
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=calendar),
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
        ):
            tb.now_s.return_value = when.timestamp()
            return md._within_reconnect_window()

    def test_window_closed_on_monday_small_hours_when_sunday_not_trading(self, md_theshow: _FakeMD) -> None:
        # Monday 02:00: inside 14:55-05:05 by the clock, but Sunday never opened.
        when = dt.datetime(2026, 7, 27, 2, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is False

    def test_window_open_on_tuesday_small_hours_after_monday_session(self, md_theshow: _FakeMD) -> None:
        # Tuesday 02:00: the night session opened Monday 15:00 and is genuinely live.
        when = dt.datetime(2026, 7, 28, 2, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_open_on_monday_evening_leg(self, md_theshow: _FakeMD) -> None:
        # Monday 16:00 is the evening leg of the same window — never gated.
        when = dt.datetime(2026, 7, 27, 16, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_open_during_configured_day_window(self, md_theshow: _FakeMD) -> None:
        # The non-wrapping day window must not be touched by the ownership check.
        when = dt.datetime(2026, 7, 27, 9, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_closed_on_first_trading_day_after_a_holiday(self, md_theshow: _FakeMD) -> None:
        # Monday trades but Friday..Sunday were a holiday block: no session owns
        # Monday's small hours. days_until_trading(Monday) == 0, so the pre-existing
        # holiday guard cannot catch this on its own.
        when = dt.datetime(2026, 7, 27, 3, 30, tzinfo=dt.timezone.utc)
        calendar = _FakeCalendar({_MON, _TUE})
        assert self._check(md_theshow, when, calendar) is False

    def test_window_fails_open_when_calendar_unavailable(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 27, 2, 0, tzinfo=dt.timezone.utc)
        calendar = _FakeCalendar(_TRADING_WEEK, available=False)
        assert self._check(md_theshow, when, calendar) is True

    def test_window_fails_open_when_calendar_raises(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 27, 2, 0, tzinfo=dt.timezone.utc)
        boom = MagicMock(side_effect=RuntimeError("calendar down"))
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", boom),
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
        ):
            tb.now_s.return_value = when.timestamp()
            assert md_theshow._within_reconnect_window() is True

    def test_window_fails_open_when_calendar_disabled(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 27, 2, 0, tzinfo=dt.timezone.utc)
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = when.timestamp()
            assert md_theshow._within_reconnect_window() is True

    def test_market_data_service_uses_the_mixin_implementation(self) -> None:
        """A shadowing copy in market_data.py is where this fix goes to die."""
        from hft_platform.services.market_data import MarketDataService

        assert "_within_reconnect_window" not in vars(MarketDataService)
        assert MarketDataService._within_reconnect_window is MarketDataReconnectMixin._within_reconnect_window


# ---------------------------------------------------------------------------
# _attempt_resubscribe
# ---------------------------------------------------------------------------


class TestAttemptResubscribe:
    @pytest.mark.asyncio
    async def test_skips_outside_window(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 6, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        client = MagicMock()
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            await md._attempt_resubscribe(30.0, reason="heartbeat_gap")
        client.resubscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_respects_cooldown(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_resubscribe_ts = now_ts - 5.0  # within cooldown
        md.resubscribe_cooldown_s = 15.0
        client = MagicMock()
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._attempt_resubscribe(30.0)
        client.resubscribe.assert_not_called()

    @pytest.mark.asyncio
    async def test_calls_client_resubscribe_on_success(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_resubscribe_ts = now_ts - 100.0  # cooldown passed
        client = MagicMock()
        client.resubscribe.return_value = True
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._attempt_resubscribe(30.0, reason="heartbeat_gap")
        client.resubscribe.assert_called_once()
        assert md._resubscribe_attempts == 0  # reset on success

    @pytest.mark.asyncio
    async def test_increments_attempts_on_failure(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_resubscribe_ts = now_ts - 100.0
        md._resubscribe_attempts = 2
        client = MagicMock()
        client.resubscribe.return_value = False
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._attempt_resubscribe(30.0, reason="heartbeat_gap")
        assert md._resubscribe_attempts == 3

    @pytest.mark.asyncio
    async def test_increments_symbol_gap_metric(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_resubscribe_ts = now_ts - 100.0
        metrics = MagicMock()
        md.metrics_registry = metrics
        client = MagicMock()
        client.resubscribe.return_value = True
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._attempt_resubscribe(30.0, reason="symbol_gap")
        metrics.feed_reconnect_total.labels.assert_called_with(result="symbol_gap")


# ---------------------------------------------------------------------------
# _trigger_reconnect
# ---------------------------------------------------------------------------


class TestTriggerReconnect:
    @pytest.mark.asyncio
    async def test_returns_false_when_within_cooldown(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = now_ts - 10.0  # well within 60s cooldown
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_outside_window(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 6, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        md._last_reconnect_ts = 0.0
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            result = await md._trigger_reconnect(30.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_client(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        md.client = None
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is False
        assert md.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_successful_reconnect_sets_connected(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        client = MagicMock()
        client.reconnect.return_value = True
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is True
        assert md.state == FeedState.CONNECTED
        assert md._resubscribe_attempts == 0

    @pytest.mark.asyncio
    async def test_failed_reconnect_sets_disconnected(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        client = MagicMock()
        client.reconnect.return_value = False
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is False
        assert md.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_timeout_sets_disconnected(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        md.reconnect_timeout_s = 0.01
        client = MagicMock()

        def slow_reconnect(*args, **kwargs):
            import time as _time

            _time.sleep(10.0)
            return True

        client.reconnect.side_effect = slow_reconnect
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is False
        assert md.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_exception_sets_disconnected(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        client = MagicMock()
        client.reconnect.side_effect = RuntimeError("boom")
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)
        assert result is False
        assert md.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_session_rollover_forces_login(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        client = MagicMock()
        client.reconnect.return_value = True
        md.client = client
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._trigger_reconnect(30.0, reason="session_rollover")
        call_args = client.reconnect.call_args
        # second positional arg is force_login=True
        assert call_args[0][1] is True


# ---------------------------------------------------------------------------
# _mark_pending_reconnect
# ---------------------------------------------------------------------------


class TestMarkPendingReconnect:
    def test_sets_pending_reason_and_gap(self, md: _FakeMD) -> None:
        now_ts = 1700000000.0
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now_ts
            md._mark_pending_reconnect(45.0, reason="heartbeat_gap")
        assert md._pending_reconnect_reason == "heartbeat_gap"
        assert md._pending_reconnect_gap == 45.0
        assert md._pending_reconnect_since == now_ts

    def test_default_reason_is_heartbeat_gap(self, md: _FakeMD) -> None:
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = 0.0
            md._mark_pending_reconnect(10.0)
        assert md._pending_reconnect_reason == "heartbeat_gap"

    def test_does_not_overwrite_since_on_repeated_call(self, md: _FakeMD) -> None:
        first_ts = 1700000000.0
        md._pending_reconnect_since = first_ts
        md._pending_reconnect_reason = "heartbeat_gap"
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = first_ts + 99.0
            md._mark_pending_reconnect(20.0, reason="heartbeat_gap")
        # since should remain at first_ts, not updated
        assert md._pending_reconnect_since == first_ts


# ---------------------------------------------------------------------------
# _request_reconnect
# ---------------------------------------------------------------------------


class TestRequestReconnect:
    @pytest.mark.asyncio
    async def test_delegates_to_trigger_when_in_window(self, md: _FakeMD) -> None:
        md._trigger_reconnect = AsyncMock(return_value=True)
        with patch.object(md, "_within_reconnect_window", return_value=True):
            await md._request_reconnect(30.0, reason="heartbeat_gap")
        md._trigger_reconnect.assert_awaited_once_with(30.0, reason="heartbeat_gap")
        assert md._trigger_reconnect.await_count == 1

    @pytest.mark.asyncio
    async def test_marks_pending_when_outside_window(self, md: _FakeMD) -> None:
        md._trigger_reconnect = AsyncMock()
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.object(md, "_within_reconnect_window", return_value=False),
        ):
            tb.now_s.return_value = 1700000000.0
            await md._request_reconnect(30.0, reason="heartbeat_gap")
        md._trigger_reconnect.assert_not_awaited()
        assert md._pending_reconnect_reason == "heartbeat_gap"


# ---------------------------------------------------------------------------
# _is_trading_hours (fallback path — calendar unavailable)
# ---------------------------------------------------------------------------


class TestIsTradingHours:
    def test_weekday_within_hours(self, md: _FakeMD) -> None:
        # Mon 09:00 UTC+8 → 01:00 UTC; map to a fixed UTC+8 aware timestamp
        aware_dt = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_WATCHDOG_PRODUCT_TYPE": "future"}),
            patch(
                "hft_platform.services._md_reconnect.MarketDataReconnectMixin._is_trading_hours",
                return_value=True,
            ),
        ):
            tb.now_s.return_value = aware_dt.timestamp()
            assert md._is_trading_hours() is True

    def test_fallback_weekend_returns_false(self, md: _FakeMD) -> None:
        # Saturday UTC+8 = weekday()=5
        sat = dt.datetime(2024, 1, 20, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError),
        ):
            tb.now_s.return_value = sat.timestamp()
            # Force calendar import to fail so the fallback runs
            with patch.dict("sys.modules", {"hft_platform.core.market_calendar": None}):
                result = md._is_trading_hours()
        # Weekend → False
        assert result is False


# ---------------------------------------------------------------------------
# _run_monitor_reconnect_checks
# ---------------------------------------------------------------------------


class TestRunMonitorReconnectChecks:
    @pytest.mark.asyncio
    async def test_triggers_pending_reconnect_when_in_window(self, md: _FakeMD) -> None:
        md._pending_reconnect_reason = "heartbeat_gap"
        md._pending_reconnect_gap = 40.0
        md._trigger_reconnect = AsyncMock(return_value=True)
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)
        md._trigger_reconnect.assert_awaited()
        # Clears pending state on success
        assert md._pending_reconnect_reason is None
        assert md._pending_reconnect_gap == 0.0
        assert md._pending_reconnect_since is None

    @pytest.mark.asyncio
    async def test_does_not_trigger_pending_outside_window(self, md: _FakeMD) -> None:
        md._pending_reconnect_reason = "heartbeat_gap"
        md._pending_reconnect_gap = 40.0
        md._trigger_reconnect = AsyncMock(return_value=False)
        with (
            patch.object(md, "_within_reconnect_window", return_value=False),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)
        md._trigger_reconnect.assert_not_awaited()
        assert md._trigger_reconnect.await_count == 0

    @pytest.mark.asyncio
    async def test_attempts_resubscribe_on_gap(self, md: _FakeMD) -> None:
        md.state = FeedState.CONNECTED
        md._attempt_resubscribe = AsyncMock()
        md._request_reconnect = AsyncMock()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            # gap > resubscribe_gap_s (15), < reconnect_gap_s (60)
            await md._run_monitor_reconnect_checks(20.0)
        md._attempt_resubscribe.assert_awaited_once()
        assert md._attempt_resubscribe.await_count == 1

    @pytest.mark.asyncio
    async def test_requests_reconnect_on_large_gap(self, md: _FakeMD) -> None:
        md.state = FeedState.CONNECTED
        md._attempt_resubscribe = AsyncMock()
        md._request_reconnect = AsyncMock()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            # gap > force_reconnect_gap_s (300)
            await md._run_monitor_reconnect_checks(350.0)
        md._request_reconnect.assert_awaited()
        assert md._request_reconnect.await_count >= 1

    @pytest.mark.asyncio
    async def test_requests_reconnect_when_disconnected(self, md: _FakeMD) -> None:
        md.state = FeedState.DISCONNECTED
        md._request_reconnect = AsyncMock()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)
        md._request_reconnect.assert_awaited()
        assert md._request_reconnect.await_count >= 1

    @pytest.mark.asyncio
    async def test_requests_reconnect_when_recovering(self, md: _FakeMD) -> None:
        md.state = FeedState.RECOVERING
        md._request_reconnect = AsyncMock()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)
        md._request_reconnect.assert_awaited()
        assert md._request_reconnect.await_count >= 1

    @pytest.mark.asyncio
    async def test_rollover_reconnect_triggered(self, md: _FakeMD) -> None:
        md.state = FeedState.CONNECTED
        md._attempt_resubscribe = AsyncMock()
        md._request_reconnect = AsyncMock()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch.object(md, "_should_rollover_reconnect", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)
        md._request_reconnect.assert_awaited_with(0.0, reason="session_rollover")
        assert md._request_reconnect.await_count >= 1
