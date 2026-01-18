import pytest

from hft_platform.feed_adapter.lob_engine import LOBEngine


@pytest.fixture
def engine():
    return LOBEngine()


def test_snapshot_application(engine):
    snapshot = {
        "type": "Snapshot",
        "symbol": "2330",
        "exch_ts": 1000,
        "bids": [{"price": 5000000, "volume": 10}],  # 500.00
        "asks": [{"price": 5010000, "volume": 20}],  # 501.00
    }

    engine.apply_snapshot(snapshot)
    stats = engine.process_event(snapshot)  # Re-applies via process_event path too, ensuring consistency

    assert stats["symbol"] == "2330"
    assert stats["best_bid"] == 5000000
    assert stats["best_ask"] == 5010000
    assert stats["mid_price"] == 5005000.0
    assert stats["spread"] == 10000.0
    assert stats["bid_depth"] == 10
    assert stats["ask_depth"] == 20

    # Imbalance: (10 - 20) / (10 + 20) = -10 / 30 = -0.333
    assert stats["imbalance"] == pytest.approx(-0.3333333)


def test_incremental_update(engine):
    # Initialize
    engine.apply_snapshot(
        {
            "symbol": "2330",
            "exch_ts": 1000,
            "bids": [{"price": 100, "volume": 10}],
            "asks": [{"price": 102, "volume": 10}],
        }
    )

    # Update Bid
    event = {
        "type": "BidAsk",
        "symbol": "2330",
        "exch_ts": 1001,
        "bids": [{"price": 101, "volume": 5}],  # Tighten spread
        "asks": [{"price": 102, "volume": 10}],
    }

    stats = engine.process_event(event)

    assert stats["best_bid"] == 101
    assert stats["mid_price"] == 101.5
    assert stats["spread"] == 1.0
    assert stats["bid_depth"] == 5


def test_tick_update(engine):
    engine.apply_snapshot(
        {
            "symbol": "2330",
            "exch_ts": 1000,
            "bids": [{"price": 100, "volume": 10}],
            "asks": [{"price": 102, "volume": 10}],
        }
    )

    tick = {"type": "Tick", "symbol": "2330", "exch_ts": 1005, "price": 101, "volume": 2}

    stats = engine.process_event(tick)

    assert stats["last_price"] == 101
    # Tick shouldn't change LOB levels in this model
    assert stats["best_bid"] == 100


def test_get_features(engine):
    engine.apply_snapshot(
        {
            "symbol": "2330",
            "exch_ts": 1000,
            "bids": [{"price": 100, "volume": 100}],
            "asks": [{"price": 101, "volume": 100}],
        }
    )

    features = engine.get_features("2330")
    assert features["mid_price"] == 100.5
    assert features["imbalance"] == 0.0


def test_missing_symbol(engine):
    assert engine.process_event({"symbol": ""}) is None
    # engine.get_features creates default book
    f = engine.get_features("UNKNOWN")
    assert f["mid_price"] == 0.0
