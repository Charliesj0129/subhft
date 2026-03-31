"""Coverage tests for services/market_data.py — targeting uncovered paths.

Focus areas (from 59% baseline coverage):
- _process_raw: tick/bidask normalization, error handling, early returns
- _record_direct_event: degraded mode, QueueFull, recovery
- _enqueue_raw: backpressure, drop counting
- _publish_events: combinatorial stats/feature_update/publish_full_events
- _maybe_update_features: happy path, engine=None, stats missing attrs, raises
- get_max_feed_gap_s / get_feed_gaps_by_symbol: option exclusion, empty
- _within_reconnect_window: time windows, day filter
- _set_state: state change
- _build_trace_id: meta/seq combinations
- _publish_to_redis: with publisher, with bids/asks/price
- _init_redis_publisher: enabled path, exception path
- _shm publisher init: symbols path
- _monitor_loop: basic iteration
- _on_shioaji_event: callback parsing, loop missing
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_symbols_config() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return tmp, cfg


def _make_service(extra_env: dict | None = None, **kwargs):
    """Create a MarketDataService with feature engine disabled and mocked dependencies."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }
    if extra_env:
        env.update(extra_env)

    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue(maxsize=100)
    client = MagicMock()
    client.login = MagicMock(return_value=None)
    client.validate_symbols = MagicMock(return_value=None)
    client.fetch_snapshots = MagicMock(return_value=[])
    client.subscribe_basket = MagicMock(return_value=None)

    with patch.dict(os.environ, env):
        svc = MarketDataService(bus, raw_queue, client, **kwargs)
    svc._tmp = tmp  # keep alive
    return svc, bus, raw_queue, client


# ---------------------------------------------------------------------------
# _set_state
# ---------------------------------------------------------------------------


def test_set_state_changes_state():
    from hft_platform.services.market_data import FeedState

    svc, *_ = _make_service()
    assert svc.state == FeedState.INIT
    svc._set_state(FeedState.CONNECTING)
    assert svc.state == FeedState.CONNECTING


def test_set_state_no_change_when_same():
    from hft_platform.services.market_data import FeedState

    svc, *_ = _make_service()
    svc._set_state(FeedState.INIT)
    assert svc.state == FeedState.INIT


# ---------------------------------------------------------------------------
# _build_trace_id
# ---------------------------------------------------------------------------


def test_build_trace_id_with_meta_seq():
    from hft_platform.services.market_data import MarketDataService

    meta = SimpleNamespace(seq=42, topic="tick")
    event = SimpleNamespace(meta=meta)
    assert MarketDataService._build_trace_id(event) == "tick:42"


def test_build_trace_id_meta_no_seq():
    from hft_platform.services.market_data import MarketDataService

    meta = SimpleNamespace(topic="tick")
    event = SimpleNamespace(meta=meta)
    assert MarketDataService._build_trace_id(event) == ""


def test_build_trace_id_no_meta():
    from hft_platform.services.market_data import MarketDataService

    event = SimpleNamespace()
    assert MarketDataService._build_trace_id(event) == ""


# ---------------------------------------------------------------------------
# _publish_events
# ---------------------------------------------------------------------------


def test_publish_events_full_with_stats_and_features():
    """publish_full_events=True, stats + feature_update → publish_many_nowait with 3 items."""
    svc, bus, *_ = _make_service()
    svc.publish_full_events = True

    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)
    feature_update = SimpleNamespace(feature_set_id="v3")

    svc._publish_events(event, stats, feature_update)
    bus.publish_many_nowait.assert_called_once_with((event, stats, feature_update))


def test_publish_events_full_stats_only():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = True

    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace(best_bid=1000)

    svc._publish_events(event, stats, None)
    bus.publish_many_nowait.assert_called_once_with((event, stats))


def test_publish_events_full_feature_only():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = True

    event = SimpleNamespace(symbol="2330")
    feature_update = SimpleNamespace(feature_set_id="v3")

    svc._publish_events(event, None, feature_update)
    bus.publish_many_nowait.assert_called_once_with((event, feature_update))


