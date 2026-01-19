import tempfile

import pytest

from hft_platform.core.pricing import FixedPriceScaleProvider, PriceCodec
from hft_platform.feed_adapter.normalizer import SymbolMetadata

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False


def _write_symbols(payload: str) -> str:
    tmp_dir = tempfile.mkdtemp(prefix="symbols_scale_")
    path = f"{tmp_dir}/symbols.yaml"
    with open(path, "w") as f:
        f.write(payload)
    return path


def test_symbol_metadata_prefers_price_scale_over_tick_size():
    path = _write_symbols(
        "\n".join(
            [
                "symbols:",
                "  - code: 'AAA'",
                "    exchange: 'TSE'",
                "    price_scale: 100",
                "    tick_size: 0.01",
            ]
        )
        + "\n"
    )
    metadata = SymbolMetadata(path)
    assert metadata.price_scale("AAA") == 100


@pytest.mark.parametrize("tick_size", [0, -1, "bad"])
def test_symbol_metadata_invalid_tick_size_fallback(tick_size):
    path = _write_symbols(
        "\n".join(
            [
                "symbols:",
                "  - code: 'AAA'",
                "    exchange: 'TSE'",
                f"    tick_size: {tick_size}",
            ]
        )
        + "\n"
    )
    metadata = SymbolMetadata(path)
    assert metadata.price_scale("AAA") == metadata.DEFAULT_SCALE


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(max_examples=50)
@given(st.integers(min_value=-1_000_000, max_value=1_000_000))
def test_price_codec_roundtrip_preserves_scaled_value(value):
    codec = PriceCodec(FixedPriceScaleProvider(scale=100))
    roundtrip = codec.scale("AAA", codec.descale("AAA", value))
    # Float rounding can cause off-by-one at scale boundaries; allow 1 tick.
    assert abs(roundtrip - value) <= 1


def test_price_codec_handles_zero_and_negative_values():
    codec = PriceCodec(FixedPriceScaleProvider(scale=100))
    assert codec.scale("AAA", 0.0) == 0
    assert codec.scale("AAA", -1.23) == -123
    assert codec.descale("AAA", -123) == -1.23
