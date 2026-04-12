"""Tests for CE2-04: ExposureStore."""

import threading

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.gateway.exposure import ExposureKey, ExposureLimitError, ExposureLimits, ExposureStore


def _make_intent(price: int = 1_000_000, qty: int = 1, intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
    )


def _key() -> ExposureKey:
    return ExposureKey(account="default", strategy_id="s1", symbol="TSE:2330")


def test_exposure_approve_within_limit():
    store = ExposureStore(global_max_notional=0)
    ok, reason = store.check_and_update(_key(), _make_intent(1_000_000, 1))
    assert ok is True
    assert reason == "OK"


def test_exposure_cancel_always_passes():
    store = ExposureStore(global_max_notional=1)
    intent = _make_intent(intent_type=IntentType.CANCEL)
    ok, reason = store.check_and_update(_key(), intent)
    assert ok is True


def test_exposure_global_limit_blocks():
    # global max = price * qty = 1_000_000 * 1 = 1_000_000; second order should exceed
    store = ExposureStore(global_max_notional=1_000_000)
    ok1, _ = store.check_and_update(_key(), _make_intent(1_000_000, 1))
    assert ok1 is True
    ok2, reason2 = store.check_and_update(_key(), _make_intent(1_000_000, 1))
    assert ok2 is False
    assert reason2 == "GLOBAL_EXPOSURE_LIMIT"


def test_exposure_strategy_limit_blocks():
    limits = {"s1": ExposureLimits(max_notional_scaled=1_000_000)}
    store = ExposureStore(global_max_notional=0, limits=limits)
    ok1, _ = store.check_and_update(_key(), _make_intent(1_000_000, 1))
    assert ok1 is True
    ok2, reason2 = store.check_and_update(_key(), _make_intent(1_000_000, 1))
    assert ok2 is False
    assert reason2 == "STRATEGY_EXPOSURE_LIMIT"


def test_exposure_release_allows_subsequent():
    store = ExposureStore(global_max_notional=1_000_000)
    intent = _make_intent(1_000_000, 1)
    store.check_and_update(_key(), intent)
    store.release_exposure(_key(), intent)

    # After release, we should be able to submit again
    ok, _ = store.check_and_update(_key(), intent)
    assert ok is True


def test_exposure_typed_path_matches_object():
    store_obj = ExposureStore(global_max_notional=2_000_000)
    store_typed = ExposureStore(global_max_notional=2_000_000)
    key = _key()
    intent = _make_intent(1_000_000, 1)

    ok_obj_1, reason_obj_1 = store_obj.check_and_update(key, intent)
    ok_t_1, reason_t_1 = store_typed.check_and_update_typed(
        key,
        intent_type=int(intent.intent_type),
        price=int(intent.price),
        qty=int(intent.qty),
    )
    assert (ok_t_1, reason_t_1) == (ok_obj_1, reason_obj_1)

    ok_obj_2, reason_obj_2 = store_obj.check_and_update(key, intent)
    ok_t_2, reason_t_2 = store_typed.check_and_update_typed(
        key,
        intent_type=int(intent.intent_type),
        price=int(intent.price),
        qty=int(intent.qty),
    )
    assert (ok_t_2, reason_t_2) == (ok_obj_2, reason_obj_2)

    store_obj.release_exposure(key, intent)
    store_typed.release_exposure_typed(
        key,
        intent_type=int(intent.intent_type),
        price=int(intent.price),
        qty=int(intent.qty),
    )
    assert store_typed.get_global_notional() == store_obj.get_global_notional()


def test_exposure_concurrent_no_overshoot():
    """10 threads each requesting same-sized order; only N that fit within limit should pass."""
    global_max = 5_000_000  # allows exactly 5 x 1_000_000
    store = ExposureStore(global_max_notional=global_max)
    approved = []
    lock = threading.Lock()

    def submit():
        ok, _ = store.check_and_update(_key(), _make_intent(1_000_000, 1))
        with lock:
            approved.append(ok)

    threads = [threading.Thread(target=submit) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 5 should have been approved
    assert sum(approved) == 5
    # Global notional must not exceed limit
    assert store.get_global_notional() <= global_max


def test_exposure_deterministic_rejection_reason():
    store = ExposureStore(global_max_notional=0)  # unlimited
    limits = {"s1": ExposureLimits(max_notional_scaled=500_000)}
    store._limits = limits
    store.check_and_update(_key(), _make_intent(500_000, 1))
    ok, reason = store.check_and_update(_key(), _make_intent(500_000, 1))
    assert ok is False
    assert reason == "STRATEGY_EXPOSURE_LIMIT"


def _make_intent_for_symbol(symbol: str) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=1_000_000,
        qty=1,
        tif=TIF.LIMIT,
    )