def test_publish_events_full_no_stats_no_features():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = True

    event = SimpleNamespace(symbol="2330")
    svc._publish_events(event, None, None)
    bus.publish_nowait.assert_called_once_with(event)


def test_publish_events_not_full_stats_and_features():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = False

    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace(best_bid=1000)
    feature_update = SimpleNamespace(feature_set_id="v3")

    svc._publish_events(event, stats, feature_update)
    bus.publish_many_nowait.assert_called_once_with((stats, feature_update))


def test_publish_events_not_full_stats_only():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = False

    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace(best_bid=1000)

    svc._publish_events(event, stats, None)
    bus.publish_many_nowait.assert_called_once_with((stats,))


def test_publish_events_not_full_feature_only():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = False

    event = SimpleNamespace(symbol="2330")
    feature_update = SimpleNamespace(feature_set_id="v3")

    svc._publish_events(event, None, feature_update)
    bus.publish_many_nowait.assert_called_once_with((feature_update,))


def test_publish_events_not_full_nothing():
    svc, bus, *_ = _make_service()
    svc.publish_full_events = False

    event = SimpleNamespace(symbol="2330")
    svc._publish_events(event, None, None)
    bus.publish_many_nowait.assert_not_called()
    bus.publish_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# _enqueue_raw
# ---------------------------------------------------------------------------


def test_enqueue_raw_normal():
    svc, *_ = _make_service()
    svc._enqueue_raw("TSE", {"code": "2330"})
    assert svc.raw_queue.qsize() == 1


def test_enqueue_raw_queue_full_increments_drop_counter():
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    # Fill the queue
    svc.raw_queue.put_nowait(("exchange", "first"))

    initial_dropped = svc._raw_dropped_count
    svc._enqueue_raw("TSE", {"code": "2330"})
    assert svc._raw_dropped_count == initial_dropped + 1


def test_enqueue_raw_queue_full_metrics_registry_inc():
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))
    svc.metrics_registry = MagicMock()

    svc._enqueue_raw("TSE", {"code": "2330"})
    svc.metrics_registry.raw_queue_dropped_total.inc.assert_called_once()


def test_enqueue_raw_no_metrics_registry():
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))
    svc.metrics_registry = None

    # Should not raise
    svc._enqueue_raw("TSE", {"code": "2330"})
    assert svc._raw_dropped_count == 1


# ---------------------------------------------------------------------------
# _record_direct_event
# ---------------------------------------------------------------------------


def _make_tick_event(symbol="2330"):
    """Create a simple mock TickEvent."""
    from hft_platform.events import TickEvent

    return TickEvent(
        meta=None,
        symbol=symbol,
        price=5000000,
        volume=100,
    )


def test_record_direct_event_normal_put():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True

    event = _make_tick_event()

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330", "price": 5000000})
        svc._record_direct_event(event)

    assert recorder_queue.qsize() == 1


def test_record_direct_event_map_returns_none():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue

    event = _make_tick_event()

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = None
        svc._record_direct_event(event)

    assert recorder_queue.qsize() == 0


def test_record_direct_event_queue_full_enters_degraded_mode():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=1)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True
    svc._record_degrade_threshold = 1
    # Fill the queue
    recorder_queue.put_nowait({"topic": "tick", "data": {}})

    event = _make_tick_event()

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    assert svc._recorder_dropped_count >= 1
    assert svc._record_degraded is True


def test_record_direct_event_degraded_skips_recording():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_degraded = True
    svc._record_degrade_last_check = time.monotonic()  # recent check
    svc._record_degrade_check_s = 1000.0  # won't trigger check

    event = _make_tick_event()
    svc._record_direct_event(event)

    assert recorder_queue.qsize() == 0
    assert svc._record_degraded_drops == 1


def test_record_direct_event_degraded_recovers_when_queue_below_threshold():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_degraded = True
    svc._record_degraded_since = time.monotonic() - 60.0
    svc._record_degraded_drops = 5
    # Force the degraded check to run
    svc._record_degrade_last_check = time.monotonic() - 100.0
    svc._record_degrade_check_s = 1.0

    event = _make_tick_event()

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    # Queue has space (qsize=0 < maxsize*0.5=50), so degraded mode exits
    assert svc._record_degraded is False


