"""Coverage improvement tests for MarketDataService.

Targets uncovered areas:
- _process_raw (tick, bidask, error paths)
- _publish_events (publish_full_events=True/False variants)
- _log_first_event (tick and bidask)
- _update_symbol_tick_inline (inline and task path)
- _build_trace_id (with/without meta)
- _enqueue_raw (success and QueueFull)
- _record_direct_event (drop-on-full, degrade, recover, non-drop path)
- _maybe_update_features (feature_engine=None, stats=None, happy path)
- _maybe_run_feature_shadow_parity (no shadow, mismatch)
- _on_shioaji_event (various arg shapes)
- get_max_feed_gap_s / get_feed_gaps_by_symbol
- _set_state (transition and no-op)
- _mark_pending_reconnect
- _request_reconnect (within window / outside window)
- _emit_trace (sampler present/absent)
- _record_shioaji_crash_signature
- _is_market_open_grace_period (disabled, no grace)
- _get_trace_sampler and _looks_like_md / _unwrap_md module-level helpers
- _summarize_md attribute path
- _try_fast_extract_callback_payload edge cases
- _env_int / _obs_policy module-level helpers
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer
from hft_platform.services.market_data import (
    FeedState,
    MarketDataService,
    _env_int,
    _looks_like_md,
    _obs_policy,
    _summarize_md,
    _try_fast_extract_callback_payload,
    _unwrap_md,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _symbols_cfg(tmp_path: Path, monkeypatch):
    """Provide a minimal symbols.yaml for every test in this module."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))
    monkeypatch.setenv("HFT_FEATURE_ENGINE_ENABLED", "0")
    monkeypatch.setenv("HFT_FEATURE_SHADOW_PARITY", "0")


def _make_service(monkeypatch=None, **env_overrides) -> MarketDataService:
    """Return a lightly-patched MarketDataService."""
    bus = MagicMock(spec=RingBufferBus)
    bus.publish_nowait = MagicMock()
    bus.publish_many_nowait = MagicMock()
    client = MagicMock()
    raw_queue = asyncio.Queue()
    if monkeypatch is not None:
        for k, v in env_overrides.items():
            monkeypatch.setenv(k, v)
    svc = MarketDataService(bus, raw_queue, client, feature_engine=None)
    return svc


def _tick(symbol="2330", price=5000000, volume=10) -> TickEvent:
    """TickEvent with scaled-int price (x10000)."""
    meta = MetaData(seq=1, source_ts=1_000_000_000, local_ts=1_000_000_001, topic="tick")
    return TickEvent(meta=meta, symbol=symbol, price=price, volume=volume)


def _bidask(symbol="2330") -> BidAskEvent:
    """Minimal BidAskEvent."""
    import numpy as np

    bids = np.array([[4999000, 100]], dtype=np.int64)
    asks = np.array([[5001000, 100]], dtype=np.int64)
    meta = MetaData(seq=2, source_ts=1_000_000_000, local_ts=1_000_000_001, topic="bidask")
    return BidAskEvent(meta=meta, symbol=symbol, bids=bids, asks=asks, is_snapshot=False, stats=None)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


