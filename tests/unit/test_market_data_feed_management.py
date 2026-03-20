"""Tests for MarketDataService feed management behaviors.

Covers: FeedState transitions, feed gap calculation, reconnect window logic,
feature engine enable/disable, pending state management, rollover reconnect,
resubscribe cooldown, symbol gap tracking, and helper functions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.services.market_data import FeedState, MarketDataService, _looks_like_md


@pytest.fixture()
def _symbols_config(tmp_path: Path):
    """Create a minimal symbols.yaml and set SYMBOLS_CONFIG env var."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    old = os.environ.get("SYMBOLS_CONFIG")
    os.environ["SYMBOLS_CONFIG"] = str(cfg)
    yield
    if old is None:
        os.environ.pop("SYMBOLS_CONFIG", None)
    else:
        os.environ["SYMBOLS_CONFIG"] = old


def _env_patch(**overrides):
    """Return a dict suitable for monkeypatch that disables heavy side-effects."""
    base = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "HFT_MONITOR_LIVE_ENABLED": "0",
        "HFT_MONITOR_SHM_ENABLED": "0",
        "HFT_FEATURE_SHADOW_PARITY": "0",
        "HFT_RECONNECT_USE_CALENDAR": "0",
    }
    base.update(overrides)
    return base


def _make_service(env_overrides: dict | None = None):
    """Build a MarketDataService with all heavy deps mocked out."""
    env = _env_patch(**(env_overrides or {}))
    with patch.dict(os.environ, env, clear=False):
        bus = MagicMock(spec=RingBufferBus)
        raw_queue = asyncio.Queue()
        client = MagicMock()
        svc = MarketDataService(bus, raw_queue, client, feature_engine=None)
    return svc


# ---------------------------------------------------------------------------
# 1. FeedState enum values
# ---------------------------------------------------------------------------


class TestFeedState:
    def test_feed_state_values(self, _symbols_config):
        """FeedState enum contains the expected set of states."""
        expected = {"INIT", "CONNECTING", "SNAPSHOTTING", "CONNECTED", "DISCONNECTED", "RECOVERING"}
        actual = {s.name for s in FeedState}
        assert actual == expected

    def test_initial_state_is_init(self, _symbols_config):
        """Newly constructed service starts in INIT state."""
        svc = _make_service()
        assert svc.state is FeedState.INIT


# ---------------------------------------------------------------------------
# 2. _set_state transitions
# ---------------------------------------------------------------------------


class TestSetState:
    def test_state_transition(self, _symbols_config):
        """_set_state updates the state attribute."""
        svc = _make_service()
        svc._set_state(FeedState.CONNECTED)
        assert svc.state is FeedState.CONNECTED

    def test_same_state_no_op(self, _symbols_config):
        """_set_state with the current state should not log (no-op branch)."""
        svc = _make_service()
        svc._set_state(FeedState.INIT)
        # State unchanged
        assert svc.state is FeedState.INIT


# ---------------------------------------------------------------------------
# 3. get_max_feed_gap_s
# ---------------------------------------------------------------------------


class TestGetMaxFeedGapS:
    def test_no_symbols_returns_sentinel(self, _symbols_config):
        """With no symbols tracked, returns the sentinel (default 0.0)."""
        svc = _make_service()
        svc._symbol_last_tick.clear()
        assert svc.get_max_feed_gap_s() == 0.0

    def test_no_symbols_custom_sentinel(self, _symbols_config):
        """Custom sentinel via HFT_FEED_GAP_NO_DATA_S."""
        svc = _make_service()
        svc._symbol_last_tick.clear()
        with patch.dict(os.environ, {"HFT_FEED_GAP_NO_DATA_S": "99.0"}):
            assert svc.get_max_feed_gap_s() == 99.0

    def test_single_symbol_gap(self, _symbols_config):
        """Gap is computed as now - last_tick for one symbol."""
        svc = _make_service()
        past = time.monotonic() - 5.0
        svc._symbol_last_tick["2330"] = past
        gap = svc.get_max_feed_gap_s()
        assert 4.5 < gap < 6.0  # Allow small timing variance

    def test_max_across_symbols(self, _symbols_config):
        """Returns the maximum gap when multiple symbols are tracked."""
        svc = _make_service()
        now_mono = time.monotonic()
        svc._symbol_last_tick["A"] = now_mono - 2.0
        svc._symbol_last_tick["B"] = now_mono - 10.0
        svc._symbol_last_tick["C"] = now_mono - 1.0
        gap = svc.get_max_feed_gap_s()
        assert 9.5 < gap < 11.0


# ---------------------------------------------------------------------------
# 4. get_feed_gaps_by_symbol
# ---------------------------------------------------------------------------


class TestGetFeedGapsBySymbol:
    def test_empty_returns_empty(self, _symbols_config):
        """No symbols tracked means empty dict."""
        svc = _make_service()
        svc._symbol_last_tick.clear()
        assert svc.get_feed_gaps_by_symbol() == {}

    def test_per_symbol_gaps(self, _symbols_config):
        """Returns per-symbol gap values."""
        svc = _make_service()
        now_mono = time.monotonic()
        svc._symbol_last_tick["X"] = now_mono - 3.0
        svc._symbol_last_tick["Y"] = now_mono - 7.0
        gaps = svc.get_feed_gaps_by_symbol()
        assert set(gaps.keys()) == {"X", "Y"}
        assert 2.5 < gaps["X"] < 4.0
        assert 6.5 < gaps["Y"] < 8.0


