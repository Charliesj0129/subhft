"""Broker back-pressure: hold new risk while the broker has not answered, release on the answer.

THESHOW 2026-11-02 day session: the order client's broker answered some calls
in 3-30 s and serialised the rest behind them. The order-RTT STORM rule (3
orders >= 2.5 s in 60 s, held 300 s) kept R47 reduce-only for 19.6% of the
session; a replay of the logged NEW spans put "held while a call is waiting"
at 2.0%. These tests pin the replacement:

- ``BrokerStallTracker`` measures silence from our own outstanding calls;
- ``StormGuard.backpressure_blocks`` / ``validate`` hold non-reducing NEW/AMEND
  while silence >= ``broker_backpressure_silence_us``, with no state change;
- a silence >= ``broker_stall_storm_us`` enters STORM through the state machine;
- ``OrderAdapter`` feeds the tracker from ``_run_blocking_call`` (a timed-out
  call keeps counting until its late answer) and re-asks before dispatch;
- risk-config StormGuard keys apply at boot, env still on top.
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.order.adapter import OrderAdapter
from hft_platform.order.broker_stall import BrokerStallTracker
from hft_platform.risk.storm_guard import RiskThresholds, StormGuard

S = 1_000_000_000  # ns per second


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000 * S

    def __call__(self) -> int:
        return self.now


def _intent(intent_type: IntentType = IntentType.NEW, side: Side = Side.BUY, strategy_id: str = "R47") -> OrderIntent:
    return OrderIntent(
        intent_id=7,
        strategy_id=strategy_id,
        symbol="TMFL6",
        intent_type=intent_type,
        side=side,
        price=230_000_000,  # 23000.0 x10000
        qty=1,
        tif=TIF.LIMIT,
    )


def _guard(silence_ns: list[int], **thresholds: int) -> StormGuard:
    guard = StormGuard(RiskThresholds(**thresholds))
    guard.set_broker_stall_provider(lambda: silence_ns[0])
    return guard


# --------------------------------------------------------------------------- #
# BrokerStallTracker                                                           #
# --------------------------------------------------------------------------- #


def test_silence_is_zero_with_nothing_outstanding() -> None:
    tracker = BrokerStallTracker(clock=_Clock())

    assert tracker.silence_ns() == 0
    assert tracker.inflight() == 0


def test_silence_grows_while_a_call_waits_and_ends_on_its_answer() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock)

    token = tracker.sent()
    clock.now += 2 * S
    assert tracker.silence_ns() == 2 * S

    tracker.answered(token)
    assert tracker.silence_ns() == 0
    assert tracker.inflight() == 0


def test_calls_queued_behind_a_stuck_one_count_from_the_oldest() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock)

    stuck = tracker.sent()
    clock.now += 1 * S
    tracker.sent()
    clock.now += 2 * S

    assert tracker.silence_ns() == 3 * S
    tracker.answered(stuck)
    # The later call was sent before that answer arrived: overtaken, so the path
    # is moving and its own wait is not silence.
    assert tracker.silence_ns() == 0


def test_a_call_overtaken_by_a_later_answer_is_not_silence() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock)

    lost = tracker.sent()
    clock.now += S // 10
    later = tracker.sent()
    clock.now += S // 10
    tracker.answered(later)
    clock.now += 5 * S

    assert tracker.inflight() == 1
    assert tracker.silence_ns() == 0
    tracker.answered(lost)
    assert tracker.inflight() == 0


def test_a_call_sent_after_the_last_answer_counts_even_with_an_older_one_lost() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock)

    tracker.sent()  # lost
    clock.now += S // 10
    tracker.answered(tracker.sent())
    clock.now += S
    tracker.sent()  # waiting, sent after the answer
    clock.now += 2 * S

    assert tracker.silence_ns() == 2 * S


def test_an_sdk_exception_stops_the_wait_but_is_not_an_answer() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock)

    first = tracker.sent()
    clock.now += S
    second = tracker.sent()
    clock.now += 2 * S
    tracker.abandoned(first)

    # ``second`` was sent before nothing answered: it still waits, from its own send.
    assert tracker.silence_ns() == 2 * S
    tracker.abandoned(second)
    assert tracker.silence_ns() == 0
    assert tracker.inflight() == 0


def test_each_answer_reports_the_silence_it_ended() -> None:
    clock = _Clock()
    episodes: list[int] = []
    tracker = BrokerStallTracker(clock=clock, on_episode=episodes.append)

    stuck = tracker.sent()
    clock.now += 1 * S
    queued = tracker.sent()
    clock.now += 4 * S
    tracker.answered(stuck)
    clock.now += S // 10
    tracker.answered(queued)  # overtaken by the first answer: no new episode

    assert episodes == [5 * S]


def test_calls_older_than_the_bound_are_dropped() -> None:
    clock = _Clock()
    tracker = BrokerStallTracker(clock=clock, max_age_ns=60 * S)

    tracker.sent()
    clock.now += 61 * S
    tracker.sent()

    assert tracker.inflight() == 1
    assert tracker.silence_ns() == 0


def test_the_entry_count_is_bounded() -> None:
    tracker = BrokerStallTracker(clock=_Clock(), max_entries=4)

    for _ in range(10):
        tracker.sent()

    assert tracker.inflight() == 4


# --------------------------------------------------------------------------- #
# StormGuard gate                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("intent_type", [IntentType.NEW, IntentType.AMEND])
def test_backpressure_holds_new_risk_while_the_broker_is_silent(intent_type) -> None:
    silence = [1 * S]
    guard = _guard(silence)

    assert guard.validate(_intent(intent_type)) == (False, "BROKER_BACKPRESSURE")
    assert guard.state is StormGuardState.NORMAL


def test_backpressure_releases_on_the_answer() -> None:
    silence = [3 * S]
    guard = _guard(silence)
    assert guard.validate(_intent())[0] is False

    silence[0] = 0

    assert guard.validate(_intent()) == (True, "OK")


def test_backpressure_lets_a_short_wait_through() -> None:
    guard = _guard([S - 1])

    assert guard.validate(_intent()) == (True, "OK")


@pytest.mark.parametrize("intent_type", [IntentType.CANCEL, IntentType.FORCE_FLAT])
def test_backpressure_never_holds_a_cancel_or_flatten(intent_type) -> None:
    guard = _guard([30 * S])

    assert guard.validate(_intent(intent_type))[0] is True


def test_backpressure_lets_a_position_reducing_order_through() -> None:
    guard = _guard([5 * S])
    guard.set_position_provider(lambda symbol, strategy_id: 1)  # long one lot

    assert guard.validate(_intent(side=Side.SELL))[0] is True
    assert guard.validate(_intent(side=Side.BUY)) == (False, "BROKER_BACKPRESSURE")


def test_backpressure_lets_a_halt_exempt_strategy_through() -> None:
    guard = StormGuard(halt_exempt_strategies=frozenset({"EXEMPT"}))
    guard.set_broker_stall_provider(lambda: 5 * S)

    assert guard.validate(_intent(strategy_id="EXEMPT"))[0] is True


def test_backpressure_threshold_zero_disarms_it() -> None:
    guard = _guard([30 * S], broker_backpressure_silence_us=0)

    assert guard.backpressure_blocks(_intent()) is False


def test_backpressure_is_off_when_unwired_or_the_probe_fails() -> None:
    unwired = StormGuard()
    failing = StormGuard()

    def _boom() -> int:
        raise RuntimeError("probe failed")

    failing.set_broker_stall_provider(_boom)

    assert unwired.validate(_intent()) == (True, "OK")
    assert failing.validate(_intent()) == (True, "OK")


def test_backpressure_does_not_affect_dispatch_tickets() -> None:
    """``begin_dispatch`` guards an order already on its way; a ticket it
    refuses is only a lost HALT-TOCTOU cancel, not a held order."""
    guard = _guard([5 * S])

    ok, _reason, ticket = guard.begin_dispatch(_intent())

    assert ok is True and ticket is not None


def test_a_long_silence_enters_storm_and_a_shorter_one_does_not() -> None:
    silence = [9 * S]
    guard = _guard(silence)
    assert guard.update() is StormGuardState.NORMAL

    silence[0] = 10 * S

    assert guard.update() is StormGuardState.STORM
    assert guard.state_reason.startswith("Broker silent")


def test_the_long_stall_storm_can_be_disarmed() -> None:
    guard = _guard([60 * S], broker_stall_storm_us=0)

    assert guard.update() is StormGuardState.NORMAL


def test_the_silence_is_published_each_tick() -> None:
    guard = _guard([2 * S])

    guard.update()

    assert guard.metrics.order_broker_silence_seconds._value.get() == pytest.approx(2.0)


def test_the_host_can_set_both_thresholds(monkeypatch) -> None:
    monkeypatch.setenv("HFT_STORMGUARD_BROKER_BACKPRESSURE_SILENCE_US", "500000")
    monkeypatch.setenv("HFT_STORMGUARD_BROKER_STALL_STORM_US", "0")

    guard = StormGuard()

    assert guard.thresholds.broker_backpressure_silence_us == 500_000
    assert guard.thresholds.broker_stall_storm_us == 0


def test_the_defaults_are_one_second_and_ten_seconds() -> None:
    t = RiskThresholds()

    assert (t.broker_backpressure_silence_us, t.broker_stall_storm_us) == (1_000_000, 10_000_000)


# --------------------------------------------------------------------------- #
# Risk config applies at boot                                                  #
# --------------------------------------------------------------------------- #


def _risk_engine(tmp_path, yaml_text: str, storm_guard: StormGuard):
    from hft_platform.risk.engine import RiskEngine

    path = tmp_path / "limits.yaml"
    path.write_text(yaml_text)
    return RiskEngine(str(path), asyncio.Queue(), asyncio.Queue(), storm_guard=storm_guard)


def test_risk_config_stormguard_keys_apply_at_boot(tmp_path) -> None:
    guard = StormGuard()

    _risk_engine(tmp_path, "global_defaults:\n  broker_backpressure_silence_us: 750000\n", guard)

    assert guard.thresholds.broker_backpressure_silence_us == 750_000


def test_env_still_wins_over_the_risk_config_at_boot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HFT_STORMGUARD_BROKER_BACKPRESSURE_SILENCE_US", "2000000")
    guard = StormGuard()

    _risk_engine(tmp_path, "global_defaults:\n  broker_backpressure_silence_us: 750000\n", guard)

    assert guard.thresholds.broker_backpressure_silence_us == 2_000_000


def test_a_risk_config_without_stormguard_keys_changes_nothing(tmp_path) -> None:
    guard = StormGuard()
    before = RiskThresholds()

    _risk_engine(tmp_path, "global_defaults:\n  max_position_lots: 1\n", guard)

    assert guard.thresholds == before


# --------------------------------------------------------------------------- #
# OrderAdapter                                                                 #
# --------------------------------------------------------------------------- #


def _adapter(tmp_path) -> OrderAdapter:
    config_file = tmp_path / "order_config.yaml"
    config_file.write_text("rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n  window_seconds: 10\n")
    client = MagicMock()
    client.get_exchange = MagicMock(return_value="TAIFEX")
    adapter = OrderAdapter(str(config_file), asyncio.Queue(), client)
    adapter.shadow_sink.enabled = False
    return adapter


def _cmd(intent: OrderIntent, cmd_id: int = 1) -> OrderCommand:
    return OrderCommand(
        cmd_id=cmd_id,
        intent=intent,
        deadline_ns=time.monotonic_ns() + 5 * S,  # _api_worker compares against monotonic
        storm_guard_state=StormGuardState.NORMAL,
        created_ns=timebase.now_ns(),
    )


def test_set_storm_guard_hands_over_the_silence_probe(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    guard = StormGuard()

    adapter.set_storm_guard(guard)
    token = adapter._broker_stall.sent()

    assert guard.broker_silence_ns() > 0
    adapter._broker_stall.answered(token)
    assert guard.broker_silence_ns() == 0


@pytest.mark.asyncio
async def test_a_timed_out_call_keeps_counting_until_its_late_answer(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    release = threading.Event()
    late = asyncio.Event()

    def _stuck() -> str:
        release.wait(5)
        return "trade"

    # 20 ms: the shortest wait that still lets the worker thread start; the
    # call is meant to outlive it.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            adapter._run_blocking_call(_stuck, _track_broker=True, _on_late=lambda kind, value: late.set()),
            timeout=0.02,
        )
    assert adapter._broker_stall.inflight() == 1
    assert adapter.broker_silence_ns() > 0

    release.set()
    await asyncio.wait_for(late.wait(), timeout=5)

    assert adapter._broker_stall.inflight() == 0
    assert adapter.broker_silence_ns() == 0


@pytest.mark.asyncio
async def test_an_untracked_call_is_not_counted(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    assert await adapter._run_blocking_call(lambda: 1) == 1

    assert adapter._broker_stall.inflight() == 0


@pytest.mark.asyncio
async def test_a_raising_call_stops_waiting(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    def _boom() -> None:
        raise RuntimeError("sdk")

    with pytest.raises(RuntimeError):
        await adapter._run_blocking_call(_boom, _track_broker=True)

    assert adapter._broker_stall.inflight() == 0


async def _run_worker_until(adapter: OrderAdapter, done: asyncio.Event) -> None:
    task = asyncio.create_task(adapter._api_worker())
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        adapter.running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_the_worker_refuses_a_queued_new_while_the_broker_is_silent(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter.running = True
    adapter.set_storm_guard(StormGuard())
    adapter._broker_stall.sent()  # a call the broker has not answered
    adapter._broker_stall._outstanding[0] -= 2 * S  # ... for 2 s
    sink: asyncio.Queue = asyncio.Queue()
    adapter.set_rejection_sink(sink)
    dispatched: list[OrderCommand] = []

    async def _dispatch(cmd: OrderCommand) -> bool:
        dispatched.append(cmd)
        return True

    adapter._dispatch_to_api = _dispatch
    before = adapter.metrics.order_local_reject_total.labels(reason="broker_backpressure")._value.get()
    dlq_before = len(adapter._dlq._buffer)

    await adapter._api_queue.put(_cmd(_intent()))
    task = asyncio.create_task(adapter._api_worker())
    try:
        feedback = await asyncio.wait_for(sink.get(), timeout=5)
    finally:
        adapter.running = False
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1.0)

    assert dispatched == []
    assert feedback.reason_code == "dispatch_broker_backpressure"
    assert feedback.was_approved is False  # nothing was sent: the slot is free to reuse
    after = adapter.metrics.order_local_reject_total.labels(reason="broker_backpressure")._value.get()
    assert after - before == 1
    assert len(adapter._dlq._buffer) == dlq_before  # a refusal, not a dead letter


@pytest.mark.asyncio
async def test_the_worker_still_dispatches_a_cancel_while_the_broker_is_silent(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter.running = True
    adapter.set_storm_guard(StormGuard())
    adapter._broker_stall.sent()
    adapter._broker_stall._outstanding[0] -= 30 * S
    done = asyncio.Event()
    dispatched: list[OrderCommand] = []

    async def _dispatch(cmd: OrderCommand) -> bool:
        dispatched.append(cmd)
        done.set()
        return True

    adapter._dispatch_to_api = _dispatch
    await adapter._api_queue.put(_cmd(_intent(IntentType.CANCEL)))

    await _run_worker_until(adapter, done)

    assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_the_worker_dispatches_a_new_once_the_broker_answered(tmp_path) -> None:
    adapter = _adapter(tmp_path)
    adapter.running = True
    adapter.set_storm_guard(StormGuard())
    token = adapter._broker_stall.sent()
    adapter._broker_stall._outstanding[token] -= 2 * S
    adapter._broker_stall.answered(token)
    done = asyncio.Event()
    dispatched: list[OrderCommand] = []

    async def _dispatch(cmd: OrderCommand) -> bool:
        dispatched.append(cmd)
        done.set()
        return True

    adapter._dispatch_to_api = _dispatch
    await adapter._api_queue.put(_cmd(_intent()))

    await _run_worker_until(adapter, done)

    assert len(dispatched) == 1


async def _until(predicate, timeout_s: float = 5.0) -> None:
    """Yield to the loop until ``predicate()``; the late answer arrives by call_soon_threadsafe."""
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.005)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["place_order", "cancel_order", "update_order"])
async def test_call_api_tracks_order_calls_past_their_timeout(tmp_path, op) -> None:
    adapter = _adapter(tmp_path)
    adapter._api_timeout_s = 0.02  # the call below is meant to outlive it
    release = threading.Event()

    def _stuck() -> str:
        release.wait(5)
        return "answer"

    await adapter._call_api(op, _stuck, max_retries=0)

    assert adapter._broker_stall.inflight() == 1
    assert adapter.broker_silence_ns() > 0
    release.set()
    await _until(lambda: adapter._broker_stall.inflight() == 0)
    assert adapter.broker_silence_ns() == 0


@pytest.mark.asyncio
async def test_call_api_does_not_track_other_broker_calls(tmp_path) -> None:
    adapter = _adapter(tmp_path)

    await adapter._call_api("list_trades", lambda: [], max_retries=0)

    assert adapter._broker_stall.inflight() == 0