def test_record_direct_event_degraded_stays_if_queue_still_full():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=10)
    svc.recorder_queue = recorder_queue
    # Fill queue to capacity
    for _ in range(10):
        recorder_queue.put_nowait({"topic": "tick", "data": {}})
    svc._record_degraded = True
    svc._record_degraded_since = time.monotonic() - 60.0
    svc._record_degraded_drops = 0
    svc._record_degrade_last_check = time.monotonic() - 100.0
    svc._record_degrade_check_s = 1.0

    event = _make_tick_event()
    svc._record_direct_event(event)

    # Queue still full, stays degraded
    assert svc._record_degraded is True
    assert svc._record_degraded_drops == 1


def test_record_direct_event_no_recorder_queue():
    svc, *_ = _make_service()
    svc.recorder_queue = None
    event = _make_tick_event()
    # Should return immediately without error
    svc._record_direct_event(event)
    assert svc.recorder_queue is None


def test_record_direct_event_map_raises():
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue

    event = _make_tick_event()

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.side_effect = RuntimeError("mapping error")
        svc._record_direct_event(event)

    # Exception handled, nothing queued
    assert recorder_queue.qsize() == 0


# ---------------------------------------------------------------------------
# get_max_feed_gap_s
# ---------------------------------------------------------------------------


def test_get_max_feed_gap_s_empty():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {}
    gap = svc.get_max_feed_gap_s()
    assert gap == 0.0


def test_get_max_feed_gap_s_core_symbol():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {"2330": time.monotonic() - 3.0}
    gap = svc.get_max_feed_gap_s()
    assert 2.5 < gap < 5.0


def test_get_max_feed_gap_s_excludes_option_symbols():
    svc, *_ = _make_service()
    # Only option symbols — should fall back to global max
    svc._symbol_last_tick = {"TXO12345": time.monotonic() - 10.0}
    gap = svc.get_max_feed_gap_s()
    assert gap > 9.0  # falls back to option max


def test_get_max_feed_gap_s_mixed_excludes_options():
    svc, *_ = _make_service()
    # Core symbol with 2s gap, option with 60s gap
    svc._symbol_last_tick = {
        "2330": time.monotonic() - 2.0,
        "TXO12345": time.monotonic() - 60.0,
        "MXO9999": time.monotonic() - 90.0,
    }
    gap = svc.get_max_feed_gap_s()
    # Should be ~2s (core only), not 90s
    assert gap < 10.0


# ---------------------------------------------------------------------------
# get_feed_gaps_by_symbol
# ---------------------------------------------------------------------------


def test_get_feed_gaps_by_symbol_empty():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {}
    assert svc.get_feed_gaps_by_symbol() == {}


def test_get_feed_gaps_by_symbol_returns_gaps():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {
        "2330": time.monotonic() - 5.0,
        "TXFD6": time.monotonic() - 2.0,
    }
    gaps = svc.get_feed_gaps_by_symbol()
    assert set(gaps.keys()) == {"2330", "TXFD6"}
    assert gaps["2330"] > 4.0
    assert gaps["TXFD6"] > 1.0


# ---------------------------------------------------------------------------
# _maybe_update_features
# ---------------------------------------------------------------------------


def test_maybe_update_features_no_engine():
    svc, *_ = _make_service()
    svc.feature_engine = None
    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)
    result = svc._maybe_update_features(event, stats)
    assert result is None


def test_maybe_update_features_no_stats():
    svc, *_ = _make_service()
    svc.feature_engine = MagicMock()
    event = SimpleNamespace(symbol="2330")
    result = svc._maybe_update_features(event, None)
    assert result is None


def test_maybe_update_features_stats_missing_best_bid():
    svc, *_ = _make_service()
    svc.feature_engine = MagicMock()
    event = SimpleNamespace(symbol="2330")
    stats = SimpleNamespace()  # missing best_bid, best_ask
    result = svc._maybe_update_features(event, stats)
    assert result is None


