"""The shared condition-wait helpers: they return as soon as the condition holds and fail loudly when it never does."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from tests import waiting
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


# --- the polling schedule, on a fake clock ----------------------------------------------------------------------


class _FakeClock:
    """Stands in for ``time`` and ``asyncio`` inside ``tests.waiting`` only: sleeping advances a fake monotonic clock,
    so the schedule is checked exactly and nothing really sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds

    async def async_sleep(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    fake = _FakeClock()
    monkeypatch.setattr(waiting, "time", SimpleNamespace(monotonic=fake.monotonic, sleep=fake.sleep))
    monkeypatch.setattr(waiting, "asyncio", SimpleNamespace(sleep=fake.async_sleep))
    return fake


def test_wait_until_sleeps_poll_s_between_checks_and_never_before_the_first(clock: _FakeClock) -> None:
    checks: list[float] = []

    def predicate() -> bool:
        checks.append(clock.now)
        return len(checks) == 4

    wait_until(predicate, poll_s=0.25)

    assert checks == [0.0, 0.25, 0.5, 0.75]
    assert clock.slept == [0.25, 0.25, 0.25]


def test_wait_until_does_not_sleep_when_the_condition_already_holds(clock: _FakeClock) -> None:
    wait_until(lambda: True)

    assert clock.slept == []


def test_wait_until_gives_up_once_the_deadline_has_passed(clock: _FakeClock) -> None:
    with pytest.raises(AssertionError, match="timed out after 1.0s"):
        wait_until(lambda: False, timeout_s=1.0, poll_s=0.4)

    assert clock.slept == [0.4, 0.4, 0.4]  # 0.4, 0.8, then 1.2 is past the deadline


def test_wait_until_default_timeout_is_five_seconds(clock: _FakeClock) -> None:
    with pytest.raises(AssertionError, match="timed out after 5.0s"):
        wait_until(lambda: False, poll_s=1.0)

    assert clock.slept == [1.0] * 5


@pytest.mark.asyncio
async def test_await_until_sleeps_poll_s_between_checks_and_never_before_the_first(clock: _FakeClock) -> None:
    checks: list[float] = []

    def predicate() -> bool:
        checks.append(clock.now)
        return len(checks) == 4

    await await_until(predicate, poll_s=0.25)

    assert checks == [0.0, 0.25, 0.5, 0.75]
    assert clock.slept == [0.25, 0.25, 0.25]


@pytest.mark.asyncio
async def test_await_until_gives_up_once_the_deadline_has_passed(clock: _FakeClock) -> None:
    with pytest.raises(AssertionError, match="timed out after 1.0s"):
        await await_until(lambda: False, timeout_s=1.0, poll_s=0.4)

    assert clock.slept == [0.4, 0.4, 0.4]


@pytest.mark.asyncio
async def test_await_until_default_timeout_is_five_seconds(clock: _FakeClock) -> None:
    with pytest.raises(AssertionError, match="timed out after 5.0s"):
        await await_until(lambda: False, poll_s=1.0)

    assert clock.slept == [1.0] * 5


@pytest.mark.asyncio
async def test_await_until_checks_once_more_after_the_deadline_passes(clock: _FakeClock) -> None:
    calls = []

    def predicate() -> bool:
        calls.append(1)
        if len(calls) == 1:
            clock.now += 10.0  # a slow probe: the deadline passes while it runs, and it reports False
            return False
        return True  # the condition is true by the time of the final check

    await await_until(predicate, timeout_s=1.0, poll_s=0.005)

    assert len(calls) == 2
    assert clock.slept == []


# --- what counts as "holds": any truthy value, not only True -------------------------------------------------------


@pytest.mark.parametrize("value", [1, "ready", [0], {"key": 1}])
def test_wait_until_accepts_any_truthy_value(value: object) -> None:
    looks: list[object] = []

    def probe() -> object:
        looks.append(value)
        return value

    wait_until(probe, timeout_s=0.5)

    assert looks == [value]  # truthy on the first look: no second poll


@pytest.mark.parametrize("value", [0, "", [], None])
def test_wait_until_does_not_accept_a_falsy_value(value: object) -> None:
    with pytest.raises(AssertionError):
        wait_until(lambda: value, timeout_s=0.02, poll_s=0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, "ready", [0], {"key": 1}])
async def test_await_until_accepts_any_truthy_value(value: object) -> None:
    looks: list[object] = []

    def probe() -> object:
        looks.append(value)
        return value

    await await_until(probe, timeout_s=0.5)

    assert looks == [value]  # truthy on the first look: no second poll


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [0, "", [], None])
async def test_await_until_does_not_accept_a_falsy_value(value: object) -> None:
    with pytest.raises(AssertionError):
        await await_until(lambda: value, timeout_s=0.02, poll_s=0.005)
