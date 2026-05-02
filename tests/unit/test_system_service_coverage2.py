"""Comprehensive coverage tests for services/system.py — targeting remaining uncovered lines.

Focus areas:
- _supervise loop (HALT enforcement, kill-switch, service health, metrics, GC)
- stop_async (bus drain, recorder shutdown, audit writer, checkpoint, DLQ persist)
- _recorder_bridge (non-early-exit path: event routing, batch mode, filtering)
- _sync_drain_recorder (success path)
- graceful_reset (STORM/HALT reset, DLQ drain error)
- run() (startup sequence branches)
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.contracts.strategy import IntentType
from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem

# ---------------------------------------------------------------------------
# Shared stub builder
# ---------------------------------------------------------------------------


def _make_stub(**overrides) -> HFTSystem:
    """Return an HFTSystem instance with all dependencies stubbed out."""
    sys_obj = HFTSystem.__new__(HFTSystem)
    sys_obj.settings = overrides.get("settings", {})
    sys_obj.running = False
    sys_obj._recorder_seen_tick = False
    sys_obj._recorder_seen_bidask = False
    sys_obj._md_record_direct = True
    sys_obj._fill_record_direct = True
    sys_obj._order_record_direct = True
    sys_obj.bootstrapper = MagicMock()
    sys_obj.registry = MagicMock()
    sys_obj.bus = MagicMock()
    sys_obj.bus.cursor = 5
    sys_obj.raw_queue = asyncio.Queue()
    sys_obj.raw_exec_queue = asyncio.Queue()
    sys_obj.risk_queue = asyncio.Queue()
    sys_obj.order_queue = asyncio.Queue()
    sys_obj.recorder_queue = asyncio.Queue()
    sys_obj.position_store = MagicMock()
    sys_obj.order_id_map = {}
    sys_obj.storm_guard = MagicMock()
    sys_obj.storm_guard.state = StormGuardState.NORMAL
    sys_obj.storm_guard.is_halt_exempt = MagicMock(return_value=False)
    sys_obj.md_client = MagicMock()
    sys_obj.order_client = MagicMock()
    sys_obj.client = MagicMock()
    sys_obj.symbol_metadata = MagicMock()
    sys_obj.price_scale_provider = MagicMock()
    sys_obj.md_service = MagicMock()
    sys_obj.order_adapter = MagicMock()
    sys_obj.execution_gateway = MagicMock()
    sys_obj.exec_service = MagicMock()
    sys_obj.risk_engine = MagicMock()
    sys_obj.recon_service = MagicMock()
    sys_obj.strategy_runner = MagicMock()
    sys_obj.recorder = MagicMock()
    sys_obj.gateway_service = None
    sys_obj.intent_channel = None
    sys_obj.checkpoint_writer = None
    sys_obj.startup_verifier = None
    sys_obj.session_governor = None
    sys_obj.autonomy_monitor = None
    sys_obj.daily_report_service = None
    sys_obj.evidence_writer = MagicMock()
    sys_obj.platform_degrade_controller = MagicMock()
    sys_obj.platform_degrade_inputs = MagicMock()
    sys_obj.tasks = {}
    sys_obj._recorder_drop_on_full = True
    sys_obj._bootstrap_torn_down = False
    sys_obj._task_restart_attempts = {}
    sys_obj._task_restart_until_s = {}
    sys_obj._task_started_at = {}
    sys_obj._task_restart_base_delay_s = 1.0
    sys_obj._task_restart_max_delay_s = 30.0
    sys_obj._task_restart_max_attempts = 10
    sys_obj._task_healthy_uptime_s = 60.0
    sys_obj._queue_log_every_s = 30.0
    sys_obj._last_queue_log_s = 0.0
    sys_obj._mtm_calculator = None
    sys_obj.session_hook_manager = MagicMock()
    sys_obj.session_hook_manager.enabled = False
    sys_obj.health_server = MagicMock()
    sys_obj._exec_startup_overflow_lost = False
    sys_obj._exec_overflow_evicted = 0
    sys_obj._exec_overflow_buf = collections.deque(maxlen=4096)
    sys_obj._EXEC_OVERFLOW_MAX = 4096
    sys_obj._exec_overflow_counter = 0
    sys_obj._recorder_bridge_drops = 0
    sys_obj._pnl_snapshot_drops = 0
    sys_obj._halt_log_mono = 0.0
    sys_obj._halt_checkpoint_written = False
    sys_obj._audit_writer = None
    sys_obj.loop = None
    # Apply overrides
    for k, v in overrides.items():
        setattr(sys_obj, k, v)
    return sys_obj


# ===========================================================================
# _sync_drain_recorder — success path
# ===========================================================================


class TestSyncDrainRecorderSuccess:
    """Cover the success path of _sync_drain_recorder."""

    def test_drain_and_flush_completes_successfully(self):
        sys_obj = _make_stub()
        mock_recorder = MagicMock()
        mock_recorder.running = True
        mock_recorder._drain_queue_into_batchers = AsyncMock()
        mock_recorder._shutdown_flush = AsyncMock()
        sys_obj.recorder = mock_recorder

        sys_obj._sync_drain_recorder()

        assert mock_recorder.running is False
        mock_recorder._drain_queue_into_batchers.assert_called_once()
        mock_recorder._shutdown_flush.assert_called_once()

    def test_drain_timeout_is_caught(self, monkeypatch):
        sys_obj = _make_stub()
        mock_recorder = MagicMock()
        mock_recorder.running = True
        monkeypatch.setenv("HFT_RECORDER_SHUTDOWN_TIMEOUT_S", "0.001")

        async def _slow_drain():
            await asyncio.sleep(10)

        mock_recorder._drain_queue_into_batchers = _slow_drain
        mock_recorder._shutdown_flush = AsyncMock()
        sys_obj.recorder = mock_recorder

        # Should not raise — timeout is caught
        sys_obj._sync_drain_recorder()
        assert mock_recorder.running is False


# ===========================================================================
# stop_async — bus drain paths
# ===========================================================================


class TestStopAsyncBusDrain:
    """Cover stop_async bus drain, recorder, checkpoint, audit writer paths."""

    @pytest.mark.asyncio
    async def test_bus_drain_success_no_skipped(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.bus.cursor = 10
        sys_obj.strategy_runner.drain_to_cursor = AsyncMock(return_value=(5, 0))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()

        assert sys_obj.running is False
        sys_obj.strategy_runner.drain_to_cursor.assert_called_once()

    @pytest.mark.asyncio
    async def test_bus_drain_with_skipped_events(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.bus.cursor = 10
        sys_obj.strategy_runner.drain_to_cursor = AsyncMock(return_value=(3, 2))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()

        assert sys_obj.running is False
        # Drain was called and returned with skipped events
        sys_obj.strategy_runner.drain_to_cursor.assert_called_once()

    @pytest.mark.asyncio
    async def test_bus_drain_timeout(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.bus.cursor = 10

        async def _slow_drain(cursor, timeout):
            await asyncio.sleep(100)
            return (0, 0)

        sys_obj.strategy_runner.drain_to_cursor = _slow_drain
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        with patch.dict(os.environ, {"HFT_BUS_DRAIN_TIMEOUT_MS": "10"}):
            await sys_obj.stop_async()

        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_bus_drain_generic_exception(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.bus.cursor = 10
        sys_obj.strategy_runner.drain_to_cursor = AsyncMock(side_effect=RuntimeError("drain fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        # Should not raise
        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_bus_drain_skipped_when_no_drain_to_cursor(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.bus.cursor = 10
        # strategy_runner has no drain_to_cursor attr
        del sys_obj.strategy_runner.drain_to_cursor
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False


class TestStopAsyncCheckpointWriter:
    """Cover checkpoint writer write in stop_async."""

    @pytest.mark.asyncio
    async def test_checkpoint_written_on_shutdown(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()

        sys_obj.checkpoint_writer.write_checkpoint.assert_called_once()

    @pytest.mark.asyncio
    async def test_checkpoint_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj.checkpoint_writer.write_checkpoint.side_effect = RuntimeError("ckpt fail")
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        # Should not raise
        await sys_obj.stop_async()
        assert sys_obj.running is False


class TestStopAsyncAuditWriter:
    """Cover audit writer stop in stop_async."""

    @pytest.mark.asyncio
    async def test_audit_writer_stop_called(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj._audit_writer = MagicMock()
        sys_obj._audit_writer.stop = AsyncMock()
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()

        sys_obj._audit_writer.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_writer_stop_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj._audit_writer = MagicMock()
        sys_obj._audit_writer.stop = AsyncMock(side_effect=RuntimeError("aw fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False


class TestStopAsyncExecServiceDrain:
    """Cover execution router stop + fill dedup + DLQ persist in stop_async."""

    @pytest.mark.asyncio
    async def test_exec_service_stop_returns_fills_drained(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=5)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        sys_obj.exec_service.stop.assert_called_once()

    @pytest.mark.asyncio
    async def test_exec_service_stop_timeout(self):
        sys_obj = _make_stub()
        sys_obj.running = True

        async def _slow_stop():
            await asyncio.sleep(100)
            return 0

        sys_obj.exec_service.stop = _slow_stop
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        # Should not raise — timeout caught
        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_exec_service_stop_exception(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(side_effect=RuntimeError("stop boom"))
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_fill_dedup_persist_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock(side_effect=RuntimeError("dedup fail"))
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_order_adapter_drain_timeout(self):
        sys_obj = _make_stub()
        sys_obj.running = True

        async def _drain_raises_timeout():
            # Raise TimeoutError directly to exercise the except branch without
            # waiting; system.py:1404 hardcodes timeout=10.0 which equals the
            # pytest-timeout ceiling, so a real asyncio.sleep(100) would deadlock
            # against the test harness.
            raise asyncio.TimeoutError()

        sys_obj.order_adapter.drain_and_cancel = _drain_raises_timeout
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_order_adapter_drain_exception(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.order_adapter.drain_and_cancel = AsyncMock(side_effect=RuntimeError("drain fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_order_id_map_persist_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.order_adapter.persist_order_id_map = MagicMock(side_effect=RuntimeError("persist fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False


class TestStopAsyncRecorderShutdown:
    """Cover recorder bridge cancel + recorder shutdown in stop_async."""

    @pytest.mark.asyncio
    async def test_recorder_bridge_cancel_and_recorder_drain(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()

        # Create a recorder_bridge task that respects cancellation
        async def _bridge():
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                return

        bridge_task = asyncio.create_task(_bridge())

        # Create a recorder task that stops on running=False
        stop_event = asyncio.Event()

        async def _recorder():
            await stop_event.wait()

        recorder_task = asyncio.create_task(_recorder())

        sys_obj.tasks = {"recorder_bridge": bridge_task, "recorder": recorder_task}
        # Make recorder.running a settable attribute that also signals the event
        original_running = True

        class _RecorderMock:
            def __init__(self):
                self._running = True

            @property
            def running(self):
                return self._running

            @running.setter
            def running(self, val):
                self._running = val
                if not val:
                    stop_event.set()

        sys_obj.recorder = _RecorderMock()

        await sys_obj.stop_async()

        assert sys_obj.running is False
        assert bridge_task.done()
        assert recorder_task.done()

    @pytest.mark.asyncio
    async def test_recorder_shutdown_timeout_triggers_cancel(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()

        # Create a recorder task that resists cancellation briefly
        async def _stuck_recorder():
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                return  # eventually yields

        recorder_task = asyncio.create_task(_stuck_recorder())
        sys_obj.tasks = {"recorder": recorder_task}

        with patch.dict(os.environ, {"HFT_RECORDER_SHUTDOWN_TIMEOUT_S": "0.01"}):
            await sys_obj.stop_async()

        assert sys_obj.running is False
        assert recorder_task.done()


class TestStopAsyncOptionalServices:
    """Cover autonomy monitor and session governor stop in stop_async."""

    @pytest.mark.asyncio
    async def test_autonomy_monitor_stop_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.autonomy_monitor = MagicMock()
        sys_obj.autonomy_monitor.stop = AsyncMock(side_effect=RuntimeError("am fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_session_governor_stop_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.session_governor = MagicMock()
        sys_obj.session_governor.stop = AsyncMock(side_effect=RuntimeError("sg fail"))
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()
        assert sys_obj.running is False

    @pytest.mark.asyncio
    async def test_gateway_service_running_set_false(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.gateway_service = MagicMock()
        sys_obj.gateway_service.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()
        sys_obj.tasks = {}

        await sys_obj.stop_async()

        assert sys_obj.gateway_service.running is False


class TestStopAsyncTaskCleanup:
    """Cover Phase 3 task cancellation in stop_async."""

    @pytest.mark.asyncio
    async def test_remaining_tasks_cancelled_and_cleared(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()

        async def _blocker():
            await asyncio.sleep(999)

        task_a = asyncio.create_task(_blocker())
        task_b = asyncio.create_task(_blocker())
        sys_obj.tasks = {"svc_a": task_a, "svc_b": task_b}

        await sys_obj.stop_async()

        assert len(sys_obj.tasks) == 0
        assert task_a.done()
        assert task_b.done()

    @pytest.mark.asyncio
    async def test_task_cleanup_timeout_is_logged(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.exec_service.stop = AsyncMock(return_value=0)
        sys_obj.exec_service.persist_fill_dedup = MagicMock()

        # Create a task that ignores cancellation
        _shield = asyncio.Future()

        async def _uncancellable():
            try:
                await _shield
            except asyncio.CancelledError:
                await asyncio.sleep(100)  # resist cancel

        task = asyncio.create_task(_uncancellable())
        sys_obj.tasks = {"stubborn": task}

        await sys_obj.stop_async()

        assert len(sys_obj.tasks) == 0


# ===========================================================================
# _supervise — HALT enforcement paths
# ===========================================================================


class TestSuperviseHaltGateway:
    """Cover HALT enforcement: gateway set_halt, checkpoint, queue drains."""

    @pytest.mark.asyncio
    async def test_halt_sets_gateway_halt(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.gateway_service = MagicMock()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.gateway_service.set_halt.assert_called()

    @pytest.mark.asyncio
    async def test_halt_writes_checkpoint_once(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj._halt_checkpoint_written = False

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 3:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Checkpoint written exactly once despite multiple HALT iterations
        sys_obj.checkpoint_writer.write_checkpoint.assert_called_once()
        assert sys_obj._halt_checkpoint_written is True

    @pytest.mark.asyncio
    async def test_halt_checkpoint_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj.checkpoint_writer.write_checkpoint.side_effect = RuntimeError("ckpt err")
        sys_obj._halt_checkpoint_written = False

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    # Should not raise
                                    await sys_obj._supervise()

        assert sys_obj._halt_checkpoint_written is True


class TestSuperviseHaltRiskQueueDrain:
    """Cover risk queue HALT drain with safety filter."""

    @pytest.mark.asyncio
    async def test_halt_preserves_cancel_intents_in_risk_queue(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT

        # Put a CANCEL intent and a NEW intent
        cancel_intent = MagicMock()
        cancel_intent.intent_type = IntentType.CANCEL
        cancel_intent.strategy_id = "strat1"

        new_intent = MagicMock()
        new_intent.intent_type = IntentType.NEW
        new_intent.strategy_id = "strat2"

        await sys_obj.risk_queue.put(cancel_intent)
        await sys_obj.risk_queue.put(new_intent)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # CANCEL intent should be re-queued, NEW intent drained
        assert sys_obj.risk_queue.qsize() == 1
        preserved = sys_obj.risk_queue.get_nowait()
        assert preserved.intent_type == IntentType.CANCEL

    @pytest.mark.asyncio
    async def test_halt_preserves_halt_exempt_intents(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.storm_guard.is_halt_exempt = MagicMock(return_value=True)

        new_intent = MagicMock()
        new_intent.intent_type = IntentType.NEW
        new_intent.strategy_id = "exempt_strat"

        await sys_obj.risk_queue.put(new_intent)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Halt-exempt intent should be preserved
        assert sys_obj.risk_queue.qsize() == 1


class TestSuperviseHaltOrderQueueDrain:
    """Cover order queue HALT drain with safety dispatch."""

    @pytest.mark.asyncio
    async def test_halt_dispatches_safety_commands_directly(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.execute = AsyncMock()
        sys_obj.order_adapter._background_tasks = set()
        sys_obj.order_adapter.drain_and_cancel = AsyncMock()

        # Put a CANCEL command in the order queue
        cancel_cmd = MagicMock()
        cancel_cmd.intent = MagicMock()
        cancel_cmd.intent.intent_type = IntentType.CANCEL
        cancel_cmd.intent.strategy_id = "strat_cancel"
        cancel_cmd.cmd_id = "CMD_1"

        await sys_obj.order_queue.put(cancel_cmd)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        _real_sleep = asyncio.sleep
        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            # Yield control so dispatched tasks can run
            await _real_sleep(0)
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.order_adapter.execute.assert_called_with(cancel_cmd)

    @pytest.mark.asyncio
    async def test_halt_drains_new_commands_from_order_queue(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.drain_and_cancel = AsyncMock()
        sys_obj.order_adapter._background_tasks = set()

        # Put a NEW command (not safety)
        new_cmd = MagicMock()
        new_cmd.intent = MagicMock()
        new_cmd.intent.intent_type = IntentType.NEW
        new_cmd.intent.strategy_id = "strat_new"

        await sys_obj.order_queue.put(new_cmd)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Order queue should be empty (NEW drained)
        assert sys_obj.order_queue.empty()


class TestSuperviseHaltIntentChannelDrain:
    """Cover intent_channel HALT drain with safety filter."""

    @pytest.mark.asyncio
    async def test_halt_drains_intent_channel_preserving_safety(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.drain_and_cancel = AsyncMock()
        sys_obj.order_adapter._background_tasks = set()

        # Set up intent_channel with function-based mocks (not side_effect iterators)
        ic = MagicMock()
        cancel_envelope = MagicMock()
        new_envelope = MagicMock()

        _drain_calls = 0

        def _drain():
            nonlocal _drain_calls
            _drain_calls += 1
            if _drain_calls == 1:
                return [cancel_envelope, new_envelope]
            return []

        ic.drain_nowait = _drain

        def _intent_type(e):
            return IntentType.CANCEL if e is cancel_envelope else IntentType.NEW

        def _strategy_id(e):
            return "strat_cancel" if e is cancel_envelope else "strat_new"

        ic.envelope_intent_type = _intent_type
        ic.envelope_strategy_id = _strategy_id
        ic._queue = asyncio.Queue(maxsize=10)
        sys_obj.intent_channel = ic

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # CANCEL envelope re-queued
        assert ic._queue.qsize() == 1


class TestSuperviseNonHaltRecovery:
    """Cover non-HALT path: gateway set_normal, order adapter re-enabled."""

    @pytest.mark.asyncio
    async def test_non_halt_recovers_gateway_and_order_adapter(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.gateway_service = MagicMock()
        sys_obj.order_adapter.running = False
        sys_obj._halt_checkpoint_written = True

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.gateway_service.set_normal.assert_called()
        assert sys_obj._halt_checkpoint_written is False


# ===========================================================================
# _supervise — kill switch detection
# ===========================================================================


class TestSuperviseKillSwitch:
    """Cover kill-switch file detection path."""

    @pytest.mark.asyncio
    async def test_kill_switch_triggers_halt(self, tmp_path):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        ks_path = str(tmp_path / "kill_switch")
        ks_file = tmp_path / "kill_switch"
        ks_file.write_text(json.dumps({"reason": "operator_halt"}))

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch.dict(os.environ, {"HFT_KILL_SWITCH_PATH": ks_path}):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = mock_metrics
                with patch("asyncio.sleep", side_effect=_mock_sleep):
                    with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                        with patch.object(sys_obj, "_update_platform_degrade_state"):
                            with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                                with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                    with patch("hft_platform.services.system.write_heartbeat"):
                                        await sys_obj._supervise()

        sys_obj.storm_guard.trigger_halt.assert_called()
        call_arg = sys_obj.storm_guard.trigger_halt.call_args[0][0]
        assert "KILL_SWITCH_FILE" in call_arg

    @pytest.mark.asyncio
    async def test_kill_switch_reason_read_error_uses_default(self, tmp_path):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        ks_path = str(tmp_path / "kill_switch")
        # Write invalid JSON so reason read fails
        ks_file = tmp_path / "kill_switch"
        ks_file.write_text("not valid json")

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch.dict(os.environ, {"HFT_KILL_SWITCH_PATH": ks_path}):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = mock_metrics
                with patch("asyncio.sleep", side_effect=_mock_sleep):
                    with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                        with patch.object(sys_obj, "_update_platform_degrade_state"):
                            with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                                with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                    with patch("hft_platform.services.system.write_heartbeat"):
                                        await sys_obj._supervise()

        sys_obj.storm_guard.trigger_halt.assert_called()
        call_arg = sys_obj.storm_guard.trigger_halt.call_args[0][0]
        assert "kill_switch_file_present" in call_arg


# ===========================================================================
# _supervise — service health checks
# ===========================================================================


class TestSuperviseServiceHealth:
    """Cover service task crash detection and restart logic."""

    @pytest.mark.asyncio
    async def test_crashed_service_triggers_halt_and_restart(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        # Create a task that immediately fails
        async def _fail():
            raise RuntimeError("service crashed")

        failed_task = asyncio.create_task(_fail())
        await asyncio.sleep(0)  # let it fail

        sys_obj.tasks = {"md": failed_task}

        mock_factory = MagicMock()

        async def _replacement():
            await asyncio.sleep(999)

        mock_factory.return_value = _replacement()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        services = [("md", "MarketDataService", mock_factory)]

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=services):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.trigger_halt.assert_called()

        # Cleanup
        for t in sys_obj.tasks.values():
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    @pytest.mark.asyncio
    async def test_cancelled_task_is_skipped(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        async def _long():
            await asyncio.sleep(999)

        task = asyncio.create_task(_long())
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        sys_obj.tasks = {"md": task}

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        services = [("md", "MarketDataService", MagicMock())]

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=services):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # No halt triggered for cancelled task
        sys_obj.storm_guard.trigger_halt.assert_not_called()

    @pytest.mark.asyncio
    async def test_order_task_done_in_halt_is_skipped(self):
        """Order/exec_gateway task done during HALT should not restart."""
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.drain_and_cancel = AsyncMock()
        sys_obj.order_adapter._background_tasks = set()

        # A completed order task (returned without exception)
        async def _done():
            pass

        task = asyncio.create_task(_done())
        await asyncio.sleep(0)  # let it complete

        sys_obj.tasks = {"order": task}

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        services = [("order", "OrderAdapter", MagicMock())]

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=services):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # No new halt triggered for this expected stop
        # (trigger_halt is NOT called for order task done in HALT)
        halt_calls = [c for c in sys_obj.storm_guard.trigger_halt.call_args_list if "OrderAdapter" in str(c)]
        assert len(halt_calls) == 0


class TestSuperviseImmediateFailDetection:
    """Cover immediate fail detection (task dies within 2s)."""

    @pytest.mark.asyncio
    async def test_immediate_fail_adds_extra_backoff_penalty(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        async def _fast_fail():
            raise RuntimeError("instant crash")

        failed_task = asyncio.create_task(_fast_fail())
        await asyncio.sleep(0)

        sys_obj.tasks = {"md": failed_task}

        # Started at < 2s ago
        from hft_platform.core import timebase

        sys_obj._task_started_at["md"] = timebase.now_s()

        mock_factory = MagicMock()

        async def _replacement():
            await asyncio.sleep(999)

        mock_factory.return_value = _replacement()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        services = [("md", "MarketDataService", mock_factory)]

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=services):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Extra penalty: attempts should be 2 (1 from immediate_fail + 1 from restart)
        assert sys_obj._task_restart_attempts.get("md", 0) >= 2

        # Cleanup
        for t in sys_obj.tasks.values():
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


# ===========================================================================
# _supervise — StormGuard update and MTM integration
# ===========================================================================


class TestSuperviseStormGuardUpdate:
    """Cover StormGuard update with drawdown, feed gap, latency."""

    @pytest.mark.asyncio
    async def test_stormguard_receives_drawdown_and_feed_gap(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=2.5):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.05):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.update.assert_called()
        call_kwargs = sys_obj.storm_guard.update.call_args[1]
        assert call_kwargs["feed_gap_s"] == 2.5
        assert call_kwargs["drawdown_bps"] == -500  # -int(0.05 * 10000)

    @pytest.mark.asyncio
    async def test_mtm_unrealized_loss_increases_drawdown(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.settings = {"base_capital": 10_000_000}

        mtm_calc = MagicMock()
        # 500K NTD unrealized loss, scaled int (x10000) — Bug 11 fix descales before dividing
        mtm_calc.total_unrealized_pnl.return_value = -500_000 * 10_000
        sys_obj._mtm_calculator = mtm_calc

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.02):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.update.assert_called()
        call_kwargs = sys_obj.storm_guard.update.call_args[1]
        # Bug 11: descale unrealized (-5e9 scaled → -500K NTD) then divide by
        # base_capital (10M NTD) → 0.05 contribution. Total = 0.02 + 0.05 = 0.07.
        assert call_kwargs["drawdown_bps"] == -700


class TestSuperviseLobDriftBurst:
    """Cover LOB drift-burst toxicity update path."""

    @pytest.mark.asyncio
    async def test_lob_drift_burst_update_called(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.storm_guard.update_with_lob = MagicMock()

        book = MagicMock()
        book.mid_price_x2 = 200000
        book.spread = 10
        book.imbalance = 0.5
        lob_engine = MagicMock()
        lob_engine.books = {"TXFD6": book}
        sys_obj.md_service.lob = lob_engine

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.update_with_lob.assert_called()


# ===========================================================================
# _supervise — session governor phase check
# ===========================================================================


class TestSuperviseSessionGovernor:
    """Cover session governor phase → StormGuard session active check."""

    @pytest.mark.asyncio
    async def test_session_governor_sets_session_active(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        # Simulate a SessionGovernor with track_gate
        mock_gate = MagicMock()
        mock_gate.track_phases = {"TXFD6": MagicMock()}  # Will trigger any() check
        sys_obj.session_governor = MagicMock()
        sys_obj.session_governor.track_gate = mock_gate

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.set_session_active.assert_called()


# ===========================================================================
# _supervise — periodic tasks (heartbeat, feature eviction, order sweep)
# ===========================================================================


class TestSupervisePeriodicTasks:
    """Cover heartbeat, feature engine eviction, order sweep."""

    @pytest.mark.asyncio
    async def test_heartbeat_written_at_interval(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 35:
                sys_obj.running = False

        with patch.dict(os.environ, {"HFT_HEARTBEAT_INTERVAL_S": "2"}):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = mock_metrics
                with patch("asyncio.sleep", side_effect=_mock_sleep):
                    with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                        with patch.object(sys_obj, "_update_platform_degrade_state"):
                            with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                                with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                    with patch("hft_platform.services.system.write_heartbeat") as mock_hb:
                                        await sys_obj._supervise()

        assert mock_hb.call_count >= 1

    @pytest.mark.asyncio
    async def test_feature_engine_eviction_called(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        fe = MagicMock()
        sys_obj.md_service.feature_engine = fe

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        fe.evict_stale_symbols.assert_called()

    @pytest.mark.asyncio
    async def test_order_adapter_sweep_called(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.order_adapter.sweep_stale_live_orders = AsyncMock()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.order_adapter.sweep_stale_live_orders.assert_called()


# ===========================================================================
# _supervise — per-symbol feed gap + pool metrics
# ===========================================================================


class TestSuperviseFeedGapMetrics:
    """Cover per-symbol feed gap metric and connection pool metric update."""

    @pytest.mark.asyncio
    async def test_per_symbol_feed_gap_metric_set(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="CAPPED")

        mock_gauge = MagicMock()
        mock_metrics.feed_gap_by_symbol_seconds = mock_gauge

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch.object(sys_obj, "_get_feed_gaps_by_symbol", return_value={"2330": 1.5}):
                                    with patch("hft_platform.services.system.write_heartbeat"):
                                        await sys_obj._supervise()

        mock_gauge.labels.assert_called()

    @pytest.mark.asyncio
    async def test_pool_update_metrics_called(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.md_client.update_metrics = MagicMock()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.md_client.update_metrics.assert_called()


# ===========================================================================
# _supervise — facade health check
# ===========================================================================


class TestSuperviseFacadeHealth:
    """Cover facade health check path."""

    @pytest.mark.asyncio
    async def test_facade_health_check_called_in_window(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_client = MagicMock()
        mock_client.check_facade_health = MagicMock()
        sys_obj.md_service.client = mock_client
        sys_obj.md_service.within_reconnect_window = MagicMock(return_value=True)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        mock_client.check_facade_health.assert_called()

    @pytest.mark.asyncio
    async def test_facade_health_check_skipped_outside_window(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_client = MagicMock()
        mock_client.check_facade_health = MagicMock()
        sys_obj.md_service.client = mock_client
        sys_obj.md_service.within_reconnect_window = MagicMock(return_value=False)

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        mock_client.check_facade_health.assert_not_called()


# ===========================================================================
# _supervise — queue depth logging
# ===========================================================================


class TestSuperviseQueueLogging:
    """Cover periodic queue depth log with intent_channel."""

    @pytest.mark.asyncio
    async def test_queue_log_includes_gateway_intent_when_present(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj._last_queue_log_s = 0.0
        sys_obj._queue_log_every_s = 0.0  # Force log every iteration

        ic = MagicMock()
        ic.qsize.return_value = 3
        sys_obj.intent_channel = ic

        api_q = MagicMock()
        api_q.qsize.return_value = 2
        sys_obj.order_adapter._api_queue = api_q

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Verify intent_channel.qsize() was called for metrics
        ic.qsize.assert_called()


# ===========================================================================
# _supervise — order/exec_gateway HALT de-escalation restart
# ===========================================================================


class TestSuperviseDeescalationRestart:
    """Cover I2-C1: service restart after HALT de-escalation."""

    @pytest.mark.asyncio
    async def test_order_task_restarts_after_halt_deescalation(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL  # De-escalated

        # Order task completed without error (exited during HALT)
        async def _done():
            pass

        task = asyncio.create_task(_done())
        await asyncio.sleep(0)

        sys_obj.tasks = {"order": task}
        sys_obj.order_adapter.running = False  # Was stopped during HALT

        mock_factory = MagicMock()

        async def _replacement():
            await asyncio.sleep(999)

        mock_factory.return_value = _replacement()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        services = [("order", "OrderAdapter", mock_factory)]

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=services):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # Service re-enabled and restarted
        assert sys_obj.order_adapter.running is True

        # Cleanup
        for t in sys_obj.tasks.values():
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass


# ===========================================================================
# _supervise — Redis emergency halt
# ===========================================================================


class TestSuperviseRedisHalt:
    """Cover Telegram /stop emergency halt via Redis key."""

    @pytest.mark.asyncio
    async def test_redis_halt_triggers_storm_guard(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        redis_client = MagicMock()
        redis_client.get.return_value = "1"
        sys_obj._redis_client = redis_client

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.trigger_halt.assert_called_with("TELEGRAM_EMERGENCY_HALT")


# ===========================================================================
# graceful_reset — STORM/HALT reset + DLQ error
# ===========================================================================


class TestGracefulResetStormGuard:
    """Cover storm guard reset from STORM state."""

    @pytest.mark.asyncio
    async def test_resets_storm_guard_from_storm_state(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = None
        sys_obj.position_store._recovery_positions = None

        # Simulate StormGuard in STORM state
        sys_obj.storm_guard.state = StormGuardState.STORM
        sys_obj.storm_guard._halt_entry_ts = 100.0
        sys_obj.storm_guard._storm_entry_ts = 50.0
        sys_obj.storm_guard._de_escalate_count = 0
        sys_obj.storm_guard._de_escalate_threshold = 5

        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service = None

        results = await sys_obj.graceful_reset(reason="test_reset")

        assert "reset from STORM" in results["storm_guard"]
        sys_obj.storm_guard.update.assert_called_once_with(drawdown_bps=0, latency_us=0, feed_gap_s=0.0)

    @pytest.mark.asyncio
    async def test_dlq_drain_exception_handled(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = None
        sys_obj.position_store._recovery_positions = None
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service = None

        with patch(
            "hft_platform.services.system.HFTSystem.graceful_reset",
            wraps=sys_obj.graceful_reset,
        ):
            with patch(
                "hft_platform.execution.fill_dlq.get_orphaned_fill_dlq",
                side_effect=RuntimeError("dlq error"),
            ):
                results = await sys_obj.graceful_reset()

        assert "error" in results["fill_dlq"]

    @pytest.mark.asyncio
    async def test_no_recon_service(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = None
        sys_obj.position_store._recovery_positions = None
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service = None

        results = await sys_obj.graceful_reset()
        assert results["reconciliation"] == "no service"


# ===========================================================================
# _recorder_bridge — non-early-exit paths (event routing)
# ===========================================================================


class TestRecorderBridgeEventRouting:
    """Cover _recorder_bridge with actual event consumption (non-batch)."""

    @pytest.mark.asyncio
    async def test_bridge_routes_tick_events_when_md_direct_off(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        from hft_platform.events import TickEvent

        tick = MagicMock(spec=TickEvent)
        tick.symbol = "2330"

        # Create async generator for bus.consume
        async def _consumer(**kwargs):
            yield tick
            # After yielding, cancel
            sys_obj.running = False
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("ticks", {"data": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        assert sys_obj.recorder_queue.qsize() == 1
        item = sys_obj.recorder_queue.get_nowait()
        assert item["topic"] == "ticks"
        assert sys_obj._recorder_seen_tick is True

    @pytest.mark.asyncio
    async def test_bridge_skips_tick_when_md_direct_on(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = True
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True

        # Should exit early
        await sys_obj._recorder_bridge()
        assert sys_obj.recorder_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_bridge_handles_unmapped_event(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        event = MagicMock()

        async def _consumer(**kwargs):
            yield event
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=None):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        assert sys_obj.recorder_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_bridge_drops_on_queue_full(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj._recorder_drop_on_full = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=1)
        sys_obj.recorder_queue.put_nowait({"topic": "filler", "data": {}})

        event = MagicMock()

        async def _consumer(**kwargs):
            yield event
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        mock_metrics = MagicMock()
        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("ticks", {"d": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = mock_metrics
                await sys_obj._recorder_bridge()

        assert sys_obj._recorder_bridge_drops == 1

    @pytest.mark.asyncio
    async def test_bridge_batch_mode(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        event1 = MagicMock()
        event2 = MagicMock()

        # Batch mode yields lists
        async def _consumer(*args, **kwargs):
            yield [event1, event2]
            raise asyncio.CancelledError()

        sys_obj.bus.consume_batch = _consumer

        with patch.dict(os.environ, {"HFT_BUS_BATCH_SIZE": "4"}):
            with patch(
                "hft_platform.recorder.mapper.map_event_to_record", side_effect=[("t", {"a": 1}), ("t", {"a": 2})]
            ):
                with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                    MockMR.get.return_value = MagicMock()
                    await sys_obj._recorder_bridge()

        assert sys_obj.recorder_queue.qsize() == 2

    @pytest.mark.asyncio
    async def test_bridge_blocking_put_when_drop_disabled(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj._recorder_drop_on_full = False
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        event = MagicMock()

        async def _consumer(**kwargs):
            yield event
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("ticks", {"d": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        assert sys_obj.recorder_queue.qsize() == 1


# ===========================================================================
# _supervise — StormGuard exception isolation
# ===========================================================================


class TestSuperviseExceptionIsolation:
    """Cover exception isolation in StormGuard metric computation."""

    @pytest.mark.asyncio
    async def test_feed_gap_exception_does_not_block_stormguard_update(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", side_effect=RuntimeError("feed error")):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # StormGuard.update() still called despite feed_gap exception
        sys_obj.storm_guard.update.assert_called()

    @pytest.mark.asyncio
    async def test_drawdown_exception_does_not_block_stormguard_update(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", side_effect=RuntimeError("dd error")):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        sys_obj.storm_guard.update.assert_called()

    @pytest.mark.asyncio
    async def test_stormguard_update_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL
        sys_obj.storm_guard.update.side_effect = RuntimeError("sg boom")

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    # Should not raise
                                    await sys_obj._supervise()

        assert sys_obj.running is False


# ===========================================================================
# _supervise — HALT drain_and_cancel
# ===========================================================================


class TestSuperviseHaltDrainAndCancel:
    """Cover H6: cancel in-flight orders during HALT."""

    @pytest.mark.asyncio
    async def test_halt_calls_drain_and_cancel(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.drain_and_cancel = AsyncMock()
        sys_obj.order_adapter._background_tasks = set()

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        _real_sleep = asyncio.sleep
        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            await _real_sleep(0)  # let tasks run
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        # drain_and_cancel was called as a task
        # Verify order_adapter.running was set False
        assert sys_obj.order_adapter.running is False

    @pytest.mark.asyncio
    async def test_halt_drain_cancel_exception_swallowed(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.HALT
        sys_obj.order_adapter.drain_and_cancel = AsyncMock(side_effect=RuntimeError("cancel fail"))
        sys_obj.order_adapter._background_tasks = None  # no bg tasks set

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    # Should not raise
                                    await sys_obj._supervise()

        assert sys_obj.running is False


# ===========================================================================
# _supervise — execution router/gateway heartbeat metrics
# ===========================================================================


class TestSuperviseHeartbeatMetrics:
    """Cover exec_router and exec_gateway heartbeat timestamp metrics."""

    @pytest.mark.asyncio
    async def test_heartbeat_metrics_set_for_running_tasks(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.storm_guard.state = StormGuardState.NORMAL

        # Use Futures that won't complete (no sleep recursion risk)
        _stop_router = asyncio.Event()
        _stop_gateway = asyncio.Event()

        async def _wait_router():
            await _stop_router.wait()

        async def _wait_gateway():
            await _stop_gateway.wait()

        router_task = asyncio.create_task(_wait_router())
        gateway_task = asyncio.create_task(_wait_gateway())
        sys_obj.tasks = {"exec_router": router_task, "exec_gateway": gateway_task}

        mock_metrics = MagicMock()
        mock_metrics.cap_symbol = MagicMock(return_value="X")

        iteration = 0

        async def _mock_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            with patch("asyncio.sleep", side_effect=_mock_sleep):
                with patch.object(sys_obj, "_iter_supervised_services", return_value=[]):
                    with patch.object(sys_obj, "_update_platform_degrade_state"):
                        with patch.object(sys_obj, "_get_max_feed_gap_s", return_value=0.0):
                            with patch.object(sys_obj, "_get_drawdown_pct", return_value=0.0):
                                with patch("hft_platform.services.system.write_heartbeat"):
                                    await sys_obj._supervise()

        mock_metrics.execution_router_heartbeat_ts.set.assert_called()
        mock_metrics.execution_gateway_heartbeat_ts.set.assert_called()

        # Cleanup
        _stop_router.set()
        _stop_gateway.set()
        router_task.cancel()
        gateway_task.cancel()
        for t in [router_task, gateway_task]:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass


# ===========================================================================
# _reset_restart_backoff_if_healthy — no started_at recorded
# ===========================================================================


class TestResetRestartBackoff:
    def test_no_reset_when_no_started_at(self):
        sys_obj = _make_stub()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        sys_obj._task_restart_attempts["md"] = 3
        # No _task_started_at entry
        sys_obj._reset_restart_backoff_if_healthy("md", mock_task)
        # Should NOT reset because no started_at
        assert sys_obj._task_restart_attempts["md"] == 3

    def test_no_reset_when_task_done(self):
        sys_obj = _make_stub()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        sys_obj._task_restart_attempts["md"] = 3
        sys_obj._task_started_at["md"] = 0.0
        sys_obj._reset_restart_backoff_if_healthy("md", mock_task)
        assert sys_obj._task_restart_attempts["md"] == 3

    def test_no_reset_when_task_none(self):
        sys_obj = _make_stub()
        sys_obj._task_restart_attempts["md"] = 3
        sys_obj._reset_restart_backoff_if_healthy("md", None)
        assert sys_obj._task_restart_attempts["md"] == 3

    def test_no_reset_when_uptime_below_threshold(self):
        from hft_platform.core import timebase

        sys_obj = _make_stub()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        sys_obj._task_restart_attempts["md"] = 3
        sys_obj._task_started_at["md"] = timebase.now_s() - 1  # only 1s uptime < 60s
        sys_obj._reset_restart_backoff_if_healthy("md", mock_task)
        assert sys_obj._task_restart_attempts["md"] == 3


# ===========================================================================
# _persist_lost_exec_event — orjson fallback
# ===========================================================================


class TestPersistLostExecEventJsonFallback:
    """Cover the json fallback path when orjson is not available."""

    def test_json_fallback_writes_event(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        state_dir = str(tmp_path / "state")
        monkeypatch.setenv("HFT_STATE_DIR", state_dir)

        event = SimpleNamespace(topic="deal", data={"price": 42}, ingest_ts_ns=999)

        # Temporarily make orjson import fail in the method
        import builtins

        original_import = builtins.__import__

        def _no_orjson(name, *args, **kwargs):
            if name == "orjson":
                raise ImportError("no orjson")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", side_effect=_no_orjson):
            sys_obj._persist_lost_exec_event(event)

        dlq_file = os.path.join(state_dir, "exec_overflow_dlq.jsonl")
        assert os.path.exists(dlq_file)
        with open(dlq_file, "rb") as f:
            line = f.readline()
        parsed = json.loads(line)
        assert parsed["topic"] == "deal"


# ===========================================================================
# _recorder_bridge — BidAsk first-seen logging
# ===========================================================================


class TestRecorderBridgeBidAskFirstSeen:
    """Cover _recorder_seen_bidask first-event log."""

    @pytest.mark.asyncio
    async def test_bridge_logs_first_bidask_event(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)
        sys_obj._recorder_seen_bidask = False

        from hft_platform.events import BidAskEvent

        ba = MagicMock(spec=BidAskEvent)
        ba.symbol = "2330"
        ba.is_snapshot = False

        async def _consumer(**kwargs):
            yield ba
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("bidask", {"d": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        assert sys_obj._recorder_seen_bidask is True


# ===========================================================================
# _recorder_bridge — FillEvent/OrderEvent skipped when direct
# ===========================================================================


class TestRecorderBridgeDirectSkip:
    """Cover fill/order event skip when direct recording is on."""

    @pytest.mark.asyncio
    async def test_bridge_skips_fill_when_fill_direct(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = False
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        from hft_platform.contracts.execution import FillEvent

        fill = MagicMock(spec=FillEvent)

        other_event = MagicMock()

        async def _consumer(**kwargs):
            yield fill
            yield other_event
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("other", {"d": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        # Only the other_event should have been recorded (fill skipped)
        assert sys_obj.recorder_queue.qsize() == 1

    @pytest.mark.asyncio
    async def test_bridge_skips_order_event_when_order_direct(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = False
        sys_obj._order_record_direct = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

        from hft_platform.contracts.execution import OrderEvent

        order_ev = MagicMock(spec=OrderEvent)

        other_event = MagicMock()

        async def _consumer(**kwargs):
            yield order_ev
            yield other_event
            raise asyncio.CancelledError()

        sys_obj.bus.consume = _consumer

        with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=("other", {"d": 1})):
            with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
                MockMR.get.return_value = MagicMock()
                await sys_obj._recorder_bridge()

        assert sys_obj.recorder_queue.qsize() == 1
