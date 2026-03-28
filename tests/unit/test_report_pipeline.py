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
    # ------------------------------------------------------------------
    # Day session: always returns "today"
    # ------------------------------------------------------------------

    def test_day_session_returns_today(self) -> None:
        now = _dt(2026, 3, 27, 13, 50)
        result = resolve_trading_date("day", now=now)
        assert result == "2026-03-27"

    def test_day_session_morning(self) -> None:
        now = _dt(2026, 3, 27, 9, 0)
        result = resolve_trading_date("day", now=now)
        assert result == "2026-03-27"

    # ------------------------------------------------------------------
    # Night session: before 15:00 → yesterday; >= 15:00 → today
    # ------------------------------------------------------------------

    def test_night_session_early_morning_returns_yesterday(self) -> None:
        # 05:10 on Mar 28 → night session still running for Mar 27
        now = _dt(2026, 3, 28, 5, 10)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_afternoon_open_returns_today(self) -> None:
        # 15:30 on Mar 27 → night session just opened for Mar 27
        now = _dt(2026, 3, 27, 15, 30)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_at_1500_exact_returns_today(self) -> None:
        # 15:00 exactly → >= 15 → today
        now = _dt(2026, 3, 27, 15, 0)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    def test_night_session_at_1459_returns_yesterday(self) -> None:
        # 14:59 on Mar 27 → before 15:00 → yesterday (Mar 26)
        now = _dt(2026, 3, 27, 14, 59)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-26"

    def test_night_session_midnight_returns_yesterday(self) -> None:
        # 01:00 on Mar 28 → before 15:00 → yesterday (Mar 27)
        now = _dt(2026, 3, 28, 1, 0)
        result = resolve_trading_date("night", now=now)
        assert result == "2026-03-27"

    # ------------------------------------------------------------------
    # Unknown session raises ValueError
    # ------------------------------------------------------------------

    def test_invalid_session_raises(self) -> None:
        now = _dt(2026, 3, 27, 10, 0)
        with pytest.raises(ValueError, match="session"):
            resolve_trading_date("evening", now=now)

    # ------------------------------------------------------------------
    # now=None falls back to current time (smoke test — just no exception)
    # ------------------------------------------------------------------

    def test_day_session_no_now_arg_does_not_raise(self) -> None:
        result = resolve_trading_date("day")
        # Should be a valid YYYY-MM-DD string
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"
