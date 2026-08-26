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

    # Two branches, not one. This asserted "an event always comes back", which
    # is false by design: a tick whose scaled price is not positive is dropped
    # and counted on ``skip_tick_negative_price`` -- garbage must not reach the
    # book. Hypothesis found it immediately with price=0.0.
    expected_price = int(round(float(price) * 100))
    if expected_price <= 0:
        assert event is None
        return

    assert event is not None
    assert event.symbol == "AAA"
    # ``round``, not truncation: the normalizer rounds, and replicating the
    # scaling with ``int(float(p) * 100)`` disagreed with it on every price a
    # binary float represents slightly low (0.29 * 100 == 28.999999999999996).
    # Truncation here would also bias every scaled price downward.
    assert event.price == expected_price
    assert event.volume == int(volume)