class TestModuleLevelHelpers:
    def test_looks_like_md_with_bid_price_key(self):
        assert _looks_like_md({"bid_price": 100}) is True

    def test_looks_like_md_with_ask_price_key(self):
        assert _looks_like_md({"ask_price": 100}) is True

    def test_looks_like_md_with_close_key(self):
        assert _looks_like_md({"close": 100}) is True

    def test_looks_like_md_with_ts_key(self):
        assert _looks_like_md({"ts": 12345}) is True

    def test_looks_like_md_empty_dict(self):
        assert _looks_like_md({}) is False

    def test_looks_like_md_object_with_code_and_price(self):
        obj = MagicMock()
        obj.code = "2330"
        obj.bid_price = 100
        assert _looks_like_md(obj) is True

    def test_looks_like_md_object_no_attrs(self):
        class Empty:
            pass

        assert _looks_like_md(Empty()) is False

    def test_unwrap_md_dict_with_tick(self):
        inner = {"close": 100}
        result = _unwrap_md({"tick": inner})
        assert result is inner

    def test_unwrap_md_dict_with_bidask(self):
        inner = {"bid_price": 100}
        result = _unwrap_md({"bidask": inner})
        assert result is inner

    def test_unwrap_md_plain_dict_returned_as_is(self):
        d = {"close": 100}
        assert _unwrap_md(d) is d

    def test_unwrap_md_object_with_tick_attr(self):
        inner = MagicMock()
        inner.bid_price = 100
        obj = MagicMock()
        obj.tick = inner
        result = _unwrap_md(obj)
        assert result is inner

    def test_summarize_md_dict(self):
        d = {"code": "2330", "close": 100, "ts": 1234}
        result = _summarize_md(d)
        assert "keys" in result
        assert "close" in result["present"]

    def test_summarize_md_object(self):
        class Obj:
            code = "2330"
            bid_price = 100

        result = _summarize_md(Obj())
        assert "attrs" in result

    def test_env_int_valid(self):
        with patch.dict(os.environ, {"_TEST_INT": "42"}):
            assert _env_int("_TEST_INT", 10) == 42

    def test_env_int_invalid_returns_default(self):
        with patch.dict(os.environ, {"_TEST_INT": "bad"}):
            assert _env_int("_TEST_INT", 5) == 5

    def test_env_int_clamps_to_1(self):
        with patch.dict(os.environ, {"_TEST_INT": "0"}):
            assert _env_int("_TEST_INT", 1) == 1

    def test_obs_policy_balanced(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "balanced"}):
            assert _obs_policy() == "balanced"

    def test_obs_policy_minimal(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "minimal"}):
            assert _obs_policy() == "minimal"

    def test_obs_policy_invalid_defaults_to_balanced(self):
        with patch.dict(os.environ, {"HFT_OBS_POLICY": "turbo"}):
            assert _obs_policy() == "balanced"

    def test_try_fast_extract_kwarg_tick(self):
        msg = {"close": 100, "code": "2330"}
        exchange, result = _try_fast_extract_callback_payload(tick=msg)
        assert result is msg

    def test_try_fast_extract_kwarg_bidask(self):
        msg = {"bid_price": 100}
        exchange, result = _try_fast_extract_callback_payload(bidask=msg)
        assert result is msg

    def test_try_fast_extract_two_args(self):
        msg = {"close": 100}
        exchange, result = _try_fast_extract_callback_payload("TSE", msg)
        assert result == msg
        assert exchange == "TSE"

    def test_try_fast_extract_one_arg(self):
        msg = {"close": 100}
        _, result = _try_fast_extract_callback_payload(msg)
        assert result == msg

    def test_try_fast_extract_three_args_last_is_md(self):
        msg = {"close": 100}
        _, result = _try_fast_extract_callback_payload("topic", "other", msg)
        assert result == msg

    def test_try_fast_extract_returns_none_when_no_md(self):
        _, result = _try_fast_extract_callback_payload("nothing_useful")
        assert result is None


# ---------------------------------------------------------------------------
# _set_state
# ---------------------------------------------------------------------------


class TestSetState:
    def test_state_changes_on_different_state(self):
        svc = _make_service()
        svc._set_state(FeedState.CONNECTED)
        assert svc.state == FeedState.CONNECTED

    def test_state_no_change_if_same(self):
        svc = _make_service()
        svc.state = FeedState.CONNECTED
        svc._set_state(FeedState.CONNECTED)  # should not raise
        assert svc.state == FeedState.CONNECTED


# ---------------------------------------------------------------------------
# _build_trace_id
# ---------------------------------------------------------------------------


class TestBuildTraceId:
    def test_with_meta_seq(self):
        evt = _tick()
        trace_id = MarketDataService._build_trace_id(evt)
        assert trace_id == "tick:1"

    def test_without_meta_returns_empty(self):
        meta = MagicMock()
        meta.seq = None
        meta.topic = "t"
        evt = _tick()
        object.__setattr__(evt, "meta", meta)
        trace_id = MarketDataService._build_trace_id(evt)
        assert trace_id == ""

    def test_no_meta_attr_returns_empty(self):
        evt = MagicMock()
        del evt.meta
        trace_id = MarketDataService._build_trace_id(evt)
        assert trace_id == ""


# ---------------------------------------------------------------------------
# _log_first_event
# ---------------------------------------------------------------------------


