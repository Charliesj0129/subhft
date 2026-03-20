"""Tests for MarketDataReconnectMixin in services/_md_reconnect.py."""

from __future__ import annotations

import datetime as dt
from enum import Enum
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services._md_reconnect import MarketDataReconnectMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FeedState(Enum):
    INIT = "INIT"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"


class FakeService(MarketDataReconnectMixin):
    """Minimal stand-in for MarketDataService with mixin attributes."""

    def __init__(self) -> None:
        self.reconnect_days: set[str] = set()
        self.reconnect_hours: str = ""
        self.reconnect_hours_2: str = ""
        self._reconnect_tzinfo: dt.tzinfo = dt.timezone(dt.timedelta(hours=8))
        self.last_event_ts: float = 0.0
        self._last_rollover_seen_date: dt.date | None = None
        self._last_rollover_reconnect_date: dt.date | None = None
        self.state: _FeedState | None = None
        self._market_open_grace_s: float = 0.0
        self.metrics_registry: object | None = None

    # Stub _set_state so reconnect paths don't break
    def _set_state(self, new_state: object) -> None:
        self.state = new_state  # type: ignore[assignment]


def _ts_for(
    year: int = 2026,
    month: int = 3,
    day: int = 20,
    hour: int = 10,
    minute: int = 0,
    second: int = 0,
    tz: dt.tzinfo | None = None,
) -> float:
    """Return a POSIX timestamp for the given local time."""
    if tz is None:
        tz = dt.timezone(dt.timedelta(hours=8))
    return dt.datetime(year, month, day, hour, minute, second, tzinfo=tz).timestamp()


# ---------------------------------------------------------------------------
# _within_reconnect_window
# ---------------------------------------------------------------------------


class TestWithinReconnectWindow:
    """Tests for _within_reconnect_window."""

    def test_no_constraints_returns_true(self) -> None:
        svc = FakeService()
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is True

    def test_reconnect_days_wrong_day(self) -> None:
        svc = FakeService()
        # 2026-03-20 is a Friday
        svc.reconnect_days = {"mon", "tue"}
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is False

    def test_reconnect_days_right_day(self) -> None:
        svc = FakeService()
        # 2026-03-20 is a Friday
        svc.reconnect_days = {"fri"}
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is True

    def test_reconnect_hours_within_window(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "09:00-11:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10)):
            assert svc._within_reconnect_window() is True

    def test_reconnect_hours_outside_window(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "09:00-11:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=12)):
            assert svc._within_reconnect_window() is False

    def test_overnight_window_before_midnight(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "23:00-01:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=23, minute=30)):
            assert svc._within_reconnect_window() is True

    def test_overnight_window_after_midnight(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "23:00-01:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=0, minute=30)):
            assert svc._within_reconnect_window() is True

    def test_overnight_window_outside(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "23:00-01:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=12)):
            assert svc._within_reconnect_window() is False

    def test_reconnect_hours_2_matches(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "02:00-03:00"
        svc.reconnect_hours_2 = "10:00-11:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10, minute=30)):
            assert svc._within_reconnect_window() is True

    def test_reconnect_hours_2_neither_matches(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "02:00-03:00"
        svc.reconnect_hours_2 = "14:00-15:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10)):
            assert svc._within_reconnect_window() is False

    def test_days_set_no_hours_right_day(self) -> None:
        """Days constraint met with no hour window => True."""
        svc = FakeService()
        svc.reconnect_days = {"fri"}
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is True

    def test_calendar_non_trading_day_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When calendar says >1 day until trading, return False."""
        svc = FakeService()
        svc.reconnect_days = {"fri"}
        monkeypatch.setenv("HFT_RECONNECT_USE_CALENDAR", "1")

        mock_cal = MagicMock()
        mock_cal.available = True
        mock_cal.days_until_trading.return_value = 2

        # The method uses a local `from ... import get_calendar` each call,
        # so we patch the canonical module attribute that the import resolves.
        import hft_platform.core.market_calendar as _mc_mod

        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for()),
            patch.object(_mc_mod, "get_calendar", return_value=mock_cal),
        ):
            assert svc._within_reconnect_window() is False

    def test_calendar_disabled_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When calendar env is disabled, calendar check is skipped."""
        svc = FakeService()
        svc.reconnect_days = {"fri"}
        monkeypatch.setenv("HFT_RECONNECT_USE_CALENDAR", "0")

        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is True

    def test_malformed_hours_string_ignored(self) -> None:
        """Bad hour string should not crash; falls through to False."""
        svc = FakeService()
        svc.reconnect_hours = "not-a-time"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._within_reconnect_window() is False

    def test_edge_exactly_at_start(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "10:00-11:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10, minute=0)):
            assert svc._within_reconnect_window() is True

    def test_edge_exactly_at_end(self) -> None:
        svc = FakeService()
        svc.reconnect_hours = "10:00-11:00"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=11, minute=0)):
            assert svc._within_reconnect_window() is True


# ---------------------------------------------------------------------------
# _should_rollover_reconnect
# ---------------------------------------------------------------------------


class TestShouldRolloverReconnect:
    """Tests for _should_rollover_reconnect."""

    def test_same_date_returns_false(self) -> None:
        svc = FakeService()
        ts = _ts_for(hour=10)
        svc.last_event_ts = ts
        with patch("hft_platform.core.timebase.now_s", return_value=ts + 60):
            assert svc._should_rollover_reconnect() is False

    def test_different_date_returns_true(self) -> None:
        svc = FakeService()
        svc.last_event_ts = _ts_for(day=19, hour=23)
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(day=20, hour=0, minute=5)):
            assert svc._should_rollover_reconnect() is True

    def test_already_seen_today_returns_false(self) -> None:
        svc = FakeService()
        svc.last_event_ts = _ts_for(day=19, hour=23)
        today = dt.date(2026, 3, 20)
        svc._last_rollover_seen_date = today
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(day=20, hour=0, minute=5)):
            assert svc._should_rollover_reconnect() is False

    def test_sets_last_rollover_seen_date(self) -> None:
        svc = FakeService()
        svc.last_event_ts = _ts_for(day=19, hour=23)
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(day=20, hour=0, minute=5)):
            svc._should_rollover_reconnect()
        assert svc._last_rollover_seen_date == dt.date(2026, 3, 20)

    def test_zero_last_event_ts(self) -> None:
        """last_event_ts=0 (epoch) is a different date from any current day."""
        svc = FakeService()
        svc.last_event_ts = 0.0
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc._should_rollover_reconnect() is True


# ---------------------------------------------------------------------------
# _is_trading_hours (fallback path)
# ---------------------------------------------------------------------------


class TestIsTradingHours:
    """Tests for _is_trading_hours — exercises the fallback branch."""

    def _patch_calendar_unavailable(self) -> patch:
        return patch(
            "hft_platform.core.market_calendar.get_calendar",
            side_effect=Exception("no calendar"),
        )

    def test_weekday_within_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = FakeService()
        monkeypatch.setenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        # 2026-03-20 Friday 10:00 => within 08:45-13:45
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10)),
            self._patch_calendar_unavailable(),
        ):
            assert svc._is_trading_hours() is True

    def test_weekday_outside_hours(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = FakeService()
        monkeypatch.setenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        # 15:00 is after 13:45
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=15)),
            self._patch_calendar_unavailable(),
        ):
            assert svc._is_trading_hours() is False

    def test_weekend_returns_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = FakeService()
        monkeypatch.setenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        # 2026-03-21 is Saturday
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(day=21, hour=10)),
            self._patch_calendar_unavailable(),
        ):
            assert svc._is_trading_hours() is False

    def test_edge_market_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = FakeService()
        monkeypatch.setenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        # 08:45 exactly
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=8, minute=45)),
            self._patch_calendar_unavailable(),
        ):
            assert svc._is_trading_hours() is True

    def test_edge_market_close(self, monkeypatch: pytest.MonkeyPatch) -> None:
        svc = FakeService()
        monkeypatch.setenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        # 13:45 exactly
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=13, minute=45)),
            self._patch_calendar_unavailable(),
        ):
            assert svc._is_trading_hours() is True

    def test_with_calendar_available(self) -> None:
        svc = FakeService()
        mock_cal = MagicMock()
        mock_cal._tz = dt.timezone(dt.timedelta(hours=8))
        mock_cal.is_trading_hours.return_value = True
        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=10)),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            assert svc._is_trading_hours() is True
            mock_cal.is_trading_hours.assert_called_once()


