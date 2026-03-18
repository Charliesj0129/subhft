import datetime as dt

from hft_platform.core.market_calendar import MarketCalendar


def _make_cal() -> MarketCalendar:
    """Create a calendar without exchange_calendars dependency (fallback mode)."""
    cal = MarketCalendar.__new__(MarketCalendar)
    cal._exchange = "XTAI"
    cal._cal = None
    cal._tz = dt.timezone(dt.timedelta(hours=8))
    cal._session_cache_size = 4
    from functools import lru_cache

    cal._get_session_times_cached = lru_cache(maxsize=4)(cal._get_session_times_uncached)
    return cal


def test_market_open_stock_session():
    cal = _make_cal()
    # Monday 10:00 TST — should be trading hours for stock
    ts = dt.datetime(2026, 3, 16, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert cal.is_trading_hours(ts, product_type="stock")


def test_market_closed_weekend():
    cal = _make_cal()
    # Saturday 10:00 TST — no trading
    ts = dt.datetime(2026, 3, 14, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert not cal.is_trading_hours(ts, product_type="stock")


def test_futures_night_session():
    cal = _make_cal()
    # Monday 22:00 TST — futures night session
    ts = dt.datetime(2026, 3, 16, 22, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert cal.is_trading_hours(ts, product_type="future")


def test_off_hours_early_morning():
    cal = _make_cal()
    # Monday 07:00 TST — between night close (05:00) and day open (08:45)
    ts = dt.datetime(2026, 3, 16, 7, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    assert not cal.is_trading_hours(ts, product_type="future")
