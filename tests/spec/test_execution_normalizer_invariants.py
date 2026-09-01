import os
import tempfile
from decimal import Decimal

import pytest

from hft_platform.contracts.execution import Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False


def _symbols_config():
    tmp_dir = tempfile.mkdtemp(prefix="symbols_")
    path = os.path.join(tmp_dir, "symbols.yaml")
    with open(path, "w") as f:
        f.write("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    return path


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(max_examples=50)
@given(
    # min_value=1: this property is about scaling and side mapping, and a fill
    # at price 0 is not a fill. NOTE the asymmetry it was papering over --
    # MarketDataNormalizer *drops* a non-positive tick price and counts it on
    # ``skip_tick_negative_price``, while ExecutionNormalizer accepts a zero
    # price and emits a FillEvent with price=0. Not settled here either way.
    price_int=st.integers(min_value=1, max_value=1_000_000),
    action=st.sampled_from(["Buy", "Sell", -1, 1]),
)
def test_fill_normalization_scales_and_sides(price_int, action):
    cfg = _symbols_config()
    old = os.environ.get("SYMBOLS_CONFIG")
    os.environ["SYMBOLS_CONFIG"] = cfg

    try:
        price_float = price_int / 100.0
        norm = ExecutionNormalizer()
        raw = RawExecEvent(
            "deal",
            {
                "seq_no": "F1",
                "ord_no": "O1",
                "code": "AAA",
                "action": action,
                "quantity": 1,
                "price": price_float,
                "ts": 1,
                # Required: a fill with no account attribution is rejected
                # outright (``fill_rejected_missing_account_id``, CRITICAL).
                "account": "ACC-TEST",
            },
            1,
        )
        event = norm.normalize_fill(raw)
        # Use Decimal for expected_price calculation to match normalizer's precision
        expected_price = int(Decimal(str(price_float)) * 100)
        assert event is not None
        assert event.price == expected_price

        expected_side = Side.SELL if action in ("Sell", -1) else Side.BUY
        assert event.side == expected_side
    finally:
        if old is None:
            os.environ.pop("SYMBOLS_CONFIG", None)
        else:
            os.environ["SYMBOLS_CONFIG"] = old
