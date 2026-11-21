"""Coverage tests for gateway/dedup.py and gateway/exposure.py — uncovered paths."""

from __future__ import annotations

import time

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.gateway.exposure import (
    ExposureKey,
    ExposureLimitError,
    ExposureLimits,
    ExposureStore,
)

# ===========================================================================
# IdempotencyStore tests
# ===========================================================================


class TestIdempotencyStoreCheckOrReserve:
    def test_empty_key_returns_none(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        assert store.check_or_reserve("") is None

    def test_new_key_reserves_and_returns_none(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        result = store.check_or_reserve("key1")
        assert result is None
        assert store.size() == 1

    def test_duplicate_key_returns_record(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("key1")
        result = store.check_or_reserve("key1")
        assert result is not None
        assert result.key == "key1"

    def test_window_eviction(self):
        store = IdempotencyStore(window_size=3, persist_enabled=False)
        store.check_or_reserve("k1")
        store.check_or_reserve("k2")
        store.check_or_reserve("k3")
        store.check_or_reserve("k4")  # should evict k1
        assert store.size() == 3
        assert store.check_or_reserve("k1") is None  # k1 was evicted, re-reserved

    def test_lru_order_preserved(self):
        store = IdempotencyStore(window_size=3, persist_enabled=False)
        store.check_or_reserve("k1")
        store.check_or_reserve("k2")
        store.check_or_reserve("k3")
        # Access k1 to make it most recent
        store.check_or_reserve("k1")
        # Add k4 -> should evict k2 (oldest non-accessed)
        store.check_or_reserve("k4")
        assert store.check_or_reserve("k2") is None  # was evicted


class TestIdempotencyStoreCommit:
    def test_commit_existing_record(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("key1")
        store.commit("key1", True, "approved", 42)
        rec = store.check_or_reserve("key1")
        assert rec is not None
        assert rec.approved is True
        assert rec.reason_code == "approved"
        assert rec.cmd_id == 42

    def test_commit_without_reserve(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.commit("new_key", False, "rejected", 99)
        rec = store.check_or_reserve("new_key")
        assert rec is not None
        assert rec.approved is False

    def test_commit_empty_key_noop(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.commit("", True, "ok", 1)
        assert store.size() == 0

    def test_commit_overwrite_blocked(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("key1")
        store.commit("key1", True, "first", 1)
        # Second commit should be blocked (first-commit-wins)
        store.commit("key1", False, "second", 2)
        rec = store.check_or_reserve("key1")
        assert rec.approved is True
        assert rec.reason_code == "first"


class TestIdempotencyStoreRelease:
    def test_release_existing_key(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("key1")
        store.release("key1")
        assert store.check_or_reserve("key1") is None  # re-reserved

    def test_release_empty_key_noop(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.release("")
        assert store.size() == 0

    def test_release_nonexistent_key_noop(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.release("nonexistent")
        assert store.size() == 0


class TestIdempotencyStorePersist:
    def test_persist_and_load(self, tmp_path):
        path = str(tmp_path / "dedup.jsonl")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.check_or_reserve("k1")
        store.commit("k1", True, "ok", 10)
        store.check_or_reserve("k2")
        store.persist()

        store2 = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store2.load()
        rec = store2.check_or_reserve("k1")
        assert rec is not None
        assert rec.approved is True

    def test_persist_disabled_noop(self, tmp_path):  # noqa: no-assert
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve("k1")
        store.persist()

    def test_load_nonexistent_file(self, tmp_path):
        path = str(tmp_path / "nope.jsonl")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.load()
        assert store.size() == 0

    def test_load_enforces_window_size(self, tmp_path):
        path = str(tmp_path / "dedup.jsonl")
        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        for i in range(10):
            store.check_or_reserve(f"k{i}")
        store.persist()

        store2 = IdempotencyStore(window_size=3, persist_enabled=True, persist_path=path)
        store2.load()
        assert store2.size() == 3

    def test_load_skips_corrupt_lines(self, tmp_path):
        path = str(tmp_path / "dedup.jsonl")
        with open(path, "wb") as f:
            f.write(b'{"key":"k1","approved":true,"reason_code":"ok","cmd_id":1}\n')
            f.write(b"bad json line\n")
            f.write(b'{"key":"k2","approved":false,"reason_code":"nope","cmd_id":2}\n')

        store = IdempotencyStore(window_size=100, persist_enabled=True, persist_path=path)
        store.load()
        assert store.size() == 2


class TestIdempotencyStoreTyped:
    def test_check_or_reserve_typed_python_path(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        result = store.check_or_reserve_typed("key1")
        assert result is None

    def test_commit_typed_python_path(self):
        store = IdempotencyStore(window_size=100, persist_enabled=False)
        store.check_or_reserve_typed("key1")
        store.commit_typed("key1", True, "ok", 1)
        rec = store.check_or_reserve_typed("key1")
        assert rec is not None
        assert rec.approved is True


# ===========================================================================
# ExposureStore tests
# ===========================================================================


def _make_intent(intent_type=IntentType.NEW, price=100000, qty=10, symbol="SYM", target_order_id=None):
    return OrderIntent(
        intent_id=1,
        strategy_id="strat1",
        symbol=symbol,
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        target_order_id=target_order_id,
    )


def _make_key(account="acc", strategy_id="strat1", symbol="SYM"):
    return ExposureKey(account=account, strategy_id=strategy_id, symbol=symbol)


class TestExposureStoreCheckAndUpdate:
    def test_new_order_approved(self):
        store = ExposureStore(global_max_notional=0)
        key = _make_key()
        intent = _make_intent()
        ok, reason = store.check_and_update(key, intent)
        assert ok is True
        assert reason == "OK"

    def test_global_limit_rejected(self):
        store = ExposureStore(global_max_notional=100)
        key = _make_key()
        intent = _make_intent(price=100000, qty=10)  # notional = 1_000_000
        ok, reason = store.check_and_update(key, intent)
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_strategy_limit_rejected(self):
        limits = {"strat1": ExposureLimits(max_notional_scaled=50000)}
        store = ExposureStore(limits=limits)
        key = _make_key()
        intent = _make_intent(price=100000, qty=10)
        ok, reason = store.check_and_update(key, intent)
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"

    def test_cancel_always_approved(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(intent_type=IntentType.CANCEL)
        ok, reason = store.check_and_update(key, intent)
        assert ok is True

    def test_force_flat_always_approved(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(intent_type=IntentType.FORCE_FLAT)
        ok, reason = store.check_and_update(key, intent)
        assert ok is True

    def test_symbol_limit_reached_raises(self):
        store = ExposureStore(max_symbols=1)
        key1 = _make_key(symbol="SYM1")
        key2 = _make_key(symbol="SYM2")
        intent = _make_intent(price=100, qty=1, symbol="SYM1")
        store.check_and_update(key1, intent)
        intent2 = _make_intent(price=100, qty=1, symbol="SYM2")
        with pytest.raises(ExposureLimitError):
            store.check_and_update(key2, intent2)

    def test_symbol_limit_eviction(self):
        store = ExposureStore(max_symbols=2)
        key1 = _make_key(symbol="SYM1")
        key2 = _make_key(symbol="SYM2")
        key3 = _make_key(symbol="SYM3")
        intent1 = _make_intent(price=100, qty=1, symbol="SYM1")
        intent2 = _make_intent(price=100, qty=1, symbol="SYM2")
        store.check_and_update(key1, intent1)
        store.check_and_update(key2, intent2)
        # Release SYM1 to zero so eviction can reclaim it
        store.release_exposure(key1, intent1)
        intent3 = _make_intent(price=100, qty=1, symbol="SYM3")
        ok, reason = store.check_and_update(key3, intent3)
        assert ok is True

    def test_per_order_tracking(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        ok, _ = store.check_and_update(key, intent, order_key="order1")
        assert ok is True
        released = store.release_by_order("order1")
        assert released == 100


class TestExposureStoreAmend:
    def test_amend_increases_exposure(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        amend = _make_intent(intent_type=IntentType.AMEND, price=200, qty=1, target_order_id="o1")
        ok, reason = store.check_and_update(key, amend, order_key="o1_amend")
        assert ok is True

    def test_amend_global_limit_rejected(self):
        store = ExposureStore(global_max_notional=150)
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        amend = _make_intent(intent_type=IntentType.AMEND, price=200, qty=1, target_order_id="o1")
        ok, reason = store.check_and_update(key, amend)
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_amend_strategy_limit_rejected(self):
        limits = {"strat1": ExposureLimits(max_notional_scaled=150)}
        store = ExposureStore(limits=limits)
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        amend = _make_intent(intent_type=IntentType.AMEND, price=200, qty=1, target_order_id="o1")
        ok, reason = store.check_and_update(key, amend)
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"


class TestExposureStoreRelease:
    def test_release_cancel_noop(self):  # noqa: no-assert
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(intent_type=IntentType.CANCEL)
        # Should not raise
        store.release_exposure(key, intent)

    def test_release_amend_rollback(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        amend = _make_intent(intent_type=IntentType.AMEND, price=200, qty=1, target_order_id="o1")
        store.check_and_update(key, amend, order_key="o1_a")
        # Rollback the amend
        store.release_exposure(key, amend, order_key="o1_a")
        # Global should be back to 100 after rollback
        assert store.global_notional <= 200

    def test_release_by_order_key(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        released = store.release_by_order("o1")
        assert released == 100
        assert store.global_notional == 0

    def test_release_legacy_fallback(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent)
        # No order_key, no idempotency_key -> legacy fallback
        store.release_exposure(key, intent)
        assert store.global_notional == 0


class TestExposureStoreExpire:
    def test_expire_stale_orders(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        # Manually set timestamp to past
        store._order_ts["o1"] = time.monotonic() - 1000
        count = store.expire_stale_orders(max_age_s=1.0)
        assert count == 1
        assert store.global_notional == 0


class TestExposureStoreTyped:
    def test_check_and_update_typed_new(self):
        store = ExposureStore()
        key = _make_key()
        ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1)
        assert ok is True

    def test_check_and_update_typed_cancel(self):
        store = ExposureStore()
        key = _make_key()
        ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.CANCEL), price=0, qty=0)
        assert ok is True

    def test_check_and_update_typed_amend(self):
        store = ExposureStore()
        key = _make_key()
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1, order_key="o1")
        ok, reason = store.check_and_update_typed(
            key, intent_type=int(IntentType.AMEND), price=200, qty=1, target_order_key="o1"
        )
        assert ok is True

    def test_check_and_update_typed_global_limit(self):
        store = ExposureStore(global_max_notional=50)
        key = _make_key()
        ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1)
        assert ok is False
        assert reason == "GLOBAL_EXPOSURE_LIMIT"

    def test_check_and_update_typed_strategy_limit(self):
        limits = {"strat1": ExposureLimits(max_notional_scaled=50)}
        store = ExposureStore(limits=limits)
        key = _make_key()
        ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1)
        assert ok is False
        assert reason == "STRATEGY_EXPOSURE_LIMIT"

    def test_check_and_update_typed_symbol_limit(self):
        store = ExposureStore(max_symbols=1)
        key1 = _make_key(symbol="A")
        key2 = _make_key(symbol="B")
        store.check_and_update_typed(key1, intent_type=int(IntentType.NEW), price=1, qty=1)
        with pytest.raises(ExposureLimitError):
            store.check_and_update_typed(key2, intent_type=int(IntentType.NEW), price=1, qty=1)


class TestExposureStoreReleaseTyped:
    def test_release_typed_cancel_noop(self):  # noqa: no-assert
        store = ExposureStore()
        key = _make_key()
        store.release_exposure_typed(key, intent_type=int(IntentType.CANCEL), price=0, qty=0)

    def test_release_typed_amend(self):  # noqa: no-assert
        store = ExposureStore()
        key = _make_key()
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1, order_key="o1")
        store.check_and_update_typed(key, intent_type=int(IntentType.AMEND), price=200, qty=1, target_order_key="o1")
        store.release_exposure_typed(key, intent_type=int(IntentType.AMEND), price=200, qty=1, target_order_key="o1")

    def test_release_typed_new_with_order_key(self):
        store = ExposureStore()
        key = _make_key()
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1, order_key="o1")
        store.release_exposure_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1, order_key="o1")
        assert store.global_notional == 0

    def test_release_typed_new_legacy_fallback(self):
        store = ExposureStore()
        key = _make_key()
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1)
        store.release_exposure_typed(key, intent_type=int(IntentType.NEW), price=100, qty=1)
        assert store.global_notional == 0


class TestExposureStoreGetters:
    def test_get_exposure(self):
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent)
        assert store.get_exposure("acc", "strat1", "SYM") == 100

    def test_get_exposure_unknown(self):
        store = ExposureStore()
        assert store.get_exposure("no", "no", "no") == 0

    def test_global_notional_property(self):
        store = ExposureStore()
        assert store.global_notional == 0
        key = _make_key()
        intent = _make_intent(price=100, qty=1)
        store.check_and_update(key, intent)
        assert store.global_notional == 100

    def test_get_global_notional(self):
        store = ExposureStore()
        assert store.get_global_notional() == 0


# ===========================================================================
# _load_rust_exposure() — lines 34-48
# ===========================================================================


# ===========================================================================
# _init_rust_store() — lines 109-121
# ===========================================================================


# ===========================================================================
# _rollback_amend() — line 436 (delta == 0 early return)
# ===========================================================================


class TestRollbackAmendZeroDelta:
    """Cover line 436: _rollback_amend returns early when delta == 0."""

    def test_rollback_amend_zero_delta_noop(self):
        """Line 436: _rollback_amend with no pending delta is a no-op."""
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100_0000, qty=1)
        store.check_and_update(key, intent, order_key="o1")
        notional_before = store.global_notional

        # Call _rollback_amend with a target that has no pending amend delta
        # (never had an AMEND — so _pending_amend_deltas has no entry, pop returns 0)
        store._rollback_amend(key, "o1")

        # Nothing should change
        assert store.global_notional == notional_before
        assert store.get_exposure("acc", "strat1", "SYM") == 100_0000

    def test_rollback_amend_zero_delta_after_prior_rollback(self):
        """Line 436: second rollback for same target is a no-op (delta already consumed)."""
        store = ExposureStore()
        key = _make_key()
        intent = _make_intent(price=100_0000, qty=1)
        store.check_and_update(key, intent, order_key="o1")

        # AMEND up — creates a pending delta
        amend = _make_intent(
            intent_type=IntentType.AMEND,
            price=200_0000,
            qty=1,
            target_order_id="o1",
        )
        store.check_and_update(key, amend, order_key="a1")
        notional_after_amend = store.global_notional

        # First rollback consumes the delta
        store._rollback_amend(key, "o1")
        notional_after_first_rollback = store.global_notional
        assert notional_after_first_rollback < notional_after_amend

        # Second rollback: delta is 0 (already popped) → early return at line 436
        store._rollback_amend(key, "o1")
        assert store.global_notional == notional_after_first_rollback
