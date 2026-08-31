"""The degrade path must not stall the event loop while it persists evidence.

``runtime_state_store.locked_state`` takes an exclusive ``flock`` it polls for up
to ``DEFAULT_LOCK_TIMEOUT_S`` (2 s) and then fsyncs twice. The operator CLI holds
the same lock from another process, so contention is a normal condition, not a
fault. The callers are ``AutonomyMonitor._check_margin`` and the supervisor's
degrade update -- the moments when the loop must keep cancelling orders.

Against a 1 ms loop budget, a bounded 2 s block is not a fix for an unbounded
one. These tests hold the lock for real and measure the loop, rather than
asserting that a particular call was wrapped.
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from hft_platform.ops.evidence import AutonomyEvidenceWriter
from hft_platform.ops.platform_degrade import PlatformDegradeController
from hft_platform.ops.runtime_state_store import locked_state

# Long enough that a blocking implementation cannot finish inside it, short
# enough to stay well under the lock's own 2 s deadline so the write still
# succeeds and the test asserts responsiveness, not failure.
HOLD_S = 0.35
# The loop is polled every 5 ms; a blocked loop records one giant gap instead.
TICK_S = 0.005
MAX_STALL_S = 0.15


async def _loop_stall_while(coro) -> tuple[float, object]:
    """Run ``coro``, sampling the event loop; return (worst gap, result).

    The final segment -- from the sampler's last tick to the moment the
    operation returns -- is measured by the CALLER, not the sampler. A sampler
    that only compares its own consecutive ticks reports 0.0 against a fully
    blocking implementation, because a blocked loop never runs it: the very
    failure being tested erases its own evidence.
    """
    worst = 0.0
    last = time.monotonic()

    async def sampler() -> None:
        nonlocal worst, last
        while True:
            await asyncio.sleep(TICK_S)
            now = time.monotonic()
            worst = max(worst, now - last)
            last = now

    watcher = asyncio.create_task(sampler())
    try:
        result = await coro
        worst = max(worst, time.monotonic() - last)
    finally:
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    return worst, result


def _hold_the_lock(path: Path, seconds: float) -> threading.Thread:
    """Hold the state lock from a worker thread, as the operator CLI would.

    The handshake is an Event set from INSIDE the locked block. Waiting for the
    lock *file* to appear does not work: the file outlives every acquisition, so
    after any earlier write the check is vacuous and the test races -- which is
    how an earlier version of this file passed against blocking code.
    """
    holding = threading.Event()

    def hold() -> None:
        with locked_state(path):
            holding.set()
            time.sleep(seconds)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert holding.wait(timeout=5.0), "the lock holder never acquired the lock"
    return t


@pytest.mark.asyncio
async def test_entering_reduce_only_does_not_stall_the_loop_while_the_lock_is_held(tmp_path):
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    holder = _hold_the_lock(tmp_path / "runtime_state.json", HOLD_S)

    worst, _ = await _loop_stall_while(controller.enter_reduce_only_async(reason="margin_critical"))

    holder.join(timeout=5)
    assert worst < MAX_STALL_S, f"event loop stalled {worst * 1000:.0f} ms during reduce-only persistence"


@pytest.mark.asyncio
async def test_reduce_only_takes_effect_before_the_write_is_awaited(tmp_path):
    """Offloading must not open a window where orders still flow.

    The in-memory half is what stops trading; if it moved into the thread, a
    margin breach would keep accepting orders until the disk caught up. This is
    the fail-open the fix must not introduce while removing a fail-slow.
    """
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    holder = _hold_the_lock(tmp_path / "runtime_state.json", HOLD_S)

    task = asyncio.create_task(controller.enter_reduce_only_async(reason="margin_critical"))
    await asyncio.sleep(0)  # one loop turn: past the sync half, into the await

    assert controller.reduce_only_active is True
    assert not task.done(), "premise: the write is still in flight behind the held lock"

    await task
    holder.join(timeout=5)


@pytest.mark.asyncio
async def test_the_awaited_write_still_commits(tmp_path):
    """Off-loop must not mean fire-and-forget: the caller's ordering is unchanged."""
    import json

    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)

    await controller.enter_reduce_only_async(reason="margin_critical")

    state = json.loads((tmp_path / "runtime_state.json").read_text())
    assert state["platform"]["manual_rearm_required"] is True
    assert "margin_critical" in str(state["platform"]["reason"])