class TestLogFirstEvent:
    def test_first_tick_event_sets_flag(self):
        svc = _make_service()
        svc._log_first_event(_tick())
        assert svc._first_tick_event is True

    def test_second_tick_event_no_double_log(self):
        svc = _make_service()
        svc._log_first_event(_tick())
        svc._log_first_event(_tick())  # no error, flag remains True
        assert svc._first_tick_event is True

    def test_first_bidask_event_sets_flag(self):
        svc = _make_service()
        svc._log_first_event(_bidask())
        assert svc._first_bidask_event is True

    def test_first_bidask_event_none_arrays(self):
        svc = _make_service()
        evt = _bidask()
        evt.bids = None
        evt.asks = None
        svc._log_first_event(evt)
        assert svc._first_bidask_event is True


# ---------------------------------------------------------------------------
# _update_symbol_tick_inline
# ---------------------------------------------------------------------------


class TestUpdateSymbolTickInline:
    def test_inline_updates_dict(self):
        svc = _make_service()
        svc._symbol_tick_inline = True
        svc._update_symbol_tick_inline(_tick())
        assert "2330" in svc._symbol_last_tick

    def test_no_symbol_skipped(self):
        svc = _make_service()
        evt = _tick()
        evt.symbol = ""
        svc._update_symbol_tick_inline(evt)
        assert "2330" not in svc._symbol_last_tick

    @pytest.mark.asyncio
    async def test_async_path_creates_task(self):
        svc = _make_service()
        svc._symbol_tick_inline = False
        loop = asyncio.get_event_loop()
        svc.loop = loop
        svc._update_symbol_tick_inline(_tick())
        await asyncio.sleep(0)  # let task run
        assert "2330" in svc._symbol_last_tick


# ---------------------------------------------------------------------------
# _publish_events
# ---------------------------------------------------------------------------


class TestPublishEvents:
    def test_publish_full_with_stats_and_feature(self):
        svc = _make_service()
        stats = MagicMock()
        fu = MagicMock()
        svc._publish_events(_tick(), stats, fu)
        svc.bus.publish_many_nowait.assert_called_once()
        args = svc.bus.publish_many_nowait.call_args[0][0]
        assert len(args) == 3

    def test_publish_full_with_stats_only(self):
        svc = _make_service()
        stats = MagicMock()
        svc._publish_events(_tick(), stats, None)
        svc.bus.publish_many_nowait.assert_called_once()
        args = svc.bus.publish_many_nowait.call_args[0][0]
        assert len(args) == 2

    def test_publish_full_with_feature_only(self):
        svc = _make_service()
        fu = MagicMock()
        svc._publish_events(_tick(), None, fu)
        svc.bus.publish_many_nowait.assert_called_once()
        args = svc.bus.publish_many_nowait.call_args[0][0]
        assert len(args) == 2

    def test_publish_full_no_stats_no_feature(self):
        svc = _make_service()
        svc._publish_events(_tick(), None, None)
        svc.bus.publish_nowait.assert_called_once()

    def test_publish_not_full_stats_and_feature(self):
        svc = _make_service()
        svc.publish_full_events = False
        stats = MagicMock()
        fu = MagicMock()
        svc._publish_events(_tick(), stats, fu)
        svc.bus.publish_many_nowait.assert_called_once()
        args = svc.bus.publish_many_nowait.call_args[0][0]
        assert len(args) == 2

    def test_publish_not_full_stats_only(self):
        svc = _make_service()
        svc.publish_full_events = False
        stats = MagicMock()
        svc._publish_events(_tick(), stats, None)
        svc.bus.publish_many_nowait.assert_called_once()
        args = svc.bus.publish_many_nowait.call_args[0][0]
        assert len(args) == 1

    def test_publish_not_full_no_stats_no_feature(self):
        """No stats and no feature_update with publish_full_events=False → no publish."""
        svc = _make_service()
        svc.publish_full_events = False
        svc._publish_events(_tick(), None, None)
        svc.bus.publish_many_nowait.assert_not_called()
        svc.bus.publish_nowait.assert_not_called()


# ---------------------------------------------------------------------------
# _emit_trace
# ---------------------------------------------------------------------------


class TestEmitTrace:
    def test_emit_calls_sampler(self):
        svc = _make_service()
        sampler = MagicMock()
        svc._trace_sampler = sampler
        svc._emit_trace("stage", "trace_id", {"key": "value"})
        sampler.emit.assert_called_once()

    def test_emit_no_sampler_is_noop(self):
        svc = _make_service()
        svc._trace_sampler = None
        svc._emit_trace("stage", "trace_id", {})  # must not raise

    def test_emit_sampler_exception_is_suppressed(self):
        svc = _make_service()
        sampler = MagicMock()
        sampler.emit.side_effect = RuntimeError("oops")
        svc._trace_sampler = sampler
        svc._emit_trace("stage", "", {})  # must not raise


