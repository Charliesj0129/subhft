"""Coverage tests for _md_reconnect.py: calendar path, grace period, stale symbols,
watchdog loop, reconnect exception metrics, post-reconnect resets, monitor checks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.services._md_ingestion import FeedState
from hft_platform.services._md_reconnect import MarketDataReconnectMixin

# ---------------------------------------------------------------------------
# Minimal concrete class mixing in MarketDataReconnectMixin
# ---------------------------------------------------------------------------


class _FakeMD(MarketDataReconnectMixin):
    def __init__(self) -> None:
        self.running = True
        self.state = FeedState.CONNECTED
        self.last_event_ts: float = 0.0
        self.last_event_mono: float = 0.0
        self._last_rollover_reconnect_date: dt.date | None = None
        self._last_rollover_seen_date: dt.date | None = None
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self._last_resubscribe_ts: float = 0.0
        self._last_reconnect_ts: float = 0.0
        self._resubscribe_attempts: int = 0
        self._reconnect_tzinfo: dt.tzinfo = dt.timezone.utc
        self.reconnect_days: set[str] = set()
        self.reconnect_hours: str = ""
        self.reconnect_hours_2: str = ""
        self.resubscribe_cooldown_s: float = 15.0
        self.reconnect_cooldown_s: float = 60.0
        self.heartbeat_threshold_s: float = 5.0
        self.resubscribe_gap_s: float = 15.0
        self.force_reconnect_gap_s: float = 300.0
        self.reconnect_gap_s: float = 60.0
        self.reconnect_timeout_s: float = 5.0
        self.metrics_registry = None
        self.client: MagicMock | None = None
        self.lob: MagicMock | None = None
        self.feature_engine: MagicMock | None = None
        self._on_reconnect_callbacks: list = []
        self._market_open_grace_s: float = 0.0
        self._market_open_grace_gap_threshold_s: float = 30.0
        self._symbol_last_tick: dict[str, float] = {}
        self._symbol_gap_consecutive_hits: int = 0
        self._symbol_gap_threshold_s: float = 6.0
        self._symbol_gap_min_active_symbols: int = 2
        self._symbol_gap_active_lookback_s: float = 90.0
        self._symbol_gap_min_stale_count: int = 2
        self._symbol_gap_stale_ratio_threshold: float = 0.30
        self._symbol_gap_severe_gap_s: float = 10.0
        self._symbol_gap_consecutive_cycles: int = 2
        self._symbol_gap_resubscribe_cooldown_s: float = 0.0
        self._last_symbol_gap_resubscribe_ts: float = 0.0
        self._symbol_gap_skip_off_hours: bool = False
        self._watchdog_interval_s: float = 0.001
        self._last_symbol_gap_off_hours_log_ts: float = 0.0
        self._symbol_gap_off_hours_log_interval_s: float = 300.0
        self._feed_reconnect_gap_metric_child = None

    def _set_state(self, state: FeedState) -> None:
        self.state = state


@pytest.fixture()
def md() -> _FakeMD:
    return _FakeMD()


# ---------------------------------------------------------------------------
# _within_reconnect_window: calendar path (lines 63-70)
# ---------------------------------------------------------------------------


class TestWithinReconnectWindowCalendar:
    def test_calendar_blocks_non_trading_day(self, md: _FakeMD) -> None:
        """Calendar available + days_until_trading > 1 => False (lines 63-64, 66-70)."""
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        mock_cal = MagicMock()
        mock_cal.available = True
        mock_cal.days_until_trading.return_value = 3

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is False

    def test_calendar_allows_trading_day(self, md: _FakeMD) -> None:
        """Calendar available + days_until_trading <= 1 => passes through to hour check."""
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        mock_cal = MagicMock()
        mock_cal.available = True
        mock_cal.days_until_trading.return_value = 0

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is True

    def test_calendar_exception_continues(self, md: _FakeMD) -> None:
        """Calendar raising exception is caught, continues to hour check (lines 69-70)."""
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=RuntimeError("no cal")),
        ):
            tb.now_s.return_value = now.timestamp()
            # Even though calendar fails, still within hour window
            assert md._within_reconnect_window() is True

    def test_calendar_not_available_continues(self, md: _FakeMD) -> None:
        """Calendar not available (.available=False) continues to hour check."""
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "08:00-13:00"
        mock_cal = MagicMock()
        mock_cal.available = False

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "1"}),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now.timestamp()
            assert md._within_reconnect_window() is True

    def test_window_malformed_hours_continues(self, md: _FakeMD) -> None:
        """Malformed reconnect_hours string is caught and continues (lines 90-91)."""
        now = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc)
        md.reconnect_hours = "INVALID_FORMAT"

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now.timestamp()
            # Malformed window => loop continues, no windows match => False
            assert md._within_reconnect_window() is False


# ---------------------------------------------------------------------------
# _attempt_resubscribe: heartbeat_gap metric caching (lines 105-109)
# ---------------------------------------------------------------------------


class TestAttemptResubscribeMetrics:
    @pytest.mark.asyncio
    async def test_heartbeat_gap_metric_caching(self, md: _FakeMD) -> None:
        """First heartbeat_gap call caches metric child, second reuses it (lines 105-109)."""
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        metrics = MagicMock()
        md.metrics_registry = metrics
        client = MagicMock()
        client.resubscribe.return_value = True
        md.client = client

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            md._last_resubscribe_ts = now_ts - 100.0
            await md._attempt_resubscribe(30.0, reason="heartbeat_gap")
            # First call sets the cached child
            assert md._feed_reconnect_gap_metric_child is not None
            cached = md._feed_reconnect_gap_metric_child

            # Second call should reuse the cached child
            md._last_resubscribe_ts = now_ts - 100.0
            tb.now_s.return_value = now_ts + 100.0
            await md._attempt_resubscribe(30.0, reason="heartbeat_gap")
            assert md._feed_reconnect_gap_metric_child is cached


# ---------------------------------------------------------------------------
# _trigger_reconnect: timeout metrics, exception metrics (lines 152, 158-160)
# ---------------------------------------------------------------------------


class TestTriggerReconnectMetrics:
    @pytest.mark.asyncio
    @pytest.mark.timeout(20)
    async def test_timeout_increments_metric(self, md: _FakeMD) -> None:
        """Timeout during reconnect increments timeout metric (line 152)."""
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        md.reconnect_timeout_s = 0.01
        metrics = MagicMock()
        metrics.feed_reconnect_timeout_total = MagicMock()
        md.metrics_registry = metrics
        client = MagicMock()

        def slow_reconnect(*args, **kwargs):
            import time as _time

            _time.sleep(5.0)
            return True

        client.reconnect.side_effect = slow_reconnect
        md.client = client

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0, reason="heartbeat_gap")

        assert result is False
        metrics.feed_reconnect_timeout_total.labels.assert_called_once()
        metrics.feed_reconnect_timeout_total.labels().inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_exception_increments_metric(self, md: _FakeMD) -> None:
        """Exception during reconnect increments exception metric (lines 158-160)."""
        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        md._last_reconnect_ts = 0.0
        metrics = MagicMock()
        metrics.feed_reconnect_exception_total = MagicMock()
        md.metrics_registry = metrics
        client = MagicMock()
        client.reconnect.side_effect = RuntimeError("boom")
        md.client = client

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_RECONNECT_USE_CALENDAR": "0"}),
        ):
            tb.now_s.return_value = now_ts
            result = await md._trigger_reconnect(30.0, reason="heartbeat_gap")

        assert result is False
        metrics.feed_reconnect_exception_total.labels.assert_called_once()


# ---------------------------------------------------------------------------
# _apply_post_reconnect_resets (lines 189-195)
# ---------------------------------------------------------------------------


class TestPostReconnectResets:
    def test_apply_pending_resets_on_client(self, md: _FakeMD) -> None:
        """Client with _apply_pending_resets calls it (lines 187-188)."""
        client = MagicMock()
        client._apply_pending_resets = MagicMock()
        md.client = client
        md._apply_post_reconnect_resets()
        client._apply_pending_resets.assert_called_once()

    def test_fallback_resets_lob_and_feature_engine(self, md: _FakeMD) -> None:
        """Without _apply_pending_resets and without get_healthy_feed_gap_s,
        resets LOB and feature engine (lines 189-195)."""
        client = MagicMock(spec=[])  # no attributes
        md.client = client
        md.lob = MagicMock()
        md.lob.reset_books = MagicMock()
        md.feature_engine = MagicMock()
        md.feature_engine.reset_all = MagicMock()
        md._apply_post_reconnect_resets()
        md.lob.reset_books.assert_called_once()
        md.feature_engine.reset_all.assert_called_once()

    def test_no_reset_when_client_has_feed_gap_method(self, md: _FakeMD) -> None:
        """Client with get_healthy_feed_gap_s but no _apply_pending_resets skips resets."""
        client = MagicMock(spec=["get_healthy_feed_gap_s"])
        md.client = client
        md.lob = MagicMock()
        md.feature_engine = MagicMock()
        md._apply_post_reconnect_resets()
        md.lob.reset_books.assert_not_called()
        md.feature_engine.reset_all.assert_not_called()


# ---------------------------------------------------------------------------
# _is_trading_hours: fallback path (lines 230-241)
# ---------------------------------------------------------------------------


class TestIsTradingHoursFallback:
    def test_fallback_weekday_within_hours(self, md: _FakeMD) -> None:
        """Weekday within 8:45-13:45 CST returns True (lines 240-241)."""
        # 10:00 CST = UTC+8
        cst = dt.timezone(dt.timedelta(hours=8))
        aware = dt.datetime(2024, 1, 15, 10, 0, 0, tzinfo=cst)  # Monday
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_WATCHDOG_PRODUCT_TYPE": "future"}),
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError),
        ):
            tb.now_s.return_value = aware.timestamp()
            assert md._is_trading_hours() is True

    def test_fallback_weekday_outside_hours(self, md: _FakeMD) -> None:
        """Weekday outside 8:45-13:45 CST returns False."""
        cst = dt.timezone(dt.timedelta(hours=8))
        aware = dt.datetime(2024, 1, 15, 7, 0, 0, tzinfo=cst)  # Monday, 07:00
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_WATCHDOG_PRODUCT_TYPE": "future"}),
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError),
        ):
            tb.now_s.return_value = aware.timestamp()
            assert md._is_trading_hours() is False

    def test_trading_hours_updates_prometheus_gate(self, md: _FakeMD) -> None:
        metrics = MagicMock()
        md.metrics_registry = metrics
        cst = dt.timezone(dt.timedelta(hours=8))
        aware = dt.datetime(2024, 1, 15, 7, 0, 0, tzinfo=cst)
        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError),
        ):
            tb.now_s.return_value = aware.timestamp()
            assert md._is_trading_hours() is False
        metrics.market_trading_hours_active.set.assert_called_once_with(0)

    def test_calendar_path_when_available(self, md: _FakeMD) -> None:
        """Calendar available uses calendar.is_trading_hours (lines 230-232)."""
        mock_cal = MagicMock()
        mock_cal._tz = dt.timezone(dt.timedelta(hours=8))
        mock_cal.is_trading_hours.return_value = True

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch.dict("os.environ", {"HFT_WATCHDOG_PRODUCT_TYPE": "future"}),
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = dt.datetime(2024, 1, 15, 10, 0, tzinfo=dt.timezone.utc).timestamp()
            assert md._is_trading_hours() is True
        mock_cal.is_trading_hours.assert_called_once()


# ---------------------------------------------------------------------------
# _is_market_open_grace_period (lines 245-268)
# ---------------------------------------------------------------------------


class TestMarketOpenGracePeriod:
    def test_grace_disabled_returns_false(self, md: _FakeMD) -> None:
        """grace_s <= 0 returns False immediately (lines 245-247)."""
        md._market_open_grace_s = 0.0
        assert md._is_market_open_grace_period() is False

    def test_calendar_import_error_returns_false(self, md: _FakeMD) -> None:
        """ImportError from get_calendar returns False (lines 249-253)."""
        md._market_open_grace_s = 60.0
        with patch("hft_platform.core.market_calendar.get_calendar", side_effect=ImportError):
            assert md._is_market_open_grace_period() is False

    def test_not_trading_day_returns_false(self, md: _FakeMD) -> None:
        """Non-trading day returns False (lines 256-257)."""
        md._market_open_grace_s = 60.0
        mock_cal = MagicMock()
        mock_cal._tz = dt.timezone.utc
        mock_cal.is_trading_day.return_value = False

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = dt.datetime(2024, 1, 15, 9, 0, tzinfo=dt.timezone.utc).timestamp()
            assert md._is_market_open_grace_period() is False

    def test_no_session_open_returns_false(self, md: _FakeMD) -> None:
        """get_session_open returning None returns False (lines 258-260)."""
        md._market_open_grace_s = 60.0
        mock_cal = MagicMock()
        mock_cal._tz = dt.timezone.utc
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = None

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = dt.datetime(2024, 1, 15, 9, 0, tzinfo=dt.timezone.utc).timestamp()
            assert md._is_market_open_grace_period() is False

    def test_within_grace_returns_true(self, md: _FakeMD) -> None:
        """Within grace period after open returns True (lines 261-266)."""
        md._market_open_grace_s = 120.0
        tz = dt.timezone.utc
        now_dt = dt.datetime(2024, 1, 15, 9, 1, 0, tzinfo=tz)
        open_dt = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=tz)
        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = open_dt

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now_dt.timestamp()
            assert md._is_market_open_grace_period() is True

    def test_outside_grace_returns_false(self, md: _FakeMD) -> None:
        """Outside grace period returns False."""
        md._market_open_grace_s = 60.0
        tz = dt.timezone.utc
        now_dt = dt.datetime(2024, 1, 15, 9, 5, 0, tzinfo=tz)  # 5 min after open
        open_dt = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=tz)
        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = open_dt

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now_dt.timestamp()
            assert md._is_market_open_grace_period() is False

    def test_grace_exception_returns_false(self, md: _FakeMD) -> None:
        """Exception in grace period calculation returns False (lines 267-268)."""
        md._market_open_grace_s = 60.0
        mock_cal = MagicMock()
        mock_cal._tz = dt.timezone.utc
        mock_cal.is_trading_day.side_effect = RuntimeError("boom")

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = dt.datetime(2024, 1, 15, 9, 0, tzinfo=dt.timezone.utc).timestamp()
            assert md._is_market_open_grace_period() is False

    def test_grace_sets_metric(self, md: _FakeMD) -> None:
        """Grace period sets metric gauge when metrics_registry available (lines 263-265)."""
        md._market_open_grace_s = 120.0
        tz = dt.timezone.utc
        now_dt = dt.datetime(2024, 1, 15, 9, 1, 0, tzinfo=tz)
        open_dt = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=tz)
        mock_cal = MagicMock()
        mock_cal._tz = tz
        mock_cal.is_trading_day.return_value = True
        mock_cal.get_session_open.return_value = open_dt
        metrics = MagicMock()
        md.metrics_registry = metrics

        with (
            patch("hft_platform.services._md_reconnect.timebase") as tb,
            patch("hft_platform.core.market_calendar.get_calendar", return_value=mock_cal),
        ):
            tb.now_s.return_value = now_dt.timestamp()
            result = md._is_market_open_grace_period()
        assert result is True
        metrics.market_open_grace_active.set.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# _find_stale_symbols (lines 274-284)
# ---------------------------------------------------------------------------


class TestFindStaleSymbols:
    def test_finds_stale_symbols(self, md: _FakeMD) -> None:
        """Symbols with gap > threshold are returned (lines 274-284)."""
        now = 100.0
        snapshot = {"SYM_A": 90.0, "SYM_B": 99.0, "SYM_C": 80.0}
        md._symbol_gap_threshold_s = 6.0
        stale = md._find_stale_symbols(snapshot, now)
        symbols = [s for s, _ in stale]
        assert "SYM_A" in symbols  # gap=10
        assert "SYM_C" in symbols  # gap=20
        assert "SYM_B" not in symbols  # gap=1

    def test_excludes_option_prefixes(self, md: _FakeMD) -> None:
        """Option symbols (TXO, MXO, etc.) are excluded (line 279)."""
        now = 100.0
        snapshot = {"TXO20240115": 80.0, "TXFD6": 80.0}
        stale = md._find_stale_symbols(snapshot, now)
        symbols = [s for s, _ in stale]
        assert "TXFD6" in symbols
        assert "TXO20240115" not in symbols

    def test_grace_period_raises_threshold(self, md: _FakeMD) -> None:
        """During grace period, threshold is raised (line 276)."""
        md._market_open_grace_s = 120.0
        md._market_open_grace_gap_threshold_s = 30.0
        md._symbol_gap_threshold_s = 6.0
        now = 100.0
        snapshot = {"SYM_A": 85.0}  # gap=15, > 6 but < 30

        with patch.object(md, "_is_market_open_grace_period", return_value=True):
            stale = md._find_stale_symbols(snapshot, now)
        assert len(stale) == 0  # 15s < 30s grace threshold

    def test_no_grace_normal_threshold(self, md: _FakeMD) -> None:
        """Without grace period, normal threshold applies."""
        md._symbol_gap_threshold_s = 6.0
        now = 100.0
        snapshot = {"SYM_A": 85.0}  # gap=15

        with patch.object(md, "_is_market_open_grace_period", return_value=False):
            stale = md._find_stale_symbols(snapshot, now)
        assert len(stale) == 1

    def test_per_symbol_override_suppresses_false_positive(self, md: _FakeMD) -> None:
        """Bug #36: a symbol with a higher per-symbol threshold must NOT be
        flagged stale at the global threshold. Used for illiquid stocks /
        far-month futures where slow trading is normal."""
        md._symbol_gap_threshold_s = 6.0
        md._symbol_gap_threshold_overrides = {"TXFG6": 60.0, "2207": 120.0}
        now = 1000.0
        # gap=20 — would trigger global 6s threshold but well below override
        snapshot = {"TXFG6": 980.0, "2207": 940.0, "TXFD6": 980.0}

        with patch.object(md, "_is_market_open_grace_period", return_value=False):
            stale = md._find_stale_symbols(snapshot, now)
        symbols = [s for s, _ in stale]
        assert "TXFG6" not in symbols  # 20s < 60s override
        assert "2207" not in symbols  # 60s < 120s override
        assert "TXFD6" in symbols  # 20s > 6s global threshold

    def test_override_does_not_lower_below_global(self, md: _FakeMD) -> None:
        """Per-symbol override is only consulted as a per-symbol threshold;
        a symbol without an override still uses the global value."""
        md._symbol_gap_threshold_s = 6.0
        md._symbol_gap_threshold_overrides = {"TXFG6": 60.0}
        now = 1000.0
        snapshot = {"UNRELATED": 985.0}  # gap=15 — no override → use global 6s

        with patch.object(md, "_is_market_open_grace_period", return_value=False):
            stale = md._find_stale_symbols(snapshot, now)
        assert ("UNRELATED", 15.0) in [(s, round(g, 1)) for s, g in stale]


# ---------------------------------------------------------------------------
# _watchdog_loop (lines 294-361)
# ---------------------------------------------------------------------------


class TestWatchdogLoop:
    @pytest.mark.asyncio
    async def test_watchdog_skips_when_not_connected(self, md: _FakeMD) -> None:
        """Watchdog loop skips iteration when state != CONNECTED (line 294)."""
        md.state = FeedState.DISCONNECTED
        md._watchdog_interval_s = 0.001
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 3:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()
        assert md._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_watchdog_skips_off_hours(self, md: _FakeMD) -> None:
        """Watchdog skips outside trading hours (lines 296-303)."""
        md._symbol_gap_skip_off_hours = True
        md._watchdog_interval_s = 0.001
        md._symbol_last_tick = {"SYM": 0.0}
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 3:
                md.running = False
            await original_sleep(delay)

        with (
            patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep),
            patch.object(md, "_is_trading_hours", return_value=False),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = time.time()
            await md._watchdog_loop()

        assert md._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_watchdog_resets_hits_when_empty_symbols(self, md: _FakeMD) -> None:
        """Empty symbol_last_tick resets hits (lines 306-308)."""
        md._symbol_last_tick = {}
        md._symbol_gap_consecutive_hits = 5
        md._watchdog_interval_s = 0.001
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 2:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()
        assert md._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_watchdog_resets_hits_below_min_active(self, md: _FakeMD) -> None:
        """Below min_active_symbols resets hits (lines 323-325)."""
        md._symbol_gap_min_active_symbols = 50
        md._symbol_gap_consecutive_hits = 5
        md._symbol_last_tick = {"SYM_A": time.monotonic()}
        md._watchdog_interval_s = 0.001
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 2:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()
        assert md._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_watchdog_detects_stale_and_triggers_resubscribe(self, md: _FakeMD) -> None:
        """Stale symbols trigger resubscribe (lines 328-359)."""
        now_mono = time.monotonic()
        md._symbol_gap_consecutive_hits = 0
        md._symbol_gap_min_active_symbols = 2
        md._symbol_gap_min_stale_count = 2
        md._symbol_gap_stale_ratio_threshold = 0.30
        md._symbol_gap_severe_gap_s = 10.0
        md._symbol_gap_consecutive_cycles = 2
        md._symbol_gap_resubscribe_cooldown_s = 0.0
        md._symbol_gap_threshold_s = 6.0
        md._watchdog_interval_s = 0.001

        # 3 symbols, 2 stale (gap > 6s)
        md._symbol_last_tick = {
            "SYM_A": now_mono - 50.0,
            "SYM_B": now_mono - 50.0,
            "SYM_C": now_mono - 1.0,
        }
        md._attempt_resubscribe = AsyncMock()
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 5:
                md.running = False
            await original_sleep(delay)

        with (
            patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = time.time()
            await md._watchdog_loop()

        assert md._attempt_resubscribe.await_count >= 1

    @pytest.mark.asyncio
    async def test_watchdog_no_stale_resets_hits(self, md: _FakeMD) -> None:
        """No stale symbols resets consecutive hits (line 361)."""
        now_mono = time.monotonic()
        md._symbol_gap_consecutive_hits = 5
        md._symbol_gap_min_active_symbols = 2
        md._symbol_last_tick = {
            "SYM_A": now_mono - 1.0,
            "SYM_B": now_mono - 1.0,
            "SYM_C": now_mono - 1.0,
        }
        md._watchdog_interval_s = 0.001
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 2:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()
        assert md._symbol_gap_consecutive_hits == 0

    @pytest.mark.asyncio
    async def test_watchdog_runtime_error_on_dict_copy(self, md: _FakeMD) -> None:
        """RuntimeError on dict copy continues loop (lines 311-312)."""
        md._symbol_gap_min_active_symbols = 2
        md._watchdog_interval_s = 0.001
        iterations = 0

        class ExplodingDict(dict):
            """Dict that raises RuntimeError on iteration."""

            _explode_count = 0

            def __iter__(self):
                ExplodingDict._explode_count += 1
                if ExplodingDict._explode_count <= 2:
                    raise RuntimeError("dict changed size during iteration")
                return super().__iter__()

        md._symbol_last_tick = ExplodingDict({"SYM_A": time.monotonic()})

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 5:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()

        # Loop continued past the RuntimeError
        assert iterations >= 3

    @pytest.mark.asyncio
    async def test_watchdog_lookback_zero_uses_full_snapshot(self, md: _FakeMD) -> None:
        """lookback=0 uses full tick_snapshot without filtering (line 321)."""
        now_mono = time.monotonic()
        md._symbol_gap_active_lookback_s = 0.0
        md._symbol_gap_min_active_symbols = 2
        md._symbol_gap_threshold_s = 6.0
        md._symbol_last_tick = {
            "SYM_A": now_mono - 1.0,
            "SYM_B": now_mono - 1.0,
        }
        md._watchdog_interval_s = 0.001
        iterations = 0

        original_sleep = asyncio.sleep

        async def counting_sleep(delay):
            nonlocal iterations
            iterations += 1
            if iterations >= 2:
                md.running = False
            await original_sleep(delay)

        with patch("hft_platform.services._md_reconnect.asyncio.sleep", counting_sleep):
            await md._watchdog_loop()

        # Hits reset because no symbols are stale
        assert md._symbol_gap_consecutive_hits == 0


# ---------------------------------------------------------------------------
# _run_monitor_reconnect_checks: session_rollover pending (lines 374-381)
# ---------------------------------------------------------------------------


class TestMonitorReconnectRollover:
    @pytest.mark.asyncio
    async def test_pending_rollover_reconnect_sets_date(self, md: _FakeMD) -> None:
        """Successful pending session_rollover sets _last_rollover_reconnect_date (lines 374-378)."""
        md._pending_reconnect_reason = "session_rollover"
        md._pending_reconnect_gap = 40.0
        md._trigger_reconnect = AsyncMock(return_value=True)

        now_ts = dt.datetime(2024, 1, 15, 9, 0, 0, tzinfo=dt.timezone.utc).timestamp()
        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = now_ts
            await md._run_monitor_reconnect_checks(0.0)

        assert md._last_rollover_reconnect_date == dt.date(2024, 1, 15)
        assert md._pending_reconnect_reason is None

    @pytest.mark.asyncio
    async def test_pending_reconnect_fails_does_not_clear(self, md: _FakeMD) -> None:
        """Failed pending reconnect does not clear pending state."""
        md._pending_reconnect_reason = "heartbeat_gap"
        md._pending_reconnect_gap = 40.0
        md._trigger_reconnect = AsyncMock(return_value=False)

        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            await md._run_monitor_reconnect_checks(0.0)

        # Pending state should NOT be cleared on failure
        assert md._pending_reconnect_reason == "heartbeat_gap"

    @pytest.mark.asyncio
    async def test_connected_with_resubscribe_attempts_escalates(self, md: _FakeMD) -> None:
        """gap > reconnect_gap_s + resubscribe_attempts > 2 triggers reconnect (line 393)."""
        md.state = FeedState.CONNECTED
        md._resubscribe_attempts = 3
        md.reconnect_gap_s = 60.0
        md._attempt_resubscribe = AsyncMock()
        md._request_reconnect = AsyncMock()

        with (
            patch.object(md, "_within_reconnect_window", return_value=True),
            patch.object(md, "_should_rollover_reconnect", return_value=False),
            patch("hft_platform.services._md_reconnect.timebase") as tb,
        ):
            tb.now_s.return_value = 1700000000.0
            # gap=80 > reconnect_gap_s=60 and attempts=3 > 2
            await md._run_monitor_reconnect_checks(80.0)

        md._request_reconnect.assert_awaited()


# ---------------------------------------------------------------------------
# register_on_reconnect (lines 216)
# ---------------------------------------------------------------------------


class TestRegisterOnReconnect:
    def test_register_creates_list_if_missing(self) -> None:
        """register_on_reconnect initializes _on_reconnect_callbacks if not present."""
        md = _FakeMD()
        del md._on_reconnect_callbacks  # remove to test lazy init
        cb = MagicMock()
        md.register_on_reconnect(cb)
        assert cb in md._on_reconnect_callbacks


# ---------------------------------------------------------------------------
# _mark_pending_reconnect: new reason changes reason (line 199-200)
# ---------------------------------------------------------------------------


class TestMarkPendingNewReason:
    def test_new_reason_updates_reason_and_gap(self, md: _FakeMD) -> None:
        """Changing reason logs warning and updates reason (lines 199-200)."""
        md._pending_reconnect_reason = "heartbeat_gap"
        md._pending_reconnect_since = 1000.0
        with patch("hft_platform.services._md_reconnect.timebase") as tb:
            tb.now_s.return_value = 2000.0
            md._mark_pending_reconnect(50.0, reason="session_rollover")
        assert md._pending_reconnect_reason == "session_rollover"
        assert md._pending_reconnect_gap == 50.0
        # since should remain at 1000.0 (not overwritten)
        assert md._pending_reconnect_since == 1000.0