def test_maybe_update_features_happy_path_process_lob_update():
    svc, *_ = _make_service()

    feature_engine = MagicMock()
    feature_update = MagicMock()
    feature_update.feature_set_id = "v3"
    feature_update.quality_flags = 0
    feature_engine.process_lob_update = MagicMock(return_value=feature_update)
    svc.feature_engine = feature_engine

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)
    svc.metrics_registry = None

    result = svc._maybe_update_features(event, stats)
    assert result is feature_update


def test_maybe_update_features_happy_path_fallback_process_lob_stats():
    from hft_platform.events import LOBStatsEvent

    svc, *_ = _make_service()

    feature_engine = MagicMock()
    feature_engine.process_lob_update = None  # not callable, triggers fallback
    feature_update = MagicMock()
    feature_update.feature_set_id = "v3"
    feature_update.quality_flags = 0
    feature_engine.process_lob_stats = MagicMock(return_value=feature_update)
    svc.feature_engine = feature_engine

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)
    svc.metrics_registry = None

    result = svc._maybe_update_features(event, stats)
    assert result is feature_update


def test_maybe_update_features_engine_raises():
    svc, *_ = _make_service()

    feature_engine = MagicMock()
    feature_engine.process_lob_update = MagicMock(side_effect=RuntimeError("boom"))
    svc.feature_engine = feature_engine
    svc.metrics_registry = None

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)

    result = svc._maybe_update_features(event, stats)
    assert result is None


def test_maybe_update_features_on_tick_called_for_classified_tick():
    from hft_platform.events import TickEvent

    svc, *_ = _make_service()

    feature_engine = MagicMock()
    feature_update = MagicMock()
    feature_update.feature_set_id = "v3"
    feature_update.quality_flags = 0
    feature_engine.process_lob_update = MagicMock(return_value=feature_update)
    svc.feature_engine = feature_engine
    svc.metrics_registry = None

    event = TickEvent(
        meta=None,
        symbol="2330",
        price=5000000,
        volume=100,
        trade_direction=1,
        trade_confidence=100,
    )
    stats = SimpleNamespace(best_bid=1000, best_ask=1001)

    svc._maybe_update_features(event, stats)
    feature_engine.on_tick.assert_called_once_with("2330", 5000000, 100, 1, 100)


# ---------------------------------------------------------------------------
# _process_raw
# ---------------------------------------------------------------------------


def _make_bidask_raw():
    return {"bid_price": [5000000, 4999000], "ask_price": [5001000, 5002000], "bid_volume": [10, 5]}


def _make_tick_raw():
    return {"code": "2330", "close": 500.0, "volume": 100}


def test_process_raw_tick_publishes_event():
    svc, bus, *_ = _make_service()

    from hft_platform.events import TickEvent

    mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    svc._process_raw(raw)

    bus.publish_nowait.assert_called_once_with(mock_event)


def test_process_raw_bidask_publishes_event():
    svc, bus, *_ = _make_service()

    from hft_platform.events import BidAskEvent
    import numpy as np

    bids = np.array([[5000000, 10]], dtype=np.int64)
    asks = np.array([[5001000, 5]], dtype=np.int64)
    mock_event = BidAskEvent(meta=None, symbol="2330", bids=bids, asks=asks, is_snapshot=False)

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_bidask = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    raw = {"bid_price": [5000000], "ask_price": [5001000], "bid_volume": [10]}
    svc._process_raw(raw)

    bus.publish_nowait.assert_called_once_with(mock_event)


def test_process_raw_normalization_error_skips():
    svc, bus, *_ = _make_service()

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(side_effect=ValueError("bad data"))

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    svc._process_raw(raw)

    bus.publish_nowait.assert_not_called()
    bus.publish_many_nowait.assert_not_called()


def test_process_raw_none_event_returns_early():
    svc, bus, *_ = _make_service()

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=None)

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    svc._process_raw(raw)

    bus.publish_nowait.assert_not_called()


def test_process_raw_object_tick():
    svc, bus, *_ = _make_service()

    from hft_platform.events import TickEvent

    mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    # Object-style raw with close/price but no bid attributes
    raw = SimpleNamespace(close=500.0)
    svc._process_raw(raw)

    bus.publish_nowait.assert_called_once_with(mock_event)


