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
from unittest.mock import MagicMock, patch

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


def test_recorder_degraded_gauge_set_on_enter():
    """Gauge goes to 1 when entering degraded mode."""
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=1)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True
    svc._record_degrade_threshold = 1
    # Fill the queue
    recorder_queue.put_nowait({"topic": "tick", "data": {}})

    gauge = MagicMock()
    counter = MagicMock()
    svc._recorder_degraded_gauge = gauge
    svc._recorder_degraded_counter = counter

    event = _make_tick_event()
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    assert svc._record_degraded is True
    gauge.set.assert_called_once_with(1)
    counter.inc.assert_called_once()


def test_recorder_degraded_gauge_cleared_on_recovery():
    """Gauge goes to 0 when recovering from degraded mode."""
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_degraded = True
    svc._record_degraded_since = time.monotonic() - 60.0
    svc._record_degraded_drops = 5
    svc._record_degrade_last_check = time.monotonic() - 100.0
    svc._record_degrade_check_s = 1.0

    gauge = MagicMock()
    svc._recorder_degraded_gauge = gauge

    event = _make_tick_event()
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    assert svc._record_degraded is False
    gauge.set.assert_called_once_with(0)


def test_recorder_degraded_counter_increments_each_entry():
    """Counter increments each time degraded mode is entered."""
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=1)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True
    svc._record_degrade_threshold = 1

    counter = MagicMock()
    gauge = MagicMock()
    svc._recorder_degraded_gauge = gauge
    svc._recorder_degraded_counter = counter

    event = _make_tick_event()

    # First entry into degraded mode
    recorder_queue.put_nowait({"topic": "tick", "data": {}})
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)
    assert svc._record_degraded is True
    assert counter.inc.call_count == 1

    # Recover: empty queue, force check
    while not recorder_queue.empty():
        recorder_queue.get_nowait()
    svc._record_degrade_last_check = time.monotonic() - 100.0
    svc._record_degrade_check_s = 1.0
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)
    assert svc._record_degraded is False

    # Second entry into degraded mode — drain queue first (recovery wrote into it)
    svc._recorder_dropped_count = 0
    while not recorder_queue.empty():
        recorder_queue.get_nowait()
    recorder_queue.put_nowait({"topic": "tick", "data": {}})
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)
    assert svc._record_degraded is True
    assert counter.inc.call_count == 2


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


def test_get_max_feed_gap_s_futures_symbol():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {"TMFD6": time.monotonic() - 3.0}
    gap = svc.get_max_feed_gap_s()
    assert 2.5 < gap < 5.0


def test_get_max_feed_gap_s_excludes_stock_symbols():
    svc, *_ = _make_service()
    # Only stock symbols (numeric) — should fall back to global max
    svc._symbol_last_tick = {"2330": time.monotonic() - 10.0}
    gap = svc.get_max_feed_gap_s()
    assert gap > 9.0  # falls back to global max (no futures)


def test_get_max_feed_gap_s_excludes_option_symbols():
    svc, *_ = _make_service()
    # Only option symbols — should fall back to global max
    svc._symbol_last_tick = {"TXO12345": time.monotonic() - 10.0}
    gap = svc.get_max_feed_gap_s()
    assert gap > 9.0  # falls back to option max


def test_get_max_feed_gap_s_mixed_excludes_options_and_stocks():
    svc, *_ = _make_service()
    # Futures symbol with 2s gap, stock with 200s gap, option with 60s gap
    svc._symbol_last_tick = {
        "TXFD6": time.monotonic() - 2.0,
        "2330": time.monotonic() - 200.0,
        "TXO12345": time.monotonic() - 60.0,
        "MXO9999": time.monotonic() - 90.0,
    }
    gap = svc.get_max_feed_gap_s()
    # Should be ~2s (futures only), not 200s (stock) or 90s (option)
    assert gap < 10.0


def test_get_max_feed_gap_s_night_session_stocks_excluded():
    svc, *_ = _make_service()
    # Night session scenario: futures active (small gap), stocks dormant (huge gap)
    svc._symbol_last_tick = {
        "TMFD6": time.monotonic() - 1.0,
        "TXFD6": time.monotonic() - 2.0,
        "2615": time.monotonic() - 600.0,
        "2890": time.monotonic() - 500.0,
        "2345": time.monotonic() - 400.0,
    }
    gap = svc.get_max_feed_gap_s()
    # Should be ~2s (TXFD6), not 600s (stocks)
    assert gap < 10.0


# ---------------------------------------------------------------------------
# get_active_feed_gap_s
# ---------------------------------------------------------------------------


def test_get_active_feed_gap_s_excludes_chronically_inactive_symbol():
    """Illiquid futures that never crossed the activity baseline must be
    excluded from the active gap signal so platform_reduce_only does not
    latch on stragglers.  Under the latched-set design, a symbol is
    excluded by virtue of NEVER being in ``_ever_active_symbols`` (not
    by an upper-bound gap filter)."""
    svc, *_ = _make_service()
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFD6": now - 0.5,  # active front-month
        "TXFD6": now - 1.0,  # active front-month
        "TMFI6": now - 2148.0,  # chronically stale far-month (real-world example)
    }
    # Only the actively-trading front-month contracts crossed the baseline.
    svc._ever_active_symbols = {"TMFD6", "TXFD6"}
    gap = svc.get_active_feed_gap_s()
    # Should reflect only TMFD6/TXFD6 (≤1.0s), not TMFI6 (2148s)
    assert gap < 5.0


def test_get_active_feed_gap_s_falls_back_when_no_active_futures():
    """If no symbol has ever crossed the activity baseline (legitimate
    cold start), fall back to ``get_max_feed_gap_s``."""
    svc, *_ = _make_service()
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFI6": now - 2148.0,
        "TXFI6": now - 2400.0,
    }
    # No symbol has crossed the 5-event baseline yet → empty latched set.
    svc._ever_active_symbols = set()
    gap = svc.get_active_feed_gap_s(active_threshold_s=300.0)
    # Empty latched set → fall back to get_max_feed_gap_s, which returns ≥2148s
    assert gap >= 2000.0


def test_get_active_feed_gap_s_deprecated_kwarg_is_ignored():
    """The ``active_threshold_s`` kwarg is deprecated under the latched-set
    design.  Passing any value (including the legacy 300s default) must
    have no effect on the returned gap."""
    svc, *_ = _make_service()
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFD6": now - 250.0,  # 250s silence on a latched symbol
    }
    svc._ever_active_symbols = {"TMFD6"}
    # Both calls must return the same latched silence (~250s) regardless
    # of the deprecated kwarg.
    gap_default = svc.get_active_feed_gap_s()
    gap_legacy = svc.get_active_feed_gap_s(active_threshold_s=100.0)
    assert 240.0 < gap_default < 270.0
    assert 240.0 < gap_legacy < 270.0


def test_get_active_feed_gap_s_empty_symbol_dict_returns_zero():
    svc, *_ = _make_service()
    svc._symbol_last_tick = {}
    gap = svc.get_active_feed_gap_s()
    assert gap == 0.0


