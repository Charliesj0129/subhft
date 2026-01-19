import pytest

from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    price=st.floats(min_value=0, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    volume=st.integers(min_value=0, max_value=1_000_000),
)
def test_normalize_tick_scales(price, volume, tmp_path, monkeypatch):
    symbols_cfg = tmp_path / "symbols.yaml"
    symbols_cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(symbols_cfg))

    normalizer = MarketDataNormalizer()
    payload = {"code": "AAA", "close": price, "volume": volume, "ts": 1}
    event = normalizer.normalize_tick(payload)

    assert event is not None
    assert event.symbol == "AAA"
    assert event.price == int(float(price) * 100)
    assert event.volume == int(volume)
