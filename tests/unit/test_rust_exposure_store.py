"""Tests for RustExposureStore — Rust-backed exposure tracking."""

from __future__ import annotations

import pytest

_RustExposureStore = None


def _get_store():
    global _RustExposureStore
    if _RustExposureStore is None:
        try:
            from hft_platform.rust_core import RustExposureStore  # type: ignore[attr-defined]
        except ImportError:
            try:
                from rust_core import RustExposureStore  # type: ignore[assignment]
            except ImportError:
                pytest.skip("rust_core not available")
        _RustExposureStore = RustExposureStore
    return _RustExposureStore


CANCEL = 2
NEW = 0


class TestRustExposureStore:
    def _make(self, global_max=0, max_symbols=10000):
        cls = _get_store()
        return cls(global_max, max_symbols)

    def test_cancel_always_passes(self):
        s = self._make(global_max=100)
        ok, code = s.check_and_update("acct", "s1", "2330", CANCEL, 0, 0)
        assert ok is True
        assert code == 0

    def test_basic_update(self):
        s = self._make()
        ok, code = s.check_and_update("acct", "s1", "2330", NEW, 1000, 10)
        assert ok is True
        assert s.get_exposure("acct", "s1", "2330") == 10_000

    def test_cumulative_exposure(self):
        s = self._make()
        s.check_and_update("acct", "s1", "2330", NEW, 1000, 10)
        s.check_and_update("acct", "s1", "2330", NEW, 2000, 5)
        assert s.get_exposure("acct", "s1", "2330") == 20_000  # 10k + 10k

    def test_global_limit(self):
        s = self._make(global_max=50_000)
        s.check_and_update("acct", "s1", "2330", NEW, 1000, 40)  # 40k
        ok, code = s.check_and_update("acct", "s1", "2330", NEW, 1000, 20)  # +20k > 50k
        assert ok is False
        assert code == 1  # GLOBAL_EXPOSURE_LIMIT

    def test_strategy_limit(self):
        s = self._make()
        s.set_limit("s1", 100_000)
        s.check_and_update("acct", "s1", "2330", NEW, 1000, 90)  # 90k
        ok, code = s.check_and_update("acct", "s1", "2330", NEW, 1000, 20)  # +20k > 100k
        assert ok is False
        assert code == 2  # STRATEGY_EXPOSURE_LIMIT

    def test_symbol_cardinality_limit(self):
        s = self._make(max_symbols=3)
        s.check_and_update("a", "s1", "SYM1", NEW, 100, 1)
        s.check_and_update("a", "s1", "SYM2", NEW, 100, 1)
        s.check_and_update("a", "s1", "SYM3", NEW, 100, 1)
        ok, code = s.check_and_update("a", "s1", "SYM4", NEW, 100, 1)
        assert ok is False
        assert code == 3  # SYMBOL_LIMIT_REACHED

    def test_symbol_eviction_on_zero(self):
        s = self._make(max_symbols=2)
        s.check_and_update("a", "s1", "SYM1", NEW, 100, 1)
        s.check_and_update("a", "s1", "SYM2", NEW, 100, 1)
        # Release SYM1 to zero
        s.release("a", "s1", "SYM1", NEW, 100, 1)
        # Now SYM1 is at 0, eviction should free a slot
        ok, code = s.check_and_update("a", "s1", "SYM3", NEW, 100, 1)
        assert ok is True

    def test_release_exposure(self):
        s = self._make()
        s.check_and_update("acct", "s1", "2330", NEW, 1000, 10)
        assert s.get_global_notional() == 10_000
        s.release("acct", "s1", "2330", NEW, 1000, 5)
        assert s.get_global_notional() == 5_000
        assert s.get_exposure("acct", "s1", "2330") == 5_000

    def test_release_clamps_to_zero(self):
        s = self._make()
        s.check_and_update("acct", "s1", "2330", NEW, 100, 1)
        s.release("acct", "s1", "2330", NEW, 200, 1)  # release more than held
        assert s.get_exposure("acct", "s1", "2330") == 0
        assert s.get_global_notional() == 0

    def test_release_cancel_is_noop(self):
        s = self._make()
        s.check_and_update("acct", "s1", "2330", NEW, 1000, 10)
        s.release("acct", "s1", "2330", CANCEL, 1000, 10)
        assert s.get_global_notional() == 10_000  # unchanged

    def test_size(self):
        s = self._make()
        assert s.size() == 0
        s.check_and_update("a", "s1", "SYM1", NEW, 100, 1)
        s.check_and_update("a", "s1", "SYM2", NEW, 100, 1)
        assert s.size() == 2

    def test_reason_str(self):
        cls = _get_store()
        assert cls.reason_str(0) == "OK"
        assert cls.reason_str(1) == "GLOBAL_EXPOSURE_LIMIT"
        assert cls.reason_str(2) == "STRATEGY_EXPOSURE_LIMIT"
        assert cls.reason_str(3) == "SYMBOL_LIMIT_REACHED"

    def test_parity_with_python_exposure_store(self):
        """Verify Rust results match Python ExposureStore for basic flow."""
        from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
        from hft_platform.gateway.exposure import ExposureKey, ExposureStore

        py_store = ExposureStore(global_max_notional=500_000, max_symbols=100)
        rs_store = self._make(global_max=500_000, max_symbols=100)

        intent = OrderIntent(
            intent_id=1,
            strategy_id="s1",
            symbol="2330",
            side=Side.BUY,
            price=10000,
            qty=40,
            intent_type=IntentType.NEW,
        )
        key = ExposureKey(account="acct", strategy_id="s1", symbol="2330")

        py_ok, _ = py_store.check_and_update(key, intent)
        rs_ok, _ = rs_store.check_and_update("acct", "s1", "2330", NEW, 10000, 40)
        assert py_ok == rs_ok

        # Second order should hit global limit
        py_ok2, _ = py_store.check_and_update(key, intent)
        rs_ok2, _ = rs_store.check_and_update("acct", "s1", "2330", NEW, 10000, 40)
        assert py_ok2 == rs_ok2 is False
