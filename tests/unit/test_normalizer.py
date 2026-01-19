import pytest

from hft_platform.events import BidAskEvent, TickEvent
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer


@pytest.fixture
def normalizer(tmp_path):
    # Mock config loading inside SymbolMetadata
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")

    nm = MarketDataNormalizer(str(cfg))
    return nm


def test_normalize_tick_success(normalizer):
    # Standard Shioaji Tick
    payload = {
        "code": "2330",
        "close": 100.0,
        "volume": 5,
        "total_volume": 1000,
        "ts": 1620000000000000,
        "simtrade": 0,
        "intraday_odd": 0,
    }

    event = normalizer.normalize_tick(payload)

    assert isinstance(event, TickEvent)
    assert event.symbol == "2330"
    assert event.price == 1000000  # 100.0 * 10000
    assert event.volume == 5
    assert event.meta.topic == "tick"


def test_normalize_tick_missing_fields(normalizer):
    # Minimum payload
    payload = {"code": "2330"}
    event = normalizer.normalize_tick(payload)

    assert event.symbol == "2330"
    assert event.price == 0


def test_normalize_tick_invalid_symbol(normalizer):
    event = normalizer.normalize_tick({})
    assert event is None


def test_normalize_tick_string_values(normalizer):
    # Strings instead of numbers
    payload = {"code": "2330", "close": "100.5", "volume": "10"}
    event = normalizer.normalize_tick(payload)

    assert event.price == 1005000
    assert event.volume == 10


def test_normalize_bidask_success(normalizer):
    payload = {
        "code": "2330",
        "ts": 1620000000000000,
        "bid_price": [100.0, 99.5, 99.0, 0, 0],
        "bid_volume": [1, 2, 3, 0, 0],
        "ask_price": [100.5, 101.0, 0, 0, 0],
        "ask_volume": [1, 5, 0, 0, 0],
    }

    event = normalizer.normalize_bidask(payload)

    assert isinstance(event, BidAskEvent)
    assert event.symbol == "2330"
    # List check
    # Bids (100.0 * 10000 -> 1000000)
    assert len(event.bids) == 3  # Zeros filtered?
    assert event.bids[0][0] == 1000000
    assert event.bids[0][1] == 1

    # Asks
    assert len(event.asks) == 2
    assert event.asks[0][0] == 1005000


def test_normalize_bidask_empty_arrays(normalizer):
    payload = {"code": "2330", "bid_price": [], "ask_price": []}
    event = normalizer.normalize_bidask(payload)
    assert len(event.bids) == 0
    assert len(event.asks) == 0


def test_normalize_snapshot_alias(normalizer):
    # normalize_snapshot falls back to bidask and marks snapshot
    payload = {"code": "2330", "bid_price": [100.0], "bid_volume": [1], "ask_price": [101.0], "ask_volume": [1]}
    event = normalizer.normalize_snapshot(payload)
    assert isinstance(event, BidAskEvent)
    assert event.is_snapshot is True


def test_normalize_snapshot_bbo(normalizer):
    payload = {
        "code": "2330",
        "buy_price": 100.0,
        "buy_volume": 2,
        "sell_price": 101.0,
        "sell_volume": 3,
    }
    event = normalizer.normalize_snapshot(payload)

    assert isinstance(event, BidAskEvent)
    assert event.is_snapshot is True
    assert event.bids[0][0] == 1000000
    assert event.bids[0][1] == 2
    assert event.asks[0][0] == 1010000
    assert event.asks[0][1] == 3


def test_normalize_snapshot_partial_bbo(normalizer):
    payload = {
        "code": "2330",
        "buy_price": 0,
        "buy_volume": 2,
        "sell_price": 101.0,
        "sell_volume": 3,
    }
    event = normalizer.normalize_snapshot(payload)

    assert isinstance(event, BidAskEvent)
    assert event.is_snapshot is True
    assert event.bids == []
    assert event.asks[0][0] == 1010000
