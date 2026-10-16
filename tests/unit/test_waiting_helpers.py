"""The shared condition-wait helpers: they return as soon as the condition holds and fail loudly when it never does."""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from tests.waiting import await_until, wait_until


def test_wait_until_returns_when_condition_already_true() -> None:
    calls = []

    def predicate() -> bool:
        calls.append(1)
        return True

    assert wait_until(predicate) is None
    assert len(calls) == 1


def test_wait_until_returns_as_soon_as_condition_becomes_true() -> None:
    state = {"checks": 0}

    def predicate() -> bool:
        state["checks"] += 1
        return state["checks"] >= 3

    wait_until(predicate, poll_s=0.001)
    assert state["checks"] == 3


def test_wait_until_sees_a_change_made_by_another_thread() -> None:
    done = threading.Event()
    worker = threading.Thread(target=done.set)
    worker.start()
    wait_until(done.is_set, message="worker never ran")
    worker.join(timeout=1.0)
    assert done.is_set()


def test_wait_until_raises_with_the_message_when_condition_never_holds() -> None:
    with pytest.raises(AssertionError, match="gauge was never set") as info:
        wait_until(lambda: False, timeout_s=0.03, poll_s=0.005, message="gauge was never set")
    assert "0.03" in str(info.value)


def test_wait_until_checks_once_more_after_the_deadline_passes() -> None:
    calls = []

    def predicate() -> bool:
        calls.append(1)
        if len(calls) == 1:
            time.sleep(0.04)  # a slow probe: the deadline (0.01 s) passes while it runs; it then reports False
            return False
        return True  # the condition is true by the time of the final check

    wait_until(predicate, timeout_s=0.01, poll_s=0.005)
    assert len(calls) == 2


def test_wait_until_propagates_an_exception_from_the_predicate() -> None:
    def predicate() -> bool:
        raise ValueError("probe broke")

    with pytest.raises(ValueError, match="probe broke"):
        wait_until(predicate, timeout_s=1.0)


@pytest.mark.asyncio
async def test_await_until_lets_the_loop_run_the_code_under_test() -> None:
    flag = {"set": False}

    async def setter() -> None:
        flag["set"] = True

    task = asyncio.create_task(setter())
    await await_until(lambda: flag["set"], message="setter never ran")
    await task
    assert flag["set"] is True


@pytest.mark.asyncio
async def test_await_until_raises_with_the_message_when_condition_never_holds() -> None:
    with pytest.raises(AssertionError, match="the sink never received a write"):
        await await_until(lambda: False, timeout_s=0.03, poll_s=0.005, message="the sink never received a write")


@pytest.mark.asyncio
async def test_await_until_propagates_an_exception_from_the_predicate() -> None:
    def predicate() -> bool:
        raise KeyError("probe broke")

    with pytest.raises(KeyError):
        await await_until(predicate, timeout_s=1.0)