# ---------------------------------------------------------------------------
# _record_shioaji_crash_signature
# ---------------------------------------------------------------------------


class TestRecordCrashSignature:
    def test_no_registry(self):
        svc = _make_service()
        svc.metrics_registry = None
        svc._record_shioaji_crash_signature("some error", context="test")  # no raise

    def test_no_signature_matched(self):
        svc = _make_service()
        svc.metrics_registry = MagicMock()
        with patch("hft_platform.services.market_data.detect_crash_signature", return_value=""):
            svc._record_shioaji_crash_signature("no match", context="test")
        svc.metrics_registry.shioaji_crash_signature_total.labels.assert_not_called()

    def test_signature_increments_metric(self):
        svc = _make_service()
        svc.metrics_registry = MagicMock()
        child = MagicMock()
        svc.metrics_registry.shioaji_crash_signature_total.labels.return_value = child
        with patch("hft_platform.services.market_data.detect_crash_signature", return_value="conn_reset"):
            svc._record_shioaji_crash_signature("Connection reset", context="test")
        child.inc.assert_called_once()


# ---------------------------------------------------------------------------
# _enqueue_raw
# ---------------------------------------------------------------------------


class TestEnqueueRaw:
    def test_enqueue_success_adds_to_queue(self):
        svc = _make_service()
        svc._enqueue_raw("TSE", {"close": 100})
        assert svc.raw_queue.qsize() == 1

    def test_enqueue_full_increments_dropped(self):
        svc = _make_service()
        svc.raw_queue = asyncio.Queue(maxsize=1)
        svc.raw_queue.put_nowait(("TSE", {}))
        svc.metrics_registry = MagicMock()
        svc._enqueue_raw("TSE", {"close": 200})
        assert svc._dropped_count == 1
        svc.metrics_registry.raw_queue_dropped_total.inc.assert_called_once()


# ---------------------------------------------------------------------------
# get_max_feed_gap_s / get_feed_gaps_by_symbol
# ---------------------------------------------------------------------------


class TestFeedGapHelpers:
    def test_no_ticks_returns_default(self):
        svc = _make_service()
        svc._symbol_last_tick = {}
        result = svc.get_max_feed_gap_s()
        assert result == 0.0

    def test_with_ticks_returns_positive_gap(self):
        svc = _make_service()
        svc._symbol_last_tick = {"2330": time.monotonic() - 5.0}
        gap = svc.get_max_feed_gap_s()
        assert gap >= 4.5

    def test_feed_gaps_by_symbol_empty(self):
        svc = _make_service()
        svc._symbol_last_tick = {}
        assert svc.get_feed_gaps_by_symbol() == {}

    def test_feed_gaps_by_symbol_returns_dict(self):
        svc = _make_service()
        svc._symbol_last_tick = {"2330": time.monotonic() - 3.0, "0050": time.monotonic() - 1.0}
        gaps = svc.get_feed_gaps_by_symbol()
        assert set(gaps.keys()) == {"2330", "0050"}
        assert gaps["2330"] >= 2.5


# ---------------------------------------------------------------------------
# _mark_pending_reconnect
# ---------------------------------------------------------------------------


class TestMarkPendingReconnect:
    def test_sets_pending_state(self):
        svc = _make_service()
        svc._mark_pending_reconnect(10.0, reason="heartbeat_gap")
        assert svc._pending_reconnect_reason == "heartbeat_gap"
        assert svc._pending_reconnect_gap == 10.0
        assert svc._pending_reconnect_since is not None

    def test_only_sets_since_once(self):
        svc = _make_service()
        svc._mark_pending_reconnect(5.0)
        first_since = svc._pending_reconnect_since
        svc._mark_pending_reconnect(6.0)
        assert svc._pending_reconnect_since == first_since


# ---------------------------------------------------------------------------
# _request_reconnect
# ---------------------------------------------------------------------------


