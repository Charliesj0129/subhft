from unittest.mock import MagicMock

import numpy as np
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
    # Minimum payload without close field — treated as zero/missing price, returns None (H4 fix)
    payload = {"code": "2330"}
    event = normalizer.normalize_tick(payload)

    assert event is None


def test_normalize_tick_invalid_symbol(normalizer):
    event = normalizer.normalize_tick({})
    assert event is None


def test_normalize_tick_string_values(normalizer):
    # Strings instead of numbers
    payload = {"code": "2330", "close": "100.5", "volume": "10"}
    event = normalizer.normalize_tick(payload)

    assert event.price == 1005000
    assert event.volume == 10


@pytest.mark.parametrize(
    "price_float,expected_scaled",
    [
        (100.15, 1001500),  # IEEE 754: float(100.15)*10000 = 1001499.999...
        (100.05, 1000500),  # float(100.05)*10000 = 1000499.999...
        (99.95, 999500),  # float(99.95)*10000  = 999499.999...
        (0.1, 1000),  # float(0.1)*10000 = 999.999...
        (100.0, 1000000),  # exact
        (500.5, 5005000),  # exact
    ],
)
def test_normalize_tick_ieee754_precision(normalizer, price_float, expected_scaled):
    """Verify round() prevents IEEE 754 truncation in Python fallback path."""
    payload = {"code": "2330", "close": price_float, "volume": 1}
    event = normalizer.normalize_tick(payload)
    assert event.price == expected_scaled, (
        f"float({price_float})*10000 = {float(price_float) * 10000}, "
        f"expected scaled {expected_scaled}, got {event.price}"
    )


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


# --- Scalar timestamp tracking tests ---


def test_scalar_ts_slots_initialized(normalizer):
    """Scalar timestamp slots should start at 0."""
    assert normalizer._last_local_ts_tick == 0
    assert normalizer._last_local_ts_bidask == 0
    assert normalizer._last_local_ts_snapshot == 0


def test_scalar_ts_tick_updated(normalizer):
    """After normalize_tick, _last_local_ts_tick should be updated."""
    payload = {"code": "2330", "close": 100.0, "volume": 1, "ts": 1620000000000000}
    normalizer.normalize_tick(payload)
    assert normalizer._last_local_ts_tick > 0
    # bidask/snapshot should remain 0
    assert normalizer._last_local_ts_bidask == 0
    assert normalizer._last_local_ts_snapshot == 0


def test_scalar_ts_bidask_updated(normalizer):
    """After normalize_bidask, _last_local_ts_bidask should be updated."""
    payload = {
        "code": "2330",
        "ts": 1620000000000000,
        "bid_price": [100.0],
        "bid_volume": [1],
        "ask_price": [101.0],
        "ask_volume": [1],
    }
    normalizer.normalize_bidask(payload)
    assert normalizer._last_local_ts_bidask > 0
    assert normalizer._last_local_ts_tick == 0


def test_scalar_ts_snapshot_updated(normalizer):
    """After normalize_snapshot, _last_local_ts_snapshot should be updated."""
    payload = {
        "code": "2330",
        "buy_price": 100.0,
        "buy_volume": 1,
        "sell_price": 101.0,
        "sell_volume": 1,
    }
    normalizer.normalize_snapshot(payload)
    assert normalizer._last_local_ts_snapshot > 0


def test_no_last_local_ts_ns_dict(normalizer):
    """Verify the old dict attribute does not exist."""
    assert not hasattr(normalizer, "_last_local_ts_ns")


# --- Fused path tests ---


