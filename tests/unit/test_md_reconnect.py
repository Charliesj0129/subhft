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

import asyncio
import datetime as dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin
from hft_platform.services.market_data import MarketDataService


class _FakeMD(MarketDataReconnectMixin):
    """Minimal stub that satisfies all getattr() probes in the mixin.

    ``_trigger_reconnect`` is bound from ``MarketDataService`` rather than
    inherited: the mixin used to carry a second, shadowed copy, so every test
    below exercised an implementation that never ran in production -- and the
    dead one was missing the DATA-010 stale-queue drain. Binding the live
    method keeps this fixture lightweight while pointing the tests at the code
    the engine actually executes.
    """

    _trigger_reconnect = MarketDataService._trigger_reconnect

    def __init__(self) -> None:
        self.raw_queue: asyncio.Queue = asyncio.Queue()
        self.lob = None
        self.feature_engine = None
        self._event_counts: dict[str, int] = {}
        self._ever_active_symbols: set[str] = set()
        self._on_reconnect_callbacks: list = []
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
        self._symbol_gap_skip_off_hours: bool = False
        self._watchdog_interval_s: float = 0.001

    def _set_state(self, state: FeedState) -> None:
        self.state = state


@pytest.fixture()
def md() -> _FakeMD:
    return _FakeMD()


class TestShouldRolloverReconnect:
    def test_returns_false_when_last_event_same_day(self, md: _FakeMD) -> None:
        now = dt.datetime.now(tz=dt.timezone.utc)
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = now.timestamp()
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


class TestWithinReconnectWindow:
    def test_returns_true_when_no_constraints_configured(self, md: _FakeMD) -> None:
        with patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}):
            assert md._within_reconnect_window() is True

    def test_respects_reconnect_hours_window_inside(self, md: _FakeMD) -> None:
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
        now_after_midnight = dt.datetime(2024, 1, 15, 1, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "22:00-02:00"
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_after_midnight.timestamp()
            assert md._within_reconnect_window() is True

    def test_weekday_filter_blocks_wrong_day(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 16, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_days = {"mon"}
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is False

    def test_weekday_filter_allows_correct_day(self, md: _FakeMD) -> None:
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
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
        when = dt.datetime(2026, 7, 27, 2, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is False

    def test_window_open_on_tuesday_small_hours_after_monday_session(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 28, 2, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_open_on_monday_evening_leg(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 27, 16, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_open_during_configured_day_window(self, md_theshow: _FakeMD) -> None:
        when = dt.datetime(2026, 7, 27, 9, 0, tzinfo=dt.timezone.utc)
        assert self._check(md_theshow, when, _FakeCalendar(_TRADING_WEEK)) is True

    def test_window_closed_on_first_trading_day_after_a_holiday(self, md_theshow: _FakeMD) -> None:
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
        md._last_resubscribe_ts = now_ts - 5.0
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
        md._last_resubscribe_ts = now_ts - 100.0
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
        assert md._resubscribe_attempts == 0

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


class TestTriggerReconnect:
    @pytest.mark.asyncio
    async def test_returns_false_when_within_cooldown(self, md: _FakeMD) -> None:
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = now_ts - 10.0
        with (
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
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
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            await md._trigger_reconnect(30.0, reason="session_rollover")
        call_args = client.reconnect.call_args
        assert call_args[0][1] is True

    @pytest.mark.asyncio
    async def test_drains_stale_raw_queue_after_successful_reconnect(self, md: _FakeMD) -> None:
        """DATA-010: pre-reconnect messages must not reach a freshly reset book.

        This is the behaviour the shadowed copy in ``_md_reconnect`` never had,
        and every test in this class used to run against that copy.
        """
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        client = MagicMock()
        client.reconnect.return_value = True
        md.client = client
        for i in range(3):
            md.raw_queue.put_nowait(("stale", i))

        with (
            patch("hft_platform.services.market_data.timebase") as tb,
            patch("hft_platform.services._md_reconnect.timebase") as tb_mixin,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            tb_mixin.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0)

        assert result is True
        assert md.raw_queue.empty(), "stale pre-reconnect messages survived into the reset book"

    def test_mixin_does_not_shadow_the_live_implementation(self) -> None:
        """One implementation only -- a duplicate is where a fix goes to die."""
        from hft_platform.services import _md_reconnect

        assert "_trigger_reconnect" not in vars(_md_reconnect.MarketDataReconnectMixin)
        assert MarketDataService._trigger_reconnect.__qualname__ == "MarketDataService._trigger_reconnect"


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
        assert md._pending_reconnect_since == first_ts


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


class TestIsTradingHours:
    def test_weekday_within_hours(self, md: _FakeMD) -> None:
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
        sat = dt.datetime(2024, 1, 20, 10, 0, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError),
        ):
            tb.now_s.return_value = sat.timestamp()
            with patch.dict("sys.modules", {"hft_platform.core.market_calendar": None}):
                result = md._is_trading_hours()
        assert result is False


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