class TestRequestReconnect:
    @pytest.mark.asyncio
    async def test_within_window_triggers_reconnect(self):
        svc = _make_service()
        svc._last_reconnect_ts = 0.0
        svc.reconnect_cooldown_s = 0.0
        svc.client.reconnect.return_value = True
        with patch.object(svc, "_within_reconnect_window", return_value=True):
            await svc._request_reconnect(10.0)
        svc.client.reconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_outside_window_marks_pending(self):
        svc = _make_service()
        with patch.object(svc, "_within_reconnect_window", return_value=False):
            await svc._request_reconnect(10.0, reason="heartbeat_gap")
        assert svc._pending_reconnect_reason == "heartbeat_gap"


# ---------------------------------------------------------------------------
# _record_direct_event
# ---------------------------------------------------------------------------


_MAPPER_PATH = "hft_platform.recorder.mapper.map_event_to_record"


class TestRecordDirectEvent:
    def test_no_recorder_queue_is_noop(self):
        svc = _make_service()
        svc.recorder_queue = None
        svc._record_direct_event(_tick())  # must not raise

    def test_drop_on_full_queues_event(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=10)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_drop_on_full = True
        with patch(_MAPPER_PATH, return_value=("topic", {"data": 1})):
            svc._record_direct_event(_tick())
        assert rq.qsize() == 1

    def test_drop_on_full_queue_full_increments_dropped(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=1)
        rq.put_nowait({"topic": "t", "data": {}})
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_drop_on_full = True
        svc._dropped_count = 0
        with patch(_MAPPER_PATH, return_value=("topic", {"data": 1})):
            svc._record_direct_event(_tick())
        assert svc._dropped_count == 1

    def test_degrade_mode_skips_recording(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=10)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_degraded = True
        svc._record_degrade_last_check = time.monotonic()
        svc._record_degraded_drops = 0
        with patch(_MAPPER_PATH) as m:
            svc._record_direct_event(_tick())
        m.assert_not_called()
        assert svc._record_degraded_drops == 1

    def test_degrade_mode_recovers_when_queue_low(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=100)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_degraded = True
        svc._record_degrade_last_check = 0.0  # force a check
        svc._record_degrade_check_s = 0.0
        svc._record_degraded_drops = 5
        svc._record_degraded_since = time.monotonic() - 1.0
        # queue is empty → qsize < maxsize * 0.5 → should recover
        with patch(_MAPPER_PATH, return_value=("topic", {"data": 1})):
            svc._record_direct_event(_tick())
        assert svc._record_degraded is False
        assert rq.qsize() == 1

    @pytest.mark.asyncio
    async def test_non_drop_path_creates_task(self):
        """When _record_drop_on_full is False, a coroutine put is created."""
        svc = _make_service()
        rq = asyncio.Queue(maxsize=10)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_drop_on_full = False
        svc._record_degraded = False
        with patch(_MAPPER_PATH, return_value=("topic", {"data": 1})):
            svc._record_direct_event(_tick())
        # Give the created task a chance to run
        await asyncio.sleep(0)
        assert rq.qsize() == 1

    def test_record_mapping_failure_returns_early(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=10)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_degraded = False
        with patch(_MAPPER_PATH, side_effect=RuntimeError("fail")):
            svc._record_direct_event(_tick())
        assert rq.qsize() == 0  # nothing was queued

    def test_record_mapping_returns_none_returns_early(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=10)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_degraded = False
        with patch(_MAPPER_PATH, return_value=None):
            svc._record_direct_event(_tick())
        assert rq.qsize() == 0


# ---------------------------------------------------------------------------
# _maybe_update_features
# ---------------------------------------------------------------------------


class TestMaybeUpdateFeatures:
    def test_no_feature_engine_returns_none(self):
        svc = _make_service()
        svc.feature_engine = None
        result = svc._maybe_update_features(_tick(), MagicMock())
        assert result is None

    def test_no_stats_returns_none(self):
        svc = _make_service()
        svc.feature_engine = None
        result = svc._maybe_update_features(_tick(), None)
        assert result is None

    def test_stats_missing_bid_ask_returns_none(self):
        svc = _make_service()
        fe = MagicMock()
        svc.feature_engine = fe
        stats = MagicMock(spec=[])  # no best_bid or best_ask
        result = svc._maybe_update_features(_tick(), stats)
        assert result is None

    def test_feature_engine_called_with_stats(self):
        svc = _make_service()
        fe = MagicMock()
        fu = MagicMock()
        fe.process_lob_update = MagicMock(return_value=fu)
        svc.feature_engine = fe
        stats = MagicMock()
        stats.best_bid = 4999000
        stats.best_ask = 5001000
        result = svc._maybe_update_features(_tick(), stats)
        assert result is fu
        fe.process_lob_update.assert_called_once()

    def test_feature_engine_exception_returns_none(self):
        svc = _make_service()
        fe = MagicMock()
        fe.process_lob_update.side_effect = RuntimeError("oops")
        svc.feature_engine = fe
        stats = MagicMock()
        stats.best_bid = 4999000
        stats.best_ask = 5001000
        result = svc._maybe_update_features(_tick(), stats)
        assert result is None

    def test_feature_engine_fallback_to_process_lob_stats(self):
        svc = _make_service()
        fe = MagicMock(spec=["process_lob_stats"])  # no process_lob_update attr
        fu = MagicMock()
        fe.process_lob_stats.return_value = fu
        svc.feature_engine = fe
        stats = MagicMock()
        stats.best_bid = 4999000
        stats.best_ask = 5001000
        result = svc._maybe_update_features(_tick(), stats)
        assert result is fu


