import pytest

import hft_platform.feed_adapter.normalizer as norm
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer


@pytest.fixture
def normalizer(tmp_path):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return MarketDataNormalizer(str(cfg))


def test_extract_ts_ns_units():
    sec = 1_700_000_000
    ms = 1_700_000_000_000
    us = 1_700_000_000_000_000
    ns = 1_700_000_000_000_000_000

    assert norm._extract_ts_ns(sec) == sec * 1_000_000_000
    assert norm._extract_ts_ns(ms) == ms * 1_000_000
    assert norm._extract_ts_ns(us) == us * 1_000
    assert norm._extract_ts_ns(ns) == ns

    sec_f = 1_700_000_000.5
    assert norm._extract_ts_ns(sec_f) == int(sec_f * 1e9)


def test_local_ts_clamp_on_skew(monkeypatch, normalizer):
    monkeypatch.setattr(norm, "_TS_MAX_LAG_NS", 10)
    monkeypatch.setattr(norm, "_TS_SKEW_LOG_COOLDOWN_NS", 0)
    monkeypatch.setattr(norm.time, "time_ns", lambda: 2_000_000_000)

    payload = {
        "code": "2330",
        "close": 100.0,
        "volume": 1,
        "ts": 1,  # seconds -> 1e9 ns
    }
    event = normalizer.normalize_tick(payload)
    assert event.meta.local_ts == 1_000_000_010
