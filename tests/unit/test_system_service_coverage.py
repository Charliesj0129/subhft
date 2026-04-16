"""Coverage tests for services/system.py — targeting uncovered lines."""

from __future__ import annotations

import asyncio
import collections
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.services.system import HFTSystem


# ---------------------------------------------------------------------------
# Helper: lightweight HFTSystem stub (bypass __init__ / SystemBootstrapper)
# ---------------------------------------------------------------------------


def _make_stub() -> HFTSystem:
    """Return an HFTSystem instance with all dependencies stubbed out."""
    sys_obj = HFTSystem.__new__(HFTSystem)
    sys_obj.settings = {}
    sys_obj.running = False
    sys_obj._recorder_seen_tick = False
    sys_obj._recorder_seen_bidask = False
    sys_obj._md_record_direct = True
    sys_obj._fill_record_direct = True
    sys_obj._order_record_direct = True
    sys_obj.bootstrapper = MagicMock()
    sys_obj.registry = MagicMock()
    sys_obj.bus = MagicMock()
    sys_obj.raw_queue = asyncio.Queue()
    sys_obj.raw_exec_queue = asyncio.Queue()
    sys_obj.risk_queue = asyncio.Queue()
    sys_obj.order_queue = asyncio.Queue()
    sys_obj.recorder_queue = asyncio.Queue()
    sys_obj.position_store = MagicMock()
    sys_obj.order_id_map = {}
    sys_obj.storm_guard = MagicMock()
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
    return sys_obj


# ===========================================================================
# Module-level functions (lines 25-38)
# ===========================================================================


class TestReadKillSwitchReason:
    """Cover _read_kill_switch_reason (lines 27, 29-31)."""

    def test_reads_reason_from_json_file(self, tmp_path):
        from hft_platform.services.system import _read_kill_switch_reason

        ks_file = tmp_path / "kill_switch.json"
        ks_file.write_text(json.dumps({"reason": "manual_operator_halt"}))

        result = _read_kill_switch_reason(str(ks_file))
        assert result == "manual_operator_halt"

    def test_returns_unknown_when_reason_key_missing(self, tmp_path):
        from hft_platform.services.system import _read_kill_switch_reason

        ks_file = tmp_path / "kill_switch.json"
        ks_file.write_text(json.dumps({"other_key": "value"}))

        result = _read_kill_switch_reason(str(ks_file))
        assert result == "unknown"


class TestLogSafetyDispatchError:
    """Cover _log_safety_dispatch_error (lines 36-38)."""

    def test_logs_when_task_has_exception(self):
        from hft_platform.services.system import _log_safety_dispatch_error

        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = RuntimeError("dispatch failed")

        # Should not raise; exercises the logger.critical path
        _log_safety_dispatch_error(task)
        task.exception.assert_called_once()

    def test_no_log_when_task_cancelled(self):
        from hft_platform.services.system import _log_safety_dispatch_error

        task = MagicMock()
        task.cancelled.return_value = True

        # exc is None when cancelled -> no critical log
        _log_safety_dispatch_error(task)
        task.exception.assert_not_called()

    def test_no_log_when_no_exception(self):
        from hft_platform.services.system import _log_safety_dispatch_error

        task = MagicMock()
        task.cancelled.return_value = False
        task.exception.return_value = None

        _log_safety_dispatch_error(task)
        task.exception.assert_called_once()


# ===========================================================================
# _get_max_feed_gap_s — client.get_healthy_feed_gap_s path (line 53)
# ===========================================================================


class TestGetMaxFeedGapClientPath:
    """Cover client-based feed gap path with within_reconnect_window."""

    def test_returns_gap_via_client_healthy_feed_gap(self):
        client = SimpleNamespace(get_healthy_feed_gap_s=lambda: 3.5)
        md = SimpleNamespace(client=client, within_reconnect_window=lambda: True)
        result = HFTSystem._get_max_feed_gap_s(md)
        assert result == 3.5

    def test_returns_zero_when_outside_reconnect_window_client_path(self):
        client = SimpleNamespace(get_healthy_feed_gap_s=lambda: 3.5)
        md = SimpleNamespace(client=client, within_reconnect_window=lambda: False)
        result = HFTSystem._get_max_feed_gap_s(md)
        assert result == 0.0

    def test_returns_gap_when_no_reconnect_window_fn_client_path(self):
        client = SimpleNamespace(get_healthy_feed_gap_s=lambda: 7.0)
        md = SimpleNamespace(client=client)
        result = HFTSystem._get_max_feed_gap_s(md)
        assert result == 7.0