def test_fused_path_selected_when_enabled(tmp_path):
    """When _fused is set, normalize_bidask should use fused path and set fused_stats."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    nm = MarketDataNormalizer(str(cfg))

    # Create a mock fused instance
    mock_fused = MagicMock()
    bids_np = np.array([[1000000, 10]], dtype=np.int64)
    asks_np = np.array([[1005000, 20]], dtype=np.int64)
    mock_fused.process_bidask.return_value = (
        bids_np,  # bids_np
        asks_np,  # asks_np
        1000000,  # best_bid
        1005000,  # best_ask
        10,  # bid_depth
        20,  # ask_depth
        2005000,  # mid_x2 (best_bid + best_ask)
        5000,  # spread_scaled
        -333333,  # imbalance_ppm
        1,  # version
        -0.333333,  # top_imbalance
    )
    nm._fused = mock_fused

    payload = {
        "code": "2330",
        "ts": 1620000000000000,
        "bid_price": [100.0],
        "bid_volume": [10],
        "ask_price": [100.5],
        "ask_volume": [20],
    }
    event = nm.normalize_bidask(payload)

    assert isinstance(event, BidAskEvent)
    assert event.symbol == "2330"
    assert event.fused_stats is not None
    assert event.fused_stats[0] == 1000000  # best_bid
    assert event.fused_stats[1] == 1005000  # best_ask
    assert event.fused_stats[4] == 2005000  # mid_x2
    assert event.fused_stats[5] == 5000  # spread_scaled
    # stats (compat) should have float mid_price
    assert event.stats is not None
    assert event.stats[4] == pytest.approx(1002500.0)  # mid_x2 / 2
    mock_fused.process_bidask.assert_called_once()


def test_fused_path_fallback_on_error(tmp_path):
    """When fused.process_bidask raises, should fall through to standard path."""
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    nm = MarketDataNormalizer(str(cfg))

    mock_fused = MagicMock()
    mock_fused.process_bidask.side_effect = RuntimeError("fused error")
    nm._fused = mock_fused

    payload = {
        "code": "2330",
        "ts": 1620000000000000,
        "bid_price": [100.0],
        "bid_volume": [10],
        "ask_price": [100.5],
        "ask_volume": [20],
    }
    event = nm.normalize_bidask(payload)

    # Should still produce a valid event via standard path
    assert isinstance(event, BidAskEvent)
    assert event.symbol == "2330"
    assert event.fused_stats is None  # Standard path does not set fused_stats


def test_fused_path_not_used_when_disabled(normalizer):
    """Default normalizer (no env var) should not have _fused set."""
    assert normalizer._fused is None
    payload = {
        "code": "2330",
        "ts": 1620000000000000,
        "bid_price": [100.0],
        "bid_volume": [10],
        "ask_price": [100.5],
        "ask_volume": [20],
    }
    event = normalizer.normalize_bidask(payload)
    assert isinstance(event, BidAskEvent)
    assert event.fused_stats is None


# --- normalization_skip_total counter tests ---


class TestNormalizationSkipCounter:
    """Verify normalization_skip_total increments for silent None returns."""

    def test_tick_missing_symbol_increments_counter(self, normalizer):
        child = normalizer._skip_tick_missing_symbol
        assert child is not None, "metrics should be available in test"
        before = child._value.get()
        result = normalizer.normalize_tick({})
        assert result is None
        after = child._value.get()
        assert after == before + 1

    def test_tick_zero_price_increments_counter(self, normalizer):
        child = normalizer._skip_tick_negative_price
        before = child._value.get()
        result = normalizer.normalize_tick({"code": "2330", "close": 0})
        assert result is None
        after = child._value.get()
        assert after == before + 1

    def test_tick_negative_price_increments_counter(self, normalizer):
        child = normalizer._skip_tick_negative_price
        before = child._value.get()
        result = normalizer.normalize_tick({"code": "2330", "close": -5.0})
        assert result is None
        after = child._value.get()
        assert after == before + 1

    def test_tick_negative_price_rust_path_increments_counter(self, normalizer, monkeypatch):
        """Rust fast path returns price=0 for a valid symbol -> skip counter increments."""
        import hft_platform.feed_adapter.normalizer as norm_mod

        child = normalizer._skip_tick_negative_price
        assert child is not None

        # Enable Rust path
        monkeypatch.setattr(norm_mod, "_RUST_ENABLED", True)
        # Mock Rust normalize to return tuple with negative price
        rust_result = ("tick", "2330", -1, 100, 100, False, False, 1_000_000)
        monkeypatch.setattr(norm_mod, "_RUST_NORMALIZE_TICK", lambda payload, sym, scale: rust_result)

        before = child._value.get()
        result = normalizer.normalize_tick({"code": "2330", "close": -5.0})
        assert result is None
        after = child._value.get()
        assert after == before + 1

    def test_bidask_missing_symbol_increments_counter(self, normalizer):
        child = normalizer._skip_bidask_missing_symbol
        before = child._value.get()
        result = normalizer.normalize_bidask({})
        assert result is None
        after = child._value.get()
        assert after == before + 1

    def test_snapshot_missing_symbol_increments_counter(self, normalizer):
        child = normalizer._skip_snapshot_missing_symbol
        before = child._value.get()
        result = normalizer.normalize_snapshot({})
        assert result is None
        after = child._value.get()
        assert after == before + 1
