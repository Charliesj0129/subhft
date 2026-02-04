import math

import numpy as np
import pytest


try:
    from hft_platform import rust_core as _rust_core
except Exception:
    try:
        import rust_core as _rust_core
    except Exception:
        _rust_core = None


def _python_get_field(payload, keys):
    for key in keys:
        if isinstance(payload, dict):
            value = payload.get(key)
        else:
            value = getattr(payload, key, None)
        if value is not None:
            return value
    return None


@pytest.mark.skipif(_rust_core is None, reason="rust_core extension not available")
def test_get_field_parity_dict_and_object():
    class Payload:
        def __init__(self):
            self.Code = "ABC"
            self.close = None
            self.volume = 42

    payload_dict = {"code": "XYZ", "close": 12.5, "volume": None}
    payload_obj = Payload()

    keys = ["code", "Code"]
    assert _rust_core.get_field(payload_dict, keys) == _python_get_field(payload_dict, keys)
    assert _rust_core.get_field(payload_obj, keys) == _python_get_field(payload_obj, keys)

    keys = ["close", "Close"]
    assert _rust_core.get_field(payload_dict, keys) == _python_get_field(payload_dict, keys)
    assert _rust_core.get_field(payload_obj, keys) == _python_get_field(payload_obj, keys)

    keys = ["volume", "Volume"]
    assert _rust_core.get_field(payload_dict, keys) == _python_get_field(payload_dict, keys)
    assert _rust_core.get_field(payload_obj, keys) == _python_get_field(payload_obj, keys)


@pytest.mark.skipif(_rust_core is None, reason="rust_core extension not available")
def test_scale_book_seq_parity():
    prices = [100.0, 0.0, 99.5, 99.0]
    vols = [10, 5, 8, 6]
    scale = 100
    expected = np.array(
        [[int(100.0 * scale), 10], [int(99.5 * scale), 8], [int(99.0 * scale), 6]], dtype=np.int64
    )
    result = _rust_core.scale_book_seq(prices, vols, scale)
    np.testing.assert_array_equal(result, expected)

@pytest.mark.skipif(_rust_core is None or not hasattr(_rust_core, "scale_book_pair"), reason="rust_core scale_book_pair not available")
def test_scale_book_pair_parity():
    bid_prices = [100.0, 99.5, 0.0]
    bid_vols = [10, 8, 7]
    ask_prices = [100.5, 101.0, 0.0]
    ask_vols = [9, 7, 5]
    scale = 100

    bids_expected = np.array([[10000, 10], [9950, 8]], dtype=np.int64)
    asks_expected = np.array([[10050, 9], [10100, 7]], dtype=np.int64)

    bids, asks = _rust_core.scale_book_pair(bid_prices, bid_vols, ask_prices, ask_vols, scale)
    np.testing.assert_array_equal(bids, bids_expected)
    np.testing.assert_array_equal(asks, asks_expected)


@pytest.mark.skipif(_rust_core is None, reason="rust_core extension not available")
def test_compute_book_stats_parity():
    bids = np.array([[10000, 10], [9900, 5]], dtype=np.int64)
    asks = np.array([[10100, 7], [10200, 3]], dtype=np.int64)

    best_bid = 10000
    best_ask = 10100
    bid_depth_total = 15
    ask_depth_total = 10
    mid_price = (best_bid + best_ask) / 2.0
    spread = best_ask - best_bid
    imbalance = (10 - 7) / (10 + 7)

    result = _rust_core.compute_book_stats(bids, asks)
    assert result[0] == best_bid
    assert result[1] == best_ask
    assert result[2] == bid_depth_total
    assert result[3] == ask_depth_total
    assert math.isclose(result[4], mid_price, rel_tol=1e-12)
    assert math.isclose(result[5], spread, rel_tol=1e-12)
    assert math.isclose(result[6], imbalance, rel_tol=1e-12)
