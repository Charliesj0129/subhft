"""A feed gap measured across a closed market is the close, not an outage.

Production shape these are written from (THESHOW, 2026-09-06..09-20, replayed
from ``feed_gap_by_symbol_seconds`` and ``platform_reduce_only_active`` over
Prometheus): 24 ``platform_reduce_only_entered`` events, **18 of them at
exactly 08:30 or 14:55 Taipei** -- the instant ``HFT_RECONNECT_HOURS`` opens,
15 minutes and 5 minutes before the market itself does.

    2026-09-15 08:31 Taipei, the first reading of the window
      EXFI6   12671 s      <- the whole overnight close
      TMFI6       3 s
      TXFJ6       3 s       (every other future: 3 s)
    threshold: 600 s  ->  feed_reconnect_unhealthy  ->  PLATFORM_REDUCE_ONLY

Nothing was wrong with the feed. The gap was the close, and the gate that was
supposed to suppress it was an env wall-clock range rather than the calendar.
"""

from __future__ import annotations

import datetime as dt

import pytest

from hft_platform.core.market_calendar import MarketCalendar

TZ = dt.timezone(dt.timedelta(hours=8))


def _at(y: int, m: int, d: int, hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


@pytest.fixture()
def calendar() -> MarketCalendar:
    return MarketCalendar()


class TestSecondsSinceSessionOpen:
    def test_a_shut_market_reports_no_session(self, calendar: MarketCalendar) -> None:
        """08:31 on a Tuesday: the env window is open, the market is not."""
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 8, 31)) is None

    def test_the_night_pre_open_reports_no_session(self, calendar: MarketCalendar) -> None:
        """14:55, five minutes before the night session."""
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 14, 55)) is None

    def test_the_day_open_instant_is_zero(self, calendar: MarketCalendar) -> None:
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 8, 45)) == 0.0

    def test_one_minute_into_the_day_session(self, calendar: MarketCalendar) -> None:
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 8, 46)) == 60.0

    def test_the_night_open_instant_is_zero(self, calendar: MarketCalendar) -> None:
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 15, 0)) == 0.0

    def test_after_midnight_carries_the_whole_evening(self, calendar: MarketCalendar) -> None:
        """04:22 belongs to the session that opened at 15:00 the day before."""
        elapsed = calendar.seconds_since_futures_session_open(_at(2026, 9, 16, 4, 22))
        assert elapsed == pytest.approx((13 * 3600) + (22 * 60))  # 15:00 -> 04:22

    def test_the_last_ten_minutes_of_the_day_session_are_covered(self, calendar: MarketCalendar) -> None:
        """13:40 is inside the session; the env window had already closed at 13:35."""
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 13, 40)) is not None

    def test_past_the_night_close_reports_no_session(self, calendar: MarketCalendar) -> None:
        """05:02 is shut; the env window ran to 05:05."""
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 16, 5, 2)) is None

    def test_the_lunch_gap_between_sessions_reports_no_session(self, calendar: MarketCalendar) -> None:
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 15, 14, 0)) is None

    def test_a_weekend_reports_no_session(self, calendar: MarketCalendar) -> None:
        assert calendar.seconds_since_futures_session_open(_at(2026, 9, 19, 10, 0)) is None


class _FakeMarketData:
    def __init__(self, gap_s: float, pending_since: float | None = None) -> None:
        self._gap_s = gap_s
        self._pending_reconnect_since = pending_since
        self.within_reconnect_window_calls = 0

    def get_active_feed_gap_s(self) -> float:
        return self._gap_s

    def within_reconnect_window(self) -> bool:
        self.within_reconnect_window_calls += 1
        return True


def _inputs(monkeypatch: pytest.MonkeyPatch, md: _FakeMarketData, since_open: float | None):
    from hft_platform.ops import platform_inputs

    monkeypatch.setattr(platform_inputs, "_seconds_since_session_open", lambda: since_open)
    probe = platform_inputs.PlatformDegradeInputs.__new__(platform_inputs.PlatformDegradeInputs)
    probe.md_service = md
    return probe


class TestFeedGapGate:
    def test_the_overnight_gap_is_not_an_outage(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The 2026-09-15 08:31 reading, exactly: 12,671 s against a shut market."""
        probe = _inputs(monkeypatch, _FakeMarketData(12671.0), since_open=None)
        assert probe._feed_gap_s() == 0.0

    def test_a_gap_cannot_outlive_the_session_carrying_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One minute after the bell, the worst possible gap is one minute."""
        probe = _inputs(monkeypatch, _FakeMarketData(12671.0), since_open=60.0)
        assert probe._feed_gap_s() == 60.0

    def test_a_real_in_session_outage_is_reported_in_full(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Two hours into the session, a 700 s dead feed must still read 700 s."""
        probe = _inputs(monkeypatch, _FakeMarketData(700.0), since_open=7200.0)
        assert probe._feed_gap_s() == 700.0

    def test_the_env_reconnect_window_no_longer_decides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HFT_RECONNECT_HOURS is wrong at all four edges; stop asking it."""
        md = _FakeMarketData(12671.0)
        probe = _inputs(monkeypatch, md, since_open=None)
        probe._feed_gap_s()
        assert md.within_reconnect_window_calls == 0

    def test_an_unreadable_calendar_does_not_latch_reduce_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Fail-closed on the gap: an unanswerable calendar must not page."""
        probe = _inputs(monkeypatch, _FakeMarketData(12671.0), since_open=None)
        assert probe._feed_gap_s() == 0.0

    def test_a_service_without_any_gap_probe_reports_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Bare:
            pass

        probe = _inputs(monkeypatch, _Bare(), since_open=3600.0)  # type: ignore[arg-type]
        assert probe._feed_gap_s() == 0.0


class TestCalendarHelperFailsClosed:
    def test_a_calendar_that_raises_reports_no_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import hft_platform.core.market_calendar as market_calendar
        from hft_platform.ops.platform_inputs import _seconds_since_session_open

        def _boom() -> object:
            raise RuntimeError("calendar unavailable")

        monkeypatch.setattr(market_calendar, "get_calendar", _boom)
        assert _seconds_since_session_open() is None