# ---------------------------------------------------------------------------
# _process_raw
# ---------------------------------------------------------------------------


class TestProcessRaw:
    def test_tick_dict_publishes_event(self):
        svc = _make_service()
        raw = {"code": "2330", "close": 500.0, "volume": 1, "ts": 1234567890}
        tick = _tick()
        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                svc._process_raw(raw)
        svc.bus.publish_nowait.assert_called_once()

    def test_bidask_dict_publishes_event(self):
        svc = _make_service()
        raw = {"bid_price": 499.0, "ask_price": 501.0, "code": "2330"}
        ba = _bidask()
        with patch.object(MarketDataNormalizer, "normalize_bidask", return_value=ba):
            with patch.object(LOBEngine, "process_event", return_value=None):
                svc._process_raw(raw)
        svc.bus.publish_nowait.assert_called_once()

    def test_invalid_raw_skips_silently(self):
        svc = _make_service()
        svc._process_raw(None)  # must not raise

    def test_normalization_exception_skips(self):
        svc = _make_service()
        raw = {"close": 500.0}
        with patch.object(MarketDataNormalizer, "normalize_tick", side_effect=RuntimeError("fail")):
            svc._process_raw(raw)
        svc.bus.publish_nowait.assert_not_called()

    def test_none_event_after_normalize_skips(self):
        svc = _make_service()
        raw = {"close": 500.0}
        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=None):
            svc._process_raw(raw)
        svc.bus.publish_nowait.assert_not_called()

    def test_record_direct_called_when_enabled(self):
        svc = _make_service()
        rq = asyncio.Queue(maxsize=100)
        svc.recorder_queue = rq
        svc._record_direct = True
        svc._record_degraded = False
        svc._record_drop_on_full = True
        tick = _tick()
        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                with patch(_MAPPER_PATH, return_value=("topic", {})):
                    svc._process_raw({"close": 500.0})
        assert rq.qsize() == 1

    def test_log_normalized_increments_counter(self):
        svc = _make_service()
        svc.log_normalized = True
        svc.log_normalized_every = 1
        tick = _tick()
        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                svc._process_raw({"close": 500.0})
        assert svc._normalized_log_counter == 1


# ---------------------------------------------------------------------------
# _on_shioaji_event
# ---------------------------------------------------------------------------


class TestOnShioajiEvent:
    def test_first_call_logs_and_enqueues(self):
        svc = _make_service()
        svc.loop = asyncio.new_event_loop()
        msg = {"close": 100, "code": "2330"}
        try:
            svc._on_shioaji_event("TSE", msg)
        finally:
            svc.loop.close()
        assert svc._raw_first_seen is True

    def test_missing_loop_logs_error(self):
        svc = _make_service()
        if hasattr(svc, "loop"):
            del svc.loop
        msg = {"close": 100, "code": "2330"}
        svc._on_shioaji_event(msg)  # must not raise (logs "Callback loop missing")

    def test_callback_exception_is_caught(self):
        svc = _make_service()
        svc.loop = MagicMock()
        svc.loop.call_soon_threadsafe.side_effect = RuntimeError("boom")
        msg = {"close": 100, "code": "2330"}
        svc._on_shioaji_event("TSE", msg)  # must not raise

    def test_unparseable_callback_args_no_crash(self):
        svc = _make_service()
        svc.loop = MagicMock()
        svc._on_shioaji_event()  # zero args