def test_partial_feed_failure_still_triggers_unhealthy():
    """Regression for the masking bug introduced by commit b80b950c.

    Scenario: TMFE6 (front-month future, normally 1000+ events/min) was
    actively trading and crossed the activity baseline. It then stops
    getting events (broker partial outage). After 2000s of silence the
    pre-fix ``get_active_feed_gap_s`` would discard TMFE6 because its gap
    exceeds the upper-bound threshold, and report only TXFE6's healthy
    0.5s gap — masking the real partial failure.

    The latched ever-active set must keep TMFE6 in the calculation; the
    reported gap must therefore reflect the 2000s silence, which then
    crosses the platform's 600s ``feed_gap_threshold_s`` upstream.
    """
    svc, *_ = _make_service()
    now_mono = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now_mono - 2000.0,  # was actively trading, now silent
        "TXFE6": now_mono - 0.5,  # still healthy front-month
    }
    # Both symbols crossed the activity baseline before the partial outage.
    svc._ever_active_symbols = {"TMFE6", "TXFE6"}
    gap = svc.get_active_feed_gap_s()
    # Must report the 2000s silence on TMFE6, not the 0.5s healthy TXFE6.
    assert gap >= 1900.0


def test_chronically_idle_symbol_does_not_trigger_unhealthy():
    """Subscribed-but-never-active illiquid symbols (e.g. TMFI6 with one
    handshake print and no ongoing activity) must NOT inflate the active
    feed gap.  They never enter the latched ever-active set, so they are
    invisible to ``get_active_feed_gap_s``."""
    svc, *_ = _make_service()
    now_mono = time.monotonic()
    svc._symbol_last_tick = {
        "TMFI6": now_mono - 2148.0,  # chronically idle, 1 lifetime event
        "TMFE6": now_mono - 0.5,  # actively trading front-month
    }
    # Only TMFE6 ever crossed the baseline.  TMFI6 had a single handshake
    # print and never qualified as active.
    svc._ever_active_symbols = {"TMFE6"}
    gap = svc.get_active_feed_gap_s()
    # Must reflect TMFE6 only (≤1.0s); TMFI6's 2148s silence is structural.
    assert gap < 5.0


def test_symbol_enters_active_set_after_baseline_events():
    """A symbol must qualify as active only after crossing the baseline
    event count (default 5).  Below baseline, the symbol is invisible to
    ``get_active_feed_gap_s``; on the baseline-crossing event it enters
    the latched set; further events keep it in the set (idempotent)."""
    svc, *_ = _make_service()

    event = SimpleNamespace(symbol="TMFE6")
    baseline = svc._ACTIVE_BASELINE_EVENT_COUNT
    assert baseline >= 1

    # Below baseline: not in the ever-active set.
    for _ in range(baseline - 1):
        svc._update_symbol_tick_inline(event)
    assert "TMFE6" not in svc._ever_active_symbols
    assert svc._event_counts.get("TMFE6", 0) == baseline - 1

    # Baseline-crossing event: enters the set and the per-symbol counter
    # is reclaimed (no further hot-path counter ops once latched).
    svc._update_symbol_tick_inline(event)
    assert "TMFE6" in svc._ever_active_symbols
    assert "TMFE6" not in svc._event_counts

    # Subsequent events: still in set (idempotent) and the counter must
    # remain reclaimed — the hot path skips counter ops after latch to
    # honour the Allocator Law (no per-event int allocations forever).
    for _ in range(10):
        svc._update_symbol_tick_inline(event)
    assert "TMFE6" in svc._ever_active_symbols
    assert "TMFE6" not in svc._event_counts


def test_event_counter_reclaimed_after_latch():
    """Hot-path Allocator Law: once a symbol joins the latched set, the
    per-symbol counter must be removed so subsequent events do NOT
    allocate a fresh ``int`` object every tick.  Pre-fix behaviour
    incremented the counter forever and dominated GC pressure under the
    800-symbol universe (commit ``2568912a``).
    """
    svc, *_ = _make_service()

    event = SimpleNamespace(symbol="TMFE6")
    baseline = svc._ACTIVE_BASELINE_EVENT_COUNT

    # Cross the baseline.
    for _ in range(baseline):
        svc._update_symbol_tick_inline(event)
    assert "TMFE6" in svc._ever_active_symbols
    # Counter slot reclaimed on the latch event.
    assert "TMFE6" not in svc._event_counts

    # 1000 further events: the counter slot must remain absent.  This is
    # the regression we want to pin: under the old design ``_event_counts``
    # would now hold ``baseline + 1000`` and a new int object had been
    # allocated for every increment.
    for _ in range(1000):
        svc._update_symbol_tick_inline(event)
    assert "TMFE6" not in svc._event_counts


def test_active_set_latches_across_silence():
    """Once latched, a symbol stays in ``_ever_active_symbols`` even after
    long silence — silence is then a real feed-gap signal, not a reason
    to drop the symbol from monitoring."""
    svc, *_ = _make_service()

    event = SimpleNamespace(symbol="TMFE6")
    # Cross the baseline.
    for _ in range(svc._ACTIVE_BASELINE_EVENT_COUNT):
        svc._update_symbol_tick_inline(event)
    assert "TMFE6" in svc._ever_active_symbols

    # Simulate 600s of silence by rewinding the symbol's last-tick monotonic
    # timestamp.  The latched set must NOT eject the symbol.
    svc._symbol_last_tick["TMFE6"] = time.monotonic() - 600.0
    assert "TMFE6" in svc._ever_active_symbols

    gap = svc.get_active_feed_gap_s()
    assert gap >= 590.0


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
    svc._fe_process_lob_update = feature_engine.process_lob_update

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    # stats must be LOBStatsEvent or tuple to pass isinstance check in _maybe_update_features
    stats = ("lobstats", "2330", 0, 200000, 100, 0.5, 100000, 100100, 500, 500)
    svc.metrics_registry = None

    result = svc._maybe_update_features(event, stats)
    assert result is feature_update


def test_maybe_update_features_happy_path_fallback_process_lob_stats():
    svc, *_ = _make_service()

    feature_engine = MagicMock()
    feature_engine.process_lob_update = None  # not callable, triggers fallback
    feature_update = MagicMock()
    feature_update.feature_set_id = "v3"
    feature_update.quality_flags = 0
    feature_engine.process_lob_stats = MagicMock(return_value=feature_update)
    svc.feature_engine = feature_engine

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    # stats must be LOBStatsEvent or tuple to pass isinstance check
    stats = ("lobstats", "2330", 0, 200000, 100, 0.5, 100000, 100100, 500, 500)
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


