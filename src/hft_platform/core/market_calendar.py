"""Market calendar integration using exchange_calendars.

Provides trading day and hours checks for Taiwan market (XTAI).
"""

from __future__ import annotations

import datetime as dt
import os
from functools import lru_cache

from structlog import get_logger

logger = get_logger("core.market_calendar")

# Lazy import exchange_calendars to avoid import-time overhead
_xcals = None


def _get_xcals():
    """Lazily import exchange_calendars."""
    global _xcals
    if _xcals is None:
        try:
            import exchange_calendars as xcals

            _xcals = xcals
        except ImportError:
            logger.warning("exchange_calendars not installed, market calendar unavailable")
            _xcals = False
    return _xcals if _xcals else None


class MarketCalendar:
    """Taiwan market calendar wrapper using exchange_calendars.

    Provides trading day checks, session times, and holiday awareness.
    Uses XTAI (Taiwan Stock Exchange) calendar by default.
    """

    def __init__(self, exchange: str = "XTAI"):
        """Initialize market calendar.

        Args:
            exchange: Exchange code for exchange_calendars (default: XTAI)
        """
        self._exchange = exchange
        self._cal = None
        self._tz_name = os.getenv("HFT_TS_TZ", "Asia/Taipei")
        self._tz: dt.tzinfo
        self._session_cache_size = int(os.getenv("HFT_CALENDAR_CACHE_SIZE", "32"))

        try:
            from zoneinfo import ZoneInfo

            self._tz = ZoneInfo(self._tz_name)
        except Exception:
            self._tz = dt.timezone(dt.timedelta(hours=8))  # Fallback to UTC+8

        xcals = _get_xcals()
        if xcals:
            try:
                self._cal = xcals.get_calendar(exchange)
                logger.info(
                    "Market calendar initialized",
                    exchange=exchange,
                    tz=self._tz_name,
                )
            except Exception as exc:
                logger.error(
                    "Failed to get calendar",
                    exchange=exchange,
                    error=str(exc),
                )
        else:
            logger.warning("Market calendar unavailable (no exchange_calendars)")

        # Create instance-bound cached method
        self._get_session_times_cached = lru_cache(maxsize=self._session_cache_size)(self._get_session_times_uncached)

    @property
    def available(self) -> bool:
        """Check if calendar is available."""
        return self._cal is not None

    def _get_session_times_uncached(self, date_str: str) -> tuple[dt.datetime | None, dt.datetime | None]:
        """Get session open/close times for a date (uncached implementation).

        Args:
            date_str: ISO format date string (YYYY-MM-DD)

        Returns:
            Tuple of (open_time, close_time), either may be None if not a trading day
        """
        date = dt.date.fromisoformat(date_str)

        if not self._cal:
            # Fallback: weekdays only with fixed times
            if date.weekday() >= 5:
                return None, None
            open_time = dt.datetime.combine(date, dt.time(9, 0), tzinfo=self._tz)
            close_time = dt.datetime.combine(date, dt.time(13, 30), tzinfo=self._tz)
            return open_time, close_time

        try:
            import pandas as pd

            session = pd.Timestamp(date)
            if not self._cal.is_session(session):
                return None, None
            return (
                self._cal.session_open(session).to_pydatetime(),
                self._cal.session_close(session).to_pydatetime(),
            )
        except Exception as exc:
            logger.debug(
                "Session times lookup failed",
                date=date_str,
                error=str(exc),
            )
            return None, None

    def clear_session_cache(self) -> None:
        """Clear the session times cache."""
        self._get_session_times_cached.cache_clear()

    def get_session_cache_info(self) -> dict:
        """Get cache statistics.

        Returns:
            Dict with hits, misses, maxsize, currsize
        """
        info = self._get_session_times_cached.cache_info()
        return {
            "hits": info.hits,
            "misses": info.misses,
            "maxsize": info.maxsize,
            "currsize": info.currsize,
        }

    def is_trading_day(self, date: dt.date | None = None) -> bool:
        """Check if date is a trading day.

        Args:
            date: Date to check (default: today)

        Returns:
            True if trading day, False if holiday/weekend
        """
        if not self._cal:
            # Fallback: weekdays only
            if date is None:
                date = dt.datetime.now(self._tz).date()
            return date.weekday() < 5

        if date is None:
            date = dt.datetime.now(self._tz).date()

        try:
            import pandas as pd

            return self._cal.is_session(pd.Timestamp(date))
        except Exception as exc:
            logger.debug("is_trading_day check failed", date=str(date), error=str(exc))
            return date.weekday() < 5

    def is_trading_hours(self, ts: dt.datetime | None = None) -> bool:
        """Check if timestamp is within trading hours.

        Args:
            ts: Timestamp to check (default: now)

        Returns:
            True if within trading hours
        """
        if ts is None:
            ts = dt.datetime.now(self._tz)

        if not self.is_trading_day(ts.date()):
            return False

        if not self._cal:
            # Fallback: 09:00-13:30 TST
            hour = ts.hour
            minute = ts.minute
            start = 9 * 60  # 09:00
            end = 13 * 60 + 30  # 13:30
            current = hour * 60 + minute
            return start <= current <= end

        try:
            import pandas as pd

            session = pd.Timestamp(ts.date())
            open_time = self._cal.session_open(session)
            close_time = self._cal.session_close(session)
            ts_pd = pd.Timestamp(ts)
            return open_time <= ts_pd <= close_time
        except Exception as exc:
            logger.debug("is_trading_hours check failed", ts=str(ts), error=str(exc))
            return False

    def get_session_open(self, date: dt.date | None = None) -> dt.datetime | None:
        """Get market open time for a trading day.

        Uses LRU cache for performance (5-10x improvement after first call).

        Args:
            date: Trading day (default: today)

        Returns:
            Market open datetime, or None if not a trading day
        """
        if date is None:
            date = dt.datetime.now(self._tz).date()

        open_time, _ = self._get_session_times_cached(date.isoformat())
        return open_time

    def get_session_close(self, date: dt.date | None = None) -> dt.datetime | None:
        """Get market close time for a trading day.

        Uses LRU cache for performance (5-10x improvement after first call).

        Args:
            date: Trading day (default: today)

        Returns:
            Market close datetime, or None if not a trading day
        """
        if date is None:
            date = dt.datetime.now(self._tz).date()

        _, close_time = self._get_session_times_cached(date.isoformat())
        return close_time

    def next_trading_day(self, date: dt.date | None = None) -> dt.date | None:
        """Get next trading day after given date.

        Args:
            date: Reference date (default: today)

        Returns:
            Next trading day date
        """
        if date is None:
            date = dt.datetime.now(self._tz).date()

        if not self._cal:
            # Fallback: skip weekends
            next_date = date + dt.timedelta(days=1)
            while next_date.weekday() >= 5:
                next_date += dt.timedelta(days=1)
            return next_date

        try:
            import pandas as pd

            next_session = self._cal.next_session(pd.Timestamp(date))
            return next_session.date()
        except Exception as exc:
            logger.debug("next_trading_day failed", date=str(date), error=str(exc))
            next_date = date + dt.timedelta(days=1)
            while next_date.weekday() >= 5:
                next_date += dt.timedelta(days=1)
            return next_date

    def previous_trading_day(self, date: dt.date | None = None) -> dt.date | None:
        """Get previous trading day before given date.

        Args:
            date: Reference date (default: today)

        Returns:
            Previous trading day date
        """
        if date is None:
            date = dt.datetime.now(self._tz).date()

        if not self._cal:
            # Fallback: skip weekends
            prev_date = date - dt.timedelta(days=1)
            while prev_date.weekday() >= 5:
                prev_date -= dt.timedelta(days=1)
            return prev_date

        try:
            import pandas as pd

            prev_session = self._cal.previous_session(pd.Timestamp(date))
            return prev_session.date()
        except Exception as exc:
            logger.debug("previous_trading_day failed", date=str(date), error=str(exc))
            prev_date = date - dt.timedelta(days=1)
            while prev_date.weekday() >= 5:
                prev_date -= dt.timedelta(days=1)
            return prev_date

    def days_until_trading(self, date: dt.date | None = None) -> int:
        """Days until next trading day (0 if today is trading day).

        Args:
            date: Reference date (default: today)

        Returns:
            Number of days until next trading day
        """
        if date is None:
            date = dt.datetime.now(self._tz).date()

        if self.is_trading_day(date):
            return 0

        next_day = self.next_trading_day(date)
        if next_day is None:
            return 1  # Fallback

        return (next_day - date).days

    def is_holiday_period(self, min_consecutive_days: int = 3) -> bool:
        """Check if we're in or approaching a holiday period.

        Args:
            min_consecutive_days: Minimum days for a period to be considered a holiday

        Returns:
            True if in a holiday period of at least min_consecutive_days
        """
        today = dt.datetime.now(self._tz).date()
        return self.days_until_trading(today) >= min_consecutive_days


# Singleton instance
_instance: MarketCalendar | None = None


def get_calendar() -> MarketCalendar:
    """Get singleton MarketCalendar instance.

    Exchange can be configured via HFT_MARKET_EXCHANGE env var.
    """
    global _instance
    if _instance is None:
        exchange = os.getenv("HFT_MARKET_EXCHANGE", "XTAI")
        _instance = MarketCalendar(exchange)
    return _instance


def reset_calendar() -> None:
    """Reset singleton for testing."""
    global _instance
    _instance = None