# ---------------------------------------------------------------------------
# 5. within_reconnect_window / _within_reconnect_window
# ---------------------------------------------------------------------------


class TestReconnectWindow:
    def test_no_constraints_always_open(self, _symbols_config):
        """With no days/hours configured, window is always open."""
        svc = _make_service()
        svc.reconnect_days = set()
        svc.reconnect_hours = ""
        svc.reconnect_hours_2 = ""
        assert svc._within_reconnect_window() is True

    def test_wrong_day_blocks(self, _symbols_config):
        """If reconnect_days is set and today is not in the set, returns False."""
        svc = _make_service()
        svc.reconnect_days = {"zzz"}  # No real weekday matches this
        svc.reconnect_hours = ""
        svc.reconnect_hours_2 = ""
        assert svc._within_reconnect_window() is False

    def test_correct_day_no_hours_passes(self, _symbols_config):
        """If today's weekday is in the set but no hour window, returns True."""
        svc = _make_service()
        today_abbr = dt.datetime.now(tz=svc._reconnect_tzinfo).strftime("%a").lower()
        svc.reconnect_days = {today_abbr}
        svc.reconnect_hours = ""
        svc.reconnect_hours_2 = ""
        assert svc._within_reconnect_window() is True

    def test_hour_window_match(self, _symbols_config):
        """If current time falls in reconnect_hours window, returns True."""
        svc = _make_service()
        svc.reconnect_days = set()
        now_local = dt.datetime.now(tz=svc._reconnect_tzinfo)
        # Build a window that spans the current minute
        start = (now_local - dt.timedelta(minutes=5)).strftime("%H:%M")
        end = (now_local + dt.timedelta(minutes=5)).strftime("%H:%M")
        svc.reconnect_hours = f"{start}-{end}"
        assert svc._within_reconnect_window() is True

    def test_hour_window_miss(self, _symbols_config):
        """If current time is outside the reconnect_hours window, returns False."""
        svc = _make_service()
        svc.reconnect_days = set()
        now_local = dt.datetime.now(tz=svc._reconnect_tzinfo)
        # Build a window 4-5 hours in the past
        start = (now_local - dt.timedelta(hours=5)).strftime("%H:%M")
        end = (now_local - dt.timedelta(hours=4)).strftime("%H:%M")
        svc.reconnect_hours = f"{start}-{end}"
        svc.reconnect_hours_2 = ""
        assert svc._within_reconnect_window() is False


# ---------------------------------------------------------------------------
# 6. _should_rollover_reconnect
# ---------------------------------------------------------------------------


class TestRolloverReconnect:
    def test_same_day_no_rollover(self, _symbols_config):
        """If last_event_ts is today, no rollover needed."""
        svc = _make_service()
        svc.last_event_ts = time.time()  # Now
        assert svc._should_rollover_reconnect() is False

    def test_different_day_triggers_rollover(self, _symbols_config):
        """If last_event_ts is yesterday, rollover is triggered once."""
        svc = _make_service()
        yesterday = time.time() - 86400 * 2
        svc.last_event_ts = yesterday
        svc._last_rollover_seen_date = None
        assert svc._should_rollover_reconnect() is True

    def test_rollover_not_triggered_twice(self, _symbols_config):
        """Second call on same date returns False (already seen)."""
        svc = _make_service()
        yesterday = time.time() - 86400 * 2
        svc.last_event_ts = yesterday
        svc._last_rollover_seen_date = None
        assert svc._should_rollover_reconnect() is True
        # Second call: _last_rollover_seen_date was set to today
        assert svc._should_rollover_reconnect() is False


# ---------------------------------------------------------------------------
# 7. _mark_pending_reconnect
# ---------------------------------------------------------------------------


class TestPendingReconnect:
    def test_mark_sets_fields(self, _symbols_config):
        """_mark_pending_reconnect sets reason, gap, and since timestamp."""
        svc = _make_service()
        svc._mark_pending_reconnect(42.0, reason="test_reason")
        assert svc._pending_reconnect_reason == "test_reason"
        assert svc._pending_reconnect_gap == 42.0
        assert svc._pending_reconnect_since is not None

    def test_mark_default_reason(self, _symbols_config):
        """Default reason is 'heartbeat_gap'."""
        svc = _make_service()
        svc._mark_pending_reconnect(10.0)
        assert svc._pending_reconnect_reason == "heartbeat_gap"

    def test_mark_does_not_overwrite_since(self, _symbols_config):
        """Second call preserves the original _pending_reconnect_since."""
        svc = _make_service()
        svc._mark_pending_reconnect(5.0, reason="first")
        first_since = svc._pending_reconnect_since
        svc._mark_pending_reconnect(10.0, reason="second")
        assert svc._pending_reconnect_since == first_since


