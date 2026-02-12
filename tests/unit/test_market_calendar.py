"""Tests for market calendar integration."""

import datetime as dt
import os
from unittest.mock import MagicMock, patch

import pytest


def test_market_calendar_initialization():
    """Test MarketCalendar initializes correctly."""
    from hft_platform.core.market_calendar import MarketCalendar, reset_calendar

    reset_calendar()
    calendar = MarketCalendar("XTAI")
    assert calendar._exchange == "XTAI"
    assert calendar._tz is not None


def test_market_calendar_available_with_xcals():
    """Test calendar availability when exchange_calendars is installed."""
    from hft_platform.core.market_calendar import MarketCalendar, reset_calendar

    reset_calendar()
    calendar = MarketCalendar("XTAI")
    # exchange_calendars should be available
    assert calendar.available is True


def test_market_calendar_fallback_weekend():
    """Test fallback logic for weekends when calendar not available."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    # Saturday
    saturday = dt.date(2026, 2, 14)
    assert calendar.is_trading_day(saturday) is False

    # Monday
    monday = dt.date(2026, 2, 16)
    assert calendar.is_trading_day(monday) is True


def test_is_trading_day_with_xcals():
    """Test is_trading_day with real exchange_calendars data."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    if not calendar.available:
        pytest.skip("exchange_calendars not available")

    # Weekend should not be trading day
    saturday = dt.date(2026, 2, 14)
    assert calendar.is_trading_day(saturday) is False


def test_is_trading_hours_fallback():
    """Test is_trading_hours fallback logic."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    # Weekday during trading hours
    monday_10am = dt.datetime(2026, 2, 16, 10, 0, tzinfo=calendar._tz)
    assert calendar.is_trading_hours(monday_10am) is True

    # Weekday before trading hours
    monday_8am = dt.datetime(2026, 2, 16, 8, 0, tzinfo=calendar._tz)
    assert calendar.is_trading_hours(monday_8am) is False

    # Weekday after trading hours
    monday_2pm = dt.datetime(2026, 2, 16, 14, 0, tzinfo=calendar._tz)
    assert calendar.is_trading_hours(monday_2pm) is False


def test_get_session_open_fallback():
    """Test get_session_open fallback logic."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    monday = dt.date(2026, 2, 16)
    open_time = calendar.get_session_open(monday)

    assert open_time is not None
    assert open_time.hour == 9
    assert open_time.minute == 0


def test_get_session_close_fallback():
    """Test get_session_close fallback logic."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    monday = dt.date(2026, 2, 16)
    close_time = calendar.get_session_close(monday)

    assert close_time is not None
    assert close_time.hour == 13
    assert close_time.minute == 30


def test_next_trading_day_skips_weekend():
    """Test next_trading_day skips weekends."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    # Friday -> Monday
    friday = dt.date(2026, 2, 13)
    next_day = calendar.next_trading_day(friday)

    assert next_day == dt.date(2026, 2, 16)  # Monday


def test_previous_trading_day_skips_weekend():
    """Test previous_trading_day skips weekends."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    # Monday -> Friday
    monday = dt.date(2026, 2, 16)
    prev_day = calendar.previous_trading_day(monday)

    assert prev_day == dt.date(2026, 2, 13)  # Friday


def test_days_until_trading_today():
    """Test days_until_trading returns 0 for trading day."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    monday = dt.date(2026, 2, 16)
    days = calendar.days_until_trading(monday)

    assert days == 0


def test_days_until_trading_weekend():
    """Test days_until_trading for weekend."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback

    # Saturday -> 2 days until Monday
    saturday = dt.date(2026, 2, 14)
    days = calendar.days_until_trading(saturday)

    assert days == 2


def test_is_holiday_period():
    """Test is_holiday_period detection."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")

    # Mock days_until_trading to simulate holiday
    with patch.object(calendar, "days_until_trading", return_value=5):
        assert calendar.is_holiday_period(min_consecutive_days=3) is True

    with patch.object(calendar, "days_until_trading", return_value=2):
        assert calendar.is_holiday_period(min_consecutive_days=3) is False


def test_get_calendar_singleton():
    """Test get_calendar returns singleton."""
    from hft_platform.core.market_calendar import get_calendar, reset_calendar

    reset_calendar()
    cal1 = get_calendar()
    cal2 = get_calendar()

    assert cal1 is cal2


