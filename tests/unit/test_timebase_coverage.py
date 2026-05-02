"""Coverage tests for core/timebase.py — uncovered coerce paths and rust loader."""

from __future__ import annotations

import builtins
import datetime as dt
from unittest.mock import patch
from zoneinfo import ZoneInfo

from hft_platform.core import timebase

# ---------------------------------------------------------------------------
# _load_rust_coerce — disabled via env var (line 27)
# ---------------------------------------------------------------------------


class TestLoadRustCoerceDisabled:
    def test_rust_coerce_disabled_by_env(self, monkeypatch):
        """HFT_TIMEBASE_RUST_COERCE=0 skips rust loading."""
        monkeypatch.setenv("HFT_TIMEBASE_RUST_COERCE", "0")
        # Reset the loaded flag to force re-evaluation
        original = timebase._rust_coerce_loaded
        timebase._rust_coerce_loaded = False
        timebase._coerce_ns_int = None
        timebase._coerce_ns_float = None
        try:
            timebase._load_rust_coerce()
            assert timebase._rust_coerce_loaded is True
            assert timebase._coerce_ns_int is None
            assert timebase._coerce_ns_float is None
        finally:
            timebase._rust_coerce_loaded = original

    def test_rust_coerce_disabled_false_string(self, monkeypatch):
        """HFT_TIMEBASE_RUST_COERCE=false also disables."""
        monkeypatch.setenv("HFT_TIMEBASE_RUST_COERCE", "false")
        timebase._rust_coerce_loaded = False
        timebase._coerce_ns_int = None
        timebase._coerce_ns_float = None
        try:
            timebase._load_rust_coerce()
            assert timebase._coerce_ns_int is None
        finally:
            timebase._rust_coerce_loaded = True

    def test_load_rust_coerce_early_return_when_already_loaded(self):
        """Second call to _load_rust_coerce is a no-op."""
        original_loaded = timebase._rust_coerce_loaded
        original_int = timebase._coerce_ns_int
        original_float = timebase._coerce_ns_float
        try:
            timebase._rust_coerce_loaded = True
            timebase._coerce_ns_int = "sentinel"
            timebase._load_rust_coerce()
            # Should not have changed
            assert timebase._coerce_ns_int == "sentinel"
        finally:
            timebase._rust_coerce_loaded = original_loaded
            timebase._coerce_ns_int = original_int
            timebase._coerce_ns_float = original_float


# ---------------------------------------------------------------------------
# _load_rust_coerce — fallback import path (lines 33-40)
# ---------------------------------------------------------------------------


class TestLoadRustCoerceFallbackImport:
    """Cover lines 33-40: first import fails, tries rust_core fallback."""

    def _reset_loader(self):
        """Reset loader state so _load_rust_coerce actually runs."""
        timebase._rust_coerce_loaded = False
        timebase._coerce_ns_int = None
        timebase._coerce_ns_float = None

    def test_both_imports_fail_gracefully(self, monkeypatch):
        """Lines 33-40: both imports raise ImportError, functions stay None."""
        monkeypatch.setenv("HFT_TIMEBASE_RUST_COERCE", "1")
        saved_loaded = timebase._rust_coerce_loaded
        saved_int = timebase._coerce_ns_int
        saved_float = timebase._coerce_ns_float
        try:
            self._reset_loader()

            real_import = builtins.__import__

            def _reject_rust(name, *args, **kwargs):
                if name in ("hft_platform.rust_core", "rust_core"):
                    raise ImportError(f"mocked: no module {name}")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_reject_rust):
                timebase._load_rust_coerce()

            assert timebase._rust_coerce_loaded is True
            assert timebase._coerce_ns_int is None
            assert timebase._coerce_ns_float is None
        finally:
            timebase._rust_coerce_loaded = saved_loaded
            timebase._coerce_ns_int = saved_int
            timebase._coerce_ns_float = saved_float

    def test_first_import_fails_fallback_succeeds(self, monkeypatch):
        """Lines 33-38: hft_platform.rust_core fails, rust_core succeeds."""
        monkeypatch.setenv("HFT_TIMEBASE_RUST_COERCE", "1")
        saved_loaded = timebase._rust_coerce_loaded
        saved_int = timebase._coerce_ns_int
        saved_float = timebase._coerce_ns_float
        try:
            self._reset_loader()

            sentinel_int = lambda x: x  # noqa: E731
            sentinel_float = lambda x: x  # noqa: E731

            # Build a fake rust_core module with the two functions
            import types

            fake_rc = types.ModuleType("rust_core")
            fake_rc.coerce_ns_int = sentinel_int
            fake_rc.coerce_ns_float = sentinel_float

            real_import = builtins.__import__

            def _reject_first(name, *args, **kwargs):
                if name == "hft_platform.rust_core":
                    raise ImportError("mocked: no hft_platform.rust_core")
                if name == "rust_core":
                    return fake_rc
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=_reject_first):
                timebase._load_rust_coerce()

            assert timebase._rust_coerce_loaded is True
            assert timebase._coerce_ns_int is sentinel_int
            assert timebase._coerce_ns_float is sentinel_float
        finally:
            timebase._rust_coerce_loaded = saved_loaded
            timebase._coerce_ns_int = saved_int
            timebase._coerce_ns_float = saved_float


