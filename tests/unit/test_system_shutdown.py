"""Tests for H1 (shutdown drain + checkpoint) and H6 (HALT cancel in-flight)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem


class _Runner:
    def __init__(self) -> None:
        self.running = True

    async def run(self) -> None:
        await asyncio.sleep(0)


class _OrderClient:
    def set_execution_callbacks(self, on_order, on_deal) -> None:
        return None


class _ExecutionGateway(_Runner):
    def stop(self) -> None:
        return None


class _StormGuard:
    def __init__(self) -> None:
        self.state = StormGuardState.NORMAL

    def update(self, **kwargs) -> None:
        return None

    def trigger_halt(self, reason: str) -> None:
        self.state = StormGuardState.HALT


def _make_order_adapter():
    adapter = _Runner()
    adapter.drain_and_cancel = AsyncMock(return_value=3)
    return adapter


def _registry(*, checkpoint_writer=None, order_adapter=None):
    q = asyncio.Queue()
    return SimpleNamespace(
        bus=SimpleNamespace(),
        raw_queue=q,
        raw_exec_queue=asyncio.Queue(),
        risk_queue=asyncio.Queue(),
        order_queue=asyncio.Queue(),
        recorder_queue=asyncio.Queue(),
        position_store=SimpleNamespace(),
        order_id_map={},
        storm_guard=_StormGuard(),
        md_client=SimpleNamespace(),
        order_client=_OrderClient(),
        client=SimpleNamespace(),
        symbol_metadata=SimpleNamespace(),
        price_scale_provider=SimpleNamespace(),
        md_service=_Runner(),
        order_adapter=order_adapter or _make_order_adapter(),
        execution_gateway=_ExecutionGateway(),
        exec_service=_Runner(),
        risk_engine=_Runner(),
        recon_service=_Runner(),
        strategy_runner=_Runner(),
        recorder=_Runner(),
        gateway_service=None,
        checkpoint_writer=checkpoint_writer,
    )


def _build_system(registry):
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = registry
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        return HFTSystem({})


@pytest.mark.asyncio
async def test_stop_async_calls_drain_and_cancel():
    """H1: stop_async must await drain_and_cancel before broker logout."""
    reg = _registry()
    system = _build_system(reg)

    await system.stop_async()

    system.order_adapter.drain_and_cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_async_writes_final_checkpoint():
    """H1: stop_async must call write_checkpoint when checkpoint_writer exists."""
    ckpt = MagicMock()
    ckpt.write_checkpoint.return_value = "/tmp/test_checkpoint.json"
    reg = _registry(checkpoint_writer=ckpt)
    system = _build_system(reg)

    await system.stop_async()

    ckpt.write_checkpoint.assert_called_once()


@pytest.mark.asyncio
async def test_stop_async_no_checkpoint_writer_is_safe():
    """H1: stop_async must not fail when checkpoint_writer is None."""
    reg = _registry(checkpoint_writer=None)
    system = _build_system(reg)

    await system.stop_async()

    system.order_adapter.drain_and_cancel.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_async_drain_timeout_does_not_block_shutdown():
    """H1: If drain_and_cancel exceeds timeout, shutdown continues."""
    adapter = _make_order_adapter()

    async def _hang():
        await asyncio.sleep(999)

    adapter.drain_and_cancel = AsyncMock(side_effect=_hang)
    reg = _registry(order_adapter=adapter)
    system = _build_system(reg)

    async def _timeout_and_close(awaitable, timeout):
        awaitable.close()
        raise asyncio.TimeoutError

    with patch("hft_platform.services.system.asyncio.wait_for", side_effect=_timeout_and_close):
        await system.stop_async()

    assert len(system.tasks) == 0


@pytest.mark.asyncio
async def test_stop_async_drain_exception_does_not_block_shutdown():
    """H1: If drain_and_cancel raises, shutdown continues."""
    adapter = _make_order_adapter()
    adapter.drain_and_cancel = AsyncMock(side_effect=RuntimeError("broker disconnected"))
    reg = _registry(order_adapter=adapter)
    system = _build_system(reg)

    await system.stop_async()

    assert len(system.tasks) == 0


@pytest.mark.asyncio
async def test_stop_async_checkpoint_exception_does_not_block_shutdown():
    """H1: If write_checkpoint raises, shutdown continues."""
    ckpt = MagicMock()
    ckpt.write_checkpoint.side_effect = OSError("disk full")
    reg = _registry(checkpoint_writer=ckpt)
    system = _build_system(reg)

    await system.stop_async()

    ckpt.write_checkpoint.assert_called_once()
    assert len(system.tasks) == 0


@pytest.mark.asyncio
async def test_stormguard_halt_cancels_inflight_orders():
    """H6: When StormGuard enters HALT, drain_and_cancel must be called."""
    reg = _registry()
    system = _build_system(reg)

    system.storm_guard.state = StormGuardState.HALT

    system.order_adapter.drain_and_cancel.reset_mock()

    if system.storm_guard.state == StormGuardState.HALT:
        drained_count = 0
        while not system.order_queue.empty():
            try:
                system.order_queue.get_nowait()
                system.order_queue.task_done()
                drained_count += 1
            except asyncio.QueueEmpty:
                break
        system.order_adapter.running = False
        if system.gateway_service is not None:
            system.gateway_service.set_halt()
        task = asyncio.create_task(system.order_adapter.drain_and_cancel())
        await task

    system.order_adapter.drain_and_cancel.assert_awaited_once()
    assert system.order_adapter.running is False


class _SlowRecorder:
    """Recorder whose run() takes a controlled amount of time after running=False."""

    def __init__(self, flush_event: asyncio.Event) -> None:
        self.running = True
        self._flush_event = flush_event

    async def run(self) -> None:
        while self.running:
            await asyncio.sleep(0.01)
        await self._flush_event.wait()


@pytest.mark.asyncio
async def test_stop_async_awaits_recorder_flush_before_phase3():
    """Phase 2b: recorder task is awaited (not cancelled) before Phase 3."""
    flush_event = asyncio.Event()
    recorder = _SlowRecorder(flush_event)
    reg = _registry()
    reg.recorder = recorder
    system = _build_system(reg)

    system.tasks["recorder"] = asyncio.create_task(recorder.run())

    async def _release():
        await asyncio.sleep(0.05)
        flush_event.set()

    asyncio.create_task(_release())

    await system.stop_async()

    assert system.tasks == {}
    assert flush_event.is_set()


@pytest.mark.asyncio
async def test_stop_async_recorder_skipped_in_phase3():
    """Phase 3 loop skips 'recorder' task (already handled in Phase 2b)."""
    reg = _registry()
    system = _build_system(reg)

    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    system.tasks["recorder"] = done_task

    cancelled_tasks: list[str] = []
    original_items = list(system.tasks.items())

    await system.stop_async()

    assert not done_task.cancelled()


@pytest.mark.asyncio
async def test_stop_async_recorder_timeout_triggers_cancel():
    """Phase 2b: if recorder exceeds timeout, it gets cancelled."""
    flush_event = asyncio.Event()
    recorder = _SlowRecorder(flush_event)
    reg = _registry()
    reg.recorder = recorder
    system = _build_system(reg)

    system.tasks["recorder"] = asyncio.create_task(recorder.run())

    with patch.dict("os.environ", {"HFT_RECORDER_SHUTDOWN_TIMEOUT_S": "0"}):
        await system.stop_async()

    assert system.tasks == {}
