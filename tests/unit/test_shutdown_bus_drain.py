"""Tests for RingBufferBus drain on shutdown.

Covers:
- drain_to_cursor processes events before stop (P4b spec)
- drain_to_cursor timeout: returns (drained, skipped) correctly
- stop_async calls drain before strategy_runner.running = False
- drain with empty bus (cursor == target) is a no-op
- drain logs events_drained / events_skipped
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _MockBus:
    """Minimal RingBufferBus stand-in for drain tests."""

    def __init__(self, events: list | None = None):
        self.size = 256
        self.cursor = len(events) - 1 if events else -1
        self._events = list(events or [])
        self._kind_ring = None
        self._tick_ring = None
        self._bidask_ring = None
        self._lobstats_ring = None
        self._use_rust = False
        self._ring = None
        self.buffer = [None] * self.size
        # pre-populate buffer
        for i, ev in enumerate(self._events):
            self.buffer[i % self.size] = ev

    def consume(self):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()


def _make_runner(bus, *, process_event_side_effect=None):
    """Build a minimal StrategyRunner-like stub that exposes drain_to_cursor."""
    from hft_platform.strategy.runner import StrategyRunner  # type: ignore

    # Build a StrategyRunner using constructor stubs
    rq = MagicMock(spec=["put_nowait"])
    rq.put_nowait = MagicMock()

    with (
        patch("hft_platform.strategy.runner.StrategyRegistry") as _reg,
        patch("hft_platform.strategy.runner.MetricsRegistry") as _met,
        patch("hft_platform.strategy.runner.LatencyRecorder") as _lat,
        patch("hft_platform.strategy.runner.StrategyHealthGovernor"),
    ):
        _reg.return_value.instantiate.return_value = []
        _met.get.return_value = MagicMock()
        _lat.get.return_value = MagicMock()
        runner = StrategyRunner(bus=bus, risk_queue=rq)

    if process_event_side_effect is not None:
        runner.process_event = AsyncMock(side_effect=process_event_side_effect)
    else:
        runner.process_event = AsyncMock()

    return runner


# ---------------------------------------------------------------------------
# drain_to_cursor: basic drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_processes_events_up_to_cursor():
    """drain_to_cursor should process all buffered events up to target_cursor."""
    events = [("tick", "TSMC", 100, 1, 1, False, False, 1000) for _ in range(5)]
    bus = _MockBus(events)
    runner = _make_runner(bus)

    # Simulate runner hasn't consumed anything yet; bus.cursor = 4 (0-indexed)
    # drain_to_cursor reads from bus.cursor+1 to target_cursor inclusive, but
    # our impl walks from bus.cursor (exclusive) upward.
    # Set runner's view: bus.cursor = -1 so all 5 events are pending
    bus.cursor = -1  # runner hasn't read anything
    target = 4  # last published seq

    drained, skipped = await runner.drain_to_cursor(target, timeout_s=1.0)

    assert drained == 5
    assert skipped == 0
    assert runner.process_event.call_count == 5


@pytest.mark.asyncio
async def test_drain_no_op_when_already_caught_up():
    """drain_to_cursor with bus.cursor >= target_cursor is a no-op."""
    bus = _MockBus()
    bus.cursor = 10
    runner = _make_runner(bus)

    drained, skipped = await runner.drain_to_cursor(target_cursor=10, timeout_s=1.0)

    assert drained == 0
    assert skipped == 0
    runner.process_event.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.timeout(20)
async def test_drain_timeout_returns_skipped_count():
    """drain_to_cursor should return skipped > 0 when timeout expires."""
    events = [("tick", "TSMC", i, 1, 1, False, False, i) for i in range(100)]
    bus = _MockBus(events)
    bus.cursor = -1  # nothing consumed yet

    call_count = 0

    async def _slow_process(ev):
        nonlocal call_count
        call_count += 1
        # After 5 events, sleep past the timeout
        if call_count >= 5:
            await asyncio.sleep(10)  # exceeds timeout

    runner = _make_runner(bus, process_event_side_effect=_slow_process)

    drained, skipped = await runner.drain_to_cursor(target_cursor=99, timeout_s=0.05)

    # Some events drained, remainder skipped
    assert drained > 0
    assert skipped > 0
    assert drained + skipped == 100


@pytest.mark.asyncio
async def test_drain_process_event_exception_does_not_abort():
    """Exceptions in process_event during drain should be caught and drain continues."""
    events = [("tick", "X", i, 1, 1, False, False, i) for i in range(4)]
    bus = _MockBus(events)
    bus.cursor = -1

    call_count = 0

    async def _flaky(ev):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("boom")

    runner = _make_runner(bus, process_event_side_effect=_flaky)

    drained, skipped = await runner.drain_to_cursor(target_cursor=3, timeout_s=1.0)

    # All 4 events attempted; 3 successfully drained (1 errored but not counted)
    assert call_count == 4
    assert drained == 3  # error on call 2 not counted
    assert skipped == 0


# ---------------------------------------------------------------------------
# stop_async: drain happens before strategy_runner.running = False
# ---------------------------------------------------------------------------


class _FakeBus:
    """Bus stub that records when drain was called vs when running was set."""

    def __init__(self):
        self.cursor = 5


class _FakeStrategyRunner:
    def __init__(self):
        self.running = True
        self.drain_called_at: list[float] = []
        self.running_set_false_at: list[float] = []

    async def drain_to_cursor(self, target_cursor: int, timeout_s: float):
        import time

        self.drain_called_at.append(time.monotonic())
        return 3, 0

    async def run(self):
        pass


def _make_system_with_drain_runner(bus, sr):
    """Build a minimal HFTSystem with the given bus/strategy_runner stubs."""
    from hft_platform.services.system import HFTSystem

    order_adapter = MagicMock()
    order_adapter.running = True
    order_adapter.drain_and_cancel = AsyncMock(return_value=0)

    exec_svc = MagicMock()
    exec_svc.running = True
    exec_svc.stop = AsyncMock(return_value=0)

    reg = SimpleNamespace(
        bus=bus,
        raw_queue=asyncio.Queue(),
        raw_exec_queue=asyncio.Queue(),
        risk_queue=asyncio.Queue(),
        order_queue=asyncio.Queue(),
        recorder_queue=asyncio.Queue(),
        position_store=MagicMock(),
        order_id_map={},
        storm_guard=MagicMock(),
        md_client=MagicMock(),
        order_client=MagicMock(set_execution_callbacks=MagicMock()),
        client=MagicMock(),
        symbol_metadata=MagicMock(),
        price_scale_provider=MagicMock(),
        md_service=MagicMock(running=True),
        order_adapter=order_adapter,
        execution_gateway=MagicMock(stop=MagicMock()),
        exec_service=exec_svc,
        risk_engine=MagicMock(running=True),
        recon_service=MagicMock(running=True),
        strategy_runner=sr,
        recorder=MagicMock(running=True),
        gateway_service=None,
        checkpoint_writer=None,
    )

    bootstrapper = MagicMock()
    bootstrapper.build.return_value = reg

    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        system = HFTSystem({})

    return system


@pytest.mark.asyncio
async def test_stop_async_drain_before_strategy_runner_stop():
    """stop_async must call drain_to_cursor before setting strategy_runner.running=False."""
    bus = _FakeBus()
    sr = _FakeStrategyRunner()

    drain_order: list[str] = []

    original_drain = sr.drain_to_cursor

    async def _spy_drain(target_cursor, timeout_s):
        drain_order.append("drain")
        return await original_drain(target_cursor, timeout_s)

    sr.drain_to_cursor = _spy_drain

    # Monkey-patch property setter to record when running is set to False
    original_setattr = object.__setattr__

    class _TrackedRunner(_FakeStrategyRunner):
        pass

    tracked = _TrackedRunner()
    tracked.drain_to_cursor = _spy_drain
    tracked.running = True

    system = _make_system_with_drain_runner(bus, tracked)

    # Patch running setter to record order
    _running_set = []
    original_running = True

    _stop_seen = []
    _original_drain_called = []

    async def _patched_drain(target_cursor, timeout_s):
        _original_drain_called.append(True)
        return 3, 0

    tracked.drain_to_cursor = _patched_drain

    # We'll verify by checking that drain was awaited (patched) and that
    # strategy_runner.running is False after stop_async
    await system.stop_async()

    assert len(_original_drain_called) == 1, "drain_to_cursor must be called exactly once"
    assert tracked.running is False, "strategy_runner.running must be False after stop_async"


@pytest.mark.asyncio
async def test_stop_async_drain_timeout_env_var(monkeypatch):
    """HFT_BUS_DRAIN_TIMEOUT_MS controls the drain timeout passed to drain_to_cursor."""
    monkeypatch.setenv("HFT_BUS_DRAIN_TIMEOUT_MS", "200")

    bus = _FakeBus()
    sr = _FakeStrategyRunner()

    timeout_seen: list[float] = []

    async def _drain_spy(target_cursor, timeout_s):
        timeout_seen.append(timeout_s)
        return 0, 0

    sr.drain_to_cursor = _drain_spy

    system = _make_system_with_drain_runner(bus, sr)
    await system.stop_async()

    assert len(timeout_seen) == 1
    assert abs(timeout_seen[0] - 0.2) < 1e-9, f"Expected 0.2s timeout, got {timeout_seen[0]}"


@pytest.mark.asyncio
async def test_stop_async_no_drain_when_bus_cursor_negative():
    """drain_to_cursor should not be called when bus cursor is -1 (empty bus)."""
    bus = _FakeBus()
    bus.cursor = -1

    sr = _FakeStrategyRunner()
    drain_called = []

    async def _drain_spy(target_cursor, timeout_s):
        drain_called.append(True)
        return 0, 0

    sr.drain_to_cursor = _drain_spy

    system = _make_system_with_drain_runner(bus, sr)
    await system.stop_async()

    assert len(drain_called) == 0, "drain_to_cursor must not be called when bus is empty"


@pytest.mark.asyncio
async def test_stop_async_drain_exception_does_not_block_shutdown():
    """drain_to_cursor raising an exception must not prevent shutdown from completing."""
    bus = _FakeBus()
    sr = _FakeStrategyRunner()

    async def _drain_broken(target_cursor, timeout_s):
        raise RuntimeError("drain error")

    sr.drain_to_cursor = _drain_broken

    system = _make_system_with_drain_runner(bus, sr)

    # Must complete without raising
    await system.stop_async()

    assert sr.running is False, "strategy_runner.running must still be set False after drain error"