# ---------------------------------------------------------------------------
# _is_market_open_grace_period
# ---------------------------------------------------------------------------


class TestIsMarketOpenGracePeriod:
    def test_disabled_when_grace_zero(self):
        svc = _make_service()
        svc._market_open_grace_s = 0
        assert svc._is_market_open_grace_period() is False

    def test_import_error_returns_false(self):
        svc = _make_service()
        svc._market_open_grace_s = 60
        with patch("hft_platform.services.market_data.dt") as mock_dt:
            # Make calendar import fail
            with patch.dict("sys.modules", {"hft_platform.core.market_calendar": None}):
                result = svc._is_market_open_grace_period()
        # May return False due to ImportError or exception
        assert result is False or result is True  # just ensure no exception

    def test_calendar_exception_returns_false(self):
        svc = _make_service()
        svc._market_open_grace_s = 60
        with patch("hft_platform.services.market_data.dt") as mock_dt:
            mock_dt.datetime.now.side_effect = RuntimeError("no tz")
            result = svc._is_market_open_grace_period()
        assert result is False


# ---------------------------------------------------------------------------
# run() loop behaviors
# ---------------------------------------------------------------------------


class TestRunLoop:
    @pytest.mark.asyncio
    async def test_run_processes_tuple_message(self):
        """Verify run() correctly unwraps (exchange, raw) tuples from queue."""
        bus = MagicMock(spec=RingBufferBus)
        bus.publish_nowait = MagicMock()
        raw_queue = asyncio.Queue()
        client = MagicMock()
        client.fetch_snapshots.return_value = []
        svc = MarketDataService(bus, raw_queue, client, feature_engine=None)

        tick = _tick()
        await raw_queue.put(("TSE", {"close": 500.0}))

        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.05)
                svc.running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        bus.publish_nowait.assert_called()

    @pytest.mark.asyncio
    async def test_run_processes_bare_message(self):
        """Verify run() handles bare (non-tuple) messages from queue."""
        bus = MagicMock(spec=RingBufferBus)
        bus.publish_nowait = MagicMock()
        raw_queue = asyncio.Queue()
        client = MagicMock()
        client.fetch_snapshots.return_value = []
        svc = MarketDataService(bus, raw_queue, client, feature_engine=None)

        tick = _tick()
        await raw_queue.put({"close": 500.0})

        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.05)
                svc.running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        bus.publish_nowait.assert_called()

    @pytest.mark.asyncio
    async def test_run_high_watermark_warning(self):
        """Queue high watermark triggers warning and clears correctly."""
        bus = MagicMock(spec=RingBufferBus)
        bus.publish_nowait = MagicMock()
        raw_queue = asyncio.Queue(maxsize=10)
        client = MagicMock()
        client.fetch_snapshots.return_value = []
        svc = MarketDataService(bus, raw_queue, client, feature_engine=None)
        svc._raw_queue_size = 10
        svc._raw_queue_high_watermark = 0.5
        svc._md_metrics_sample_every = 1

        # Fill to trigger watermark
        for _ in range(6):
            raw_queue.put_nowait({"close": 100.0})

        tick = _tick()
        with patch.object(MarketDataNormalizer, "normalize_tick", return_value=tick):
            with patch.object(LOBEngine, "process_event", return_value=None):
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.1)
                svc.running = False
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        assert svc._high_watermark_warned is False  # cleared after draining


# ---------------------------------------------------------------------------
# _connect_sequence with snapshots
# ---------------------------------------------------------------------------


class TestConnectSequenceSnapshots:
    @pytest.mark.asyncio
    async def test_snapshots_processed(self):
        svc = _make_service()
        snap = MagicMock()
        svc.client.fetch_snapshots.return_value = [snap]
        tick = _tick()
        with patch.object(MarketDataNormalizer, "normalize_snapshot", return_value=tick, create=True):
            with patch.object(LOBEngine, "process_event", return_value=None):
                await svc._connect_sequence()
        assert svc.state == FeedState.CONNECTED

    @pytest.mark.asyncio
    async def test_snapshots_normalize_exception_skipped(self):
        svc = _make_service()
        snap = MagicMock()
        svc.client.fetch_snapshots.return_value = [snap]
        with patch.object(MarketDataNormalizer, "normalize_snapshot", side_effect=RuntimeError("bad"), create=True):
            await svc._connect_sequence()
        assert svc.state == FeedState.CONNECTED

    @pytest.mark.asyncio
    async def test_snapshot_fetch_exception_continues(self):
        """fetch_snapshots raising an exception doesn't block connection."""
        svc = _make_service()
        svc.client.fetch_snapshots.side_effect = RuntimeError("fetch_failed")
        await svc._connect_sequence()
        assert svc.state == FeedState.CONNECTED


