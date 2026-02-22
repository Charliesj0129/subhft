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
