"""Tests for RustRiskValidator — fused PriceBand + MaxNotional in Rust."""

from __future__ import annotations

import pytest

_RustRiskValidator = None


def _get_validator():
    global _RustRiskValidator
    if _RustRiskValidator is None:
        try:
            from hft_platform.rust_core import RustRiskValidator  # type: ignore[attr-defined]
        except ImportError:
            try:
                from rust_core import RustRiskValidator  # type: ignore[assignment]
            except ImportError:
                pytest.skip("rust_core not available")
        _RustRiskValidator = RustRiskValidator
    return _RustRiskValidator


# Intent types: 0=NEW, 1=MODIFY, 2=CANCEL
NEW = 0
MODIFY = 1
CANCEL = 2


class TestRustRiskValidator:
    def _make(
        self,
        max_price_cap=50_000_000,  # 5000 * 10000
        tick_size=100,             # 0.01 * 10000
        band_ticks=20,
        max_notional=100_000_000_000,  # 10M * 10000
    ):
        cls = _get_validator()
        return cls(max_price_cap, tick_size, band_ticks, max_notional)

    def test_cancel_always_passes(self):
        v = self._make()
        ok, code = v.check(CANCEL, 0, 0, "s1", "2330", 0)
        assert ok is True
        assert code == 0

    def test_price_zero_rejected(self):
        v = self._make()
        ok, code = v.check(NEW, 0, 10, "s1", "2330", 0)
        assert ok is False
        assert code == v.PRICE_ZERO_OR_NEG

    def test_negative_price_rejected(self):
        v = self._make()
        ok, code = v.check(NEW, -100, 10, "s1", "2330", 0)
        assert ok is False
        assert code == v.PRICE_ZERO_OR_NEG

    def test_price_exceeds_cap(self):
        v = self._make(max_price_cap=1_000_000)
        ok, code = v.check(NEW, 2_000_000, 1, "s1", "2330", 0)
        assert ok is False
        assert code == v.PRICE_EXCEEDS_CAP

    def test_price_at_cap_passes(self):
        v = self._make(max_price_cap=1_000_000)
        ok, code = v.check(NEW, 1_000_000, 1, "s1", "2330", 0)
        assert ok is True

    def test_price_band_within(self):
        v = self._make(tick_size=100, band_ticks=20)
        # mid=1_000_000, band=20*100=2000, range=[998000, 1002000]
        ok, code = v.check(NEW, 999_000, 1, "s1", "2330", 1_000_000)
        assert ok is True

    def test_price_band_outside_low(self):
        v = self._make(tick_size=100, band_ticks=20)
        # mid=1_000_000, band=2000, lower=998000
        ok, code = v.check(NEW, 990_000, 1, "s1", "2330", 1_000_000)
        assert ok is False
        assert code == v.PRICE_OUTSIDE_BAND

    def test_price_band_outside_high(self):
        v = self._make(tick_size=100, band_ticks=20)
        ok, code = v.check(NEW, 1_010_000, 1, "s1", "2330", 1_000_000)
        assert ok is False
        assert code == v.PRICE_OUTSIDE_BAND

    def test_price_band_skipped_when_no_mid(self):
        v = self._make(tick_size=100, band_ticks=20)
        # mid_price=0 means no LOB data, skip band check
        ok, code = v.check(NEW, 999_000, 1, "s1", "2330", 0)
        assert ok is True

    def test_max_notional_exceeded(self):
        v = self._make(max_notional=1_000_000)
        # price=1000, qty=1001 → notional=1_001_000 > 1_000_000
        ok, code = v.check(NEW, 1000, 1001, "s1", "2330", 0)
        assert ok is False
        assert code == v.MAX_NOTIONAL_EXCEEDED

    def test_max_notional_within(self):
        v = self._make(max_notional=1_000_000)
        ok, code = v.check(NEW, 1000, 999, "s1", "2330", 0)
        assert ok is True

    def test_per_strategy_band_ticks(self):
        v = self._make(tick_size=100, band_ticks=20)
        v.set_band_ticks("tight_strat", 5)
        # mid=1_000_000, tight band=5*100=500, range=[999500, 1000500]
        ok, code = v.check(NEW, 999_000, 1, "tight_strat", "2330", 1_000_000)
        assert ok is False  # 999_000 < 999_500

    def test_per_strategy_symbol_notional(self):
        v = self._make(max_notional=100_000_000)
        v.set_max_notional("s1", "2330", 500_000)
        ok, code = v.check(NEW, 1000, 501, "s1", "2330", 0)
        assert ok is False  # 501_000 > 500_000

    def test_modify_also_checked(self):
        v = self._make()
        ok, code = v.check(MODIFY, 0, 10, "s1", "2330", 0)
        assert ok is False  # price=0

    def test_reason_str(self):
        cls = _get_validator()
        assert cls.reason_str(0) == "OK"
        assert cls.reason_str(1) == "PRICE_ZERO_OR_NEG"
        assert cls.reason_str(2) == "PRICE_EXCEEDS_CAP"
        assert cls.reason_str(3) == "PRICE_OUTSIDE_BAND"
        assert cls.reason_str(4) == "MAX_NOTIONAL_EXCEEDED"

    def test_reset_clears_caches(self):
        v = self._make(max_notional=100_000_000)
        v.set_band_ticks("s1", 5)
        v.set_max_notional("s1", "2330", 500_000)
        v.reset()
        # After reset, should use defaults again
        ok, code = v.check(NEW, 1000, 501, "s1", "2330", 0)
        assert ok is True  # 501_000 < default 100_000_000

    def test_parity_with_python_price_band(self):
        """Rust check should match Python PriceBandValidator logic."""
        v = self._make(
            max_price_cap=50_000_000,  # 5000 * 10000
            tick_size=100,              # 0.01 * 10000
            band_ticks=20,
        )
        mid = 1_500_000  # mid_price scaled
        band_width = 20 * 100  # 2000
        # Price exactly at lower bound
        ok, _ = v.check(NEW, mid - band_width, 1, "s1", "2330", mid)
        assert ok is True
        # Price one below lower bound
        ok, code = v.check(NEW, mid - band_width - 1, 1, "s1", "2330", mid)
        assert ok is False
        assert code == v.PRICE_OUTSIDE_BAND
