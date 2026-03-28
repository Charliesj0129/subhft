"""Tests for reports.collector — pure-function tests only (no CH connection needed)."""
from __future__ import annotations

from hft_platform.reports.collector import _ch_to_platform, _day_filter, _night_filter


class TestChToPlatform:
    def test_typical_price(self) -> None:
        # 32_375_000_000 (CH x1,000,000) → 323_750_000 (platform x10,000)
        assert _ch_to_platform(32_375_000_000) == 323_750_000

    def test_zero(self) -> None:
        assert _ch_to_platform(0) == 0

    def test_round_trip_to_human(self) -> None:
        # A price of 18000 points → stored as 18000 * 10000 = 180_000_000 (platform)
        # CH stores at x1,000,000 so 18000 * 1_000_000 = 18_000_000_000
        # After conversion: 18_000_000_000 // 100 = 180_000_000 (platform x10000)
        # Human: 180_000_000 / 10000 = 18000.0
        ch_price = 18_000 * 1_000_000
        platform_price = _ch_to_platform(ch_price)
        assert platform_price == 18_000 * 10_000
        assert platform_price / 10_000 == 18_000.0


class TestDayFilter:
    def test_contains_date(self) -> None:
        sql = _day_filter("2026-03-27")
        assert "2026-03-27" in sql

    def test_contains_start_time(self) -> None:
        sql = _day_filter("2026-03-27")
        assert "07:00" in sql

    def test_contains_end_time(self) -> None:
        sql = _day_filter("2026-03-27")
        assert "13:45" in sql

    def test_contains_timezone(self) -> None:
        sql = _day_filter("2026-03-27")
        assert "Asia/Taipei" in sql

    def test_no_to_date(self) -> None:
        sql = _day_filter("2026-03-27")
        assert "toDate(" not in sql

    def test_returns_string(self) -> None:
        sql = _day_filter("2026-03-27")
        assert isinstance(sql, str)
        assert len(sql) > 0


class TestNightFilter:
    def test_contains_date(self) -> None:
        sql = _night_filter("2026-03-27")
        assert "2026-03-27" in sql

    def test_contains_start_time(self) -> None:
        sql = _night_filter("2026-03-27")
        assert "15:00" in sql

    def test_contains_interval_14_hour(self) -> None:
        sql = _night_filter("2026-03-27")
        # Either INTERVAL 14 HOUR or + INTERVAL '14' HOUR style
        assert "14" in sql
        assert "HOUR" in sql.upper()

    def test_contains_timezone(self) -> None:
        sql = _night_filter("2026-03-27")
        assert "Asia/Taipei" in sql

    def test_no_to_date(self) -> None:
        sql = _night_filter("2026-03-27")
        assert "toDate(" not in sql

    def test_returns_string(self) -> None:
        sql = _night_filter("2026-03-27")
        assert isinstance(sql, str)
        assert len(sql) > 0