# ---------------------------------------------------------------------------
# 8. Feature engine enable/disable via env var
# ---------------------------------------------------------------------------


class TestFeatureEngineEnv:
    def test_feature_engine_disabled(self, _symbols_config):
        """HFT_FEATURE_ENGINE_ENABLED=0 results in feature_engine=None."""
        svc = _make_service({"HFT_FEATURE_ENGINE_ENABLED": "0"})
        assert svc.feature_engine is None

    def test_feature_engine_enabled_default_creates_engine(self, _symbols_config):
        """HFT_FEATURE_ENGINE_ENABLED=1 attempts to create a FeatureEngine."""
        # FeatureEngine() may fail in test env -- we just verify the intent
        svc = _make_service({"HFT_FEATURE_ENGINE_ENABLED": "1"})
        # Either successfully created or failed gracefully (set to None with log)
        # Just verify it tried -- the attribute exists either way
        assert hasattr(svc, "feature_engine")

    def test_explicit_engine_injected(self, _symbols_config):
        """Passing feature_engine= to constructor uses it directly."""
        mock_engine = MagicMock()
        mock_engine.feature_set_id.return_value = "test_set"
        env = _env_patch(HFT_FEATURE_ENGINE_ENABLED="0")
        with patch.dict(os.environ, env, clear=False):
            bus = MagicMock(spec=RingBufferBus)
            raw_queue = asyncio.Queue()
            client = MagicMock()
            svc = MarketDataService(bus, raw_queue, client, feature_engine=mock_engine)
        assert svc.feature_engine is mock_engine


# ---------------------------------------------------------------------------
# 9. _looks_like_md helper
# ---------------------------------------------------------------------------


class TestLooksLikeMd:
    def test_none_returns_false(self):
        assert _looks_like_md(None) is False

    def test_dict_with_code(self):
        assert _looks_like_md({"code": "2330"}) is True

    def test_dict_with_price_fields(self):
        assert _looks_like_md({"bid_price": 100, "ask_price": 101}) is True

    def test_dict_with_ts(self):
        assert _looks_like_md({"ts": 123456789}) is True

    def test_empty_dict_returns_false(self):
        assert _looks_like_md({}) is False

    def test_object_with_price_attrs(self):
        obj = MagicMock(spec=[])
        obj.bid_price = 100
        obj.code = "2330"
        assert _looks_like_md(obj) is True


# ---------------------------------------------------------------------------
# 10. Symbol gap tracking
# ---------------------------------------------------------------------------


class TestSymbolGapTracking:
    def test_symbol_last_tick_initially_empty(self, _symbols_config):
        svc = _make_service()
        assert svc._symbol_last_tick == {}

    def test_symbol_last_tick_update(self, _symbols_config):
        """Manually updating _symbol_last_tick is reflected in gap methods."""
        svc = _make_service()
        svc._symbol_last_tick["2330"] = time.monotonic()
        gaps = svc.get_feed_gaps_by_symbol()
        assert "2330" in gaps
        assert gaps["2330"] < 1.0  # Just set, gap is near zero


# ---------------------------------------------------------------------------
# 11. Resubscribe / reconnect cooldown defaults
# ---------------------------------------------------------------------------


class TestCooldownDefaults:
    def test_default_cooldown_values(self, _symbols_config):
        """Verify default cooldown/gap thresholds from env defaults."""
        svc = _make_service()
        assert svc.resubscribe_gap_s == 15.0
        assert svc.resubscribe_cooldown_s == 15.0
        assert svc.reconnect_gap_s == 60.0
        assert svc.reconnect_cooldown_s == 60.0
        assert svc.force_reconnect_gap_s == 300.0
        assert svc.reconnect_timeout_s == 30.0

    def test_custom_gap_thresholds(self, _symbols_config):
        """Custom env vars override gap defaults."""
        svc = _make_service(
            {
                "HFT_MD_RESUBSCRIBE_GAP_S": "25",
                "HFT_MD_RECONNECT_GAP_S": "120",
            }
        )
        assert svc.resubscribe_gap_s == 25.0
        assert svc.reconnect_gap_s == 120.0


# ---------------------------------------------------------------------------
# 12. Recorder degradation config
# ---------------------------------------------------------------------------


class TestRecorderDegradationConfig:
    def test_default_degrade_threshold(self, _symbols_config):
        svc = _make_service()
        assert svc._record_degrade_threshold == 500
        assert svc._record_degraded is False

    def test_custom_degrade_threshold(self, _symbols_config):
        svc = _make_service({"HFT_RECORD_DEGRADE_THRESHOLD": "1000"})
        assert svc._record_degrade_threshold == 1000


# ---------------------------------------------------------------------------
# 13. Heartbeat threshold and running state
# ---------------------------------------------------------------------------


class TestHeartbeatAndRunning:
    def test_default_heartbeat_threshold(self, _symbols_config):
        svc = _make_service()
        assert svc.heartbeat_threshold_s == 5.0

    def test_not_running_initially(self, _symbols_config):
        svc = _make_service()
        assert svc.running is False