def test_get_calendar_respects_env():
    """Test get_calendar uses HFT_MARKET_EXCHANGE env var."""
    from hft_platform.core.market_calendar import get_calendar, reset_calendar

    reset_calendar()
    with patch.dict(os.environ, {"HFT_MARKET_EXCHANGE": "XNYS"}):
        reset_calendar()
        calendar = get_calendar()
        assert calendar._exchange == "XNYS"


def test_session_close_returns_none_for_non_trading_day():
    """Test get_session_close returns None for non-trading day."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")

    # Mock is_trading_day to return False
    with patch.object(calendar, "is_trading_day", return_value=False):
        result = calendar.get_session_close(dt.date(2026, 2, 14))
        assert result is None


def test_session_open_returns_none_for_non_trading_day():
    """Test get_session_open returns None for non-trading day."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback mode

    # Saturday
    result = calendar.get_session_open(dt.date(2026, 2, 14))
    assert result is None


def test_session_time_cache_hit():
    """Test that session times are cached and reused."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback mode
    calendar.clear_session_cache()

    monday = dt.date(2026, 2, 16)

    # First call - cache miss
    open1 = calendar.get_session_open(monday)
    info1 = calendar.get_session_cache_info()
    assert info1["misses"] == 1
    assert info1["hits"] == 0

    # Second call - cache hit (same date)
    open2 = calendar.get_session_open(monday)
    info2 = calendar.get_session_cache_info()
    assert info2["hits"] == 1
    assert info2["misses"] == 1

    # Third call for close - still cache hit (same date, both open/close cached)
    close1 = calendar.get_session_close(monday)
    info3 = calendar.get_session_cache_info()
    assert info3["hits"] == 2  # Both open and close from same cache entry

    # Verify values are consistent
    assert open1 == open2
    assert open1.hour == 9
    assert close1.hour == 13


def test_session_time_cache_invalidation():
    """Test that cache can be cleared."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback mode
    calendar.clear_session_cache()

    monday = dt.date(2026, 2, 16)

    # First call
    calendar.get_session_open(monday)
    info1 = calendar.get_session_cache_info()
    assert info1["currsize"] == 1

    # Clear cache
    calendar.clear_session_cache()
    info2 = calendar.get_session_cache_info()
    assert info2["currsize"] == 0
    assert info2["hits"] == 0
    assert info2["misses"] == 0

    # Another call should miss again
    calendar.get_session_open(monday)
    info3 = calendar.get_session_cache_info()
    assert info3["misses"] == 1


def test_session_cache_different_dates():
    """Test that different dates have separate cache entries."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None  # Force fallback mode
    calendar.clear_session_cache()

    monday = dt.date(2026, 2, 16)
    tuesday = dt.date(2026, 2, 17)

    # First date
    calendar.get_session_open(monday)
    info1 = calendar.get_session_cache_info()
    assert info1["currsize"] == 1
    assert info1["misses"] == 1

    # Second date - new cache entry
    calendar.get_session_open(tuesday)
    info2 = calendar.get_session_cache_info()
    assert info2["currsize"] == 2
    assert info2["misses"] == 2

    # Repeat first date - cache hit
    calendar.get_session_open(monday)
    info3 = calendar.get_session_cache_info()
    assert info3["hits"] == 1


def test_market_calendar_zoneinfo_fallback_and_no_xcals():
    """Force ZoneInfo failure and missing exchange_calendars to hit fallback paths."""
    from hft_platform.core import market_calendar as mc

    mc._xcals = None
    with patch("zoneinfo.ZoneInfo", side_effect=Exception("boom")):
        with patch.object(mc, "_get_xcals", return_value=None):
            calendar = mc.MarketCalendar("XTAI")
            assert calendar.available is False
            assert calendar._tz.utcoffset(None) == dt.timedelta(hours=8)


def test_market_calendar_session_times_exception():
    """Ensure session lookup errors return None and do not raise."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.is_session.side_effect = RuntimeError("boom")

    open_time, close_time = calendar._get_session_times_uncached("2026-02-11")
    assert open_time is None
    assert close_time is None