def test_process_raw_records_when_record_direct():
    svc, bus, *_ = _make_service()

    from hft_platform.events import TickEvent

    mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True

    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        raw = {"code": "2330", "close": 500.0, "volume": 100}
        svc._process_raw(raw)

    assert recorder_queue.qsize() == 1


# ---------------------------------------------------------------------------
# _within_reconnect_window
# ---------------------------------------------------------------------------


def test_within_reconnect_window_no_constraints():
    svc, *_ = _make_service()
    svc.reconnect_days = set()
    svc.reconnect_hours = ""
    svc.reconnect_hours_2 = ""
    assert svc._within_reconnect_window() is True


def test_within_reconnect_window_inside_hours():
    import datetime as dt
    from zoneinfo import ZoneInfo

    svc, *_ = _make_service()
    svc.reconnect_days = set()
    svc.reconnect_hours = "08:00-15:00"
    svc.reconnect_hours_2 = ""
    svc._reconnect_tzinfo = ZoneInfo("UTC")

    # Patch to a time inside the window
    fake_now = dt.datetime(2025, 1, 6, 10, 30, 0, tzinfo=dt.timezone.utc)  # Monday 10:30
    with patch("hft_platform.services.market_data.dt") as mock_dt:
        mock_dt.datetime = MagicMock()
        mock_dt.datetime.now = MagicMock(return_value=fake_now)
        mock_dt.time = dt.time
        mock_dt.date = dt.date
        mock_dt.UTC = dt.UTC
        with patch.dict(os.environ, {"HFT_RECONNECT_USE_CALENDAR": "0"}):
            result = svc._within_reconnect_window()
    assert result is True


def test_within_reconnect_window_outside_hours():
    import datetime as dt
    from zoneinfo import ZoneInfo

    svc, *_ = _make_service()
    svc.reconnect_days = set()
    svc.reconnect_hours = "08:00-09:00"
    svc.reconnect_hours_2 = ""
    svc._reconnect_tzinfo = ZoneInfo("UTC")

    # Patch to a time outside the window (15:30 UTC)
    fake_now = dt.datetime(2025, 1, 6, 15, 30, 0, tzinfo=dt.timezone.utc)
    with patch("hft_platform.services.market_data.dt") as mock_dt:
        mock_dt.datetime = MagicMock()
        mock_dt.datetime.now = MagicMock(return_value=fake_now)
        mock_dt.time = dt.time
        mock_dt.date = dt.date
        mock_dt.UTC = dt.UTC
        with patch.dict(os.environ, {"HFT_RECONNECT_USE_CALENDAR": "0"}):
            result = svc._within_reconnect_window()
    assert result is False


# ---------------------------------------------------------------------------
# _publish_to_redis
# ---------------------------------------------------------------------------


def test_publish_to_redis_no_publisher():
    svc, *_ = _make_service()
    svc._redis_publisher = None
    event = SimpleNamespace(symbol="2330", meta=None)
    stats = SimpleNamespace()
    # Should not raise
    svc._publish_to_redis(event, stats)
    assert svc._redis_publisher is None


def test_publish_to_redis_with_publisher_tick():
    svc, *_ = _make_service()
    pub = MagicMock()
    svc._redis_publisher = pub

    event = SimpleNamespace(symbol="2330", meta=None, local_ts=0, price=5000000, volume=100, bids=None, asks=None)
    stats = SimpleNamespace()

    svc._publish_to_redis(event, stats)
    pub.publish_market_data.assert_called_once()
    call_args = pub.publish_market_data.call_args[0][0]
    assert call_args["symbol"] == "2330"
    assert call_args["price_scaled"] == 5000000


def test_publish_to_redis_with_bids_and_asks():
    svc, *_ = _make_service()
    pub = MagicMock()
    svc._redis_publisher = pub

    bids = [[5000000, 10], [4999000, 5]]
    asks = [[5001000, 8], [5002000, 3]]
    event = SimpleNamespace(
        symbol="2330",
        meta=None,
        local_ts=0,
        price=None,
        bids=bids,
        asks=asks,
    )

    svc._publish_to_redis(event, SimpleNamespace())
    call_args = pub.publish_market_data.call_args[0][0]
    assert "bids_price" in call_args
    assert "asks_price" in call_args