# ---------------------------------------------------------------------------
# coerce_ns — int paths without rust (lines 96-103)
# ---------------------------------------------------------------------------


class TestCoerceNsIntWithoutRust:
    def test_int_seconds_range(self):
        """Int in seconds range: multiply by 1e9."""
        # Force Python path
        original_int = timebase._coerce_ns_int
        timebase._coerce_ns_int = None
        try:
            result = timebase.coerce_ns(1_700_000_000)
            assert result == 1_700_000_000 * 1_000_000_000
        finally:
            timebase._coerce_ns_int = original_int

    def test_int_milliseconds_range(self):
        """Int in ms range: multiply by 1e6."""
        original_int = timebase._coerce_ns_int
        timebase._coerce_ns_int = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000)
            assert result == 1_700_000_000_000 * 1_000_000
        finally:
            timebase._coerce_ns_int = original_int

    def test_int_microseconds_range(self):
        """Int in us range: multiply by 1e3."""
        original_int = timebase._coerce_ns_int
        timebase._coerce_ns_int = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000_000)
            assert result == 1_700_000_000_000_000 * 1_000
        finally:
            timebase._coerce_ns_int = original_int

    def test_int_nanoseconds_range(self):
        """Int already in ns range: no conversion."""
        original_int = timebase._coerce_ns_int
        timebase._coerce_ns_int = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000_000_000)
            assert result == 1_700_000_000_000_000_000
        finally:
            timebase._coerce_ns_int = original_int


# ---------------------------------------------------------------------------
# coerce_ns — float paths without rust (lines 107-114)
# ---------------------------------------------------------------------------


class TestCoerceNsFloatWithoutRust:
    def test_float_seconds_range(self):
        """Float in seconds range: multiply by 1e9."""
        original_float = timebase._coerce_ns_float
        timebase._coerce_ns_float = None
        try:
            result = timebase.coerce_ns(1_700_000_000.5)
            assert result == int(1_700_000_000.5 * 1e9)
        finally:
            timebase._coerce_ns_float = original_float

    def test_float_milliseconds_range(self):
        """Float in ms range: multiply by 1e6."""
        original_float = timebase._coerce_ns_float
        timebase._coerce_ns_float = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000.5)
            assert result == int(1_700_000_000_000.5 * 1e6)
        finally:
            timebase._coerce_ns_float = original_float

    def test_float_microseconds_range(self):
        """Float in us range: multiply by 1e3."""
        original_float = timebase._coerce_ns_float
        timebase._coerce_ns_float = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000_000.5)
            assert result == int(1_700_000_000_000_000.5 * 1e3)
        finally:
            timebase._coerce_ns_float = original_float

    def test_float_nanoseconds_range(self):
        """Float already in ns range: truncate to int."""
        original_float = timebase._coerce_ns_float
        timebase._coerce_ns_float = None
        try:
            result = timebase.coerce_ns(1_700_000_000_000_000_000.5)
            assert result == int(1_700_000_000_000_000_000.5)
        finally:
            timebase._coerce_ns_float = original_float


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