# ===========================================================================
# _try_restart_service (lines 608-642)
# ===========================================================================


class TestTryRestartService:
    """Cover _try_restart_service including max-attempts HALT path."""

    @pytest.mark.asyncio
    async def test_restart_creates_new_task(self):
        sys_obj = _make_stub()

        async def _noop():
            await asyncio.sleep(100)

        coro_factory = MagicMock(return_value=_noop())
        sys_obj._task_restart_attempts = {}
        sys_obj._task_restart_until_s = {}

        sys_obj._try_restart_service("md", "MarketDataService", coro_factory)

        assert sys_obj._task_restart_attempts["md"] == 1
        assert "md" in sys_obj.tasks
        coro_factory.assert_called_once()

        # Cleanup
        sys_obj.tasks["md"].cancel()
        try:
            await sys_obj.tasks["md"]
        except asyncio.CancelledError:
            pass

    def test_skips_restart_when_backoff_not_elapsed(self):
        sys_obj = _make_stub()
        from hft_platform.core import timebase

        # Set allowed_at far in the future
        sys_obj._task_restart_until_s["md"] = timebase.now_s() + 9999
        coro_factory = MagicMock()

        sys_obj._try_restart_service("md", "MarketDataService", coro_factory)

        coro_factory.assert_not_called()

    def test_triggers_halt_on_max_attempts_exceeded(self):
        sys_obj = _make_stub()
        sys_obj._task_restart_max_attempts = 3
        sys_obj._task_restart_attempts["md"] = 3  # next will be 4 > 3

        coro_factory = MagicMock()
        sys_obj._try_restart_service("md", "MarketDataService", coro_factory)

        sys_obj.storm_guard.trigger_halt.assert_called_once()
        coro_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_exponential_backoff_delay(self):
        sys_obj = _make_stub()
        sys_obj._task_restart_base_delay_s = 2.0
        sys_obj._task_restart_max_delay_s = 30.0
        sys_obj._task_restart_attempts["md"] = 2  # next attempt = 3

        async def _noop():
            await asyncio.sleep(100)

        coro_factory = MagicMock(return_value=_noop())
        sys_obj._try_restart_service("md", "MarketDataService", coro_factory)

        # attempt 3 -> delay = 2.0 * 2^(3-1) = 8.0
        assert sys_obj._task_restart_attempts["md"] == 3
        from hft_platform.core import timebase

        expected_delay = 8.0
        assert sys_obj._task_restart_until_s["md"] == pytest.approx(
            timebase.now_s() + expected_delay, abs=2.0
        )

        # Cleanup
        sys_obj.tasks["md"].cancel()
        try:
            await sys_obj.tasks["md"]
        except asyncio.CancelledError:
            pass


# ===========================================================================
# _update_platform_degrade_state (lines 644-655)
# ===========================================================================


class TestUpdatePlatformDegradeState:
    def test_calls_enter_reduce_only_for_each_reason(self):
        sys_obj = _make_stub()
        sys_obj.platform_degrade_inputs.reduce_only_reasons.return_value = ["reason_a", "reason_b"]

        sys_obj._update_platform_degrade_state()

        assert sys_obj.platform_degrade_controller.enter_reduce_only.call_count == 2
        sys_obj.platform_degrade_controller.check_auto_recovery.assert_called_once()

    def test_skips_when_controller_is_none(self):
        sys_obj = _make_stub()
        sys_obj.platform_degrade_controller = None

        # Should not raise
        sys_obj._update_platform_degrade_state()

    def test_skips_when_inputs_is_none(self):
        sys_obj = _make_stub()
        sys_obj.platform_degrade_inputs = None

        sys_obj._update_platform_degrade_state()


