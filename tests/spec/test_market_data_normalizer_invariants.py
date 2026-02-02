import os
import tempfile

import pytest

from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False


def _config_path():
    tmp_dir = tempfile.mkdtemp(prefix="symbols_")
    path = os.path.join(tmp_dir, "symbols.yaml")
    with open(path, "w") as f:
        f.write("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    return path


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(max_examples=50)
@given(
    bids=st.lists(
        st.tuples(st.integers(min_value=0, max_value=100000), st.integers(min_value=0, max_value=1000)), max_size=5
    ),
    asks=st.lists(
        st.tuples(st.integers(min_value=0, max_value=100000), st.integers(min_value=0, max_value=1000)), max_size=5
    ),
)
def test_bidask_normalization_filters_and_scales(bids, asks):
    cfg = _config_path()
    normalizer = MarketDataNormalizer(cfg)

    bid_price = [p / 100 for p, _ in bids]
    bid_volume = [v for _, v in bids]
    ask_price = [p / 100 for p, _ in asks]
    ask_volume = [v for _, v in asks]

    payload = {
        "code": "AAA",
        "ts": 1,
        "bid_price": bid_price,
        "bid_volume": bid_volume,
        "ask_price": ask_price,
        "ask_volume": ask_volume,
    }

    event = normalizer.normalize_bidask(payload)
    expected_bids = [[int(float(p) * 100), int(v)] for p, v in zip(bid_price, bid_volume) if p > 0]
    expected_asks = [[int(float(p) * 100), int(v)] for p, v in zip(ask_price, ask_volume) if p > 0]

    def _normalize_levels(levels):
        if levels is None:
            return []
        to_list = getattr(levels, "tolist", None)
        if callable(to_list):
            return to_list()
        return [list(level) for level in levels]

    assert _normalize_levels(event.bids) == expected_bids
    assert _normalize_levels(event.asks) == expected_asks