def test_symbol_limit_evicts_zeroes_and_admits():
    """After releasing all positions, zero-balance eviction allows new symbols."""
    max_symbols = 10
    store = ExposureStore(global_max_notional=0, max_symbols=max_symbols)

    # Fill to max with symbols 0..9
    intents = {}
    for i in range(max_symbols):
        sym = f"TSE:{i:04d}"
        intent = _make_intent_for_symbol(sym)
        intents[sym] = intent
        key = ExposureKey(account="default", strategy_id="s1", symbol=sym)
        ok, _ = store.check_and_update(key, intent)
        assert ok is True

    assert store._symbol_count == max_symbols

    # Release all positions → all notional → 0
    for sym, intent in intents.items():
        key = ExposureKey(account="default", strategy_id="s1", symbol=sym)
        store.release_exposure(key, intent)

    # Now adding symbol 10 should trigger eviction and succeed
    new_sym = "TSE:9999"
    new_intent = _make_intent_for_symbol(new_sym)
    new_key = ExposureKey(account="default", strategy_id="s1", symbol=new_sym)
    ok, _ = store.check_and_update(new_key, new_intent)
    assert ok is True
    # symbol_count should be 1 (just the new one; zeroes were evicted)
    assert store._symbol_count == 1


def test_symbol_limit_raises_after_10001_symbols():
    """10,001 unique symbols with non-zero exposure raises ExposureLimitError."""
    max_symbols = 10_000
    store = ExposureStore(global_max_notional=0, max_symbols=max_symbols)

    # Add exactly max_symbols symbols (all with live exposure)
    for i in range(max_symbols):
        sym = f"SYM:{i:05d}"
        intent = _make_intent_for_symbol(sym)
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        store.check_and_update(key, intent)

    assert store._symbol_count == max_symbols

    # The 10,001st symbol should trigger eviction (no zeroes) then raise
    overflow_sym = "SYM:OVERFLOW"
    overflow_intent = _make_intent_for_symbol(overflow_sym)
    overflow_key = ExposureKey(account="acct", strategy_id="strat", symbol=overflow_sym)

    with pytest.raises(ExposureLimitError):
        store.check_and_update(overflow_key, overflow_intent)

    # symbol_count must not have grown
    assert store._symbol_count == max_symbols


def test_symbol_limit_same_symbol_repeated_does_not_count():
    """Repeated updates to the same symbol do not increment symbol_count."""
    store = ExposureStore(global_max_notional=0, max_symbols=5)
    key = _key()
    intent = _make_intent()
    for _ in range(100):
        store.check_and_update(key, intent)
    assert store._symbol_count == 1