def test_market_calendar_trading_day_exception():
    """Ensure is_trading_day falls back when calendar check fails."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.is_session.side_effect = RuntimeError("boom")

    monday = dt.date(2026, 2, 16)
    assert calendar.is_trading_day(monday) is True


def test_market_calendar_trading_hours_exception():
    """Ensure is_trading_hours returns False when calendar lookup fails."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.session_open.side_effect = RuntimeError("boom")

    monday_10am = dt.datetime(2026, 2, 16, 10, 0, tzinfo=calendar._tz)
    assert calendar.is_trading_hours(monday_10am) is False


def test_get_xcals_import_error_returns_none():
    """Ensure ImportError path returns None and caches False."""
    import builtins

    from hft_platform.core import market_calendar as mc

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "exchange_calendars":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    mc._xcals = None
    with patch("builtins.__import__", side_effect=fake_import):
        assert mc._get_xcals() is None
        assert mc._xcals is False


def test_market_calendar_get_calendar_error():
    """Ensure calendar init handles get_calendar errors."""
    from hft_platform.core import market_calendar as mc

    class Dummy:
        def get_calendar(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    with patch.object(mc, "_get_xcals", return_value=Dummy()):
        calendar = mc.MarketCalendar("XTAI")
        assert calendar.available is False


def test_market_calendar_next_trading_day_exception_fallback():
    """Ensure next_trading_day falls back on exception."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.next_session.side_effect = RuntimeError("boom")

    friday = dt.date(2026, 2, 13)
    assert calendar.next_trading_day(friday) == dt.date(2026, 2, 16)


def test_market_calendar_previous_trading_day_exception_fallback():
    """Ensure previous_trading_day falls back on exception."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.previous_session.side_effect = RuntimeError("boom")

    monday = dt.date(2026, 2, 16)
    assert calendar.previous_trading_day(monday) == dt.date(2026, 2, 13)


def test_days_until_trading_next_none_fallback():
    """Ensure days_until_trading falls back when next_trading_day is None."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    with patch.object(calendar, "is_trading_day", return_value=False):
        with patch.object(calendar, "next_trading_day", return_value=None):
            assert calendar.days_until_trading(dt.date(2026, 2, 14)) == 1


def test_is_trading_day_default_branches():
    """Cover default date paths with and without calendar."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None
    assert isinstance(calendar.is_trading_day(), bool)

    calendar._cal = MagicMock()
    calendar._cal.is_session.return_value = True
    assert calendar.is_trading_day() is True


def test_is_trading_hours_default_and_success():
    """Cover default timestamp branch and calendar success path."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    calendar._cal = None
    with patch.object(calendar, "is_trading_day", return_value=False):
        assert calendar.is_trading_hours() is False

    ts = dt.datetime(2026, 2, 16, 10, 0, tzinfo=calendar._tz)
    calendar._cal = MagicMock()
    calendar._cal.session_open.return_value = ts - dt.timedelta(minutes=1)
    calendar._cal.session_close.return_value = ts + dt.timedelta(minutes=1)
    assert calendar.is_trading_hours(ts) is True


def test_get_session_open_close_default_date():
    """Cover default date paths for get_session_open/close."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    open_time = dt.datetime(2026, 2, 16, 9, 0, tzinfo=calendar._tz)
    close_time = dt.datetime(2026, 2, 16, 13, 30, tzinfo=calendar._tz)
    calendar._get_session_times_cached = MagicMock(return_value=(open_time, close_time))

    assert calendar.get_session_open() == open_time
    assert calendar.get_session_close() == close_time


def test_next_previous_trading_day_success_paths():
    """Cover calendar success paths for next/previous trading day."""
    from hft_platform.core.market_calendar import MarketCalendar

    class FakeSession:
        def __init__(self, day: dt.date):
            self._day = day

        def date(self):
            return self._day

    calendar = MarketCalendar("XTAI")
    calendar._cal = MagicMock()
    calendar._cal.next_session.return_value = FakeSession(dt.date(2026, 2, 17))
    calendar._cal.previous_session.return_value = FakeSession(dt.date(2026, 2, 13))

    assert calendar.next_trading_day() == dt.date(2026, 2, 17)
    assert calendar.previous_trading_day() == dt.date(2026, 2, 13)


def test_days_until_trading_default_date():
    """Cover default date branch in days_until_trading."""
    from hft_platform.core.market_calendar import MarketCalendar

    calendar = MarketCalendar("XTAI")
    with patch.object(calendar, "is_trading_day", return_value=True):
        assert calendar.days_until_trading() == 0