def test_maybe_update_features_error_counted_without_sampling():
    """Every FeatureEngine error must increment the metric counter unconditionally.

    The sampling guard (_feature_metrics_sample_every) must NOT gate error
    counting — errors are rare and losing any count is unacceptable for
    alerting.  This test triggers N errors where N < sample_every and asserts
    that the error metric child was incremented exactly N times.
    """
    svc, *_ = _make_service()

    # Set up a feature engine that always raises via the fast-path callable.
    feature_engine = MagicMock()
    raising_fn = MagicMock(side_effect=RuntimeError("test error"))
    svc.feature_engine = feature_engine
    svc._fe_process_lob_update = raising_fn

    # Build a minimal metrics registry with a mock counter.
    error_child = MagicMock()
    mock_metric = MagicMock()
    mock_metric.labels.return_value = error_child

    metrics_registry = MagicMock()
    metrics_registry.feature_plane_updates_total = mock_metric
    svc.metrics_registry = metrics_registry

    # Use a sample_every value large enough that the old (broken) code would
    # never fire the counter within our test loop.
    sample_every = 100
    svc._feature_metrics_sample_every = sample_every
    svc._feature_metrics_counter = 0

    event = SimpleNamespace(symbol="2330", trade_direction=0, meta=None)
    # stats must be a tuple or LOBStatsEvent to pass isinstance guard in the method.
    stats = ("lobstats", "2330", 0, 200000, 100, 0.5, 100000, 100100, 500, 500)

    num_errors = 5  # well below sample_every=100
    for _ in range(num_errors):
        result = svc._maybe_update_features(event, stats)
        assert result is None

    # With the fix: error_child.inc() called once per error regardless of sampling.
    assert error_child.inc.call_count == num_errors, (
        f"Expected {num_errors} error increments, got {error_child.inc.call_count}. "
        "Error metrics must NOT be gated by the sampling guard."
    )


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

    import numpy as np

    from hft_platform.events import BidAskEvent

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

    # Monday 10:30 UTC — inside window
    fake_ts = dt.datetime(2025, 1, 6, 10, 30, 0, tzinfo=dt.timezone.utc).timestamp()
    with patch("hft_platform.services.market_data.timebase") as mock_tb:
        mock_tb.now_s.return_value = fake_ts
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

    # Monday 15:30 UTC — outside window
    fake_ts = dt.datetime(2025, 1, 6, 15, 30, 0, tzinfo=dt.timezone.utc).timestamp()
    with patch("hft_platform.services.market_data.timebase") as mock_tb:
        mock_tb.now_s.return_value = fake_ts
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
    import numpy as np

    from hft_platform.events import BidAskEvent

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
    with patch(
        "hft_platform.services._md_ingestion.try_fast_extract_callback_payload", side_effect=RuntimeError("oops")
    ):
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
    # _bus_publish_many_nowait is cached at init; set to None to trigger fallback
    svc._bus_publish_many_nowait = None
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


# ---------------------------------------------------------------------------
# _get_trace_sampler (lines 59-60, 62-65)
# ---------------------------------------------------------------------------


def test_get_trace_sampler_success():
    """Covers the import-success path of _get_trace_sampler (lines 59-62)."""
    from hft_platform.services.market_data import _get_trace_sampler

    # If diagnostics.trace is importable, returns a sampler object (or None).
    # Either way the function should not raise.
    result = _get_trace_sampler()
    # Result is either a sampler or None — both are acceptable.
    assert result is None or hasattr(result, "emit")


def test_get_trace_sampler_exception_returns_none():
    """Covers the except path of _get_trace_sampler (lines 63-65)."""
    from hft_platform.services.market_data import _get_trace_sampler

    # Patch the inner import to raise
    with patch.dict("sys.modules", {"hft_platform.diagnostics.trace": None}):
        result = _get_trace_sampler()
    assert result is None


# ---------------------------------------------------------------------------
# _summarize_md (lines 124-134)
# ---------------------------------------------------------------------------


def test_summarize_md_none():
    from hft_platform.services.market_data import _summarize_md

    assert _summarize_md(None) == {}


def test_summarize_md_dict_with_code_and_price():
    """Covers lines 124-128: dict path with present keys and nested."""
    from hft_platform.services.market_data import _summarize_md

    obj = {"code": "2330", "close": 500.0, "ts": 123456, "tick": {"price": 100}}
    result = _summarize_md(obj)
    assert "keys" in result
    assert "present" in result
    assert "nested" in result
    assert "code" in result["present"]
    assert "tick" in result["nested"]


def test_summarize_md_object_with_attrs():
    """Covers lines 129-134: object path with hasattr checks."""
    from hft_platform.services.market_data import _summarize_md

    obj = SimpleNamespace(code="2330", close=500.0, ts=123456, tick=SimpleNamespace(price=100))
    result = _summarize_md(obj)
    assert "attrs" in result
    assert "nested" in result
    assert "code" in result["attrs"]
    assert "tick" in result["nested"]


def test_summarize_md_object_without_nested():
    """Covers lines 129-134: object path without nested fields."""
    from hft_platform.services.market_data import _summarize_md

    obj = SimpleNamespace(code="2330", bid_price=100)
    result = _summarize_md(obj)
    assert "attrs" in result
    assert result["nested"] == {}


# ---------------------------------------------------------------------------
# _try_fast_extract_callback_payload (lines 159-161)
# ---------------------------------------------------------------------------


def test_fast_extract_payload_alternate_order_a0_md_a1_exchange():
    """Covers lines 157-161: argc==2 but second arg is exchange, first is MD."""
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    md_obj = {"code": "2330", "close": 500.0}
    exchange, msg = _try_fast_extract_callback_payload(md_obj, "TSE")
    assert msg is not None
    assert exchange == "TSE"


def test_fast_extract_payload_argc_1():
    """Covers line 162-165: argc==1."""
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    md_obj = {"code": "2330", "close": 500.0}
    exchange, msg = _try_fast_extract_callback_payload(md_obj)
    assert msg is not None


def test_fast_extract_payload_argc_3_last_is_md():
    """Covers lines 166-173: argc>=3."""
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    exchange, msg = _try_fast_extract_callback_payload("TSE", "topic_str", {"code": "2330", "close": 500.0})
    assert msg is not None
    assert exchange == "TSE"


# ---------------------------------------------------------------------------
# __init__ LOB feature_engine setattr exception (lines 237-239)
# ---------------------------------------------------------------------------


def test_init_lob_feature_engine_setattr_exception():
    """Covers lines 237-239: setattr(self.lob, 'feature_engine', ...) raises."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue(maxsize=100)
    client = MagicMock()

    # Make LOBEngine raise on setattr for feature_engine
    lob_mock = MagicMock()
    type(lob_mock).feature_engine = property(
        lambda self: None,
        lambda self, v: (_ for _ in ()).throw(TypeError("read-only")),
    )

    with patch.dict(os.environ, env):
        with patch("hft_platform.services.market_data.LOBEngine", return_value=lob_mock):
            svc = MarketDataService(bus, raw_queue, client)

    # Service should still be constructed despite the setattr exception
    assert svc.lob is lob_mock
    svc._tmp = tmp


# ---------------------------------------------------------------------------
# __init__ invalid timezone (lines 276-278)
# ---------------------------------------------------------------------------


def test_init_invalid_timezone_defaults_to_utc():
    """Covers lines 276-278: ZoneInfo raises, falls back to UTC."""
    import datetime as dt

    svc, *_ = _make_service(extra_env={"HFT_RECONNECT_TZ": "Invalid/NoSuchTZ"})
    assert svc._reconnect_tzinfo == dt.UTC


# ---------------------------------------------------------------------------
# __init__ recorder mapper import exception (lines 348-349)
# ---------------------------------------------------------------------------


def test_init_recorder_mapper_import_exception():
    """Covers lines 348-349: recorder mapper import fails gracefully."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
        "HFT_MD_RECORD_DIRECT": "1",
    }
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue(maxsize=100)
    client = MagicMock()
    recorder_queue = asyncio.Queue(maxsize=100)

    with patch.dict(os.environ, env):
        with patch.dict("sys.modules", {"hft_platform.recorder.mapper": None}):
            svc = MarketDataService(bus, raw_queue, client, recorder_queue=recorder_queue)

    # Despite import failure, service should initialize (mapper resolved lazily)
    assert svc._map_event_to_record is None
    svc._tmp = tmp


