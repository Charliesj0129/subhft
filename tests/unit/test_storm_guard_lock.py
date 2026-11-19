"""Tests for threading.Lock protection in StormGuard FSM."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
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
        price=5000000,
        qty=1,
        tif=TIF.LIMIT,
    )


def test_state_lock_exists(guard):
    assert hasattr(guard, "_state_lock"), "_state_lock attribute must exist"
    assert isinstance(guard._state_lock, type(threading.Lock())), "_state_lock must be a threading.Lock instance"


def test_concurrent_update_no_corruption(guard):
    """Multiple threads calling update() with halt-level drawdown must result in HALT."""
    n_threads = 10
    barrier = threading.Barrier(n_threads)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait()
            guard.trigger_halt("test halt")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Exceptions in worker threads: {errors}"
    assert guard.state == StormGuardState.HALT, f"Expected HALT after concurrent halt-level updates, got {guard.state}"


def test_concurrent_validate_during_transition(guard):
    """validate() must never see a partially-written state mid-transition."""
    n_readers = 8
    barrier = threading.Barrier(n_readers + 1)
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

    valid_pairs = {
        (True, "OK"),
        (False, "STORMGUARD_HALT"),
        (False, "STORMGUARD_STORM_NEW_BLOCKED"),
    }
    for pair in results:
        assert pair in valid_pairs, f"Unexpected validate() result: {pair}"

    assert guard.state == StormGuardState.HALT

    ok, msg = guard.validate(intent)
    assert not ok
    assert msg == "STORMGUARD_HALT"

    cancel_intent = _make_intent(IntentType.CANCEL)
    ok, msg = guard.validate(cancel_intent)
    assert ok
    assert msg == "OK"


def test_halt_callback_no_deadlock_on_reentrant():
    """A halt callback that calls guard.validate() must not deadlock."""
    callback_completed = threading.Event()
    validate_result: list[tuple[bool, str]] = []

    def reentrant_callback(g: "StormGuard") -> None:
        intent = _make_intent(IntentType.CANCEL)
        result = g.validate(intent)
        validate_result.append(result)
        callback_completed.set()

    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        guard = StormGuard(on_halt_callback=lambda: reentrant_callback(guard))

    t = threading.Thread(target=guard.trigger_halt, args=("deadlock-test",))
    t.start()
    t.join(timeout=3.0)

    assert not t.is_alive(), "trigger_halt deadlocked — thread still alive after 3s"
    assert callback_completed.is_set(), "halt callback did not complete"
    assert guard.state == StormGuardState.HALT
    assert len(validate_result) == 1
    ok, msg = validate_result[0]
    assert ok is True
    assert msg == "OK"


def test_halt_callback_fires_after_lock_released():
    """A halt callback calling trigger_halt() must not deadlock."""
    callback_calls: list[str] = []

    def reentrant_halt_callback(g: "StormGuard") -> None:
        callback_calls.append("entered")
        g.trigger_halt("from_callback")
        callback_calls.append("completed")

    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        guard = StormGuard(on_halt_callback=lambda: reentrant_halt_callback(guard))

    t = threading.Thread(target=guard.trigger_halt, args=("initial-halt",))
    t.start()
    t.join(timeout=3.0)

    assert not t.is_alive(), "trigger_halt deadlocked — thread still alive after 3s"
    assert guard.state == StormGuardState.HALT
    assert callback_calls.count("entered") == 1, (
        f"callback entered {callback_calls.count('entered')} times — recursion detected"
    )
    assert callback_calls.count("completed") == 1


def test_async_halt_callback_uses_threadsafe():
    """When halt callback returns a coroutine, run_coroutine_threadsafe must be used."""

    async def async_callback():
        pass

    with patch("hft_platform.risk.storm_guard.MetricsRegistry.get", return_value=MagicMock()):
        guard = StormGuard(on_halt_callback=async_callback)

    mock_loop = MagicMock()
    mock_future = MagicMock()

    def _close_and_return(coro, loop):
        coro.close()
        return mock_future

    with (
        patch("asyncio.get_running_loop", return_value=mock_loop),
        patch("asyncio.run_coroutine_threadsafe", side_effect=_close_and_return) as mock_rcts,
    ):
        guard.trigger_halt("async-test")

    assert guard.state == StormGuardState.HALT
    assert mock_rcts.call_count == 1
    args = mock_rcts.call_args
    assert args[0][1] is mock_loop
    mock_loop.create_task.assert_not_called()
