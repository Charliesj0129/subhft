"""Tests for threading.Lock protection in StormGuard FSM."""
import threading
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType, OrderIntent, Side, TIF
from hft_platform.risk.storm_guard import StormGuard, StormGuardState


@pytest.fixture
def guard():
    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        yield StormGuard()


def _make_intent(intent_type: IntentType = IntentType.NEW) -> OrderIntent:
    return OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="2330",
        intent_type=intent_type,
        side=Side.BUY,
        price=5000000,  # 500.0000 x10000
        qty=1,
        tif=TIF.LIMIT,
    )


# ---------------------------------------------------------------------------
# Test 1: _state_lock attribute exists and is a threading.Lock
# ---------------------------------------------------------------------------


def test_state_lock_exists(guard):
    assert hasattr(guard, "_state_lock"), "_state_lock attribute must exist"
    assert isinstance(guard._state_lock, type(threading.Lock())), (
        "_state_lock must be a threading.Lock instance"
    )


# ---------------------------------------------------------------------------
# Test 2: Concurrent update() calls do not corrupt state
# ---------------------------------------------------------------------------


def test_concurrent_update_no_corruption(guard):
    """Multiple threads calling update() with halt-level drawdown must result in HALT."""
    n_threads = 10
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            # Each thread sends a halt-level drawdown (-300 bps < -200 threshold)
            guard.update(drawdown_bps=-300)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions in worker threads: {errors}"
    # Final state must be HALT (all threads sent halt-level drawdown)
    assert guard.state == StormGuardState.HALT, (
        f"Expected HALT after concurrent halt-level updates, got {guard.state}"
    )


# ---------------------------------------------------------------------------
# Test 3: validate() observes consistent state during concurrent transitions
# ---------------------------------------------------------------------------


def test_concurrent_validate_during_transition(guard):
    """validate() must never see a partially-written state mid-transition."""
    n_readers = 8
    barrier = threading.Barrier(n_readers + 1)  # readers + 1 writer
    results: list[tuple[bool, str]] = []
    errors: list[Exception] = []
    intent = _make_intent(IntentType.NEW)

    def reader():
        try:
            barrier.wait()
            for _ in range(200):
                ok, msg = guard.validate(intent)
                results.append((ok, msg))
        except Exception as exc:
            errors.append(exc)

    def writer():
        try:
            barrier.wait()
            guard.trigger_halt("concurrent-test")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(n_readers)]
    threads.append(threading.Thread(target=writer))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions in threads: {errors}"
    assert results, "Expected at least one validate() result"

    # Every result must be a valid (ok, msg) pair — no partial/corrupted state
    valid_pairs = {
        (True, "OK"),
        (False, "STORMGUARD_HALT"),
        (False, "STORMGUARD_STORM_NEW_BLOCKED"),
    }
    for pair in results:
        assert pair in valid_pairs, f"Unexpected validate() result: {pair}"

    # After writer completes, HALT must have been reached
    assert guard.state == StormGuardState.HALT

    # After HALT, a NEW intent must be rejected
    ok, msg = guard.validate(intent)
    assert not ok
    assert msg == "STORMGUARD_HALT"

    # After HALT, a CANCEL intent must still be allowed
    cancel_intent = _make_intent(IntentType.CANCEL)
    ok, msg = guard.validate(cancel_intent)
    assert ok
    assert msg == "OK"