# ===========================================================================
# _persist_lost_exec_event (lines 1353-1383)
# ===========================================================================


class TestPersistLostExecEvent:
    def test_writes_event_to_dlq_file(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        state_dir = str(tmp_path / "state")
        monkeypatch.setenv("HFT_STATE_DIR", state_dir)

        event = SimpleNamespace(topic="deal", data={"price": 100}, ingest_ts_ns=123456)
        sys_obj._persist_lost_exec_event(event)

        dlq_file = os.path.join(state_dir, "exec_overflow_dlq.jsonl")
        assert os.path.exists(dlq_file)
        with open(dlq_file, "rb") as f:
            line = f.readline()
        parsed = json.loads(line)
        assert parsed["topic"] == "deal"
        assert parsed["data"]["price"] == 100

    def test_handles_write_failure_gracefully(self, monkeypatch):
        sys_obj = _make_stub()
        # Point to an invalid directory that cannot be created
        monkeypatch.setenv("HFT_STATE_DIR", "/dev/null/impossible")

        event = SimpleNamespace(topic="deal", data={}, ingest_ts_ns=0)
        # Should not raise
        sys_obj._persist_lost_exec_event(event)


# ===========================================================================
# _safe_enqueue_exec (lines 1385-1413)
# ===========================================================================


class TestSafeEnqueueExec:
    def test_enqueues_normally_when_queue_has_space(self):
        sys_obj = _make_stub()
        sys_obj.raw_exec_queue = asyncio.Queue(maxsize=10)
        event = SimpleNamespace(topic="deal", data={})

        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = MagicMock()
            sys_obj._safe_enqueue_exec(event)

        assert sys_obj.raw_exec_queue.qsize() == 1

    def test_routes_to_overflow_buffer_when_queue_full(self):
        sys_obj = _make_stub()
        sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
        sys_obj.raw_exec_queue.put_nowait("filler")
        event = SimpleNamespace(topic="deal", data={})

        mock_metrics = MagicMock()
        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            sys_obj._safe_enqueue_exec(event)

        assert len(sys_obj._exec_overflow_buf) == 1
        assert sys_obj._exec_overflow_counter == 1

    def test_triggers_halt_on_overflow_buf_full(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
        sys_obj.raw_exec_queue.put_nowait("filler")
        sys_obj._EXEC_OVERFLOW_MAX = 2
        sys_obj._exec_overflow_buf = collections.deque([1, 2], maxlen=4096)
        monkeypatch.setenv("HFT_STATE_DIR", str(tmp_path / "state"))
        event = SimpleNamespace(topic="deal", data={}, ingest_ts_ns=0)

        mock_metrics = MagicMock()
        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            sys_obj._safe_enqueue_exec(event)

        assert sys_obj._exec_overflow_evicted == 1
        sys_obj.storm_guard.trigger_halt.assert_called_once_with("exec_overflow_buf_exhausted")

    def test_triggers_halt_after_three_overflows(self):
        sys_obj = _make_stub()
        sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
        sys_obj.raw_exec_queue.put_nowait("filler")
        sys_obj._exec_overflow_counter = 2  # next will be 3

        event = SimpleNamespace(topic="deal", data={})
        mock_metrics = MagicMock()
        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            sys_obj._safe_enqueue_exec(event)

        assert sys_obj._exec_overflow_counter == 3
        sys_obj.storm_guard.trigger_halt.assert_called_once_with("exec_queue_overflow_repeated")


# ===========================================================================
# _on_exec (lines 1415-1541) — broker thread callback
# ===========================================================================


class TestOnExec:
    def test_buffers_event_when_not_running_and_no_loop(self):
        sys_obj = _make_stub()
        sys_obj.running = False

        sys_obj._on_exec("order", {"state": "submitted", "payload": {}})

        assert len(sys_obj._exec_overflow_buf) == 1

    def test_buffers_event_pre_start_overflow_full_persists_to_dlq(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        sys_obj.running = False
        sys_obj._EXEC_OVERFLOW_MAX = 0  # immediately full
        monkeypatch.setenv("HFT_STATE_DIR", str(tmp_path / "state"))

        sys_obj._on_exec("deal", {"payload": {"price": 42}})

        assert sys_obj._exec_overflow_evicted == 1
        assert sys_obj._exec_startup_overflow_lost is True

    def test_enqueues_via_loop_when_running(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        mock_loop = MagicMock()
        sys_obj.loop = mock_loop

        sys_obj._on_exec("order", {"state": "filled", "payload": {}})

        mock_loop.call_soon_threadsafe.assert_called_once()

    def test_deal_resolves_strategy_from_dict_payload(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = "strat_A"
        sys_obj.order_adapter.order_id_resolver = resolver

        data = {"payload": {"ordno": "12345", "full_code": "TXFD6", "action": "Buy"}}
        sys_obj._on_exec("deal", data)

        assert data["_resolved_strategy_id"] == "strat_A"

    def test_deal_resolves_strategy_from_object_payload(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = "UNKNOWN"
        sys_obj.order_adapter.order_id_resolver = resolver
        sys_obj.order_adapter.resolve_strategy_from_deal_candidates.return_value = "strat_B"

        payload = SimpleNamespace(
            ordno="12345",
            ord_no=None,
            seqno=None,
            seq_no=None,
            order_id=None,
            id=None,
            custom_field=None,
            full_code="TXFD6",
            code="TXF",
            action="Sell",
            order=None,
        )
        data = {"payload": payload}
        sys_obj._on_exec("deal", data)

        assert data["_resolved_strategy_id"] == "strat_B"

    def test_deal_with_nested_order_object_in_payload(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = "strat_C"
        sys_obj.order_adapter.order_id_resolver = resolver

        inner_order = SimpleNamespace(
            ordno="ORD999",
            ord_no=None,
            seqno=None,
            seq_no=None,
            order_id=None,
            id=None,
            custom_field=None,
        )
        payload = SimpleNamespace(
            ordno=None,
            ord_no=None,
            seqno=None,
            seq_no=None,
            order_id=None,
            id=None,
            custom_field=None,
            full_code=None,
            code=None,
            action=None,
            order=inner_order,
        )
        data = {"payload": payload}
        sys_obj._on_exec("deal", data)

        assert data["_resolved_strategy_id"] == "strat_C"

    def test_buffers_to_overflow_when_loop_runtime_error(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        mock_loop = MagicMock()
        mock_loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        sys_obj.loop = mock_loop

        sys_obj._on_exec("order", {"state": "cancelled", "payload": {}})

        assert len(sys_obj._exec_overflow_buf) == 1


# ===========================================================================
# _start_service (lines 374-387) — exec_router/exec_gateway metrics
# ===========================================================================


class TestStartService:
    @pytest.mark.asyncio
    async def test_start_service_sets_exec_router_metric(self):
        sys_obj = _make_stub()

        async def _noop():
            await asyncio.sleep(0.01)

        mock_metrics = MagicMock()
        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            sys_obj._start_service("exec_router", _noop())

        assert "exec_router" in sys_obj.tasks
        # Clean up
        sys_obj.tasks["exec_router"].cancel()
        try:
            await sys_obj.tasks["exec_router"]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_start_service_sets_exec_gateway_metric(self):
        sys_obj = _make_stub()

        async def _noop():
            await asyncio.sleep(0.01)

        mock_metrics = MagicMock()
        with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
            MockMR.get.return_value = mock_metrics
            sys_obj._start_service("exec_gateway", _noop())

        assert "exec_gateway" in sys_obj.tasks
        sys_obj.tasks["exec_gateway"].cancel()
        try:
            await sys_obj.tasks["exec_gateway"]
        except asyncio.CancelledError:
            pass


# ===========================================================================
# graceful_reset (lines 528-606)
# ===========================================================================


class TestGracefulReset:
    @pytest.mark.asyncio
    async def test_resets_all_components(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj.checkpoint_writer._path = "/tmp/test_ckpt.json"
        # No actual file to delete -> "no file" path

        sys_obj.position_store._recovery_positions = {"sym1": MagicMock()}
        sys_obj.storm_guard.state = MagicMock()
        sys_obj.storm_guard.state.__ge__ = lambda self, other: False  # not in STORM
        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service._halt_triggered = True
        sys_obj.recon_service._consecutive_failures = 5
        sys_obj.recon_service._broker_zero_streak = 3
        sys_obj.recon_service._noncritical_drift_streak = 2

        results = await sys_obj.graceful_reset(reason="test_reset")

        assert results["checkpoint"] == "no file"
        assert "cleared 1 entries" in results["recovery_positions"]
        assert results["storm_guard"] == "already NORMAL"
        assert results["reduce_only"] == "not active"
        assert results["reconciliation"] == "state reset"
        assert sys_obj.recon_service._halt_triggered is False

    @pytest.mark.asyncio
    async def test_deletes_checkpoint_file_when_exists(self, tmp_path):
        sys_obj = _make_stub()
        ckpt_file = tmp_path / "checkpoint.json"
        ckpt_file.write_text("{}")
        sys_obj.checkpoint_writer = MagicMock()
        sys_obj.checkpoint_writer._path = str(ckpt_file)

        sys_obj.position_store._recovery_positions = None
        sys_obj.storm_guard.state = MagicMock()
        sys_obj.storm_guard.state.__ge__ = lambda self, other: False
        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service = None

        results = await sys_obj.graceful_reset()

        assert "deleted" in results["checkpoint"]
        assert not ckpt_file.exists()

    @pytest.mark.asyncio
    async def test_no_checkpoint_writer(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = None
        sys_obj.position_store._recovery_positions = None
        sys_obj.storm_guard.state = MagicMock()
        sys_obj.storm_guard.state.__ge__ = lambda self, other: False
        sys_obj.platform_degrade_controller.reduce_only_active = False
        sys_obj.recon_service = None

        results = await sys_obj.graceful_reset()
        assert results["checkpoint"] == "no writer"

    @pytest.mark.asyncio
    async def test_exits_reduce_only_when_active(self):
        sys_obj = _make_stub()
        sys_obj.checkpoint_writer = None
        sys_obj.position_store._recovery_positions = None
        sys_obj.storm_guard.state = MagicMock()
        sys_obj.storm_guard.state.__ge__ = lambda self, other: False
        sys_obj.platform_degrade_controller.reduce_only_active = True
        sys_obj.recon_service = None

        results = await sys_obj.graceful_reset(reason="operator_manual")

        assert results["reduce_only"] == "exited"
        sys_obj.platform_degrade_controller.exit_reduce_only.assert_called_once_with(reason="operator_manual")


# ===========================================================================
# _recorder_bridge — early exit path (lines 1546-1551)
# ===========================================================================


class TestRecorderBridgeEarlyExit:
    @pytest.mark.asyncio
    async def test_exits_when_all_direct_recording_enabled(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = True
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True

        # Should return immediately without consuming bus
        await sys_obj._recorder_bridge()
        sys_obj.bus.consume.assert_not_called()


# ===========================================================================
# _pnl_snapshot_exporter (lines 458-494)
# ===========================================================================


class TestPnlSnapshotExporter:
    @pytest.mark.asyncio
    async def test_exports_position_snapshots(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=100)
        pos = SimpleNamespace(
            account_id="ACC1",
            strategy_id="strat1",
            symbol="TXFD6",
            net_qty=1,
            avg_price_scaled=100000,
            realized_pnl_scaled=500,
            fees_scaled=10,
        )
        sys_obj.position_store.total_pnl = 1000
        sys_obj.position_store._peak_equity_scaled = 2000
        sys_obj.position_store.get_drawdown_pct.return_value = 0.01
        sys_obj.position_store.positions = {"TXFD6": pos}

        # Run one iteration then stop
        iteration = 0

        original_sleep = asyncio.sleep

        async def _short_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False
            await original_sleep(0.001)

        with patch("asyncio.sleep", side_effect=_short_sleep):
            with patch.dict(os.environ, {"HFT_PNL_SNAPSHOT_INTERVAL_S": "0.001"}):
                await sys_obj._pnl_snapshot_exporter()

        assert sys_obj.recorder_queue.qsize() >= 1
        item = sys_obj.recorder_queue.get_nowait()
        assert item["topic"] == "pnl_snapshots"
        assert item["data"]["symbol"] == "TXFD6"

    @pytest.mark.asyncio
    async def test_drops_when_queue_full(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.recorder_queue = asyncio.Queue(maxsize=1)
        sys_obj.recorder_queue.put_nowait({"topic": "filler", "data": {}})

        pos = SimpleNamespace(
            account_id="ACC1",
            strategy_id="strat1",
            symbol="TXFD6",
            net_qty=1,
            avg_price_scaled=100000,
            realized_pnl_scaled=500,
            fees_scaled=10,
        )
        sys_obj.position_store.total_pnl = 1000
        sys_obj.position_store._peak_equity_scaled = 2000
        sys_obj.position_store.get_drawdown_pct.return_value = 0.01
        sys_obj.position_store.positions = {"TXFD6": pos}

        iteration = 0
        original_sleep = asyncio.sleep

        async def _short_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False
            await original_sleep(0.001)

        with patch("asyncio.sleep", side_effect=_short_sleep):
            with patch.dict(os.environ, {"HFT_PNL_SNAPSHOT_INTERVAL_S": "0.001"}):
                await sys_obj._pnl_snapshot_exporter()

        assert sys_obj._pnl_snapshot_drops >= 1


# ===========================================================================
# _iter_supervised_services — recorder_bridge inclusion (lines 505-509)
# ===========================================================================


class TestIterSupervisedServicesRecorderBridge:
    def test_includes_recorder_bridge_when_not_all_direct(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = False
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.gateway_service = None

        services = sys_obj._iter_supervised_services()
        names = [s[0] for s in services]
        assert "recorder_bridge" in names

    def test_excludes_recorder_bridge_when_all_direct(self):
        sys_obj = _make_stub()
        sys_obj._md_record_direct = True
        sys_obj._fill_record_direct = True
        sys_obj._order_record_direct = True
        sys_obj.gateway_service = None

        services = sys_obj._iter_supervised_services()
        names = [s[0] for s in services]
        assert "recorder_bridge" not in names

    def test_includes_autonomy_monitor_when_set(self):
        sys_obj = _make_stub()
        sys_obj.autonomy_monitor = MagicMock()
        sys_obj.gateway_service = None

        services = sys_obj._iter_supervised_services()
        names = [s[0] for s in services]
        assert "autonomy_monitor" in names


# ===========================================================================
# _get_drawdown_pct — base_capital=0 edge case
# ===========================================================================


class TestGetDrawdownPctEdge:
    def test_returns_zero_when_base_capital_is_zero(self):
        ps = SimpleNamespace(total_pnl=-100_000)
        settings = {"base_capital": 0}
        result = HFTSystem._get_drawdown_pct(ps, settings)
        assert result == 0.0


# ===========================================================================
# _sync_drain_recorder — no recorder (line 426-427)
# ===========================================================================


class TestSyncDrainRecorderNone:
    def test_returns_early_when_no_recorder(self):
        sys_obj = _make_stub()
        sys_obj.recorder = None

        # Should return immediately without error
        sys_obj._sync_drain_recorder()

    def test_handles_drain_exception(self):
        sys_obj = _make_stub()
        mock_recorder = MagicMock()
        mock_recorder.running = True

        async def _boom():
            raise RuntimeError("drain boom")

        mock_recorder._drain_queue_into_batchers = _boom
        mock_recorder._shutdown_flush = AsyncMock()
        sys_obj.recorder = mock_recorder

        # Should not raise — exception is caught
        sys_obj._sync_drain_recorder()
        assert mock_recorder.running is False


# ===========================================================================
# stop() — sync fallback path (lines 1321-1337)
# ===========================================================================


class TestStopSyncFallback:
    def test_stop_sync_fallback_no_loop(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.recorder = None  # skip drain
        sys_obj._bootstrap_torn_down = False

        # No loop attribute -> sync fallback
        sys_obj.stop()

        assert sys_obj.running is False
        sys_obj.evidence_writer.record_transition.assert_called()

    def test_stop_schedules_async_when_loop_running(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        mock_loop = MagicMock()
        mock_loop.is_running.return_value = True
        sys_obj.loop = mock_loop

        with patch("asyncio.create_task") as mock_ct:
            sys_obj.stop()

        assert sys_obj.running is False
        mock_ct.assert_called_once()


# ===========================================================================
# _on_exec — loop not assigned, overflow full (lines 1522-1540)
# ===========================================================================


class TestOnExecNoLoopOverflow:
    def test_overflow_full_no_loop_persists_and_flags(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        sys_obj.running = True  # running but no loop
        sys_obj._EXEC_OVERFLOW_MAX = 0
        monkeypatch.setenv("HFT_STATE_DIR", str(tmp_path / "state"))

        sys_obj._on_exec("deal", {"payload": {"price": 50}})

        assert sys_obj._exec_overflow_evicted == 1
        assert sys_obj._exec_startup_overflow_lost is True

    def test_overflow_normal_no_loop_buffers(self):
        sys_obj = _make_stub()
        sys_obj.running = True  # running but no loop

        sys_obj._on_exec("order", {"state": "submitted", "payload": {}})

        assert len(sys_obj._exec_overflow_buf) == 1


# ===========================================================================
# _on_exec — deal with nested dict order (line 1441-1452)
# ===========================================================================


class TestOnExecDealDictOrder:
    def test_deal_dict_payload_with_nested_order_dict(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = "strat_D"
        sys_obj.order_adapter.order_id_resolver = resolver

        inner_order = {"ordno": "ORD_INNER", "custom_field": "CF_123"}
        data = {
            "payload": {
                "ordno": None,
                "order": inner_order,
                "full_code": "TXFD6",
                "code": "TXF",
                "action": "Buy",
            }
        }
        sys_obj._on_exec("deal", data)

        assert data["_resolved_strategy_id"] == "strat_D"
        # Verify inner order fields were passed to resolver
        call_args = resolver.resolve_strategy_id_from_candidates.call_args[0][0]
        assert "ORD_INNER" in call_args
        assert "CF_123" in call_args


# ===========================================================================
# _on_exec — loop closing RuntimeError then overflow full (lines 1515-1521)
# ===========================================================================


class TestOnExecLoopClosingOverflowFull:
    def test_overflow_full_on_loop_close_persists_to_dlq(self, tmp_path, monkeypatch):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj._EXEC_OVERFLOW_MAX = 0
        mock_loop = MagicMock()
        mock_loop.call_soon_threadsafe.side_effect = RuntimeError("loop closed")
        sys_obj.loop = mock_loop
        monkeypatch.setenv("HFT_STATE_DIR", str(tmp_path / "state"))

        sys_obj._on_exec("deal", {"payload": {"price": 99}})

        assert sys_obj._exec_overflow_evicted == 1


# ===========================================================================
# stop() — gateway_service branch (line 1316)
# ===========================================================================


class TestStopWithGateway:
    def test_stop_sets_gateway_running_false(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.gateway_service = MagicMock()
        sys_obj.gateway_service.running = True
        sys_obj.recorder = None

        sys_obj.stop()

        assert sys_obj.gateway_service.running is False

    def test_stop_sync_without_gateway(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.gateway_service = None
        sys_obj.recorder = None

        sys_obj.stop()

        assert sys_obj.running is False


# ===========================================================================
# _pnl_snapshot_exporter — exception handling path (line 493-494)
# ===========================================================================


class TestPnlSnapshotExporterException:
    @pytest.mark.asyncio
    async def test_handles_position_store_exception(self):
        sys_obj = _make_stub()
        sys_obj.running = True
        sys_obj.position_store.total_pnl = property(lambda s: (_ for _ in ()).throw(RuntimeError("boom")))

        # total_pnl raises -> exception caught, loop continues
        iteration = 0
        original_sleep = asyncio.sleep

        async def _short_sleep(s):
            nonlocal iteration
            iteration += 1
            if iteration >= 2:
                sys_obj.running = False
            await original_sleep(0.001)

        with patch("asyncio.sleep", side_effect=_short_sleep):
            with patch.dict(os.environ, {"HFT_PNL_SNAPSHOT_INTERVAL_S": "0.001"}):
                # Should not raise — exception is caught inside
                await sys_obj._pnl_snapshot_exporter()

        # Survived without crashing
        assert sys_obj.running is False


# ===========================================================================
# _on_exec — deal fallback to resolve_strategy_from_deal_candidates (dict)
# ===========================================================================


class TestOnExecDealDictFallback:
    def test_falls_back_to_deal_candidates_when_resolver_returns_unknown(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = "UNKNOWN"
        sys_obj.order_adapter.order_id_resolver = resolver
        sys_obj.order_adapter.resolve_strategy_from_deal_candidates.return_value = "strat_fallback"

        data = {
            "payload": {
                "ordno": "X123",
                "full_code": "TMFD6",
                "code": "TMF",
                "action": "Buy",
            }
        }
        sys_obj._on_exec("deal", data)

        assert data["_resolved_strategy_id"] == "strat_fallback"
        sys_obj.order_adapter.resolve_strategy_from_deal_candidates.assert_called_once()

    def test_no_resolution_when_resolver_returns_none_and_no_action(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        resolver.resolve_strategy_id_from_candidates.return_value = None
        sys_obj.order_adapter.order_id_resolver = resolver

        data = {
            "payload": {
                "ordno": "X123",
                "full_code": None,
                "code": None,
                "action": None,
            }
        }
        sys_obj._on_exec("deal", data)

        assert "_resolved_strategy_id" not in data


# ===========================================================================
# _start_service — non-exec service path (no metrics set)
# ===========================================================================


class TestStartServiceNonExec:
    @pytest.mark.asyncio
    async def test_start_service_regular_does_not_set_metrics(self):
        sys_obj = _make_stub()

        async def _noop():
            await asyncio.sleep(0.01)

        sys_obj._start_service("recorder", _noop())
        assert "recorder" in sys_obj.tasks

        sys_obj.tasks["recorder"].cancel()
        try:
            await sys_obj.tasks["recorder"]
        except asyncio.CancelledError:
            pass


# ===========================================================================
# _cleanup_tasks — task that raises non-CancelledError (line 1348-1349)
# ===========================================================================


class TestCleanupTasksExceptionPath:
    @pytest.mark.asyncio
    async def test_cleanup_handles_task_exception(self):
        sys_obj = _make_stub()
        sys_obj._teardown_bootstrap = MagicMock()

        async def _erroring():
            raise RuntimeError("unexpected")

        task = asyncio.create_task(_erroring())
        # Let it fail
        await asyncio.sleep(0.01)
        sys_obj.tasks = {"bad_task": task}

        await sys_obj._cleanup_tasks()
        assert len(sys_obj.tasks) == 0


# ===========================================================================
# _on_exec — deal with no order_adapter (skip resolution)
# ===========================================================================


class TestOnExecNoOrderAdapter:
    def test_deal_without_order_adapter_skips_resolution(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        sys_obj.order_adapter = None

        data = {"payload": {"ordno": "12345"}}
        # Should not raise even with order_adapter=None
        sys_obj._on_exec("deal", data)

        assert "_resolved_strategy_id" not in data
        assert len(sys_obj._exec_overflow_buf) == 1


# ===========================================================================
# _on_exec — non-deal topic (order) skips resolution
# ===========================================================================


class TestOnExecOrderTopicSkipsResolution:
    def test_order_topic_does_not_resolve_strategy(self):
        sys_obj = _make_stub()
        sys_obj.running = False
        resolver = MagicMock()
        sys_obj.order_adapter.order_id_resolver = resolver

        data = {"state": "submitted", "payload": {}}
        sys_obj._on_exec("order", data)

        resolver.resolve_strategy_id_from_candidates.assert_not_called()
        assert "_resolved_strategy_id" not in data