# ---------------------------------------------------------------------------
# _init_shm_publisher: symbols fallback + exception (lines 484, 491-493)
# ---------------------------------------------------------------------------


def test_init_shm_publisher_uses_symbols_fallback():
    """Covers line 484: falls back to client.symbols when subscribed_symbols is None."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue()
    client = MagicMock(spec=[])  # empty spec — no subscribed_symbols attribute

    # Add 'symbols' attribute as fallback
    client.symbols = ["2330", "TXFD6"]

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

    assert "2330" in svc._shm_symbol_index
    assert "TXFD6" in svc._shm_symbol_index
    svc._tmp = tmp


def test_init_shm_publisher_exception_sets_none():
    """Covers lines 491-493: ShmSnapshotWriter init raises."""
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue()
    client = MagicMock()

    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
    }

    with patch.dict(os.environ, env):
        with patch(
            "hft_platform.services.market_data.ShmSnapshotWriter",
            side_effect=OSError("shm create failed"),
        ):
            svc = MarketDataService(bus, raw_queue, client)

    assert svc._shm_publisher is None
    svc._tmp = tmp


# ---------------------------------------------------------------------------
# _publish_to_shm (lines 587-638)
# ---------------------------------------------------------------------------


def test_publish_to_shm_no_publisher():
    """Covers line 588-589: early return when publisher is None."""
    svc, *_ = _make_service()
    svc._shm_publisher = None
    # Should not raise
    svc._publish_to_shm("2330", SimpleNamespace(), None)
    assert svc._shm_publisher is None


def test_publish_to_shm_known_symbol_with_stats():
    """Covers lines 590, 602-624: known symbol path, LOB field population."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}
    svc._shm_symbol_hashes = {"2330": 12345}

    stats = SimpleNamespace(
        best_bid=5000000,
        best_ask=5001000,
        mid_price_x2=10001000,
        spread_scaled=1000,
        bid_depth=500,
        ask_depth=600,
        l1_bid_qty=10,
        l1_ask_qty=12,
        microprice_x2=10001500,
        local_ts=1000000000,
    )
    svc._publish_to_shm("2330", stats, None)
    publisher.publish.assert_called_once()
    call_args = publisher.publish.call_args[0]
    assert call_args[0] == 0  # idx
    assert call_args[2] == 12345  # sym_hash
    # LOB fields
    lob = call_args[3]
    assert lob[0] == 5000000  # best_bid
    assert lob[1] == 5001000  # best_ask
    # Features should be zeros (no feature_tuple)
    feats = call_args[4]
    assert all(f == 0 for f in feats)


def test_publish_to_shm_with_feature_tuple():
    """Covers lines 627-629: feature_tuple with >= 16 entries."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}
    svc._shm_symbol_hashes = {"2330": 12345}

    stats = SimpleNamespace(local_ts=1000000000)
    feature_tuple = tuple(range(100, 116))  # 16 feature values
    svc._publish_to_shm("2330", stats, feature_tuple)
    publisher.publish.assert_called_once()
    feats = publisher.publish.call_args[0][4]
    assert feats[0] == 100
    assert feats[15] == 115


def test_publish_to_shm_feature_tuple_too_short():
    """Covers lines 630-632: feature_tuple with < 16 entries zeros out."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}
    svc._shm_symbol_hashes = {"2330": 12345}

    stats = SimpleNamespace(local_ts=0)
    feature_tuple = (1, 2, 3)  # Too short
    svc._publish_to_shm("2330", stats, feature_tuple)
    publisher.publish.assert_called_once()
    feats = publisher.publish.call_args[0][4]
    assert all(f == 0 for f in feats)


def test_publish_to_shm_unknown_symbol_lazy_assign():
    """Covers lines 591-600: lazy slot assignment for unknown symbol."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {}
    svc._shm_symbol_hashes = {}

    stats = SimpleNamespace(local_ts=0)
    svc._publish_to_shm("TXFD6", stats, None)

    assert "TXFD6" in svc._shm_symbol_index
    assert svc._shm_symbol_index["TXFD6"] == 0
    publisher.publish.assert_called_once()


def test_publish_to_shm_max_symbols_exceeded():
    """Covers line 596-597: lazy assign returns when max_symbols reached."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 1
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}  # Already at capacity
    svc._shm_symbol_hashes = {"2330": 12345}

    stats = SimpleNamespace(local_ts=0)
    svc._publish_to_shm("TXFD6", stats, None)  # Should return early
    publisher.publish.assert_not_called()
    assert "TXFD6" not in svc._shm_symbol_index