def test_publish_to_redis_publisher_raises_silently():
    svc, *_ = _make_service()
    pub = MagicMock()
    pub.publish_market_data = MagicMock(side_effect=RuntimeError("connection error"))
    svc._redis_publisher = pub

    event = SimpleNamespace(symbol="2330", meta=None, local_ts=0, price=None, bids=None, asks=None)
    # Should not raise — fire-and-forget
    svc._publish_to_redis(event, SimpleNamespace())
    pub.publish_market_data.assert_called_once()


# ---------------------------------------------------------------------------
# _init_redis_publisher
# ---------------------------------------------------------------------------


def test_init_redis_publisher_disabled():
    svc, *_ = _make_service()
    svc._redis_publisher = None
    with patch.dict(os.environ, {"HFT_MONITOR_LIVE_ENABLED": "0"}):
        svc._init_redis_publisher()
    assert svc._redis_publisher is None


def test_init_redis_publisher_enabled_success():
    svc, *_ = _make_service()
    mock_pub = MagicMock()
    with patch.dict(os.environ, {"HFT_MONITOR_LIVE_ENABLED": "1"}):
        with patch("hft_platform.monitor._redis_publish.MonitorLivePublisher", return_value=mock_pub):
            svc._init_redis_publisher()
    assert svc._redis_publisher is mock_pub
    mock_pub.start.assert_called_once()


def test_init_redis_publisher_import_error_sets_none():
    svc, *_ = _make_service()
    with patch.dict(os.environ, {"HFT_MONITOR_LIVE_ENABLED": "1"}):
        with patch.dict("sys.modules", {"hft_platform.monitor._redis_publish": None}):
            svc._init_redis_publisher()
    assert svc._redis_publisher is None


# ---------------------------------------------------------------------------
# _log_first_event
# ---------------------------------------------------------------------------


def test_log_first_tick_event():
    from hft_platform.events import TickEvent

    svc, *_ = _make_service()
    svc._first_tick_event = False

    event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
    svc._log_first_event(event)
    assert svc._first_tick_event is True


def test_log_first_tick_event_only_once():
    from hft_platform.events import TickEvent

    svc, *_ = _make_service()
    svc._first_tick_event = True  # already seen

    event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
    svc._log_first_event(event)
    assert svc._first_tick_event is True


def test_log_first_bidask_event():
    from hft_platform.events import BidAskEvent
    import numpy as np

    svc, *_ = _make_service()
    svc._first_bidask_event = False

    bids = np.array([[5000000, 10]], dtype=np.int64)
    asks = np.array([[5001000, 5]], dtype=np.int64)
    event = BidAskEvent(meta=None, symbol="2330", bids=bids, asks=asks, is_snapshot=False)
    svc._log_first_event(event)
    assert svc._first_bidask_event is True


# ---------------------------------------------------------------------------
# _update_symbol_tick_inline
# ---------------------------------------------------------------------------


def test_update_symbol_tick_inline_updates_dict():
    svc, *_ = _make_service()
    svc._symbol_tick_inline = True
    event = SimpleNamespace(symbol="2330")
    svc._update_symbol_tick_inline(event)
    assert "2330" in svc._symbol_last_tick


def test_update_symbol_tick_inline_no_symbol():
    svc, *_ = _make_service()
    svc._symbol_tick_inline = True
    event = SimpleNamespace(symbol=None)
    svc._update_symbol_tick_inline(event)
    assert None not in svc._symbol_last_tick


# ---------------------------------------------------------------------------
# _shm publisher init with symbols
# ---------------------------------------------------------------------------


