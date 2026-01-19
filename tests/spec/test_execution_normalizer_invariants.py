import os
import tempfile

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
    price_int=st.integers(min_value=0, max_value=1_000_000),
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
            },
            1,
        )
        event = norm.normalize_fill(raw)
        expected_price = int(float(price_float) * 100)
        assert event.price == expected_price

        expected_side = Side.SELL if action in ("Sell", -1) else Side.BUY
        assert event.side == expected_side
    finally:
        if old is None:
            os.environ.pop("SYMBOLS_CONFIG", None)
        else:
            os.environ["SYMBOLS_CONFIG"] = old