def test_publish_to_shm_publisher_raises_silently():
    """Covers lines 634-638: publish raises, silently caught."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    publisher.publish.side_effect = RuntimeError("shm write failed")
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}
    svc._shm_symbol_hashes = {"2330": 12345}

    stats = SimpleNamespace(local_ts=0)
    # Should not raise
    svc._publish_to_shm("2330", stats, None)
    publisher.publish.assert_called_once()


def test_publish_to_shm_caches_buffers():
    """Covers lines 606-613: LOB + feature buffers are cached and reused."""
    svc, *_ = _make_service()
    publisher = MagicMock()
    publisher.max_symbols = 64
    svc._shm_publisher = publisher
    svc._shm_symbol_index = {"2330": 0}
    svc._shm_symbol_hashes = {"2330": 12345}

    stats1 = SimpleNamespace(best_bid=100, local_ts=0)
    svc._publish_to_shm("2330", stats1, None)
    assert "2330" in svc._shm_lob_cache
    assert "2330" in svc._shm_feat_cache

    # Second call reuses the cached buffer
    stats2 = SimpleNamespace(best_bid=200, local_ts=0)
    svc._publish_to_shm("2330", stats2, None)
    # Should be the same list object
    assert svc._shm_lob_cache["2330"][0] == 200


# ---------------------------------------------------------------------------
# _init_feature_shadow_engine (lines 650-652, 671, 673-675)
# ---------------------------------------------------------------------------


def test_init_feature_shadow_kernel_backend_exception():
    """Covers lines 650-652: kernel_backend() raises, defaults to 'python'."""
    svc, *_ = _make_service()
    fe = MagicMock()
    fe.kernel_backend = MagicMock(side_effect=RuntimeError("no backend"))
    svc.feature_engine = fe

    with patch.dict(os.environ, {"HFT_FEATURE_SHADOW_PARITY": "1"}):
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            shadow = MagicMock()
            shadow.kernel_backend.return_value = "rust"
            MockFE.return_value = shadow
            svc._init_feature_shadow_engine()

    # Shadow engine should be assigned since backends differ
    assert svc._feature_shadow_engine is shadow


def test_init_feature_shadow_auto_mode_same_backend_returns():
    """Covers line 671: auto mode with matching backends returns early."""
    svc, *_ = _make_service()
    fe = MagicMock()
    fe.kernel_backend.return_value = "python"
    fe.feature_set_id.return_value = "lob_shared_v3"
    svc.feature_engine = fe

    with patch.dict(
        os.environ,
        {"HFT_FEATURE_SHADOW_PARITY": "1", "HFT_FEATURE_SHADOW_BACKEND": ""},
    ):
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            shadow = MagicMock()
            shadow.kernel_backend.return_value = "python"  # Same as primary
            MockFE.return_value = shadow
            svc._init_feature_shadow_engine()

    assert svc._feature_shadow_engine is None


def test_init_feature_shadow_create_raises():
    """Covers lines 673-675: FeatureEngine() raises."""
    svc, *_ = _make_service()
    fe = MagicMock()
    fe.kernel_backend.return_value = "python"
    svc.feature_engine = fe

    with patch.dict(os.environ, {"HFT_FEATURE_SHADOW_PARITY": "1"}):
        with patch(
            "hft_platform.services.market_data.FeatureEngine",
            side_effect=RuntimeError("boom"),
        ):
            svc._init_feature_shadow_engine()

    assert svc._feature_shadow_engine is None


def test_init_feature_shadow_explicit_backend_same_ok():
    """Covers line 665-672: explicit backend requested, even if same as primary."""
    svc, *_ = _make_service()
    fe = MagicMock()
    fe.kernel_backend.return_value = "python"
    fe.feature_set_id.return_value = "lob_shared_v3"
    svc.feature_engine = fe

    with patch.dict(
        os.environ,
        {"HFT_FEATURE_SHADOW_PARITY": "1", "HFT_FEATURE_SHADOW_BACKEND": "python"},
    ):
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            shadow = MagicMock()
            shadow.kernel_backend.return_value = "python"
            MockFE.return_value = shadow
            svc._init_feature_shadow_engine()

    # Explicit request: shadow should be assigned even if same backend
    assert svc._feature_shadow_engine is shadow


# ---------------------------------------------------------------------------
# _process_raw: norm_recovery path (lines 770-771)
# ---------------------------------------------------------------------------


def test_process_raw_norm_recovery_triggers_after_failures():
    """Covers lines 767-771: storm_guard.report_norm_recovery after failures."""
    svc, bus, *_ = _make_service()

    from hft_platform.events import TickEvent

    mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    sg = MagicMock()
    svc._storm_guard = sg
    svc._NORM_FAILURE_ESCALATE = 5
    svc._norm_consecutive_failures = 5  # At threshold

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    svc._process_raw(raw)

    sg.report_norm_recovery.assert_called_once()


def test_process_raw_norm_recovery_exception_swallowed():
    """Covers line 770-771: report_norm_recovery raises, swallowed silently."""
    svc, bus, *_ = _make_service()

    from hft_platform.events import TickEvent

    mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
    svc.lob = MagicMock()
    svc.lob.process_event = MagicMock(return_value=None)
    svc.feature_engine = None

    sg = MagicMock()
    sg.report_norm_recovery.side_effect = RuntimeError("guard error")
    svc._storm_guard = sg
    svc._NORM_FAILURE_ESCALATE = 3
    svc._norm_consecutive_failures = 3

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    # Should not raise
    svc._process_raw(raw)
    sg.report_norm_recovery.assert_called_once()
    # Counter should be reset to 0 after successful normalization
    assert svc._norm_consecutive_failures == 0


# ---------------------------------------------------------------------------
# _md_wal_fallback_write
# ---------------------------------------------------------------------------


def test_md_wal_fallback_write_no_writer():
    """WAL fallback does nothing when _wal_writer is None."""
    svc, *_ = _make_service()
    svc._wal_writer = None
    svc._md_wal_fallback_write("tick", {"symbol": "2330"})
    assert svc._wal_fallback_count == 0


def test_md_wal_fallback_write_skips_by_sample_rate():
    """WAL fallback writes only 1-in-N events."""
    svc, *_ = _make_service()
    writer = MagicMock()
    # Return a fresh coroutine each call to avoid event loop issues
    writer.write = MagicMock(side_effect=lambda *a, **kw: asyncio.coroutine(lambda: None)())
    svc._wal_writer = writer
    svc._wal_fallback_sample_rate = 10
    svc._wal_fallback_count = 0

    # First 9 calls should be skipped (count not divisible by 10)
    for _ in range(9):
        svc._md_wal_fallback_write("tick", {"symbol": "2330"})
    assert svc._wal_fallback_count == 9


def test_on_wal_fallback_done_no_exception():
    """CF-5: done callback does nothing when no exception."""
    from hft_platform.services.market_data import MarketDataService

    # Use a mock future to avoid event-loop dependency
    fut = MagicMock()
    fut.exception.return_value = None
    # Should not raise
    MarketDataService._on_wal_fallback_done(fut)
    fut.exception.assert_called_once()


def test_on_wal_fallback_done_with_exception():
    """CF-5: done callback logs warning on exception."""
    from hft_platform.services.market_data import MarketDataService

    fut = MagicMock()
    fut.exception.return_value = IOError("disk full")
    # Should not raise — just logs
    MarketDataService._on_wal_fallback_done(fut)
    fut.exception.assert_called_once()


# ---------------------------------------------------------------------------
# _record_direct_event: non-drop path with pending puts (line 1523-1530)
# ---------------------------------------------------------------------------


def test_record_direct_non_drop_pending_puts_exceeded():
    """When drop_on_full=False and pending_puts exceeds max, drops events."""
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=100)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = False
    svc._record_pending_puts = 100
    svc._record_pending_puts_max = 100

    event = _make_tick_event()
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    assert svc._recorder_dropped_count == 1


# ---------------------------------------------------------------------------
# _looks_like_md edge cases
# ---------------------------------------------------------------------------


def test_looks_like_md_none():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md(None) is False


def test_looks_like_md_dict_with_ts():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"ts": 123}) is True


def test_looks_like_md_dict_with_buy_price():
    """buy_price and sell_price are also market data indicators."""
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"buy_price": 100}) is True
    assert _looks_like_md({"sell_price": 100}) is True


def test_looks_like_md_object_with_code_and_time():
    from hft_platform.services.market_data import _looks_like_md

    obj = SimpleNamespace(code="2330", ts=123)
    assert _looks_like_md(obj) is True


def test_looks_like_md_object_with_symbol_only():
    from hft_platform.services.market_data import _looks_like_md

    # symbol alone without price or time — the condition is (has_price) or (has_code and (has_price or has_time))
    obj = SimpleNamespace(symbol="2330")
    assert _looks_like_md(obj) is False


# ---------------------------------------------------------------------------
# _unwrap_md edge cases
# ---------------------------------------------------------------------------


def test_unwrap_md_none():
    from hft_platform.services.market_data import _unwrap_md

    assert _unwrap_md(None) is None


def test_unwrap_md_dict_with_tick():
    from hft_platform.services.market_data import _unwrap_md

    tick_data = {"code": "2330", "close": 500.0}
    assert _unwrap_md({"tick": tick_data}) == tick_data


def test_unwrap_md_dict_with_bidask():
    from hft_platform.services.market_data import _unwrap_md

    bidask_data = {"bid_price": [100], "ask_price": [200]}
    assert _unwrap_md({"bidask": bidask_data}) == bidask_data


def test_unwrap_md_object_with_tick():
    from hft_platform.services.market_data import _unwrap_md

    tick_data = SimpleNamespace(code="2330", close=500.0)
    obj = SimpleNamespace(tick=tick_data, bidask=None)
    assert _unwrap_md(obj) is tick_data


def test_unwrap_md_object_with_bidask():
    from hft_platform.services.market_data import _unwrap_md

    bidask_data = SimpleNamespace(bid_price=[100], ask_price=[200])
    obj = SimpleNamespace(tick=None, bidask=bidask_data)
    assert _unwrap_md(obj) is bidask_data


def test_unwrap_md_plain_dict_passthrough():
    from hft_platform.services.market_data import _unwrap_md

    d = {"code": "2330"}
    assert _unwrap_md(d) is d


# ---------------------------------------------------------------------------
# _is_futures_symbol
# ---------------------------------------------------------------------------


def test_is_futures_symbol_alpha_prefix():
    from hft_platform.services.market_data import MarketDataService

    assert MarketDataService._is_futures_symbol("TXFD6") is True
    assert MarketDataService._is_futures_symbol("TMFD6") is True


def test_is_futures_symbol_numeric_code():
    from hft_platform.services.market_data import MarketDataService

    assert MarketDataService._is_futures_symbol("2330") is False
    assert MarketDataService._is_futures_symbol("00878") is False


def test_is_futures_symbol_empty():
    from hft_platform.services.market_data import MarketDataService

    assert MarketDataService._is_futures_symbol("") is False


# ---------------------------------------------------------------------------
# _record_shioaji_crash_signature with match
# ---------------------------------------------------------------------------


def test_record_crash_signature_with_match():
    svc, *_ = _make_service()
    metric = MagicMock()
    svc.metrics_registry = MagicMock()
    svc.metrics_registry.shioaji_crash_signature_total = metric

    with patch(
        "hft_platform.services.market_data.detect_crash_signature",
        return_value="conn_reset",
    ):
        svc._record_shioaji_crash_signature("connection reset by peer", context="md_callback")

    metric.labels.assert_called_once_with(signature="conn_reset", context="md_callback")
    metric.labels.return_value.inc.assert_called_once()


def test_record_crash_signature_metric_inc_raises():  # noqa: no-assert
    """Covers the except path in _record_shioaji_crash_signature."""
    svc, *_ = _make_service()
    metric = MagicMock()
    metric.labels.side_effect = RuntimeError("metric error")
    svc.metrics_registry = MagicMock()
    svc.metrics_registry.shioaji_crash_signature_total = metric

    with patch(
        "hft_platform.services.market_data.detect_crash_signature",
        return_value="conn_reset",
    ):
        # Should not raise
        svc._record_shioaji_crash_signature("connection reset", context="md_callback")


# ---------------------------------------------------------------------------
# _process_raw: norm failure escalation to storm_guard
# ---------------------------------------------------------------------------


def test_process_raw_norm_failure_escalates_to_storm_guard():
    """Covers lines 786-790: consecutive norm failures trigger storm_guard."""
    svc, bus, *_ = _make_service()
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick = MagicMock(side_effect=ValueError("bad"))

    sg = MagicMock()
    svc._storm_guard = sg
    svc._NORM_FAILURE_ESCALATE = 2
    svc._norm_consecutive_failures = 1  # One away from threshold

    raw = {"code": "2330", "close": 500.0, "volume": 100}
    svc._process_raw(raw)

    assert svc._norm_consecutive_failures == 2
    sg.report_norm_failure.assert_called_once_with(2)


# ---------------------------------------------------------------------------
# _record_direct_event: WAL fallback on QueueFull
# ---------------------------------------------------------------------------


def test_record_direct_queue_full_triggers_wal_fallback():
    """When recorder queue is full, _md_wal_fallback_write is called."""
    svc, *_ = _make_service()
    recorder_queue = asyncio.Queue(maxsize=1)
    svc.recorder_queue = recorder_queue
    svc._record_direct = True
    svc._record_drop_on_full = True
    svc._record_degrade_threshold = 1000  # High so degraded mode is not entered
    recorder_queue.put_nowait({"topic": "tick", "data": {}})

    svc._wal_writer = MagicMock()
    svc._md_wal_fallback_write = MagicMock()

    event = _make_tick_event()
    with patch("hft_platform.recorder.mapper.map_event_to_record") as mock_map:
        mock_map.return_value = ("tick", {"symbol": "2330"})
        svc._record_direct_event(event)

    svc._md_wal_fallback_write.assert_called_once_with("tick", {"symbol": "2330"})


# ---------------------------------------------------------------------------
# _enqueue_raw: sliding window drops and storm guard escalation
# ---------------------------------------------------------------------------


def test_enqueue_raw_consecutive_drops_trigger_storm():
    """Covers storm guard escalation on consecutive drops."""
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))  # Fill

    sg = MagicMock()
    svc._storm_guard = sg
    svc._raw_drop_degrade_threshold = 2
    svc._raw_drop_halt_threshold = 1000
    svc._raw_consecutive_drops = 1  # Will become 2 after next drop

    svc._enqueue_raw("TSE", {"code": "2330"})
    sg.trigger_storm.assert_called_once()


def test_enqueue_raw_halt_threshold_triggers_halt():
    """Covers halt escalation on sustained drops."""
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))

    sg = MagicMock()
    svc._storm_guard = sg
    svc._raw_drop_halt_threshold = 2
    svc._raw_consecutive_drops = 1

    svc._enqueue_raw("TSE", {"code": "2330"})
    sg.trigger_halt.assert_called_once()


def test_enqueue_raw_feature_engine_mark_gap():
    """Covers FeatureEngine.mark_gap_all on drop threshold."""
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))

    fe = MagicMock()
    svc.feature_engine = fe
    svc._storm_guard = None
    svc._raw_drop_degrade_threshold = 2
    svc._raw_consecutive_drops = 1  # Will become 2 = threshold

    svc._enqueue_raw("TSE", {"code": "2330"})
    fe.mark_gap_all.assert_called_once()


def test_enqueue_raw_sliding_window_storm():
    """Covers sliding window drop rate triggering storm guard."""
    svc, *_ = _make_service()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait(("x", "y"))

    sg = MagicMock()
    svc._storm_guard = sg
    svc._raw_drop_degrade_threshold = 1000  # High enough to skip
    svc._raw_drop_halt_threshold = 1000
    svc._raw_drop_window_threshold = 1  # Trigger on any window count
    svc._raw_drop_window_count = 100.0  # Already high
    svc._raw_drop_window_last_ns = 1
    svc._raw_consecutive_drops = 0

    svc._enqueue_raw("TSE", {"code": "2330"})
    # Should have triggered storm via window check
    sg.trigger_storm.assert_called_once()


# ---------------------------------------------------------------------------
# RC-2 v2: get_active_feed_gap_s subscription-membership de-latch
# ---------------------------------------------------------------------------
#
# v1 (recency-window) was reverted because it silently dropped any latched
# symbol whose silence exceeded the recency window — exactly the partial
# outage masking pattern that b80b950c originally tried to address.
#
# v2 separates "expired contract" from "partial outage" via
# ``client.subscribed_codes`` (set[str]).  ``contracts_runtime.refresh()``
# discards expired codes from this set; latched symbols no longer in the
# set are de-latched.  Symbols still in the set always contribute their
# silence age to ``max_gap`` regardless of how long they have been silent.
# ---------------------------------------------------------------------------


def test_feed_gap_excludes_silent_latched_symbol():
    """RC-2 v2: a latched symbol that has been removed from
    ``client.subscribed_codes`` (typical case: contract expired, the
    rollover routine in ``contracts_runtime`` discarded it) must NOT
    contribute to ``max_gap`` and must be de-latched on the next call.

    Real-world case that motivated the fix: TMFI6 accumulated 5 ticks
    early in the session, latched into ``_ever_active_symbols``,
    expired mid-session and was discarded from ``subscribed_codes``,
    then dominated the max-gap signal for 3+ hours and locked the
    platform in reduce_only.
    """
    svc, _bus, _raw, client = _make_service()
    # Simulate post-rollover state: TMFI6 has expired and is no longer
    # subscribed, while TMFD6/TXFD6 remain the active front-month codes.
    client.subscribed_codes = {"TMFD6", "TXFD6"}
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFD6": now - 0.5,  # active front-month, still subscribed
        "TXFD6": now - 1.0,  # active front-month, still subscribed
        "TMFI6": now - 1200.0,  # latched but already unsubscribed
    }
    svc._ever_active_symbols = {"TMFD6", "TXFD6", "TMFI6"}
    gap = svc.get_active_feed_gap_s()
    # Must reflect only TMFD6/TXFD6 (≤1.0s), NOT 1200s from unsubscribed TMFI6.
    assert gap < 5.0, f"unsubscribed TMFI6 leaked into max gap: {gap=}"
    # TMFI6 must be auto-pruned from the latched set on this call.
    assert "TMFI6" not in svc._ever_active_symbols
    assert {"TMFD6", "TXFD6"}.issubset(svc._ever_active_symbols)


def test_feed_gap_delatches_when_symbol_drops_from_subscriptions():
    """When a latched symbol is removed from ``client.subscribed_codes``
    (e.g. by contract-rollover machinery), the next call to
    ``get_active_feed_gap_s`` must de-latch it so the latched set does
    not grow unboundedly across long sessions or multi-day uptime."""
    svc, _bus, _raw, client = _make_service()
    client.subscribed_codes = {"TMFD6"}  # TMFI6 has rolled out
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFD6": now - 0.5,
        "TMFI6": now - 3600.0,
    }
    svc._ever_active_symbols = {"TMFD6", "TMFI6"}
    _ = svc.get_active_feed_gap_s()
    # TMFI6 should be auto-pruned from the latched set.
    assert "TMFI6" not in svc._ever_active_symbols
    assert "TMFD6" in svc._ever_active_symbols


def test_feed_gap_real_outage_still_surfaces():
    """Defense-in-depth: a real partial outage on a previously-active
    front-month that is still in ``subscribed_codes`` must STILL
    surface — no time-based skip masks it.  This is the regression
    that broke RC-2 v1 (recency window): silence ≥ recency was
    silently dropped from ``max_gap``."""
    svc, _bus, _raw, client = _make_service()
    client.subscribed_codes = {"TXFD6", "TMFD6"}  # both still subscribed
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TXFD6": now - 0.5,  # healthy
        "TMFD6": now - 250.0,  # silent 250s — partial outage
    }
    svc._ever_active_symbols = {"TXFD6", "TMFD6"}
    gap = svc.get_active_feed_gap_s()
    # TMFD6 silence must be reported in full.
    assert gap >= 200.0, f"real outage masked: {gap=}"
    # And both symbols stay latched (silence on a subscribed symbol
    # does NOT trigger de-latch).
    assert {"TXFD6", "TMFD6"} <= svc._ever_active_symbols


def test_feed_gap_long_outage_on_subscribed_symbol_still_surfaces():
    """Direct mirror of ``test_partial_feed_failure_still_triggers_unhealthy``
    but with an explicit subscription-set on the client.  A latched
    symbol silent for 2000s while still in ``subscribed_codes`` MUST
    surface its full silence — no recency cap, no time-based mask."""
    svc, _bus, _raw, client = _make_service()
    client.subscribed_codes = {"TMFE6", "TXFE6"}  # both still subscribed
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now - 2000.0,  # 2000s silence on a subscribed symbol
        "TXFE6": now - 0.5,
    }
    svc._ever_active_symbols = {"TMFE6", "TXFE6"}
    gap = svc.get_active_feed_gap_s()
    assert gap >= 1900.0, f"long outage on subscribed symbol masked: {gap=}"


# ---------------------------------------------------------------------------
# RC-2 v2 alias-resolution patch (Codex P1 #3)
# ---------------------------------------------------------------------------
#
# ``client.subscribed_codes`` records *config-time* codes.  For rollover
# aliases (TMFR1, TXFR1, …) these differ from the resolved month code
# the broker callback delivers (TMFE6, TXFE6, …).  Quote callbacks
# populate ``_ever_active_symbols`` with the resolved code, so a naive
# ``symbol in subscribed_codes`` check would treat every alias-resolved
# future as "unsubscribed" and silently mask its partial outage.
#
# ``ContractsRuntime.resolve_symbol_aliases()`` populates
# ``client.alias_to_actual`` (config_code → resolved_code).  The
# de-latch logic must union that mapping into the membership set.
# ---------------------------------------------------------------------------


def test_feed_gap_alias_resolved_symbol_not_delatched():
    """``subscribed_codes`` holds the config alias (e.g. TMFR1) while the
    broker callback latches the resolved month code (e.g. TMFE6) into
    ``_ever_active_symbols``.  ``alias_to_actual`` is the source of
    truth that bridges the two; the membership check MUST honour it,
    otherwise a 2000s outage on TMFE6 would be silently de-latched
    just because a healthy non-alias front-month exists alongside."""
    svc, _bus, _raw, client = _make_service()
    # Config used rollover aliases; broker resolved them mid-session.
    client.subscribed_codes = {"TMFR1", "TXFE6"}
    client.alias_to_actual = {"TMFR1": "TMFE6"}
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now - 2000.0,  # alias-resolved symbol, real outage
        "TXFE6": now - 0.5,  # healthy non-alias front-month
    }
    svc._ever_active_symbols = {"TMFE6", "TXFE6"}
    gap = svc.get_active_feed_gap_s()
    # TMFE6 must remain a contributor: its 2000s silence MUST surface
    # rather than being de-latched as "unsubscribed" against TMFR1.
    assert gap >= 1900.0, f"alias-resolved outage masked: {gap=}"
    assert "TMFE6" in svc._ever_active_symbols
    assert "TXFE6" in svc._ever_active_symbols


def test_feed_gap_alias_resolution_failure_falls_back_safe():
    """If reading ``client.alias_to_actual`` raises (broken broker
    adapter, partially-initialised state), the function MUST fall
    back to the alias-blind path (``subscription_set = None``) so
    every latched symbol contributes to ``max_gap``.  Defense-in-
    depth: a broken resolver must never silently de-latch every
    resolved future and mask a partial outage."""
    svc, _bus, _raw, client = _make_service()

    class _ExplodingMap:
        """Drop-in for ``alias_to_actual`` that fails ``isinstance(dict)``
        but raises when ``dict(...)`` snapshots it."""

        def keys(self):  # pragma: no cover - never reached
            raise RuntimeError("alias map exploded")

    # Force ``isinstance(raw_alias_map, dict)`` to be True yet make
    # ``dict(raw_alias_map)`` snapshot raise.  Subclassing dict and
    # overriding ``__iter__`` is the cleanest way to guarantee both.
    class _BoomDict(dict):
        def __iter__(self):
            raise RuntimeError("alias snapshot exploded")

    client.subscribed_codes = {"TMFR1"}
    client.alias_to_actual = _BoomDict({"TMFR1": "TMFE6"})
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now - 2000.0,  # alias-resolved symbol, real outage
    }
    svc._ever_active_symbols = {"TMFE6"}
    gap = svc.get_active_feed_gap_s()
    # Resolver failure → subscription_set=None → every latched symbol
    # contributes → 2000s silence on TMFE6 surfaces in full.
    assert gap >= 1900.0, f"resolver failure masked outage: {gap=}"
    assert "TMFE6" in svc._ever_active_symbols


def test_feed_gap_falls_back_when_subscriptions_unavailable():
    """When ``client.subscribed_codes`` is missing or not a real set
    (test fixtures using ``MagicMock``, brokers without a subscription
    set), the membership de-latch must NOT engage and every latched
    symbol must contribute to ``max_gap``.  This preserves the
    partial-outage signal in any environment that cannot answer the
    membership question authoritatively (Defense-in-Depth: missing
    signal must not silently mask a partial failure)."""
    svc, *_ = _make_service()
    # NB: client is a plain MagicMock, so ``client.subscribed_codes``
    # auto-resolves to a MagicMock attribute (not a real set).
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now - 2000.0,  # silent 2000s
        "TXFE6": now - 0.5,
    }
    svc._ever_active_symbols = {"TMFE6", "TXFE6"}
    gap = svc.get_active_feed_gap_s()
    assert gap >= 1900.0, (
        f"fallback path must surface long silence on every latched symbol when membership cannot be determined: {gap=}"
    )


# ---------------------------------------------------------------------------
# RC-3 patches (Codex P1 #5 + P2 #6)
# ---------------------------------------------------------------------------
#
# P1 #5: production runs with ``HFT_QUOTE_CONNECTIONS > 1`` use a
#        ``QuoteConnectionPool`` as the platform's ``client`` handle.  The
#        pool MUST aggregate ``subscribed_codes`` and ``alias_to_actual``
#        across its underlying clients, otherwise ``subscription_set``
#        stays ``None`` in production and the de-latch path never engages.
#
# P2 #6: when every latched symbol is de-latched in the same call (typical
#        rollover window: new front-month subscribed but no tick has
#        crossed the 5-event baseline; old contract just pruned), falling
#        back to ``get_max_feed_gap_s`` re-scans ``_symbol_last_tick`` —
#        which still contains the just-de-latched expired contract's stale
#        gap — and falsely reports it as the active feed gap.  Must
#        instead return ``0.0`` (honest "no active latched contract") and
#        only fall back when membership was never resolvable.
# ---------------------------------------------------------------------------


def test_feed_gap_pool_aggregates_subscribed_codes():
    """``QuoteConnectionPool.subscribed_codes`` MUST union the per-client
    sets so ``MarketDataService.get_active_feed_gap_s`` can engage the
    de-latch path under ``HFT_QUOTE_CONNECTIONS > 1``."""
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
        QuoteConnectionPool,
    )

    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    client_a = SimpleNamespace(subscribed_codes={"TMFD6", "TXFD6"})
    client_b = SimpleNamespace(subscribed_codes={"TXFD6", "MXFD6"})
    pool._clients = [client_a, client_b]

    aggregated = pool.subscribed_codes
    assert aggregated == {"TMFD6", "TXFD6", "MXFD6"}
    # Snapshot semantics: mutating the pool's view must NOT touch the
    # underlying per-client sets.
    aggregated.add("BOGUS")
    assert "BOGUS" not in client_a.subscribed_codes
    assert "BOGUS" not in client_b.subscribed_codes


def test_feed_gap_pool_aggregates_alias_map():
    """``QuoteConnectionPool.alias_to_actual`` MUST union the per-client
    rollover maps so the alias-bridge survives the multi-conn config."""
    from hft_platform.feed_adapter.shioaji.quote_connection_pool import (
        QuoteConnectionPool,
    )

    pool = QuoteConnectionPool.__new__(QuoteConnectionPool)
    client_a = SimpleNamespace(alias_to_actual={"TMFR1": "TMFE6"})
    client_b = SimpleNamespace(alias_to_actual={"TXFR1": "TXFE6"})
    pool._clients = [client_a, client_b]

    aggregated = pool.alias_to_actual
    assert aggregated == {"TMFR1": "TMFE6", "TXFR1": "TXFE6"}
    # Snapshot semantics: mutating the pool's view must NOT touch the
    # underlying per-client dicts.
    aggregated["INJECTED"] = "BOGUS"
    assert "INJECTED" not in client_a.alias_to_actual
    assert "INJECTED" not in client_b.alias_to_actual


def test_feed_gap_returns_zero_when_all_delatched():
    """P2 #6 regression: when every latched symbol is de-latched in the
    same call (rollover window: old contract just unsubscribed, new
    front-month not yet baseline-active), the function MUST return
    ``0.0`` instead of falling back to ``get_max_feed_gap_s``.  The
    fallback would re-scan ``_symbol_last_tick`` and resurrect the
    just-de-latched expired contract's stale gap, recreating the
    rollover-window false-reduce-only the de-latch was meant to fix."""
    svc, _bus, _raw, client = _make_service()
    # New front-month subscribed; old TMFI6 has just rolled out.
    client.subscribed_codes = {"TMFD7"}
    client.alias_to_actual = {}
    now = time.monotonic()
    # ``_symbol_last_tick`` still carries TMFI6's stale 1200s gap (it
    # has not been pruned from the per-symbol last-tick map).
    svc._symbol_last_tick = {
        "TMFI6": now - 1200.0,
    }
    # TMFI6 is the only latched symbol, and it is no longer in the
    # subscription set — the loop will de-latch it.
    svc._ever_active_symbols = {"TMFI6"}

    gap = svc.get_active_feed_gap_s()

    # Pre-fix: the fallback re-scanned ``_symbol_last_tick`` and
    # returned 1200.0, tripping the 600s threshold and locking the
    # platform into reduce-only across every rollover window.
    # Post-fix: honest "no active latched contract right now".
    assert gap == 0.0, f"stale gap leaked through fallback: {gap=}"
    # And the de-latch was applied for real.
    assert "TMFI6" not in svc._ever_active_symbols


def test_feed_gap_falls_back_only_when_subscription_unknown():
    """P2 #6 boundary: when ``subscription_set`` is ``None`` (membership
    truly unknowable) AND no latched symbols qualified, the function
    MUST still fall back to ``get_max_feed_gap_s`` to preserve the
    cold-start dead-feed signal.  This is the ONLY case the fallback
    is allowed to fire."""
    svc, _bus, _raw, client = _make_service()
    # Force ``subscription_set = None``: the MagicMock client returns a
    # MagicMock for ``subscribed_codes`` which is not isinstance(set).
    # Latched set is empty so ``active_count == 0`` via the empty loop,
    # not via the de-latch path.
    now = time.monotonic()
    svc._symbol_last_tick = {
        "TMFE6": now - 2500.0,  # genuinely-dead cold-start signal
    }
    svc._ever_active_symbols = set()  # no symbol latched yet

    gap = svc.get_active_feed_gap_s()

    # Membership unknown + nothing latched → fall back to legacy
    # max-gap so cold-start dead feeds still surface.
    assert gap >= 2400.0, f"fallback must fire when membership is unknown and nothing is latched: {gap=}"
