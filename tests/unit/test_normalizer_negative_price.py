"""Tests for negative/zero price filtering in normalize_tick."""

import pytest

from hft_platform.events import TickEvent
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer


@pytest.fixture
def normalizer(tmp_path):
    cfg = tmp_path / "test_symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return MarketDataNormalizer(str(cfg))


def _tick_payload(close, volume: int = 10) -> dict:
    return {
        "code": "2330",
        "close": close,
        "volume": volume,
        "total_volume": volume,
        "ts": 1620000000000000,
        "simtrade": 0,
        "intraday_odd": 0,
    }


def test_negative_close_returns_none(normalizer):
    """Shioaji sends close=-1 as 'no data'; must be filtered before LOBEngine."""
    result = normalizer.normalize_tick(_tick_payload(close=-1))
    assert result is None


def test_zero_close_returns_none(normalizer):
    """Zero close price is invalid and must be filtered."""
    result = normalizer.normalize_tick(_tick_payload(close=0))
    assert result is None


def test_positive_close_returns_tick(normalizer):
    """Valid positive price must pass through and be scaled correctly."""
    result = normalizer.normalize_tick(_tick_payload(close=100.5))
    assert isinstance(result, TickEvent)
    assert result.price == 1005000  # 100.5 * 10000
    assert result.symbol == "2330"


def test_negative_close_rust_path_returns_none(normalizer):
    """Rust fast path must also filter negative prices."""
    try:
        import hft_platform.rust_core as rc  # noqa: F401

        rust_available = True
    except ImportError:
        rust_available = False

    if not rust_available:
        pytest.skip("rust_core not available")

    # Force the Rust path by ensuring _RUST_ENABLED is True; the fixture uses a real
    # normalizer so the Rust path will be attempted naturally.  A close of -1 scaled
    # by 10000 yields -10000; the guard must catch it before a TickEvent is returned.
    result = normalizer.normalize_tick(_tick_payload(close=-1))
    assert result is None
