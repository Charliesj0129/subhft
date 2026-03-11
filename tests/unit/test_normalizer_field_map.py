"""Tests for NormalizerFieldMap — configurable broker field names."""

import pytest

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import (
    MarketDataNormalizer,
    NormalizerFieldMap,
)


@pytest.fixture
def symbols_cfg(tmp_path):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text(
        "symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n"
    )
    return str(cfg)


def test_default_field_map_matches_shioaji(symbols_cfg):
    """Default NormalizerFieldMap should produce identical results to hardcoded Shioaji keys."""
    nm = MarketDataNormalizer(symbols_cfg)
    payload = {
        "code": "2330",
        "close": 100.0,
        "volume": 5,
        "total_volume": 1000,
        "ts": 1620000000000000,
        "simtrade": 0,
        "intraday_odd": 0,
    }
    event = nm.normalize_tick(payload)
    assert isinstance(event, TickEvent)
    assert event.symbol == "2330"
    assert event.price == 1_000_000


def test_custom_field_map_tick(symbols_cfg):
    """A broker with different field names should normalize correctly via custom field map."""
    fm = NormalizerFieldMap(
        symbol_key="sym",
        price_key="last",
        volume_key="qty",
        ts_key="timestamp",
        total_volume_key="cumulative_vol",
        simtrade_key="is_sim",
        odd_lot_key="is_odd",
    )
    nm = MarketDataNormalizer(symbols_cfg, field_map=fm)

    payload = {
        "sym": "2330",
        "last": 100.0,
        "qty": 5,
        "cumulative_vol": 1000,
        "timestamp": 1620000000000000,
        "is_sim": 0,
        "is_odd": 0,
    }
    event = nm.normalize_tick(payload)
    assert isinstance(event, TickEvent)
    assert event.symbol == "2330"
    assert event.price == 1_000_000
    assert event.volume == 5
    assert event.total_volume == 1000


def test_custom_field_map_bidask(symbols_cfg):
    """Custom field map should work for bidask normalization."""
    fm = NormalizerFieldMap(
        symbol_key="ticker",
        ts_key="exchange_ts",
        bid_price_key="bp",
        ask_price_key="ap",
        bid_volume_key="bv",
        ask_volume_key="av",
    )
    nm = MarketDataNormalizer(symbols_cfg, field_map=fm)

    payload = {
        "ticker": "2330",
        "exchange_ts": 1620000000000000,
        "bp": [100.0, 99.5],
        "bv": [1, 2],
        "ap": [100.5, 101.0],
        "av": [1, 5],
    }
    event = nm.normalize_bidask(payload)
    assert isinstance(event, BidAskEvent)
    assert event.symbol == "2330"
    assert len(event.bids) == 2
    assert event.bids[0][0] == 1_000_000  # 100.0 * 10000


def test_custom_field_map_old_keys_ignored(symbols_cfg):
    """When using a custom field map, the old Shioaji keys should NOT match."""
    fm = NormalizerFieldMap(symbol_key="ticker")
    nm = MarketDataNormalizer(symbols_cfg, field_map=fm)

    # Payload uses old "code" key — should not resolve symbol
    payload = {"code": "2330", "close": 100.0, "volume": 5}
    event = nm.normalize_tick(payload)
    assert event is None  # symbol not found → None


def test_field_map_is_frozen():
    """NormalizerFieldMap should be immutable."""
    fm = NormalizerFieldMap()
    with pytest.raises(AttributeError):
        fm.symbol_key = "other"  # type: ignore[misc]
