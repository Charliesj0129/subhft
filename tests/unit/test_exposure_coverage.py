"""Coverage gap tests for gateway/exposure.py.

Targets uncovered branches: AMEND check/rollback, per-order lifecycle
release, typed fast-path, cardinality bound with eviction, expire_stale_orders,
release_exposure_typed paths, and global_notional property.
"""

from __future__ import annotations

import time

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.gateway.exposure import ExposureKey, ExposureLimitError, ExposureLimits, ExposureStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _key(acct="acc1", strat="s1", sym="2330"):
    return ExposureKey(account=acct, strategy_id=strat, symbol=sym)


def _intent(**kwargs):
    defaults = dict(
        intent_id=1,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=1000,
        qty=10,
        tif=TIF.ROD,
    )
    defaults.update(kwargs)
    return OrderIntent(**defaults)


# ---------------------------------------------------------------------------
# Basic check_and_update
# ---------------------------------------------------------------------------


class TestCheckAndUpdate:
    def test_new_order_approved(self):
        store = ExposureStore()
        ok, reason = store.check_and_update(_key(), _intent())
        assert ok is True
        assert reason == "OK"

    def test_cancel_always_approved(self):
        store = ExposureStore()
        intent = _intent(intent_type=IntentType.CANCEL)
        ok, reason = store.check_and_update(_key(), intent)
        assert ok is True

    def test_force_flat_always_approved(self):
        store = ExposureStore()
        intent = _intent(intent_type=IntentType.FORCE_FLAT)
        ok, reason = store.check_and_update(_key(), intent)
        assert ok is True

    def test_global_limit_exceeded(self):
        store = ExposureStore(global_max_notional=5000)
        intent = _intent(price=1000, qty=10)  # notional=10000 > 5000
        ok, reason = store.check_and_update(_key(), intent)
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_strategy_limit_exceeded(self):
        limits = {"s1": ExposureLimits(max_notional_scaled=5000)}
        store = ExposureStore(limits=limits)
        intent = _intent(price=1000, qty=10)  # notional=10000 > 5000
        ok, reason = store.check_and_update(_key(), intent)
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"

    def test_symbol_cardinality_limit(self):
        store = ExposureStore(max_symbols=1)
        store.check_and_update(_key(sym="A"), _intent(symbol="A"))
        with pytest.raises(ExposureLimitError):
            store.check_and_update(_key(sym="B"), _intent(symbol="B"))

    def test_symbol_cardinality_eviction(self):
        """Zero-balance entries are evicted before rejecting."""
        store = ExposureStore(max_symbols=1)
        store.check_and_update(_key(sym="A"), _intent(symbol="A", price=100, qty=1))
        # Release A to zero
        store.release_exposure(_key(sym="A"), _intent(symbol="A", price=100, qty=1))
        # Now B should succeed after eviction of zeroed A
        ok, reason = store.check_and_update(_key(sym="B"), _intent(symbol="B"))
        assert ok is True

    def test_per_order_tracking(self):
        store = ExposureStore()
        ok, _ = store.check_and_update(_key(), _intent(price=100, qty=5), order_key="ord1")
        assert ok is True
        # Order should be tracked
        assert "ord1" in store._order_notionals
        assert store._order_notionals["ord1"] == 500


# ---------------------------------------------------------------------------
# AMEND
# ---------------------------------------------------------------------------


class TestAmend:
    def test_amend_increases_exposure(self):
        store = ExposureStore()
        store.check_and_update(_key(), _intent(price=100, qty=5), order_key="ord1")
        # AMEND: increase to price=100, qty=10
        amend = _intent(
            intent_type=IntentType.AMEND,
            price=100,
            qty=10,
            target_order_id="ord1",
        )
        ok, reason = store.check_and_update(_key(), amend)
        assert ok is True
        assert store._order_notionals["ord1"] == 1000  # Updated

    def test_amend_decreases_exposure(self):
        store = ExposureStore()
        store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
        amend = _intent(
            intent_type=IntentType.AMEND,
            price=100,
            qty=5,
            target_order_id="ord1",
        )
        ok, reason = store.check_and_update(_key(), amend)
        assert ok is True

    def test_amend_global_limit_exceeded(self):
        store = ExposureStore(global_max_notional=1500)
        store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
        amend = _intent(
            intent_type=IntentType.AMEND,
            price=100,
            qty=20,
            target_order_id="ord1",
        )
        ok, reason = store.check_and_update(_key(), amend)
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_amend_strategy_limit_exceeded(self):
        limits = {"s1": ExposureLimits(max_notional_scaled=1500)}
        store = ExposureStore(limits=limits)
        store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
        amend = _intent(
            intent_type=IntentType.AMEND,
            price=100,
            qty=20,
            target_order_id="ord1",
        )
        ok, reason = store.check_and_update(_key(), amend)
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"


# ---------------------------------------------------------------------------
# release_exposure
# ---------------------------------------------------------------------------