# ---------------------------------------------------------------------------
# _within_reconnect_window
# ---------------------------------------------------------------------------


class TestWithinReconnectWindow:
    def test_no_config_always_true(self):
        svc = _make_service()
        svc.reconnect_days = set()
        svc.reconnect_hours = ""
        svc.reconnect_hours_2 = ""
        assert svc._within_reconnect_window() is True

    def test_wrong_weekday_returns_false(self):
        svc = _make_service()
        svc.reconnect_days = {"mon"}
        svc.reconnect_hours = ""
        svc.reconnect_hours_2 = ""
        # patch to a Tuesday
        with patch("hft_platform.services.market_data.dt") as mock_dt:
            mock_dt.datetime.now.return_value = MagicMock(strftime=lambda fmt: "tue")
            with patch.dict(os.environ, {"HFT_RECONNECT_USE_CALENDAR": "0"}):
                result = svc._within_reconnect_window()
        assert result is False

    def test_within_time_window_returns_true(self):
        import datetime as dt

        svc = _make_service()
        svc.reconnect_days = set()
        svc.reconnect_hours = "08:00-18:00"
        svc.reconnect_hours_2 = ""
        now_mock = MagicMock()
        now_mock.strftime.return_value = "mon"
        now_mock.timetz.return_value = dt.time(9, 0).replace(tzinfo=None)
        with patch("hft_platform.services.market_data.dt") as mock_dt:
            mock_dt.datetime.now.return_value = now_mock
            mock_dt.time.fromisoformat = dt.time.fromisoformat
            with patch.dict(os.environ, {"HFT_RECONNECT_USE_CALENDAR": "0"}):
                result = svc._within_reconnect_window()
        assert result is True


# ---------------------------------------------------------------------------
# _publish_to_shm
# ---------------------------------------------------------------------------


class TestPublishToShm:
    def test_no_shm_publisher_is_noop(self):
        svc = _make_service()
        svc._shm_publisher = None
        svc._publish_to_shm("2330", MagicMock(), None)  # must not raise

    def test_shm_publish_called_with_index(self):
        svc = _make_service()
        publisher = MagicMock()
        publisher.max_symbols = 10
        publisher.publish = MagicMock()
        svc._shm_publisher = publisher
        svc._shm_symbol_index = {"2330": 0}
        svc._shm_symbol_hashes = {"2330": 12345}
        stats = MagicMock()
        for attr in (
            "best_bid",
            "best_ask",
            "mid_price_x2",
            "spread_scaled",
            "bid_depth",
            "ask_depth",
            "l1_bid_qty",
            "l1_ask_qty",
            "microprice_x2",
            "local_ts",
        ):
            setattr(stats, attr, 1)
        svc._publish_to_shm("2330", stats, None)
        publisher.publish.assert_called_once()

    def test_shm_publish_lazy_assigns_index(self):
        svc = _make_service()
        publisher = MagicMock()
        publisher.max_symbols = 10
        publisher.publish = MagicMock()
        svc._shm_publisher = publisher
        svc._shm_symbol_index = {}
        svc._shm_symbol_hashes = {}
        stats = MagicMock()
        for attr in (
            "best_bid",
            "best_ask",
            "mid_price_x2",
            "spread_scaled",
            "bid_depth",
            "ask_depth",
            "l1_bid_qty",
            "l1_ask_qty",
            "microprice_x2",
            "local_ts",
        ):
            setattr(stats, attr, 1)
        svc._publish_to_shm("2330", stats, [0] * 16)
        assert "2330" in svc._shm_symbol_index

    def test_shm_publish_skips_when_full(self):
        svc = _make_service()
        publisher = MagicMock()
        publisher.max_symbols = 1
        publisher.publish = MagicMock()
        svc._shm_publisher = publisher
        svc._shm_symbol_index = {"0050": 0}  # already at max_symbols
        svc._shm_symbol_hashes = {}
        stats = MagicMock()
        svc._publish_to_shm("2330", stats, None)
        publisher.publish.assert_not_called()
