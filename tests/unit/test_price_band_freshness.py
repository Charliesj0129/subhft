"""Unit tests for PriceBandValidator mid_price freshness check."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.risk.validators import PriceBandValidator

SCALE = 10_000

# A mid_price_x2 value of 2_000_000 means mid_price = 1_000_000 (i.e. $100.00 scaled)
MID_PRICE_X2 = 2_000_000
MID_PRICE = MID_PRICE_X2 // 2  # 1_000_000


def _make_validator(mid_price_max_age_s: float = 10.0) -> PriceBandValidator:
    config = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "price_band_ticks": 20,
            "tick_size": 0.01,
            "mid_price_max_age_s": mid_price_max_age_s,
        },
        "strategies": {},
    }
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        validator = PriceBandValidator(config)
    return validator


def _make_lob_with_book(exch_ts: int) -> MagicMock:
    """Return a mock LOB that provides a get_book() result with the given exch_ts."""
    lob = MagicMock()
    book_state = SimpleNamespace(exch_ts=exch_ts)
    lob.get_book.return_value = book_state

    # l1 tuple: index 3 = mid_price_x2
    l1 = (None, None, None, MID_PRICE_X2)
    lob.get_l1_scaled.return_value = l1
    return lob


class TestMidPriceFreshness:
    def setup_method(self):
        self._now_ns_patcher = patch("hft_platform.core.timebase.now_ns")
        self.mock_now = self._now_ns_patcher.start()

    def teardown_method(self):
        self._now_ns_patcher.stop()

    def test_fresh_mid_price_returned(self):
        """When book is fresh (within max age), mid_price is returned normally."""
        now = 1_000_000_000_000  # arbitrary ns
        age_ns = 5 * 1_000_000_000  # 5 seconds — within 10s limit
        exch_ts = now - age_ns
        self.mock_now.return_value = now

        validator = _make_validator(mid_price_max_age_s=10.0)
        validator.lob = _make_lob_with_book(exch_ts=exch_ts)

        result = validator._get_mid_price("TEST")

        assert result == MID_PRICE

    def test_stale_mid_price_returns_none(self):
        """When book is stale (older than max age), mid_price is rejected — returns None."""
        now = 1_000_000_000_000
        age_ns = 15 * 1_000_000_000  # 15 seconds — exceeds 10s limit
        exch_ts = now - age_ns
        self.mock_now.return_value = now

        validator = _make_validator(mid_price_max_age_s=10.0)
        validator.lob = _make_lob_with_book(exch_ts=exch_ts)

        result = validator._get_mid_price("TEST")

        assert result is None

    def test_no_exch_ts_returns_mid_price(self):
        """When book_state has no exch_ts attribute, mid_price is still returned (backward compat)."""
        now = 1_000_000_000_000
        self.mock_now.return_value = now

        lob = MagicMock()
        # get_book returns object without exch_ts
        book_state = SimpleNamespace()  # no exch_ts attribute
        lob.get_book.return_value = book_state

        l1 = (None, None, None, MID_PRICE_X2)
        lob.get_l1_scaled.return_value = l1

        validator = _make_validator(mid_price_max_age_s=10.0)
        validator.lob = lob

        result = validator._get_mid_price("TEST")

        assert result == MID_PRICE

    def test_no_get_book_method_returns_mid_price(self):
        """When LOB lacks get_book method, no freshness check runs — mid_price returned."""
        now = 1_000_000_000_000
        self.mock_now.return_value = now

        lob = MagicMock(spec=["get_l1_scaled"])  # no get_book
        l1 = (None, None, None, MID_PRICE_X2)
        lob.get_l1_scaled.return_value = l1

        validator = _make_validator(mid_price_max_age_s=10.0)
        validator.lob = lob

        result = validator._get_mid_price("TEST")

        assert result == MID_PRICE

    def test_zero_exch_ts_skips_freshness_check(self):
        """When book_state.exch_ts == 0, freshness check is skipped and mid_price returned."""
        now = 1_000_000_000_000
        self.mock_now.return_value = now

        validator = _make_validator(mid_price_max_age_s=10.0)
        validator.lob = _make_lob_with_book(exch_ts=0)

        result = validator._get_mid_price("TEST")

        assert result == MID_PRICE

    def test_custom_max_age_respected(self):
        """Custom mid_price_max_age_s config value is respected."""
        now = 1_000_000_000_000
        age_ns = 3 * 1_000_000_000  # 3 seconds
        exch_ts = now - age_ns
        self.mock_now.return_value = now

        # With 2s max age, a 3s-old book should be stale
        validator = _make_validator(mid_price_max_age_s=2.0)
        validator.lob = _make_lob_with_book(exch_ts=exch_ts)

        result = validator._get_mid_price("TEST")

        assert result is None