def test_symbol_limit_concurrent_no_overshoot():
    """D5: 20 threads each submitting a unique symbol with max_symbols=5 must not overshoot."""
    max_syms = 5
    store = ExposureStore(global_max_notional=0, max_symbols=max_syms)
    errors: list[str] = []
    approved: list[bool] = []
    lock = threading.Lock()

    def try_add(sym: str) -> None:
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        intent = _make_intent_for_symbol(sym)
        try:
            ok, _ = store.check_and_update(key, intent)
            with lock:
                approved.append(ok)
        except ExposureLimitError:
            with lock:
                errors.append(sym)

    threads = [threading.Thread(target=try_add, args=(f"SYM{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Symbol count must never exceed the configured maximum
    assert store._symbol_count <= max_syms
    # All approvals must be within the allowed set (no overshoot)
    assert sum(approved) <= max_syms


# ── Additional coverage tests ──────────────────────────────────────────────────


def test_release_exposure_cancel_intent_is_no_op():
    """release_exposure() with CANCEL intent skips the notional reduction."""
    store = ExposureStore(global_max_notional=5_000_000)
    intent = _make_intent(price=1_000_000, qty=1)
    key = _key()
    store.check_and_update(key, intent)
    notional_before = store.get_global_notional()

    cancel_intent = _make_intent(intent_type=IntentType.CANCEL)
    store.release_exposure(key, cancel_intent)

    # Notional must be unchanged — cancel intent was skipped
    assert store.get_global_notional() == notional_before


def test_release_exposure_symbol_not_present_does_not_raise():
    """release_exposure() for a symbol that was never tracked must not raise."""
    store = ExposureStore(global_max_notional=0)
    key = ExposureKey(account="acct", strategy_id="strat", symbol="TSE:NEVER")
    intent = _make_intent(price=500_000, qty=2)
    # No prior check_and_update — symbol is absent
    store.release_exposure(key, intent)
    # Global notional should clamp to 0 (not go negative)
    assert store.get_global_notional() == 0


def test_release_exposure_clamps_global_notional_at_zero():
    """release_exposure() clamps global notional to 0, never negative."""
    store = ExposureStore(global_max_notional=0)
    key = _key()
    intent = _make_intent(price=1_000_000, qty=2)
    store.check_and_update(key, intent)
    # Release more than what was added
    store.release_exposure(key, _make_intent(price=1_000_000, qty=10))
    assert store.get_global_notional() == 0


def test_release_exposure_typed_cancel_is_no_op():
    """release_exposure_typed() with CANCEL intent_type is a no-op."""
    store = ExposureStore(global_max_notional=5_000_000)
    key = _key()
    intent = _make_intent(1_000_000, 1)
    store.check_and_update(key, intent)
    notional_before = store.get_global_notional()

    store.release_exposure_typed(
        key,
        intent_type=int(IntentType.CANCEL),
        price=1_000_000,
        qty=1,
    )
    assert store.get_global_notional() == notional_before


def test_release_exposure_typed_symbol_not_present_does_not_raise():
    """release_exposure_typed() for absent symbol is safe."""
    store = ExposureStore(global_max_notional=0)
    key = ExposureKey(account="acct", strategy_id="strat", symbol="TSE:ABSENT")
    store.release_exposure_typed(
        key,
        intent_type=int(IntentType.NEW),
        price=500_000,
        qty=1,
    )
    assert store.get_global_notional() == 0


def test_get_exposure_existing_key():
    """get_exposure() returns accurate notional for tracked symbol."""
    store = ExposureStore(global_max_notional=0)
    key = _key()
    intent = _make_intent(price=2_000_000, qty=3)
    store.check_and_update(key, intent)
    exposure = store.get_exposure(key.account, key.strategy_id, key.symbol)
    assert exposure == 2_000_000 * 3


def test_get_exposure_nonexistent_key_returns_zero():
    """get_exposure() returns 0 for a symbol that was never tracked."""
    store = ExposureStore(global_max_notional=0)
    assert store.get_exposure("no-account", "no-strat", "TSE:NONE") == 0


def test_evict_zeroes_removes_empty_strategy_and_account_maps():
    """_evict_zeroes() cascades cleanup of empty strategy and account maps."""
    store = ExposureStore(global_max_notional=0, max_symbols=2)
    # Add two symbols for the same account/strategy
    for sym in ("TSE:A", "TSE:B"):
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        intent = _make_intent_for_symbol(sym)
        store.check_and_update(key, intent)

    # Release both — sets notional to 0
    for sym in ("TSE:A", "TSE:B"):
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        intent = _make_intent_for_symbol(sym)
        store.release_exposure(key, intent)

    assert store._symbol_count == 2
    # Call _evict_zeroes manually under lock
    with store._lock:
        store._evict_zeroes()

    # After eviction, symbol count should be 0 and maps should be empty
    assert store._symbol_count == 0
    assert "acct" not in store._exposure


def test_check_and_update_typed_cancel_returns_ok():
    """check_and_update_typed() with CANCEL intent_type always returns (True, OK)."""
    store = ExposureStore(global_max_notional=1)  # very tight limit
    key = _key()
    ok, reason = store.check_and_update_typed(
        key,
        intent_type=int(IntentType.CANCEL),
        price=999_999_999,
        qty=999,
    )
    assert ok is True
    assert reason == "OK"


def test_check_and_update_typed_global_limit_blocks():
    """check_and_update_typed() Python fallback rejects when global max exceeded."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    ok1, _ = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok1 is True
    ok2, reason2 = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok2 is False
    assert reason2 == "GLOBAL_EXPOSURE_LIMIT"


def test_check_and_update_typed_strategy_limit_blocks():
    """check_and_update_typed() Python fallback rejects when strategy limit exceeded."""
    limits = {"s1": ExposureLimits(max_notional_scaled=1_000_000)}
    store = ExposureStore(global_max_notional=0, limits=limits)
    key = _key()
    ok1, _ = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok1 is True
    ok2, reason2 = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok2 is False
    assert reason2 == "STRATEGY_EXPOSURE_LIMIT"


def test_check_and_update_typed_symbol_limit_raises():
    """check_and_update_typed() Python fallback raises ExposureLimitError at cardinality bound."""
    import pytest

    store = ExposureStore(global_max_notional=0, max_symbols=2)
    for sym in ("TSE:A", "TSE:B"):
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)

    overflow_key = ExposureKey(account="acct", strategy_id="strat", symbol="TSE:C")
    with pytest.raises(ExposureLimitError):
        store.check_and_update_typed(overflow_key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)


def test_check_and_update_typed_with_rust_store_not_ok():
    """check_and_update_typed() with Rust store propagates rejection reason."""

    class _MockRust:
        def check_and_update(self, account, strategy_id, symbol, intent_type, price, qty):
            return (False, 1)  # code 1 → not ExposureLimitError

        def reason_str(self, code):
            return "GLOBAL_EXPOSURE_LIMIT"

    store = ExposureStore(global_max_notional=0)
    store._rust_store = _MockRust()
    key = _key()
    ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok is False
    assert reason == "GLOBAL_EXPOSURE_LIMIT"


def test_check_and_update_typed_with_rust_store_symbol_limit_error():
    """check_and_update_typed() with Rust store raises ExposureLimitError when code==3."""
    import pytest

    class _MockRust:
        def check_and_update(self, account, strategy_id, symbol, intent_type, price, qty):
            return (False, 3)

        def reason_str(self, code):
            return "SYMBOL_LIMIT"

    store = ExposureStore(global_max_notional=0)
    store._rust_store = _MockRust()
    key = _key()
    with pytest.raises(ExposureLimitError):
        store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)


def test_check_and_update_typed_with_rust_store_ok():
    """check_and_update_typed() delegates to Rust store and returns OK on success."""

    class _MockRust:
        def check_and_update(self, account, strategy_id, symbol, intent_type, price, qty):
            return (True, 0)

        def reason_str(self, code):
            return "OK"

    store = ExposureStore(global_max_notional=0)
    store._rust_store = _MockRust()
    key = _key()
    ok, reason = store.check_and_update_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert ok is True
    assert reason == "OK"


def test_release_exposure_typed_with_rust_store_delegates():
    """release_exposure_typed() calls Rust store release() when available."""

    class _MockRust:
        def __init__(self):
            self.calls = []

        def release(self, account, strategy_id, symbol, intent_type, price, qty):
            self.calls.append((account, strategy_id, symbol, intent_type, price, qty))

    store = ExposureStore(global_max_notional=0)
    mock = _MockRust()
    store._rust_store = mock
    key = ExposureKey(account="acct", strategy_id="strat", symbol="TSE:2330")
    store.release_exposure_typed(key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1)
    assert len(mock.calls) == 1
    assert mock.calls[0] == ("acct", "strat", "TSE:2330", int(IntentType.NEW), 1_000_000, 1)


def test_get_global_notional_accumulates_across_symbols():
    """get_global_notional() returns sum across all accounts, strategies, symbols."""
    store = ExposureStore(global_max_notional=0)
    for sym in ("TSE:A", "TSE:B", "TSE:C"):
        key = ExposureKey(account="acct", strategy_id="strat", symbol=sym)
        intent = _make_intent_for_symbol(sym)
        store.check_and_update(key, intent)
    # Each intent is price=1_000_000, qty=1 → notional=1_000_000
    assert store.get_global_notional() == 3_000_000


def test_exposure_env_var_global_max(monkeypatch):
    """HFT_EXPOSURE_GLOBAL_MAX_NOTIONAL env var sets the global max."""
    monkeypatch.setenv("HFT_EXPOSURE_GLOBAL_MAX_NOTIONAL", "500000")
    store = ExposureStore()
    assert store._global_max == 500_000


def test_exposure_env_var_max_symbols(monkeypatch):
    """HFT_EXPOSURE_MAX_SYMBOLS env var controls max symbol cardinality."""
    monkeypatch.setenv("HFT_EXPOSURE_MAX_SYMBOLS", "7")
    store = ExposureStore()
    assert store._max_symbols == 7


# ── H10: global_notional property ──────────────────────────────────────────


def test_global_notional_property_returns_zero_on_init():
    """global_notional property returns 0 on a freshly constructed store."""
    store = ExposureStore(global_max_notional=0)
    assert store.global_notional == 0


def test_global_notional_property_reflects_approved_intent():
    """global_notional property increases after an approved check_and_update."""
    store = ExposureStore(global_max_notional=0)
    store.check_and_update(_key(), _make_intent(price=2_000_000, qty=3))
    # notional = 2_000_000 * 3
    assert store.global_notional == 6_000_000


def test_global_notional_property_decreases_after_release():
    """global_notional property decreases after release_exposure."""
    store = ExposureStore(global_max_notional=0)
    intent = _make_intent(price=1_000_000, qty=2)
    store.check_and_update(_key(), intent)
    store.release_exposure(_key(), intent)
    assert store.global_notional == 0


def test_global_notional_property_matches_get_global_notional():
    """global_notional property is consistent with get_global_notional()."""
    store = ExposureStore(global_max_notional=0)
    store.check_and_update(_key(), _make_intent(price=500_000, qty=5))
    assert store.global_notional == store.get_global_notional()


# ── Bug #6: Per-order tracking (exposure held past dispatch) ─────────────


def _make_intent_with_key(
    price: int = 1_000_000,
    qty: int = 1,
    intent_type: IntentType = IntentType.NEW,
    idempotency_key: str = "ord-1",
    target_order_id: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TSE:2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.LIMIT,
        idempotency_key=idempotency_key,
        target_order_id=target_order_id,
    )


def test_per_order_tracking_holds_exposure():
    """Bug #6: exposure must NOT be released after dispatch — subsequent
    orders that exceed the cap must be blocked."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    ok1, _ = store.check_and_update(key, intent, order_key="ord-1")
    assert ok1 is True

    # Second order should be BLOCKED because first is still held
    intent2 = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-2")
    ok2, reason2 = store.check_and_update(key, intent2, order_key="ord-2")
    assert ok2 is False
    assert reason2 == "GLOBAL_EXPOSURE_LIMIT"


def test_release_by_order_frees_capacity():
    """release_by_order releases the specific order's notional."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent, order_key="ord-1")

    released = store.release_by_order("ord-1")
    assert released == 1_000_000
    assert store.get_global_notional() == 0

    # Now capacity is free
    ok, _ = store.check_and_update(key, intent, order_key="ord-1b")
    assert ok is True


def test_release_by_order_unknown_key_returns_zero():
    """release_by_order with unknown key is a safe no-op."""
    store = ExposureStore(global_max_notional=0)
    assert store.release_by_order("nonexistent") == 0


def test_expire_stale_orders_clears_old_reservations():
    """expire_stale_orders releases orders older than TTL."""
    store = ExposureStore(global_max_notional=0)
    key = _key()
    intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="old-ord")
    store.check_and_update(key, intent, order_key="old-ord")

    # Manually backdate the timestamp
    import time
    store._order_ts["old-ord"] = time.monotonic() - 100

    expired = store.expire_stale_orders(max_age_s=30)
    assert expired == 1
    assert store.get_global_notional() == 0


def test_expire_stale_orders_keeps_fresh():
    """expire_stale_orders does not release recent orders."""
    store = ExposureStore(global_max_notional=0)
    key = _key()
    intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="fresh-ord")
    store.check_and_update(key, intent, order_key="fresh-ord")

    expired = store.expire_stale_orders(max_age_s=30)
    assert expired == 0
    assert store.get_global_notional() == 1_000_000


# ── Bug #7: AMEND must check delta exposure ──────────────────────────────


def test_amend_increase_blocked_by_global_limit():
    """Bug #7: AMEND that increases notional must be checked against limits."""
    store = ExposureStore(global_max_notional=1_500_000)
    key = _key()
    # Place original order: 1M notional
    intent_new = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    ok, _ = store.check_and_update(key, intent_new, order_key="ord-1")
    assert ok is True
    assert store.get_global_notional() == 1_000_000

    # AMEND to 2M notional — delta +1M exceeds remaining capacity (500K)
    intent_amend = _make_intent_with_key(
        price=2_000_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="ord-1",
    )
    ok2, reason2 = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok2 is False
    assert reason2 == "GLOBAL_EXPOSURE_LIMIT"
    # Exposure unchanged
    assert store.get_global_notional() == 1_000_000


def test_amend_increase_blocked_by_strategy_limit():
    """AMEND that increases notional checked against per-strategy limit."""
    limits = {"s1": ExposureLimits(max_notional_scaled=1_500_000)}
    store = ExposureStore(global_max_notional=0, limits=limits)
    key = _key()
    intent_new = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent_new, order_key="ord-1")

    intent_amend = _make_intent_with_key(
        price=2_000_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="ord-1",
    )
    ok, reason = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok is False
    assert reason == "STRATEGY_EXPOSURE_LIMIT"


def test_amend_decrease_always_allowed():
    """AMEND that decreases notional is always approved (delta <= 0)."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    intent_new = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent_new, order_key="ord-1")

    # AMEND to 500K — delta is -500K, always allowed
    intent_amend = _make_intent_with_key(
        price=500_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="ord-1",
    )
    ok, _ = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok is True
    assert store.get_global_notional() == 500_000


def test_amend_within_limit_approved_and_updates_tracking():
    """AMEND within limits is approved and updates the per-order record."""
    store = ExposureStore(global_max_notional=5_000_000)
    key = _key()
    intent_new = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent_new, order_key="ord-1")

    intent_amend = _make_intent_with_key(
        price=2_000_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="ord-1",
    )
    ok, _ = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok is True
    assert store.get_global_notional() == 2_000_000
    # Per-order record updated
    assert store._order_notionals["ord-1"] == 2_000_000


def test_amend_rejection_rollback():
    """Rejected AMEND delta is rolled back via release_exposure."""
    store = ExposureStore(global_max_notional=5_000_000)
    key = _key()
    intent_new = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent_new, order_key="ord-1")

    # Approved AMEND: +1M delta
    intent_amend = _make_intent_with_key(
        price=2_000_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="ord-1",
    )
    ok, _ = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok is True
    assert store.get_global_notional() == 2_000_000

    # Risk rejects → rollback via release_exposure
    store.release_exposure(key, intent_amend, order_key="amend-1")
    assert store.get_global_notional() == 1_000_000
    assert store._order_notionals["ord-1"] == 1_000_000


def test_amend_unknown_target_treats_full_notional_as_delta():
    """AMEND with unknown target_order_id conservatively treats full notional as delta."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    intent_amend = _make_intent_with_key(
        price=2_000_000, qty=1,
        intent_type=IntentType.AMEND,
        idempotency_key="amend-1",
        target_order_id="unknown-ord",
    )
    ok, reason = store.check_and_update(key, intent_amend, order_key="amend-1")
    assert ok is False
    assert reason == "GLOBAL_EXPOSURE_LIMIT"


def test_amend_typed_path_checks_delta():
    """Bug #7 typed path: AMEND delta is checked via check_and_update_typed."""
    store = ExposureStore(global_max_notional=1_500_000)
    key = _key()
    # Place via typed path
    store.check_and_update_typed(
        key, intent_type=int(IntentType.NEW), price=1_000_000, qty=1, order_key="ord-1",
    )
    # AMEND via typed path — delta +1M exceeds remaining 500K
    ok, reason = store.check_and_update_typed(
        key,
        intent_type=int(IntentType.AMEND),
        price=2_000_000,
        qty=1,
        order_key="amend-1",
        target_order_key="ord-1",
    )
    assert ok is False
    assert reason == "GLOBAL_EXPOSURE_LIMIT"


def test_release_exposure_new_uses_per_order():
    """release_exposure for NEW intent uses per-order tracking when order_key provided."""
    store = ExposureStore(global_max_notional=0)
    key = _key()
    intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key="ord-1")
    store.check_and_update(key, intent, order_key="ord-1")
    assert store.get_global_notional() == 1_000_000

    store.release_exposure(key, intent, order_key="ord-1")
    assert store.get_global_notional() == 0


def test_consecutive_orders_blocked_with_per_order_tracking():
    """Bug #6 end-to-end: N consecutive NEW orders cannot each bypass cap."""
    store = ExposureStore(global_max_notional=1_000_000)
    key = _key()
    results = []
    for i in range(5):
        intent = _make_intent_with_key(price=1_000_000, qty=1, idempotency_key=f"ord-{i}")
        ok, _ = store.check_and_update(key, intent, order_key=f"ord-{i}")
        results.append(ok)
    # Only first should pass; rest blocked
    assert results == [True, False, False, False, False]
    assert store.get_global_notional() == 1_000_000