def test_init_shm_publisher_with_subscribed_symbols():
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue()
    client = MagicMock()
    client.subscribed_symbols = ["2330", "TXFD6"]

    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }

    mock_shm = MagicMock()
    mock_shm.max_symbols = 64

    with patch.dict(os.environ, env):
        with patch("hft_platform.ipc.shm_snapshot.ShmSnapshotWriter", return_value=mock_shm):
            svc = MarketDataService(bus, raw_queue, client)

    # Both symbols should be indexed
    assert "2330" in svc._shm_symbol_index
    assert "TXFD6" in svc._shm_symbol_index
    assert svc._shm_symbol_index["2330"] == 0
    assert svc._shm_symbol_index["TXFD6"] == 1
    svc._tmp = tmp


# ---------------------------------------------------------------------------
# _on_shioaji_event — basic coverage
# ---------------------------------------------------------------------------


def test_on_shioaji_event_no_loop():
    svc, *_ = _make_service()
    # No 'loop' attribute — should hit error log path
    if hasattr(svc, "loop"):
        del svc.loop

    # Should not raise
    svc._on_shioaji_event({"code": "2330", "close": 500.0, "volume": 100})
    assert svc.raw_queue.qsize() == 0


def test_on_shioaji_event_with_loop_enqueues():
    svc, *_ = _make_service()
    mock_loop = MagicMock()
    svc.loop = mock_loop

    svc._on_shioaji_event({"code": "2330", "close": 500.0, "volume": 100})
    mock_loop.call_soon_threadsafe.assert_called_once()


def test_on_shioaji_event_exception_is_handled():
    svc, *_ = _make_service()
    # Make try_fast_extract_callback_payload raise
    with patch("hft_platform.services._md_ingestion.try_fast_extract_callback_payload", side_effect=RuntimeError("oops")):
        # Should not raise
        svc._on_shioaji_event({"code": "2330"})
    assert svc.raw_queue.qsize() == 0


# ---------------------------------------------------------------------------
# _env_int and _obs_policy module-level helpers
# ---------------------------------------------------------------------------


def test_env_int_valid():
    from hft_platform.services.market_data import _env_int

    with patch.dict(os.environ, {"TEST_KEY": "42"}):
        assert _env_int("TEST_KEY", 10) == 42


def test_env_int_invalid_falls_back():
    from hft_platform.services.market_data import _env_int

    with patch.dict(os.environ, {"TEST_KEY": "not_a_number"}):
        assert _env_int("TEST_KEY", 10) == 10


def test_env_int_missing_uses_default():
    from hft_platform.services.market_data import _env_int

    os.environ.pop("TEST_KEY_MISSING", None)
    assert _env_int("TEST_KEY_MISSING", 5) == 5


def test_obs_policy_valid():
    from hft_platform.services.market_data import _obs_policy

    with patch.dict(os.environ, {"HFT_OBS_POLICY": "minimal"}):
        assert _obs_policy() == "minimal"


def test_obs_policy_invalid_defaults_to_balanced():
    from hft_platform.services.market_data import _obs_policy

    with patch.dict(os.environ, {"HFT_OBS_POLICY": "unknown_policy"}):
        assert _obs_policy() == "balanced"


# ---------------------------------------------------------------------------
# _init_feature_shadow_engine
# ---------------------------------------------------------------------------


def test_init_feature_shadow_engine_disabled_by_default():
    svc, *_ = _make_service()
    svc.feature_engine = MagicMock()
    with patch.dict(os.environ, {"HFT_FEATURE_SHADOW_PARITY": "0"}):
        svc._init_feature_shadow_engine()
    assert svc._feature_shadow_engine is None


def test_init_feature_shadow_engine_no_feature_engine():
    svc, *_ = _make_service()
    svc.feature_engine = None
    with patch.dict(os.environ, {"HFT_FEATURE_SHADOW_PARITY": "1"}):
        svc._init_feature_shadow_engine()
    assert svc._feature_shadow_engine is None


# ---------------------------------------------------------------------------
# _emit_trace
# ---------------------------------------------------------------------------


def test_emit_trace_no_sampler():
    svc, *_ = _make_service()
    svc._trace_sampler = None
    # Should not raise
    svc._emit_trace("stage", "trace_id", {"key": "val"})
    assert svc._trace_sampler is None


