"""Smoke tests for MarketDataService."""
import asyncio
import os
from unittest.mock import MagicMock

import pytest

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import MetaData, TickEvent
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


@pytest.fixture(autouse=True)
def _sc(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    old = os.environ.get("SYMBOLS_CONFIG")
    os.environ["SYMBOLS_CONFIG"] = str(cfg)
    yield
    os.environ.pop("SYMBOLS_CONFIG", None) if old is None else os.environ.__setitem__("SYMBOLS_CONFIG", old)

@pytest.fixture()
def svc():
    return MarketDataService(MagicMock(spec=RingBufferBus), asyncio.Queue(), MagicMock())

def test_looks_like_md():
    assert _looks_like_md({"code": "2330", "close": 100}) is True
    assert _looks_like_md(None) is False

def test_unwrap_md():
    assert _unwrap_md(None) is None

def test_summarize_md():
    assert _summarize_md(None) == {}

def test_env_int(monkeypatch):
    monkeypatch.setenv("X", "42")
    assert _env_int("X", 10) == 42

def test_obs_policy(monkeypatch):
    monkeypatch.delenv("HFT_OBS_POLICY", raising=False)
    assert _obs_policy() == "balanced"

def test_callback():
    _, msg = _try_fast_extract_callback_payload(quote={"code": "2330", "close": 100})
    assert msg is not None

def test_init(svc):
    assert svc.state == FeedState.INIT

def test_enqueue(svc):
    svc._enqueue_raw("TSE", {"code": "2330", "close": 100})
    assert not svc.raw_queue.empty()

def test_publish(svc):
    tick = TickEvent(meta=MetaData(seq=0, source_ts=0, local_ts=0), symbol="2330", price=1000000, volume=1)
    svc._publish_nowait(tick)
    svc.bus.publish_nowait.assert_called_once_with(tick)