# ---------------------------------------------------------------------------
# _is_market_open_grace_period
# ---------------------------------------------------------------------------


class TestIsMarketOpenGracePeriod:
    """Tests for _is_market_open_grace_period."""

    def test_grace_zero_returns_false(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 0.0
        assert svc._is_market_open_grace_period() is False

    def test_negative_grace_returns_false(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = -10.0
        assert svc._is_market_open_grace_period() is False

    def test_within_grace_period(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 300.0
        tz = dt.timezone(dt.timedelta(hours=8))
        open_time = dt.datetime(2026, 3, 20, 8, 45, tzinfo=tz)
        now_ts = _ts_for(hour=8, minute=47)  # 2 min after open

        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = open_time

        with (
            patch("hft_platform.core.timebase.now_s", return_value=now_ts),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            assert svc._is_market_open_grace_period() is True

    def test_outside_grace_period(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 60.0
        tz = dt.timezone(dt.timedelta(hours=8))
        open_time = dt.datetime(2026, 3, 20, 8, 45, tzinfo=tz)
        now_ts = _ts_for(hour=9, minute=0)  # 15 min after open

        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = open_time

        with (
            patch("hft_platform.core.timebase.now_s", return_value=now_ts),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            assert svc._is_market_open_grace_period() is False

    def test_not_trading_day(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 300.0
        tz = dt.timezone(dt.timedelta(hours=8))

        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = False

        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=9)),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            assert svc._is_market_open_grace_period() is False

    def test_no_session_open_returns_false(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 300.0
        tz = dt.timezone(dt.timedelta(hours=8))

        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = None

        with (
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=9)),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            assert svc._is_market_open_grace_period() is False

    def test_calendar_import_error(self) -> None:
        svc = FakeService()
        svc._market_open_grace_s = 300.0
        with patch(
            "hft_platform.core.market_calendar.get_calendar",
            side_effect=ImportError("no module"),
        ):
            assert svc._is_market_open_grace_period() is False


# ---------------------------------------------------------------------------
# within_reconnect_window (public wrapper)
# ---------------------------------------------------------------------------


class TestPublicWithinReconnectWindow:
    """Tests for the public within_reconnect_window wrapper."""

    def test_delegates_to_private(self) -> None:
        svc = FakeService()
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc.within_reconnect_window() is True

    def test_returns_false_when_private_returns_false(self) -> None:
        svc = FakeService()
        svc.reconnect_days = {"mon"}
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            assert svc.within_reconnect_window() is False
