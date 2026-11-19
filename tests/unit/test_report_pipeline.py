"""Unit tests for hft_platform.reports.pipeline date resolution and CLI."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from hft_platform.reports.pipeline import resolve_trading_date

TZ = ZoneInfo("Asia/Taipei")


def _dt(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """Build a timezone-aware datetime in Asia/Taipei."""
    return datetime(year, month, day, hour, minute, tzinfo=TZ)


class TestResolveTradingDate:
    def test_day_session_returns_today(self) -> None:
        now = _dt(2026, 3, 27, 13, 50)
        result = resolve_trading_date("day", now=now)
        assert result == "2026-03-27"

    def test_day_session_morning(self) -> None:
        now = _dt(2026, 3, 27, 9, 0)
        result = resolve_trading_date("day", now=now)
        assert result == "2026-03-27"

    def test_night_session_early_morning_returns_yesterday(self) -> None:
        now = _dt(2026, 3, 28, 5, 10)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_afternoon_open_returns_today(self) -> None:
        now = _dt(2026, 3, 27, 15, 30)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_at_1500_exact_returns_today(self) -> None:
        now = _dt(2026, 3, 27, 15, 0)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_at_1459_returns_yesterday(self) -> None:
        now = _dt(2026, 3, 27, 14, 59)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-26"

    def test_night_session_midnight_returns_yesterday(self) -> None:
        now = _dt(2026, 3, 28, 1, 0)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_invalid_session_raises(self) -> None:
        now = _dt(2026, 3, 27, 10, 0)
        with pytest.raises(ValueError, match="session"):
            resolve_trading_date("evening", now=now)

    def test_day_session_no_now_arg_does_not_raise(self) -> None:
        result = resolve_trading_date("day")
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"