@pytest.mark.asyncio
async def test_auto_recovery_does_not_stall_the_loop(tmp_path):
    """The supervisor calls this every cycle; the recovering cycle writes.

    The lock is not the vehicle here. An auto-recovery exit carries
    ``manual_rearm_required=False`` and is not a strategy re-arm ack, so
    ``record_transition`` deliberately skips ``_update_runtime_state`` -- a
    recovery that cannot prove it is one must not clear a latch. It still writes
    the audit trail: a jsonl append, a summary read-modify-write and a markdown
    append, all synchronous file IO.

    So this measures the property directly -- whatever the writer does, it must
    not do it on the loop -- by making the writer slow, rather than by relying on
    an internal path that this transition does not take.
    """
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    real = writer.record_transition

    def slow_record(**kwargs):
        time.sleep(HOLD_S)
        return real(**kwargs)

    writer.record_transition = slow_record  # type: ignore[method-assign]

    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    controller._auto_recovery_enabled = True
    await controller.enter_reduce_only_async(reason="feed_reconnect_pending")

    now = 10**12
    await controller.check_auto_recovery_async(current_reasons=[], now_ns=now)  # starts cooldown
    later = now + int((controller._auto_recovery_cooldown_s + 1) * 1e9)

    worst, recovered = await _loop_stall_while(controller.check_auto_recovery_async(current_reasons=[], now_ns=later))

    assert recovered is True, "premise: this is the cycle that actually recovers and writes"
    assert controller.reduce_only_active is False
    assert worst < MAX_STALL_S, f"event loop stalled {worst * 1000:.0f} ms during auto-recovery"


@pytest.mark.asyncio
async def test_manual_platform_rearm_does_not_stall_the_loop_while_the_lock_is_held(tmp_path):
    """The operator path was the one un-awaited call left on the supervisor tick.

    ``_update_platform_degrade_state`` offloaded its state read and awaited every
    other member of the family, then called ``_consume_platform_rearm_request``
    synchronously -- straight into ``force_clear`` -> ``exit_reduce_only`` ->
    ``record_transition``. The stall therefore landed exactly while permissions
    were being restored and cancellation and risk processing had to stay live.
    """
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    controller.enter_reduce_only(reason="margin_critical")
    holder = _hold_the_lock(tmp_path / "runtime_state.json", HOLD_S)

    worst, transition = await _loop_stall_while(controller.force_clear_async(reason="manual_rearm_gate"))

    holder.join(timeout=5)
    assert transition is not None
    assert controller.reduce_only_active is False
    assert worst < MAX_STALL_S, f"event loop stalled {worst * 1000:.0f} ms during manual re-arm"


@pytest.mark.asyncio
async def test_manual_rearm_takes_effect_before_the_write_is_awaited(tmp_path):
    """With nothing still firing, permissions come back on the loop, not on the disk.

    This is the half of the behaviour that SHOULD be immediate: the operator
    attested the conditions are safe and no input contradicts them.
    """
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    controller.enter_reduce_only(reason="margin_critical")
    holder = _hold_the_lock(tmp_path / "runtime_state.json", HOLD_S)

    task = asyncio.create_task(controller.force_clear_async(reason="manual_rearm_gate"))
    await asyncio.sleep(0)  # one loop turn: past the sync half, into the await

    assert controller.reduce_only_active is False
    assert not task.done(), "premise: the write is still in flight behind the held lock"

    await task
    holder.join(timeout=5)


@pytest.mark.asyncio
async def test_manual_rearm_never_opens_risk_while_a_reason_is_still_firing(tmp_path):
    """Taking the stall off the loop must not put a fail-open in its place.

    The synchronous predecessor cleared reduce-only and re-checked live reasons
    with no suspension between the two. Awaiting the audit write in the middle
    made ``allow_open()`` true for the whole write latency -- during exactly the
    condition reduce-only exists to contain, and on a contended lock that is
    hundreds of milliseconds of unrestricted order flow.

    The first version of the test above asserted that mid-flight clear as if it
    were the goal, which is why this one names the input that must survive it.
    """
    writer = AutonomyEvidenceWriter(base_dir=tmp_path)
    controller = PlatformDegradeController(evidence_writer=writer, metrics=None)
    controller.enter_reduce_only(reason="queue_depth_exceeded")
    holder = _hold_the_lock(tmp_path / "runtime_state.json", HOLD_S)

    task = asyncio.create_task(
        controller.force_clear_async(reason="manual_rearm_gate", current_reasons=["queue_depth_exceeded"])
    )

    # Sample allow_open on every loop turn for the whole life of the write.
    opened = False
    while not task.done():
        await asyncio.sleep(0)
        opened = opened or controller.allow_open()

    await task
    holder.join(timeout=5)
    assert not opened, "allow_open() was true while queue_depth_exceeded was still firing"
    assert controller.reduce_only_active is True
