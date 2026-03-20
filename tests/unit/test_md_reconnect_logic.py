"""Tests for MarketDataReconnectMixin in services/_md_reconnect.py."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin


class FakeService(MarketDataReconnectMixin):
    """Minimal stand-in for MarketDataService with mixin attributes."""

    def __init__(self) -> None:
        self.reconnect_days: set[str] = set()
        self.reconnect_hours: str = ""
        self.reconnect_hours_2: str = ""
        self._reconnect_tzinfo: dt.tzinfo = dt.timezone(dt.timedelta(hours=8))
        self.last_event_ts: float = 0.0
        self.last_event_mono: float = 0.0
        self._last_rollover_seen_date: dt.date | None = None
        self._last_rollover_reconnect_date: dt.date | None = None
        self.state: FeedState | None = None
        self._market_open_grace_s: float = 0.0
        self.metrics_registry: object | None = None
        # Reconnect / resubscribe state
        self._last_resubscribe_ts: float = 0.0
        self.resubscribe_cooldown_s: float = 15.0
        self._resubscribe_attempts: int = 0
        self._last_reconnect_ts: float = 0.0
        self.reconnect_cooldown_s: float = 60.0
        self.reconnect_timeout_s: float = 30.0
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self.client: object | None = None
        self._feed_reconnect_gap_metric_child: object | None = None
        # Watchdog state
        self.running: bool = False
        self._watchdog_interval_s: float = 0.01  # fast for tests
        self._symbol_gap_skip_off_hours: bool = True
        self._symbol_gap_consecutive_hits: int = 0
        self._last_symbol_gap_off_hours_log_ts: float = 0.0
        self._symbol_gap_off_hours_log_interval_s: float = 300.0
        self._symbol_last_tick: dict[str, float] = {}
        self._symbol_gap_active_lookback_s: float = 90.0
        self._symbol_gap_min_active_symbols: int = 24
        self._symbol_gap_threshold_s: float = 6.0
        self._market_open_grace_gap_threshold_s: float = 30.0
        self._symbol_gap_min_stale_count: int = 5
        self._symbol_gap_stale_ratio_threshold: float = 0.85
        self._symbol_gap_severe_gap_s: float = 30.0
        self._symbol_gap_consecutive_cycles: int = 5
        self._symbol_gap_resubscribe_cooldown_s: float = 120.0
        self._last_symbol_gap_resubscribe_ts: float = 0.0
        # Monitor loop state
        self.heartbeat_threshold_s: float = 5.0
        self.resubscribe_gap_s: float = 15.0
        self.force_reconnect_gap_s: float = 300.0
        self.reconnect_gap_s: float = 60.0

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


# ---------------------------------------------------------------------------
# _mark_pending_reconnect
# ---------------------------------------------------------------------------


class TestMarkPendingReconnect:
    """Tests for _mark_pending_reconnect."""

    def test_sets_pending_fields(self) -> None:
        svc = FakeService()
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            svc._mark_pending_reconnect(42.5, reason="heartbeat_gap")
        assert svc._pending_reconnect_reason == "heartbeat_gap"
        assert svc._pending_reconnect_gap == 42.5
        assert svc._pending_reconnect_since == now

    def test_defaults_reason_to_heartbeat_gap(self) -> None:
        svc = FakeService()
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            svc._mark_pending_reconnect(10.0)
        assert svc._pending_reconnect_reason == "heartbeat_gap"

    def test_does_not_overwrite_since_on_repeat(self) -> None:
        svc = FakeService()
        first_ts = _ts_for(hour=10)
        svc._pending_reconnect_since = first_ts
        svc._pending_reconnect_reason = "heartbeat_gap"
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=11)):
            svc._mark_pending_reconnect(20.0, reason="heartbeat_gap")
        # since should stay at original value
        assert svc._pending_reconnect_since == first_ts

    def test_updates_gap_on_repeat(self) -> None:
        svc = FakeService()
        svc._pending_reconnect_reason = "heartbeat_gap"
        svc._pending_reconnect_since = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=11)):
            svc._mark_pending_reconnect(99.0, reason="heartbeat_gap")
        assert svc._pending_reconnect_gap == 99.0

    def test_preserves_since_on_new_reason(self) -> None:
        """_since is preserved even when reason changes (only set when None)."""
        svc = FakeService()
        original_ts = _ts_for(hour=10)
        svc._pending_reconnect_reason = "heartbeat_gap"
        svc._pending_reconnect_since = original_ts
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=11)):
            svc._mark_pending_reconnect(5.0, reason="session_rollover")
        assert svc._pending_reconnect_reason == "session_rollover"
        assert svc._pending_reconnect_since == original_ts


# ---------------------------------------------------------------------------
# _attempt_resubscribe
# ---------------------------------------------------------------------------


class TestAttemptResubscribe:
    """Tests for _attempt_resubscribe (async)."""

    @pytest.mark.asyncio
    async def test_skips_outside_reconnect_window(self) -> None:
        svc = FakeService()
        svc.reconnect_days = {"mon"}  # not Friday
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            await svc._attempt_resubscribe(20.0)
        # No resubscribe attempted — client is None, would blow up if called
        assert svc._resubscribe_attempts == 0

    @pytest.mark.asyncio
    async def test_skips_within_cooldown(self) -> None:
        svc = FakeService()
        now = _ts_for(hour=10)
        svc._last_resubscribe_ts = now - 5  # 5s ago, cooldown=15s
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0)
        assert svc._resubscribe_attempts == 0

    @pytest.mark.asyncio
    async def test_successful_resubscribe_resets_attempts(self) -> None:
        svc = FakeService()
        svc._resubscribe_attempts = 3
        mock_client = MagicMock()
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0)
        assert svc._resubscribe_attempts == 0
        assert svc._last_resubscribe_ts == now

    @pytest.mark.asyncio
    async def test_failed_resubscribe_increments_attempts(self) -> None:
        svc = FakeService()
        svc._resubscribe_attempts = 1
        mock_client = MagicMock()
        mock_client.resubscribe.return_value = False
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0)
        assert svc._resubscribe_attempts == 2

    @pytest.mark.asyncio
    async def test_no_client_returns_false(self) -> None:
        svc = FakeService()
        svc.client = None
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0)
        assert svc._resubscribe_attempts == 1

    @pytest.mark.asyncio
    async def test_gap_metric_incremented(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        mock_metric_child = MagicMock()
        mock_registry = MagicMock()
        mock_registry.feed_reconnect_total.labels.return_value = mock_metric_child
        svc.metrics_registry = mock_registry
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0, reason="heartbeat_gap")
        mock_metric_child.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_symbol_gap_metric_incremented(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        mock_metric_child = MagicMock()
        mock_registry = MagicMock()
        mock_registry.feed_reconnect_total.labels.return_value = mock_metric_child
        svc.metrics_registry = mock_registry
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._attempt_resubscribe(20.0, reason="symbol_gap")
        mock_registry.feed_reconnect_total.labels.assert_called_with(result="symbol_gap")


# ---------------------------------------------------------------------------
# _trigger_reconnect
# ---------------------------------------------------------------------------


class TestTriggerReconnect:
    """Tests for _trigger_reconnect (async)."""

    @pytest.mark.asyncio
    async def test_within_cooldown_returns_false(self) -> None:
        svc = FakeService()
        now = _ts_for(hour=10)
        svc._last_reconnect_ts = now - 30  # 30s ago, cooldown=60s
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_outside_window_returns_false(self) -> None:
        svc = FakeService()
        svc.reconnect_days = {"mon"}  # not Friday
        with patch("hft_platform.core.timebase.now_s", return_value=_ts_for()):
            result = await svc._trigger_reconnect(99.0)
        assert result is False

    @pytest.mark.asyncio
    async def test_no_client_sets_disconnected(self) -> None:
        svc = FakeService()
        svc.client = None
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0)
        assert result is False
        assert svc.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_successful_reconnect(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        svc._resubscribe_attempts = 5
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0, reason="heartbeat_gap")
        assert result is True
        assert svc.state == FeedState.CONNECTED
        assert svc._resubscribe_attempts == 0
        assert svc._last_reconnect_ts == now

    @pytest.mark.asyncio
    async def test_failed_reconnect_sets_disconnected(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.return_value = False
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0)
        assert result is False
        assert svc.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_timeout_sets_disconnected(self) -> None:
        svc = FakeService()
        svc.reconnect_timeout_s = 0.1

        async def slow_reconnect(*args: object, **kwargs: object) -> bool:
            await asyncio.sleep(5.0)
            return True

        mock_client = MagicMock()
        mock_client.reconnect.side_effect = lambda *a, **kw: time.sleep(5)
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0)
        assert result is False
        assert svc.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_exception_sets_disconnected(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.side_effect = RuntimeError("connection refused")
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            result = await svc._trigger_reconnect(99.0)
        assert result is False
        assert svc.state == FeedState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_session_rollover_forces_login(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._trigger_reconnect(99.0, reason="session_rollover")
        mock_client.reconnect.assert_called_once()
        call_args = mock_client.reconnect.call_args[0]
        assert call_args[1] is True  # force_login=True

    @pytest.mark.asyncio
    async def test_timeout_metric_incremented(self) -> None:
        svc = FakeService()
        svc.reconnect_timeout_s = 0.1
        mock_client = MagicMock()
        mock_client.reconnect.side_effect = lambda *a, **kw: time.sleep(5)
        svc.client = mock_client
        mock_registry = MagicMock()
        mock_registry.feed_reconnect_timeout_total = MagicMock()
        svc.metrics_registry = mock_registry
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._trigger_reconnect(99.0)
        mock_registry.feed_reconnect_timeout_total.labels.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_metric_incremented(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.side_effect = ValueError("bad")
        svc.client = mock_client
        mock_registry = MagicMock()
        mock_registry.feed_reconnect_exception_total = MagicMock()
        svc.metrics_registry = mock_registry
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._trigger_reconnect(99.0)
        mock_registry.feed_reconnect_exception_total.labels.assert_called_once()


# ---------------------------------------------------------------------------
# _request_reconnect
# ---------------------------------------------------------------------------


class TestRequestReconnect:
    """Tests for _request_reconnect (async)."""

    @pytest.mark.asyncio
    async def test_within_window_triggers_reconnect(self) -> None:
        svc = FakeService()
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._request_reconnect(50.0, reason="heartbeat_gap")
        assert svc.state == FeedState.CONNECTED

    @pytest.mark.asyncio
    async def test_outside_window_marks_pending(self) -> None:
        svc = FakeService()
        svc.reconnect_days = {"mon"}  # not Friday
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._request_reconnect(50.0, reason="session_rollover")
        assert svc._pending_reconnect_reason == "session_rollover"
        assert svc._pending_reconnect_gap == 50.0


# ---------------------------------------------------------------------------
# _run_monitor_reconnect_checks
# ---------------------------------------------------------------------------


class TestRunMonitorReconnectChecks:
    """Tests for _run_monitor_reconnect_checks (async)."""

    @pytest.mark.asyncio
    async def test_pending_reconnect_cleared_on_success(self) -> None:
        svc = FakeService()
        svc._pending_reconnect_reason = "heartbeat_gap"
        svc._pending_reconnect_gap = 30.0
        svc._pending_reconnect_since = _ts_for(hour=9)
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        svc.state = FeedState.CONNECTED
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=2.0)
        assert svc._pending_reconnect_reason is None
        assert svc._pending_reconnect_gap == 0.0
        assert svc._pending_reconnect_since is None

    @pytest.mark.asyncio
    async def test_rollover_pending_sets_rollover_date(self) -> None:
        svc = FakeService()
        svc._pending_reconnect_reason = "session_rollover"
        svc._pending_reconnect_gap = 10.0
        svc._pending_reconnect_since = _ts_for(hour=9)
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        svc.state = FeedState.CONNECTED
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=2.0)
        assert svc._last_rollover_reconnect_date == dt.date(2026, 3, 20)

    @pytest.mark.asyncio
    async def test_connected_heartbeat_triggers_resubscribe(self) -> None:
        svc = FakeService()
        svc.state = FeedState.CONNECTED
        svc.resubscribe_gap_s = 15.0
        mock_client = MagicMock()
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=20.0)
        mock_client.resubscribe.assert_called_once()

    @pytest.mark.asyncio
    async def test_connected_force_reconnect(self) -> None:
        svc = FakeService()
        svc.state = FeedState.CONNECTED
        svc.force_reconnect_gap_s = 300.0
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=400.0)
        mock_client.reconnect.assert_called()

    @pytest.mark.asyncio
    async def test_disconnected_requests_reconnect(self) -> None:
        svc = FakeService()
        svc.state = FeedState.DISCONNECTED
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=5.0)
        mock_client.reconnect.assert_called()

    @pytest.mark.asyncio
    async def test_recovering_requests_reconnect(self) -> None:
        svc = FakeService()
        svc.state = FeedState.RECOVERING
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        svc.client = mock_client
        now = _ts_for(hour=10)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=5.0)
        mock_client.reconnect.assert_called()

    @pytest.mark.asyncio
    async def test_rollover_detected_during_connected(self) -> None:
        svc = FakeService()
        svc.state = FeedState.CONNECTED
        svc.last_event_ts = _ts_for(day=19, hour=23)
        mock_client = MagicMock()
        mock_client.reconnect.return_value = True
        mock_client.resubscribe.return_value = True
        svc.client = mock_client
        now = _ts_for(day=20, hour=0, minute=5)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=2.0)
        # Should have called reconnect for session_rollover
        assert mock_client.reconnect.called

    @pytest.mark.asyncio
    async def test_small_gap_no_action(self) -> None:
        svc = FakeService()
        svc.state = FeedState.CONNECTED
        svc.last_event_ts = _ts_for(hour=10)
        mock_client = MagicMock()
        svc.client = mock_client
        now = _ts_for(hour=10, minute=1)
        with patch("hft_platform.core.timebase.now_s", return_value=now):
            await svc._run_monitor_reconnect_checks(gap=2.0)
        mock_client.resubscribe.assert_not_called()
        mock_client.reconnect.assert_not_called()


# ---------------------------------------------------------------------------
# _watchdog_loop
# ---------------------------------------------------------------------------


class TestWatchdogLoop:
    """Tests for _watchdog_loop (async) — runs a few cycles then stops."""

    @pytest.mark.asyncio
    async def test_stops_when_not_running(self) -> None:
        svc = FakeService()
        svc.running = False
        # Should return immediately since running=False
        await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_skips_non_connected_state(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.DISCONNECTED

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with patch("asyncio.sleep", side_effect=_stop_after_one):
            await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_skips_off_hours(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.CONNECTED
        svc._symbol_gap_skip_off_hours = True
        svc._symbol_gap_consecutive_hits = 5

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with (
            patch("asyncio.sleep", side_effect=_stop_after_one),
            patch.object(svc, "_is_trading_hours", return_value=False),
            patch("hft_platform.core.timebase.now_s", return_value=_ts_for(hour=20)),
        ):
            await svc._watchdog_loop()
        # hits reset to 0 during off-hours
        assert svc._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_empty_symbol_last_tick_resets_hits(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.CONNECTED
        svc._symbol_gap_skip_off_hours = False
        svc._symbol_last_tick = {}
        svc._symbol_gap_consecutive_hits = 3

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with patch("asyncio.sleep", side_effect=_stop_after_one):
            await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_below_min_active_resets_hits(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.CONNECTED
        svc._symbol_gap_skip_off_hours = False
        svc._symbol_gap_min_active_symbols = 24
        # Only 3 active symbols, below min_active=24
        now_mono = time.monotonic()
        svc._symbol_last_tick = {f"SYM{i}": now_mono for i in range(3)}
        svc._symbol_gap_consecutive_hits = 2

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with patch("asyncio.sleep", side_effect=_stop_after_one):
            await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_stale_symbols_increment_hits(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.CONNECTED
        svc._symbol_gap_skip_off_hours = False
        svc._symbol_gap_min_active_symbols = 1
        svc._symbol_gap_threshold_s = 6.0
        svc._symbol_gap_active_lookback_s = 0  # disable lookback filter
        # All symbols are stale (last tick = 100s ago)
        now_mono = time.monotonic()
        svc._symbol_last_tick = {f"SYM{i}": now_mono - 100 for i in range(30)}
        svc._symbol_gap_consecutive_hits = 0

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with patch("asyncio.sleep", side_effect=_stop_after_one):
            await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits >= 1

    @pytest.mark.asyncio
    async def test_no_stale_symbols_resets_hits(self) -> None:
        svc = FakeService()
        svc.running = True
        svc.state = FeedState.CONNECTED
        svc._symbol_gap_skip_off_hours = False
        svc._symbol_gap_min_active_symbols = 1
        svc._symbol_gap_threshold_s = 6.0
        svc._symbol_gap_active_lookback_s = 0
        # All fresh
        now_mono = time.monotonic()
        svc._symbol_last_tick = {f"SYM{i}": now_mono for i in range(30)}
        svc._symbol_gap_consecutive_hits = 5

        _real_sleep = asyncio.sleep
        call_count = 0

        async def _stop_after_one(delay: float) -> None:
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                svc.running = False
            await _real_sleep(0)

        with patch("asyncio.sleep", side_effect=_stop_after_one):
            await svc._watchdog_loop()
        assert svc._symbol_gap_consecutive_hits == 0