class TestReleaseExposure:
    def test_release_cancel_noop(self):
        store = ExposureStore()
        store.release_exposure(_key(), _intent(intent_type=IntentType.CANCEL))
        # No crash

    def test_release_amend_rollback(self):
        store = ExposureStore()
        store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
        amend = _intent(intent_type=IntentType.AMEND, price=100, qty=15, target_order_id="ord1")
        store.check_and_update(_key(), amend)
        # Rollback the AMEND
        store.release_exposure(_key(), amend)
        # Delta should be rolled back

    def test_release_new_by_order(self):
        store = ExposureStore()
        store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
        store.release_exposure(_key(), _intent(price=100, qty=10, idempotency_key="ord1"), order_key="ord1")
        assert store._global_notional == 0

    def test_release_new_legacy_fallback(self):
        store = ExposureStore()
        store.check_and_update(_key(), _intent(price=100, qty=10))
        store.release_exposure(_key(), _intent(price=100, qty=10))
        assert store.get_global_notional() == 0


# ---------------------------------------------------------------------------
# release_by_order
# ---------------------------------------------------------------------------


def test_release_by_order():
    store = ExposureStore()
    store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
    released = store.release_by_order("ord1")
    assert released == 1000
    assert store.get_global_notional() == 0


def test_release_by_order_unknown():
    store = ExposureStore()
    released = store.release_by_order("unknown")
    assert released == 0


# ---------------------------------------------------------------------------
# expire_stale_orders
# ---------------------------------------------------------------------------


def test_expire_stale_orders():
    store = ExposureStore()
    store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
    # Set old timestamp
    store._order_ts["ord1"] = time.monotonic() - 100
    expired = store.expire_stale_orders(max_age_s=1)
    assert expired == 1
    assert store.get_global_notional() == 0


def test_expire_stale_orders_none_expired():
    store = ExposureStore()
    store.check_and_update(_key(), _intent(price=100, qty=10), order_key="ord1")
    expired = store.expire_stale_orders(max_age_s=9999)
    assert expired == 0


# ---------------------------------------------------------------------------
# check_and_update_typed
# ---------------------------------------------------------------------------


class TestCheckAndUpdateTyped:
    def test_typed_new_order(self):
        store = ExposureStore()
        ok, reason = store.check_and_update_typed(
            _key(),
            intent_type=int(IntentType.NEW),
            price=100,
            qty=10,
        )
        assert ok is True

    def test_typed_cancel(self):
        store = ExposureStore()
        ok, reason = store.check_and_update_typed(
            _key(),
            intent_type=int(IntentType.CANCEL),
            price=0,
            qty=0,
        )
        assert ok is True

    def test_typed_amend(self):
        store = ExposureStore()
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10, order_key="ord1",
        )
        ok, reason = store.check_and_update_typed(
            _key(),
            intent_type=int(IntentType.AMEND),
            price=100,
            qty=15,
            target_order_key="ord1",
        )
        assert ok is True

    def test_typed_global_limit(self):
        store = ExposureStore(global_max_notional=500)
        ok, reason = store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10,
        )
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_typed_strategy_limit(self):
        limits = {"s1": ExposureLimits(max_notional_scaled=500)}
        store = ExposureStore(limits=limits)
        ok, reason = store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10,
        )
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"

    def test_typed_cardinality_limit(self):
        store = ExposureStore(max_symbols=1)
        store.check_and_update_typed(
            _key(sym="A"), intent_type=int(IntentType.NEW), price=100, qty=1,
        )
        with pytest.raises(ExposureLimitError):
            store.check_and_update_typed(
                _key(sym="B"), intent_type=int(IntentType.NEW), price=100, qty=1,
            )

    def test_typed_per_order_tracking(self):
        store = ExposureStore()
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10, order_key="ord1",
        )
        assert "ord1" in store._order_notionals


# ---------------------------------------------------------------------------
# release_exposure_typed
# ---------------------------------------------------------------------------


class TestReleaseExposureTyped:
    def test_typed_cancel_noop(self):
        store = ExposureStore()
        store.release_exposure_typed(
            _key(), intent_type=int(IntentType.CANCEL), price=0, qty=0,
        )

    def test_typed_amend_rollback(self):
        store = ExposureStore()
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10, order_key="ord1",
        )
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.AMEND), price=100, qty=15,
            target_order_key="ord1",
        )
        store.release_exposure_typed(
            _key(), intent_type=int(IntentType.AMEND), price=100, qty=15,
            target_order_key="ord1",
        )

    def test_typed_new_release_by_order(self):
        store = ExposureStore()
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10, order_key="ord1",
        )
        store.release_exposure_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10, order_key="ord1",
        )
        assert store.get_global_notional() == 0

    def test_typed_new_legacy_fallback(self):
        store = ExposureStore()
        store.check_and_update_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10,
        )
        store.release_exposure_typed(
            _key(), intent_type=int(IntentType.NEW), price=100, qty=10,
        )
        assert store.get_global_notional() == 0


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------


def test_global_notional_property():
    store = ExposureStore()
    store.check_and_update(_key(), _intent(price=100, qty=10))
    assert store.global_notional == 1000


def test_get_exposure():
    store = ExposureStore()
    store.check_and_update(_key(), _intent(price=100, qty=10))
    assert store.get_exposure("acc1", "s1", "2330") == 1000
    assert store.get_exposure("acc1", "s1", "9999") == 0
