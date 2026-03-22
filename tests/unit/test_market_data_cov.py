"""Coverage tests for services/market_data.py — targeting 80%+ line coverage."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helper: module-level functions
# ---------------------------------------------------------------------------


def test_looks_like_md_none():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md(None) is False


def test_looks_like_md_dict_with_code():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"code": "TSMC"}) is True


def test_looks_like_md_dict_with_symbol():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"symbol": "TSMC"}) is True


def test_looks_like_md_dict_with_price():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"bid_price": 100.0}) is True


def test_looks_like_md_dict_with_ts():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"ts": "09:00:00"}) is True


def test_looks_like_md_dict_empty():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({}) is False


def test_looks_like_md_obj_with_code():
    from hft_platform.services.market_data import _looks_like_md

    obj = SimpleNamespace(code="TSMC", price=100.0)
    assert _looks_like_md(obj) is True


def test_looks_like_md_obj_without_md():
    from hft_platform.services.market_data import _looks_like_md

    obj = SimpleNamespace(x=1, y=2)
    assert _looks_like_md(obj) is False


def test_looks_like_md_dict_ask_price():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"ask_price": 100.0}) is True


def test_looks_like_md_dict_close():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"close": 99.0}) is True


def test_looks_like_md_dict_buy_price():
    from hft_platform.services.market_data import _looks_like_md

    assert _looks_like_md({"buy_price": 99.0}) is True


def test_looks_like_md_obj_bid_price():
    from hft_platform.services.market_data import _looks_like_md

    obj = SimpleNamespace(bid_price=100.0)
    assert _looks_like_md(obj) is True


def test_looks_like_md_obj_has_ts():
    from hft_platform.services.market_data import _looks_like_md

    obj = SimpleNamespace(ts="09:00:00", code="TSMC")
    assert _looks_like_md(obj) is True


# ---------------------------------------------------------------------------
# _unwrap_md
# ---------------------------------------------------------------------------


def test_unwrap_md_none():
    from hft_platform.services.market_data import _unwrap_md

    assert _unwrap_md(None) is None


def test_unwrap_md_dict_with_tick():
    from hft_platform.services.market_data import _unwrap_md

    inner = {"code": "TSMC", "price": 100.0}
    result = _unwrap_md({"tick": inner})
    assert result is inner


def test_unwrap_md_dict_with_bidask():
    from hft_platform.services.market_data import _unwrap_md

    inner = {"code": "TSMC", "bid_price": 100.0}
    result = _unwrap_md({"bidask": inner})
    assert result is inner


def test_unwrap_md_plain_dict():
    from hft_platform.services.market_data import _unwrap_md

    d = {"code": "TSMC"}
    assert _unwrap_md(d) is d


def test_unwrap_md_obj_with_tick():
    from hft_platform.services.market_data import _unwrap_md

    inner = SimpleNamespace(code="TSMC", price=100.0)
    obj = SimpleNamespace(tick=inner)
    result = _unwrap_md(obj)
    assert result is inner


def test_unwrap_md_obj_with_bidask():
    from hft_platform.services.market_data import _unwrap_md

    inner = SimpleNamespace(code="TSMC", bid_price=100.0)
    obj = SimpleNamespace(bidask=inner)
    result = _unwrap_md(obj)
    assert result is inner


def test_unwrap_md_obj_plain():
    from hft_platform.services.market_data import _unwrap_md

    obj = SimpleNamespace(code="TSMC")
    assert _unwrap_md(obj) is obj


# ---------------------------------------------------------------------------
# _env_int
# ---------------------------------------------------------------------------


def test_env_int_default(monkeypatch):
    from hft_platform.services.market_data import _env_int

    monkeypatch.delenv("HFT_TEST_INT_VAR", raising=False)
    assert _env_int("HFT_TEST_INT_VAR", 42) == 42


def test_env_int_from_env(monkeypatch):
    from hft_platform.services.market_data import _env_int

    monkeypatch.setenv("HFT_TEST_INT_VAR2", "99")
    assert _env_int("HFT_TEST_INT_VAR2", 1) == 99


def test_env_int_invalid(monkeypatch):
    from hft_platform.services.market_data import _env_int

    monkeypatch.setenv("HFT_TEST_INT_VAR3", "not_int")
    assert _env_int("HFT_TEST_INT_VAR3", 5) == 5


def test_env_int_minimum_one(monkeypatch):
    from hft_platform.services.market_data import _env_int

    monkeypatch.setenv("HFT_TEST_INT_VAR4", "0")
    assert _env_int("HFT_TEST_INT_VAR4", 1) == 1


# ---------------------------------------------------------------------------
# _obs_policy
# ---------------------------------------------------------------------------


def test_obs_policy_balanced(monkeypatch):
    from hft_platform.services.market_data import _obs_policy

    monkeypatch.setenv("HFT_OBS_POLICY", "balanced")
    assert _obs_policy() == "balanced"


def test_obs_policy_minimal(monkeypatch):
    from hft_platform.services.market_data import _obs_policy

    monkeypatch.setenv("HFT_OBS_POLICY", "minimal")
    assert _obs_policy() == "minimal"


def test_obs_policy_invalid(monkeypatch):
    from hft_platform.services.market_data import _obs_policy

    monkeypatch.setenv("HFT_OBS_POLICY", "bogus")
    assert _obs_policy() == "balanced"


def test_obs_policy_debug(monkeypatch):
    from hft_platform.services.market_data import _obs_policy

    monkeypatch.setenv("HFT_OBS_POLICY", "debug")
    assert _obs_policy() == "debug"


# ---------------------------------------------------------------------------
# Module-level helper: _try_fast_extract_callback_payload
# ---------------------------------------------------------------------------


def test_try_fast_extract_kwargs_quote():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    inner = {"code": "TSMC", "price": 100.0}
    exc, msg = _try_fast_extract_callback_payload(quote=inner)
    assert msg is inner


def test_try_fast_extract_kwargs_tick():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    inner = {"code": "TSMC", "price": 100.0}
    exc, msg = _try_fast_extract_callback_payload(tick=inner)
    assert msg is inner


def test_try_fast_extract_args_2_exchange_msg():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    inner = {"code": "TSMC", "price": 100.0}
    exc, msg = _try_fast_extract_callback_payload("TSE", inner)
    assert msg is inner
    assert exc == "TSE"


def test_try_fast_extract_args_1():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    inner = {"code": "TSMC", "price": 100.0}
    exc, msg = _try_fast_extract_callback_payload(inner)
    assert msg is inner


def test_try_fast_extract_args_3():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    topic = "TSE"
    inner = {"code": "TSMC", "bid_price": 100.0}
    exc, msg = _try_fast_extract_callback_payload(topic, inner, "extra")
    assert msg is not None


def test_try_fast_extract_no_match():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    exc, msg = _try_fast_extract_callback_payload("garbage", 42)
    assert msg is None


def test_try_fast_extract_exchange_from_kwargs():
    from hft_platform.services.market_data import _try_fast_extract_callback_payload

    inner = {"code": "TSMC", "price": 100.0}
    exc, msg = _try_fast_extract_callback_payload(exchange="TSE", tick=inner)
    assert exc == "TSE"
    assert msg is inner


# ---------------------------------------------------------------------------
# MarketDataService._set_state
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_lob_engine(monkeypatch):
    """Prevent LOBEngine init from requiring Rust."""
    with patch("hft_platform.services.market_data.LOBEngine") as MockLOB:
        MockLOB.return_value = MagicMock()
        with patch("hft_platform.services.market_data.FeatureEngine") as MockFE:
            MockFE.return_value = MagicMock()
            with patch("hft_platform.services.market_data.MetricsRegistry") as mr:
                mr.get.return_value = MagicMock()
                with patch("hft_platform.services.market_data.LatencyRecorder") as lr:
                    lr.get.return_value = MagicMock()
                    yield


@pytest.fixture()
def mds_factory():
    def _make():
        bus = MagicMock()
        bus.publish_nowait = MagicMock()
        raw_queue = asyncio.Queue()
        client = MagicMock()
        recorder_queue = asyncio.Queue()
        with patch("hft_platform.services.market_data.ShmSnapshotWriter"):
            with patch("hft_platform.services.market_data.MarketDataNormalizer"):
                from hft_platform.services.market_data import MarketDataService

                svc = MarketDataService(
                    bus=bus,
                    raw_queue=raw_queue,
                    client=client,
                    recorder_queue=recorder_queue,
                    feature_engine=MagicMock(),
                )
        return svc

    return _make


def test_set_state_transition(mds_factory):
    from hft_platform.services.market_data import FeedState

    svc = mds_factory()
    svc._set_state(FeedState.CONNECTED)
    assert svc.state == FeedState.CONNECTED


def test_set_state_same_state(mds_factory):
    from hft_platform.services.market_data import FeedState

    svc = mds_factory()
    svc.state = FeedState.CONNECTED
    svc._set_state(FeedState.CONNECTED)  # Should not log anything special


# ---------------------------------------------------------------------------
# _build_trace_id
# ---------------------------------------------------------------------------


def test_build_trace_id(mds_factory):
    from hft_platform.events import TickEvent, MetaData

    svc = mds_factory()
    meta = MetaData(seq=42, source_ts=1000, local_ts=1000, topic="tick")
    event = MagicMock(spec=TickEvent)
    event.meta = meta
    trace_id = svc._build_trace_id(event)
    assert isinstance(trace_id, str)


# ---------------------------------------------------------------------------
# _log_first_event
# ---------------------------------------------------------------------------


def test_log_first_event_tick(mds_factory):
    from hft_platform.events import TickEvent

    svc = mds_factory()
    svc._first_tick_event = False
    event = MagicMock(spec=TickEvent)
    svc._log_first_event(event)
    assert svc._first_tick_event is True


def test_log_first_event_already_seen(mds_factory):
    from hft_platform.events import TickEvent

    svc = mds_factory()
    svc._first_tick_event = True
    event = MagicMock(spec=TickEvent)
    svc._log_first_event(event)  # Should not raise


# ---------------------------------------------------------------------------
# _enqueue_raw
# ---------------------------------------------------------------------------


def test_enqueue_raw_puts_to_queue(mds_factory):
    svc = mds_factory()
    payload = {"code": "TSMC", "price": 100.0}
    svc._enqueue_raw("TSE", payload)
    assert not svc.raw_queue.empty()


def test_enqueue_raw_queue_full(mds_factory):
    svc = mds_factory()
    svc.raw_queue = asyncio.Queue(maxsize=1)
    svc.raw_queue.put_nowait("item1")
    svc._enqueue_raw("TSE", {"code": "TSMC"})  # Should not raise


# ---------------------------------------------------------------------------
# _record_direct_event
# ---------------------------------------------------------------------------


def test_record_direct_event_tick(mds_factory):
    from hft_platform.events import TickEvent

    svc = mds_factory()
    svc.recorder_queue = asyncio.Queue(maxsize=100)
    event = MagicMock(spec=TickEvent)
    event.symbol = "TSMC"
    svc._record_direct_event(event)
    assert not svc.recorder_queue.empty()


def test_record_direct_event_recorder_queue_full(mds_factory):
    from hft_platform.events import TickEvent

    svc = mds_factory()
    svc.recorder_queue = asyncio.Queue(maxsize=1)
    svc.recorder_queue.put_nowait("old")
    event = MagicMock(spec=TickEvent)
    svc._record_direct_event(event)  # Should not raise


def test_record_direct_event_no_recorder(mds_factory):
    from hft_platform.events import TickEvent

    svc = mds_factory()
    svc.recorder_queue = None
    event = MagicMock(spec=TickEvent)
    svc._record_direct_event(event)  # Should not raise
