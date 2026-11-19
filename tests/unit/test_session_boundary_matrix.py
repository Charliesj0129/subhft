"""Session-boundary matrix for TAIFEX/TWSE trading-hours checks.

Regression contract for the off-by-one that made the whole closing *minute*
read as trading hours.  Because ``MarketCalendar`` compared at minute
granularity with an inclusive close, 13:45:00–13:45:59 (and 05:00:00–05:00:59)
still answered "open" after the feed had gone quiet — long enough for the 15 s
quote watchdog to declare the feed dead and force a relogin on every facade at
every session close, which is where most of the daily
``451 Too Many Connections`` errors came from.

Every window is half-open ``[open, close)``: the opening instant is inside the
session, the closing instant is already outside it.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import pytest

from hft_platform.core.market_calendar import MarketCalendar

_HOLIDAY = dt.date(2026, 3, 12)
_TRADING_DAYS = frozenset(
    {
        dt.date(2026, 3, 9),
        dt.date(2026, 3, 10),
        dt.date(2026, 3, 11),
        dt.date(2026, 3, 13),
    }
)


def _calendar() -> MarketCalendar:
    """A calendar with exchange_calendars forced off and a known holiday map."""
    cal = MarketCalendar("XTAI")
    cal._cal = None
    cal.is_trading_day = lambda date=None: date in _TRADING_DAYS  # type: ignore[method-assign]
    return cal


def _at(date: dt.date, hour: int, minute: int, second: int = 0) -> dt.datetime:
    return dt.datetime(date.year, date.month, date.day, hour, minute, second, tzinfo=_calendar()._tz)


_TRADING_DAY = dt.date(2026, 3, 10)


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (8, 44, 59, False),
        (8, 45, 0, True),
        (8, 45, 1, True),
        (13, 44, 59, True),
        (13, 45, 0, False),
        (13, 45, 30, False),
        (13, 45, 59, False),
        (13, 46, 0, False),
        (14, 59, 59, False),
        (15, 0, 0, True),
    ],
)
def test_futures_day_session_boundaries_on_trading_day(hour, minute, second, expected):
    cal = _calendar()
    ts = _at(_TRADING_DAY, hour, minute, second)
    assert cal.is_trading_hours(ts, product_type="future") is expected


@pytest.mark.parametrize(
    ("hour", "minute", "second"),
    [(8, 45, 0), (10, 0, 0), (13, 44, 59), (13, 45, 0), (15, 0, 0), (20, 0, 0)],
)
def test_futures_sessions_closed_all_day_on_holiday(hour, minute, second):
    cal = _calendar()
    ts = _at(_HOLIDAY, hour, minute, second)
    assert cal.is_trading_hours(ts, product_type="future") is False


_MORNING_AFTER_TRADING_DAY = dt.date(2026, 3, 11)
_MORNING_AFTER_HOLIDAY = dt.date(2026, 3, 13)


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (0, 0, 0, True),
        (4, 59, 59, True),
        (5, 0, 0, False),
        (5, 0, 30, False),
        (5, 0, 59, False),
        (5, 1, 0, False),
        (8, 44, 59, False),
        (8, 45, 0, True),
    ],
)
def test_futures_night_session_boundaries_after_trading_day(hour, minute, second, expected):
    cal = _calendar()
    ts = _at(_MORNING_AFTER_TRADING_DAY, hour, minute, second)
    assert cal.is_trading_hours(ts, product_type="future") is expected


@pytest.mark.parametrize(("hour", "minute", "second"), [(0, 0, 0), (3, 0, 0), (4, 59, 59), (5, 0, 0)])
def test_futures_night_session_closed_morning_after_holiday(hour, minute, second):
    """No night session ran overnight because the previous day was a holiday."""
    cal = _calendar()
    ts = _at(_MORNING_AFTER_HOLIDAY, hour, minute, second)
    assert cal.is_trading_hours(ts, product_type="future") is False


def test_futures_night_session_opens_on_day_after_holiday():
    """The holiday only kills the overnight tail, not the evening that follows."""
    cal = _calendar()
    assert cal.is_trading_hours(_at(_MORNING_AFTER_HOLIDAY, 15, 0, 0), product_type="future") is True


@pytest.mark.parametrize(
    ("hour", "minute", "second", "expected"),
    [
        (8, 59, 59, False),
        (9, 0, 0, True),
        (13, 29, 59, True),
        (13, 30, 0, False),
        (13, 30, 30, False),
        (13, 31, 0, False),
    ],
)
def test_stock_session_boundaries_on_trading_day(hour, minute, second, expected):
    cal = _calendar()
    ts = _at(_TRADING_DAY, hour, minute, second)
    assert cal.is_trading_hours(ts, product_type="stock") is expected
    assert cal.is_trading_hours(ts) is expected


def test_stock_session_closed_on_holiday():
    cal = _calendar()
    assert cal.is_trading_hours(_at(_HOLIDAY, 10, 0, 0), product_type="stock") is False


_FALLBACK_CASES = [
    (8, 44, 59, False),
    (8, 45, 0, True),
    (13, 44, 59, True),
    (13, 45, 0, False),
    (13, 45, 30, False),
    (14, 0, 0, False),
    (15, 0, 0, True),
    (4, 59, 59, True),
    (5, 0, 0, False),
    (5, 0, 30, False),
]


@pytest.mark.parametrize(("hour", "minute", "second", "expected"), _FALLBACK_CASES)
def test_orchestrator_fallback_matches_calendar_at_session_close(hour, minute, second, expected):
    """When the calendar import fails the orchestrator must not regress the close.

    The fallback duplicates the session arithmetic, so it duplicated the
    off-by-one too. It is the fail-safe path, so it has to answer the same way.
    """
    from hft_platform.feed_adapter.shioaji import reconnect_orchestrator as orch_mod

    tz = dt.timezone(dt.timedelta(hours=8))
    now_dt = dt.datetime(2026, 3, 10, hour, minute, second, tzinfo=tz)

    orch = orch_mod.ReconnectOrchestrator.__new__(orch_mod.ReconnectOrchestrator)
    with (
        patch("hft_platform.core.market_calendar.get_calendar", side_effect=RuntimeError("calendar down")),
        patch("hft_platform.core.timebase.now_s", return_value=now_dt.timestamp()),
    ):
        assert orch.is_trading_hours() is expected


@pytest.mark.parametrize(("hour", "minute", "second", "_expected"), _FALLBACK_CASES)
def test_orchestrator_fallback_closed_on_weekend(hour, minute, second, _expected):
    from hft_platform.feed_adapter.shioaji import reconnect_orchestrator as orch_mod

    tz = dt.timezone(dt.timedelta(hours=8))
    now_dt = dt.datetime(2026, 3, 7, hour, minute, second, tzinfo=tz)

    orch = orch_mod.ReconnectOrchestrator.__new__(orch_mod.ReconnectOrchestrator)
    with (
        patch("hft_platform.core.market_calendar.get_calendar", side_effect=RuntimeError("calendar down")),
        patch("hft_platform.core.timebase.now_s", return_value=now_dt.timestamp()),
    ):
        assert orch.is_trading_hours() is False
