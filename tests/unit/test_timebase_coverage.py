"""Coverage tests for core/timebase.py."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from hft_platform.core import timebase


class TestCoerceNsInt:
    def test_int_seconds_range(self):
        """Int in seconds range: multiply by 1e9."""
        result = timebase.coerce_ns(1_700_000_000)
        assert result == 1_700_000_000 * 1_000_000_000

    def test_int_milliseconds_range(self):
        """Int in ms range: multiply by 1e6."""
        result = timebase.coerce_ns(1_700_000_000_000)
        assert result == 1_700_000_000_000 * 1_000_000

    def test_int_microseconds_range(self):
        """Int in us range: multiply by 1e3."""
        result = timebase.coerce_ns(1_700_000_000_000_000)
        assert result == 1_700_000_000_000_000 * 1_000

    def test_int_nanoseconds_range(self):
        """Int already in ns range: no conversion."""
        result = timebase.coerce_ns(1_700_000_000_000_000_000)
        assert result == 1_700_000_000_000_000_000


class TestCoerceNsFloat:
    def test_float_seconds_range(self):
        """Float in seconds range: multiply by 1e9."""
        result = timebase.coerce_ns(1_700_000_000.5)
        assert result == int(1_700_000_000.5 * 1e9)

    def test_float_milliseconds_range(self):
        """Float in ms range: multiply by 1e6."""
        result = timebase.coerce_ns(1_700_000_000_000.5)
        assert result == int(1_700_000_000_000.5 * 1e6)

    def test_float_microseconds_range(self):
        """Float in us range: multiply by 1e3."""
        result = timebase.coerce_ns(1_700_000_000_000_000.5)
        assert result == int(1_700_000_000_000_000.5 * 1e3)

    def test_float_nanoseconds_range(self):
        """Float already in ns range: truncate to int."""
        result = timebase.coerce_ns(1_700_000_000_000_000_000.5)
        assert result == int(1_700_000_000_000_000_000.5)


# ---------------------------------------------------------------------------
# coerce_ns — datetime with existing tzinfo (line 89->91)
# ---------------------------------------------------------------------------


class TestCoerceNsDatetime:
    def test_datetime_with_tzinfo_preserves_tz(self):
        """Datetime with tzinfo should not be replaced."""
        utc_dt = dt.datetime(2020, 6, 15, 12, 0, 0, tzinfo=dt.timezone.utc)
        result = timebase.coerce_ns(utc_dt)
        expected = int(utc_dt.timestamp() * 1e9)
        assert result == expected

    def test_datetime_naive_assumes_hft_tz(self):
        """Naive datetime gets HFT_TS_TZ timezone applied."""
        naive = dt.datetime(2020, 6, 15, 12, 0, 0)
        result = timebase.coerce_ns(naive)
        expected = int(naive.replace(tzinfo=timebase.TZINFO).timestamp() * 1e9)
        assert result == expected


# ---------------------------------------------------------------------------
# now_ns and now_s — basic type checks (lines 57, 62)
# ---------------------------------------------------------------------------


class TestNowFunctions:
    def test_now_ns_returns_int(self):
        result = timebase.now_ns()
        assert isinstance(result, int)
        assert result > 0

    def test_now_s_returns_float(self):
        result = timebase.now_s()
        assert isinstance(result, float)
        assert result > 0.0


# ---------------------------------------------------------------------------
# _resolve_tz — various env scenarios
# ---------------------------------------------------------------------------


class TestResolveTz:
    def test_valid_tz_returns_zoneinfo(self, monkeypatch):
        monkeypatch.setenv("HFT_TS_TZ", "US/Eastern")
        tz_name, tzinfo = timebase._resolve_tz()
        assert tz_name == "US/Eastern"
        assert isinstance(tzinfo, ZoneInfo)

    def test_hft_ts_assume_tz_fallback(self, monkeypatch):
        """HFT_TS_ASSUME_TZ is used when HFT_TS_TZ is not set."""
        monkeypatch.delenv("HFT_TS_TZ", raising=False)
        monkeypatch.setenv("HFT_TS_ASSUME_TZ", "Europe/London")
        tz_name, tzinfo = timebase._resolve_tz()
        assert tz_name == "Europe/London"

    def test_default_is_asia_taipei(self, monkeypatch):
        """When no env is set, default is Asia/Taipei."""
        monkeypatch.delenv("HFT_TS_TZ", raising=False)
        monkeypatch.delenv("HFT_TS_ASSUME_TZ", raising=False)
        tz_name, tzinfo = timebase._resolve_tz()
        assert tz_name == "Asia/Taipei"