def test_emit_trace_with_sampler():
    svc, *_ = _make_service()
    sampler = MagicMock()
    svc._trace_sampler = sampler
    svc._emit_trace("md_event", "tick:1", {"symbol": "2330"})
    sampler.emit.assert_called_once_with(stage="md_event", trace_id="tick:1", payload={"symbol": "2330"})


def test_emit_trace_sampler_raises_silently():
    svc, *_ = _make_service()
    sampler = MagicMock()
    sampler.emit = MagicMock(side_effect=RuntimeError("trace error"))
    svc._trace_sampler = sampler
    # Should not raise
    svc._emit_trace("stage", "", {})
    sampler.emit.assert_called_once()


# ---------------------------------------------------------------------------
# _publish_nowait / _publish_many_nowait fallback paths
# ---------------------------------------------------------------------------


def test_publish_nowait_uses_publish_nowait():
    svc, bus, *_ = _make_service()
    event = SimpleNamespace(symbol="2330")
    svc._publish_nowait(event)
    bus.publish_nowait.assert_called_once_with(event)


def test_publish_many_nowait_uses_publish_many_nowait():
    svc, bus, *_ = _make_service()
    events = [SimpleNamespace(symbol="2330"), SimpleNamespace(symbol="TXFD6")]
    svc._publish_many_nowait(events)
    bus.publish_many_nowait.assert_called_once_with(events)


def test_publish_many_nowait_fallback_to_individual():
    svc, bus, *_ = _make_service()
    # Remove publish_many_nowait so it falls back
    del bus.publish_many_nowait
    events = [SimpleNamespace(symbol="A"), SimpleNamespace(symbol="B")]
    svc._publish_many_nowait(events)
    assert bus.publish_nowait.call_count == 2


# ---------------------------------------------------------------------------
# _record_shioaji_crash_signature
# ---------------------------------------------------------------------------


def test_record_crash_signature_no_registry():
    svc, *_ = _make_service()
    svc.metrics_registry = None
    # Should not raise
    svc._record_shioaji_crash_signature("connection reset", context="md_callback")
    assert svc.metrics_registry is None


def test_record_crash_signature_no_metric_attr():
    svc, *_ = _make_service()
    svc.metrics_registry = MagicMock(spec=[])  # empty spec, no attributes
    svc._record_shioaji_crash_signature("connection reset", context="md_callback")
    assert svc.metrics_registry.method_calls == []


def test_record_crash_signature_no_match():
    svc, *_ = _make_service()
    svc.metrics_registry = MagicMock()
    # Text that doesn't match any known crash signature
    with patch("hft_platform.services._md_observability.detect_crash_signature", return_value=None):
        svc._record_shioaji_crash_signature("just a regular log message", context="md_callback")
    svc.metrics_registry.shioaji_crash_signature_total.labels.assert_not_called()


# ---------------------------------------------------------------------------
# FeedState enum
# ---------------------------------------------------------------------------


def test_feed_state_values():
    from hft_platform.services.market_data import FeedState

    assert hasattr(FeedState, "INIT")
    assert hasattr(FeedState, "CONNECTING")
    assert hasattr(FeedState, "CONNECTED")
    assert hasattr(FeedState, "DISCONNECTED")
    assert hasattr(FeedState, "RECOVERING")


# ---------------------------------------------------------------------------
# _mark_pending_reconnect
# ---------------------------------------------------------------------------


def test_mark_pending_reconnect_sets_fields():
    svc, *_ = _make_service()
    svc._pending_reconnect_reason = None
    svc._pending_reconnect_gap = 0.0
    svc._pending_reconnect_since = None

    svc._mark_pending_reconnect(45.0, reason="heartbeat_gap")

    assert svc._pending_reconnect_reason == "heartbeat_gap"
    assert svc._pending_reconnect_gap == 45.0
    assert svc._pending_reconnect_since is not None


def test_mark_pending_reconnect_preserves_since():
    svc, *_ = _make_service()
    original_since = 12345.0
    svc._pending_reconnect_since = original_since
    svc._pending_reconnect_reason = "heartbeat_gap"

    svc._mark_pending_reconnect(30.0, reason="heartbeat_gap")

    # since should NOT be updated if already set
    assert svc._pending_reconnect_since == original_since
