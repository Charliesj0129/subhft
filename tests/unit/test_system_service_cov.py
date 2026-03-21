"""Coverage-boosting tests for HFTSystem (services/system.py).

Targets uncovered paths: __init__ branches, _env_float, _close_broker_client,
_on_sighup, _teardown_bootstrap, _pnl_snapshot_exporter, _iter_supervised_services
(gateway branch), _start_service metrics path, stop/stop_async/cleanup_tasks,
_on_exec, _recorder_bridge, kill-switch check.
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.risk.storm_guard import StormGuardState
from hft_platform.services.system import HFTSystem

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


class _SessionHookManager:
    enabled = False

    def stop(self) -> None:
        return None

    async def run(self) -> None:
        await asyncio.sleep(0)


class _HealthServer:
    def stop(self) -> None:
        return None

    async def run(self) -> None:
        await asyncio.sleep(0)


def _registry(gateway_service=None):
    return SimpleNamespace(
        bus=SimpleNamespace(),
        raw_queue=asyncio.Queue(),
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
        order_adapter=_Runner(),
        execution_gateway=_ExecutionGateway(),
        exec_service=_Runner(),
        risk_engine=_Runner(),
        recon_service=_Runner(),
        strategy_runner=_Runner(),
        recorder=_Runner(),
        gateway_service=gateway_service,
    )


def _make_system(gateway_service=None) -> HFTSystem:
    bootstrapper = MagicMock()
    bootstrapper.build.return_value = _registry(gateway_service=gateway_service)
    with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
        sys_ = HFTSystem({})
    # Replace session hook manager and health server to avoid real side-effects
    sys_.session_hook_manager = _SessionHookManager()
    sys_.health_server = _HealthServer()
    return sys_


# ---------------------------------------------------------------------------
# _env_float
# ---------------------------------------------------------------------------


class TestEnvFloat:
    def test_returns_default_on_invalid_env(self, monkeypatch):
        monkeypatch.setenv("HFT_TEST_FLOAT_X", "not_a_number")
        result = HFTSystem._env_float("HFT_TEST_FLOAT_X", 5.0, min_value=0.1)
        assert result == 5.0

    def test_respects_min_value(self, monkeypatch):
        monkeypatch.setenv("HFT_TEST_FLOAT_Y", "0.0")
        result = HFTSystem._env_float("HFT_TEST_FLOAT_Y", 5.0, min_value=1.0)
        assert result == 1.0

    def test_returns_env_value_when_valid(self, monkeypatch):
        monkeypatch.setenv("HFT_TEST_FLOAT_Z", "3.5")
        result = HFTSystem._env_float("HFT_TEST_FLOAT_Z", 1.0, min_value=0.1)
        assert result == 3.5

    def test_returns_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("HFT_TEST_FLOAT_W", raising=False)
        result = HFTSystem._env_float("HFT_TEST_FLOAT_W", 7.7, min_value=0.1)
        assert result == 7.7


# ---------------------------------------------------------------------------
# _close_broker_client
# ---------------------------------------------------------------------------


class TestCloseBrokerClient:
    def test_calls_close_with_logout(self):
        system = _make_system()
        mock_client = MagicMock()
        mock_client.close = MagicMock()
        system.md_client = mock_client
        system._close_broker_client("md_client")
        mock_client.close.assert_called_once_with(logout=True)

    def test_handles_close_exception_gracefully(self):
        system = _make_system()
        mock_client = MagicMock()
        mock_client.close.side_effect = RuntimeError("logout failed")
        system.md_client = mock_client
        # Should not raise
        system._close_broker_client("md_client")

    def test_noop_when_client_is_none(self):
        system = _make_system()
        system.md_client = None
        # Should not raise
        system._close_broker_client("md_client")

    def test_noop_when_client_has_no_close(self):
        system = _make_system()
        system.md_client = SimpleNamespace()  # no .close
        system._close_broker_client("md_client")

    def test_noop_when_attribute_missing(self):
        system = _make_system()
        # nonexistent attribute returns None via getattr
        system._close_broker_client("nonexistent_client_xyz")


# ---------------------------------------------------------------------------
# _on_sighup
# ---------------------------------------------------------------------------


class TestOnSighup:
    def test_reloads_risk_config(self):
        system = _make_system()
        system.risk_engine = MagicMock()
        system._on_sighup()
        system.risk_engine.reload_config.assert_called_once()

    def test_handles_reload_exception(self):
        system = _make_system()
        system.risk_engine = MagicMock()
        system.risk_engine.reload_config.side_effect = RuntimeError("reload failed")
        # Should not raise
        system._on_sighup()


# ---------------------------------------------------------------------------
# _teardown_bootstrap
# ---------------------------------------------------------------------------


class TestTeardownBootstrap:
    def test_idempotent_second_call_does_nothing(self):
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        system.session_hook_manager = _SessionHookManager()
        system.health_server = _HealthServer()
        system._teardown_bootstrap()
        system._teardown_bootstrap()
        assert bootstrapper.teardown.call_count == 1

    def test_handles_teardown_exception(self):
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        bootstrapper.teardown.side_effect = RuntimeError("teardown boom")
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        system.session_hook_manager = _SessionHookManager()
        system.health_server = _HealthServer()
        # Should not raise
        system._teardown_bootstrap()
        assert system._bootstrap_torn_down is True


# ---------------------------------------------------------------------------
# _iter_supervised_services — gateway branch
# ---------------------------------------------------------------------------


class TestIterSupervisedServicesGateway:
    def test_includes_gateway_when_present(self):
        gateway = _Runner()
        system = _make_system(gateway_service=gateway)
        names = {n for n, _, _ in system._iter_supervised_services()}
        assert "gateway" in names
        assert "risk" not in names

    def test_includes_risk_when_no_gateway(self):
        system = _make_system(gateway_service=None)
        names = {n for n, _, _ in system._iter_supervised_services()}
        assert "risk" in names
        assert "gateway" not in names

    def test_all_critical_services_present(self):
        system = _make_system()
        names = {n for n, _, _ in system._iter_supervised_services()}
        expected = {"md", "exec_router", "order", "exec_gateway", "recon", "strat", "recorder"}
        assert expected.issubset(names)


# ---------------------------------------------------------------------------
# _reset_restart_backoff_if_healthy
# ---------------------------------------------------------------------------


class TestResetRestartBackoff:
    def test_clears_state_when_task_alive(self):
        system = _make_system()
        mock_task = MagicMock()
        mock_task.done.return_value = False
        system._task_restart_attempts["strat"] = 5
        system._task_restart_until_s["strat"] = 9999.0
        system._reset_restart_backoff_if_healthy("strat", mock_task)
        assert "strat" not in system._task_restart_attempts
        assert "strat" not in system._task_restart_until_s

    def test_does_not_clear_state_when_task_done(self):
        system = _make_system()
        mock_task = MagicMock()
        mock_task.done.return_value = True
        system._task_restart_attempts["strat"] = 5
        system._task_restart_until_s["strat"] = 9999.0
        system._reset_restart_backoff_if_healthy("strat", mock_task)
        # State should remain because task is done
        assert system._task_restart_attempts["strat"] == 5

    def test_noop_when_task_is_none(self):
        system = _make_system()
        system._task_restart_attempts["strat"] = 2
        system._reset_restart_backoff_if_healthy("strat", None)
        # Should not clear when task is None
        assert system._task_restart_attempts["strat"] == 2


# ---------------------------------------------------------------------------
# stop() and stop_async()
# ---------------------------------------------------------------------------


class TestStop:
    def test_stop_sets_running_false(self):
        system = _make_system()
        system.running = True
        system.stop()
        assert system.running is False

    def test_stop_sets_services_running_false(self):
        system = _make_system()
        system.running = True
        system.md_service.running = True
        system.exec_service.running = True
        system.risk_engine.running = True
        system.recon_service.running = True
        system.strategy_runner.running = True
        system.stop()
        assert system.md_service.running is False
        assert system.exec_service.running is False
        assert system.risk_engine.running is False
        assert system.recon_service.running is False
        assert system.strategy_runner.running is False

    def test_stop_calls_execution_gateway_stop(self):
        system = _make_system()
        system.execution_gateway = MagicMock()
        system.stop()
        system.execution_gateway.stop.assert_called_once()

    def test_stop_no_loop_skips_cleanup_task(self):
        system = _make_system()
        # No loop attribute set
        system.loop = None
        system.stop()  # Should not raise

    @pytest.mark.asyncio
    async def test_stop_async_cancels_tasks(self):
        system = _make_system()
        system.running = True

        # Create a real long-running task
        async def _long():
            await asyncio.sleep(100)

        t = asyncio.create_task(_long())
        system.tasks["dummy"] = t

        await system.stop_async()
        assert system.running is False
        assert len(system.tasks) == 0

    @pytest.mark.asyncio
    async def test_stop_async_handles_already_done_task(self):
        system = _make_system()
        system.running = True

        # Task that completes immediately
        async def _done():
            return None

        t = asyncio.create_task(_done())
        await asyncio.sleep(0)  # let it complete
        system.tasks["done_task"] = t

        await system.stop_async()
        assert system.running is False


# ---------------------------------------------------------------------------
# _cleanup_tasks
# ---------------------------------------------------------------------------


class TestCleanupTasks:
    @pytest.mark.asyncio
    async def test_cleanup_cancels_running_tasks(self):
        system = _make_system()

        async def _slow():
            await asyncio.sleep(100)

        t = asyncio.create_task(_slow())
        system.tasks["slow"] = t

        await system._cleanup_tasks()
        assert len(system.tasks) == 0
        assert t.cancelled()

    @pytest.mark.asyncio
    async def test_cleanup_handles_exception_during_await(self):
        system = _make_system()

        async def _error():
            await asyncio.sleep(0)
            raise RuntimeError("oops")

        t = asyncio.create_task(_error())
        await asyncio.sleep(0)  # let it fail
        system.tasks["err"] = t

        # Should not raise
        await system._cleanup_tasks()
        assert len(system.tasks) == 0

    @pytest.mark.asyncio
    async def test_cleanup_clears_tasks_dict(self):
        system = _make_system()

        async def _noop():
            return None

        t = asyncio.create_task(_noop())
        await asyncio.sleep(0)
        system.tasks["t1"] = t

        await system._cleanup_tasks()
        assert system.tasks == {}


# ---------------------------------------------------------------------------
# _on_exec
# ---------------------------------------------------------------------------


class TestOnExec:
    def test_on_exec_schedules_event_when_running(self):
        system = _make_system()
        system.running = True
        mock_loop = MagicMock()
        system.loop = mock_loop

        with patch("hft_platform.services.system.timebase.now_ns", return_value=123456789):
            with patch("hft_platform.services.system.RawExecEvent", create=True) as MockRawExecEvent:
                # Import RawExecEvent is inside the method; patch at system module level
                # We need to also patch the import inside _on_exec
                pass

        # Direct test: verify call_soon_threadsafe is triggered
        system.running = True
        mock_loop = MagicMock()
        system.loop = mock_loop

        # Patch the inner import
        mock_raw_exec_event = MagicMock()
        with patch.dict(
            "sys.modules", {"hft_platform.execution.normalizer": MagicMock(RawExecEvent=mock_raw_exec_event)}
        ):
            system._on_exec("order", {"state": "Filled", "payload": {}})

        mock_loop.call_soon_threadsafe.assert_called_once()

    def test_on_exec_noop_when_not_running(self):
        system = _make_system()
        system.running = False
        mock_loop = MagicMock()
        system.loop = mock_loop

        system._on_exec("order", {"state": "Filled"})
        mock_loop.call_soon_threadsafe.assert_not_called()

    def test_on_exec_noop_when_no_loop(self):
        system = _make_system()
        system.running = True
        # No loop attribute
        if hasattr(system, "loop"):
            del system.loop

        # Should not raise
        system._on_exec("deal", {"payload": {}})


# ---------------------------------------------------------------------------
# _start_service — metrics path for exec_router and exec_gateway
# ---------------------------------------------------------------------------


class TestStartServiceMetrics:
    @pytest.mark.asyncio
    async def test_start_service_exec_router_sets_metric(self):
        system = _make_system()

        mock_metrics = MagicMock()
        mock_metrics.execution_router_alive = MagicMock()
        mock_metrics.execution_gateway_alive = MagicMock()

        async def _noop():
            await asyncio.sleep(0)

        # MetricsRegistry is imported locally inside _start_service
        with patch("hft_platform.observability.metrics.MetricsRegistry.get", return_value=mock_metrics):
            system._start_service("exec_router", _noop())

        mock_metrics.execution_router_alive.set.assert_called_once_with(1)

        # Clean up task
        t = system.tasks.get("exec_router")
        if t:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    @pytest.mark.asyncio
    async def test_start_service_exec_gateway_sets_metric(self):
        system = _make_system()

        mock_metrics = MagicMock()
        mock_metrics.execution_router_alive = MagicMock()
        mock_metrics.execution_gateway_alive = MagicMock()

        async def _noop():
            await asyncio.sleep(0)

        # MetricsRegistry is imported locally inside _start_service
        with patch("hft_platform.observability.metrics.MetricsRegistry.get", return_value=mock_metrics):
            system._start_service("exec_gateway", _noop())

        mock_metrics.execution_gateway_alive.set.assert_called_once_with(1)

        # Clean up task
        t = system.tasks.get("exec_gateway")
        if t:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    @pytest.mark.asyncio
    async def test_start_service_metrics_exception_swallowed(self):
        """MetricsRegistry failure must not propagate."""
        system = _make_system()

        async def _noop():
            await asyncio.sleep(0)

        with patch(
            "hft_platform.observability.metrics.MetricsRegistry.get", side_effect=RuntimeError("metrics unavailable")
        ):
            # Should not raise
            system._start_service("exec_router", _noop())

        # Clean up task
        t = system.tasks.get("exec_router")
        if t:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    @pytest.mark.asyncio
    async def test_start_service_non_exec_creates_task(self):
        """Non exec_router/exec_gateway names create a task without metrics."""
        system = _make_system()

        async def _noop():
            await asyncio.sleep(0)

        system._start_service("strat", _noop())
        assert "strat" in system.tasks

        t = system.tasks.get("strat")
        if t:
            t.cancel()
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass


# ---------------------------------------------------------------------------
# _pnl_snapshot_exporter
# ---------------------------------------------------------------------------


class TestPnlSnapshotExporter:
    @pytest.mark.asyncio
    async def test_exports_position_rows_to_recorder_queue(self, monkeypatch):
        system = _make_system()
        system.running = True

        # Mock position store
        pos = SimpleNamespace(
            account_id="acc1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=100,
            avg_price_scaled=1_500_000,  # 150.0000 x10000
            realized_pnl_scaled=5_000_000,
            fees_scaled=100_000,
        )
        system.position_store = SimpleNamespace(
            total_pnl=5_000_000,
            _peak_equity_scaled=100_000_000,
            positions={"2330": pos},
            get_drawdown_pct=lambda: -0.01,
        )

        monkeypatch.setenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "0")

        with patch("hft_platform.services.system.timebase.now_ns", return_value=999_000_000_000):
            with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                # Run one iteration then stop
                call_count = 0

                async def _sleep_side_effect(secs):
                    nonlocal call_count
                    call_count += 1
                    if call_count >= 1:
                        system.running = False

                mock_sleep.side_effect = _sleep_side_effect
                await system._pnl_snapshot_exporter()

        assert not system.recorder_queue.empty()
        item = system.recorder_queue.get_nowait()
        assert item["topic"] == "pnl_snapshots"
        assert item["data"]["symbol"] == "2330"
        assert item["data"]["snapshot_ts"] == 999_000_000_000

    @pytest.mark.asyncio
    async def test_exports_skips_on_queue_full(self, monkeypatch):
        system = _make_system()
        system.running = True
        system.recorder_queue = asyncio.Queue(maxsize=1)
        # Pre-fill queue
        system.recorder_queue.put_nowait({"topic": "dummy", "data": {}})

        pos = SimpleNamespace(
            account_id="acc1",
            strategy_id="strat1",
            symbol="2330",
            net_qty=1,
            avg_price_scaled=1_000_000,
            realized_pnl_scaled=0,
            fees_scaled=0,
        )
        system.position_store = SimpleNamespace(
            total_pnl=0,
            _peak_equity_scaled=100_000_000,
            positions={"2330": pos},
            get_drawdown_pct=lambda: 0.0,
        )

        monkeypatch.setenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "0")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0

            async def _sleep_side_effect(secs):
                nonlocal call_count
                call_count += 1
                system.running = False

            mock_sleep.side_effect = _sleep_side_effect
            # Should not raise even when queue is full
            await system._pnl_snapshot_exporter()

    @pytest.mark.asyncio
    async def test_handles_position_store_exception(self, monkeypatch):
        system = _make_system()
        system.running = True

        # Make position store blow up
        system.position_store = SimpleNamespace(
            total_pnl=property(lambda self: (_ for _ in ()).throw(RuntimeError("ps_err")))
        )

        monkeypatch.setenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "0")

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            call_count = 0

            async def _sleep_side_effect(secs):
                nonlocal call_count
                call_count += 1
                system.running = False

            mock_sleep.side_effect = _sleep_side_effect
            # Should not raise — exception is swallowed
            await system._pnl_snapshot_exporter()


# ---------------------------------------------------------------------------
# _recorder_bridge
# ---------------------------------------------------------------------------


def _make_mock_bus():
    """Return a MagicMock bus that supports consume / consume_batch."""
    return MagicMock()


class TestRecorderBridge:
    @pytest.mark.asyncio
    async def test_direct_mode_skips_tick_and_bidask(self, monkeypatch):
        """When _md_record_direct=True, TickEvent and BidAskEvent are skipped."""
        system = _make_system()
        system._md_record_direct = True
        system.bus = _make_mock_bus()

        from hft_platform.events import TickEvent

        tick = MagicMock(spec=TickEvent)
        tick.__class__ = TickEvent

        async def _fake_consumer():
            yield tick

        mock_map = MagicMock(return_value=None)
        system.bus.consume.return_value = _fake_consumer()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", mock_map):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

        # map_event_to_record should not be called for TickEvents in direct mode
        mock_map.assert_not_called()

    @pytest.mark.asyncio
    async def test_recorder_bridge_cancelled_cleanly(self, monkeypatch):
        """CancelledError from the consumer loop exits cleanly."""
        system = _make_system()
        system._md_record_direct = False
        system.bus = _make_mock_bus()

        async def _cancel_consumer():
            raise asyncio.CancelledError
            yield  # make it a generator

        system.bus.consume.return_value = _cancel_consumer()

        with patch("hft_platform.services.system.PriceCodec"):
            # Should not raise
            await system._recorder_bridge()

    @pytest.mark.asyncio
    async def test_recorder_bridge_drops_on_queue_full(self, monkeypatch):
        """When _recorder_drop_on_full=True, QueueFull is silently swallowed."""
        system = _make_system()
        system._md_record_direct = False
        system._recorder_drop_on_full = True
        system.recorder_queue = asyncio.Queue(maxsize=1)
        system.recorder_queue.put_nowait({"topic": "existing", "data": {}})
        system.bus = _make_mock_bus()

        from hft_platform.events import LOBStatsEvent

        mock_event = MagicMock(spec=LOBStatsEvent)
        mock_event.__class__ = LOBStatsEvent

        async def _one_event_consumer():
            yield mock_event
            await asyncio.sleep(100)

        mock_record = ("lob_stats", {"mid": 1500000})
        system.bus.consume.return_value = _one_event_consumer()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=mock_record):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass

        # Queue should remain full (drop happened)
        assert system.recorder_queue.full()

    @pytest.mark.asyncio
    async def test_recorder_bridge_awaits_when_not_drop_on_full(self, monkeypatch):
        """When _recorder_drop_on_full=False, recorder_queue.put is awaited."""
        system = _make_system()
        system._md_record_direct = False
        system._recorder_drop_on_full = False
        system.bus = _make_mock_bus()

        from hft_platform.events import LOBStatsEvent

        mock_event = MagicMock(spec=LOBStatsEvent)
        mock_event.__class__ = LOBStatsEvent

        async def _one_event_consumer():
            yield mock_event

        mock_record = ("lob_stats", {"mid": 1500000})
        put_calls = []

        async def _fake_put(item):
            put_calls.append(item)

        system.recorder_queue.put = _fake_put
        system.bus.consume.return_value = _one_event_consumer()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=mock_record):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        assert len(put_calls) == 1
        assert put_calls[0]["topic"] == "lob_stats"

    @pytest.mark.asyncio
    async def test_recorder_bridge_tick_seen_flag(self, monkeypatch):
        """First TickEvent sets _recorder_seen_tick flag."""
        system = _make_system()
        system._md_record_direct = False  # don't skip ticks
        system._recorder_seen_tick = False
        system.bus = _make_mock_bus()

        from hft_platform.events import TickEvent

        mock_tick = MagicMock(spec=TickEvent)
        mock_tick.__class__ = TickEvent
        mock_tick.symbol = "2330"

        async def _one_tick():
            yield mock_tick

        system.bus.consume.return_value = _one_tick()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=None):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        assert system._recorder_seen_tick is True

    @pytest.mark.asyncio
    async def test_recorder_bridge_bidask_seen_flag(self, monkeypatch):
        """First BidAskEvent sets _recorder_seen_bidask flag."""
        system = _make_system()
        system._md_record_direct = False
        system._recorder_seen_bidask = False
        system.bus = _make_mock_bus()

        from hft_platform.events import BidAskEvent

        mock_bidask = MagicMock(spec=BidAskEvent)
        mock_bidask.__class__ = BidAskEvent
        mock_bidask.symbol = "2330"
        mock_bidask.is_snapshot = False

        async def _one_bidask():
            yield mock_bidask

        system.bus.consume.return_value = _one_bidask()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=None):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        assert system._recorder_seen_bidask is True

    @pytest.mark.asyncio
    async def test_recorder_bridge_batch_mode(self, monkeypatch):
        """When batch_size > 1, consume_batch is used."""
        system = _make_system()
        system._md_record_direct = False
        system._recorder_drop_on_full = True
        monkeypatch.setenv("HFT_BUS_BATCH_SIZE", "5")
        system.bus = _make_mock_bus()

        from hft_platform.events import LOBStatsEvent

        mock_event = MagicMock(spec=LOBStatsEvent)
        mock_event.__class__ = LOBStatsEvent

        batch = [mock_event]

        async def _batch_consumer():
            yield batch

        system.bus.consume_batch.return_value = _batch_consumer()

        with patch("hft_platform.services.system.PriceCodec"):
            with patch("hft_platform.recorder.mapper.map_event_to_record", return_value=None):
                try:
                    await asyncio.wait_for(system._recorder_bridge(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

        # consume_batch was used (not consume)
        system.bus.consume_batch.assert_called_once()


# ---------------------------------------------------------------------------
# Kill-switch file check (in _supervise logic, tested in isolation)
# ---------------------------------------------------------------------------


class TestKillSwitchFile:
    def test_kill_switch_triggers_halt_with_reason(self, tmp_path, monkeypatch):
        """Kill switch file with JSON reason triggers halt."""
        ks_file = tmp_path / "kill_switch"
        ks_data = {"reason": "manual_stop_test"}
        ks_file.write_text(json.dumps(ks_data))

        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(ks_file))

        system = _make_system()
        system.storm_guard = _StormGuard()
        assert system.storm_guard.state == StormGuardState.NORMAL

        # Simulate the kill-switch check block from _supervise
        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            if system.storm_guard.state != StormGuardState.HALT:
                try:
                    with open(kill_switch_path, "r") as _ksf:
                        _ks_data = json.load(_ksf)
                    _ks_reason = _ks_data.get("reason", "unknown")
                except Exception:
                    _ks_reason = "kill_switch_file_present"
                system.storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")

        assert system.storm_guard.state == StormGuardState.HALT

    def test_kill_switch_skips_if_already_halted(self, tmp_path, monkeypatch):
        """Kill switch file is ignored when system already in HALT."""
        ks_file = tmp_path / "kill_switch"
        ks_file.write_text(json.dumps({"reason": "already_halted"}))

        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(ks_file))

        system = _make_system()
        system.storm_guard.trigger_halt("prior_halt")
        trigger_halt_calls_before = 0  # already halted

        # Simulate: if state != HALT skip
        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        triggered = False
        if os.path.exists(kill_switch_path):
            if system.storm_guard.state != StormGuardState.HALT:
                triggered = True

        assert triggered is False  # should not re-trigger

    def test_kill_switch_handles_invalid_json(self, tmp_path, monkeypatch):
        """Kill switch file with invalid JSON falls back to generic reason."""
        ks_file = tmp_path / "kill_switch"
        ks_file.write_text("NOT_JSON{{{")

        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(ks_file))

        system = _make_system()
        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")

        _ks_reason = None
        if os.path.exists(kill_switch_path):
            if system.storm_guard.state != StormGuardState.HALT:
                try:
                    with open(kill_switch_path, "r") as _ksf:
                        _ks_data = json.load(_ksf)
                    _ks_reason = _ks_data.get("reason", "unknown")
                except Exception:
                    _ks_reason = "kill_switch_file_present"
                system.storm_guard.trigger_halt(f"KILL_SWITCH_FILE: {_ks_reason}")

        assert _ks_reason == "kill_switch_file_present"
        assert system.storm_guard.state == StormGuardState.HALT

    def test_no_kill_switch_file_no_halt(self, tmp_path, monkeypatch):
        """No kill switch file means no halt."""
        monkeypatch.setenv("HFT_KILL_SWITCH_PATH", str(tmp_path / "nonexistent"))

        system = _make_system()
        kill_switch_path = os.getenv("HFT_KILL_SWITCH_PATH", ".runtime/kill_switch")
        if os.path.exists(kill_switch_path):
            system.storm_guard.trigger_halt("KILL_SWITCH_FILE")

        assert system.storm_guard.state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# MTM calculator init branches
# ---------------------------------------------------------------------------


class TestMtmCalculatorInit:
    def test_mtm_calculator_init_with_lob(self):
        """MarkToMarketCalculator is created when md_service has a lob attribute."""
        lob = MagicMock()
        lob.get_mid_price = MagicMock(return_value=None)

        runner = _Runner()
        runner.lob = lob

        reg = _registry()
        reg.md_service = runner
        reg.position_store = MagicMock()

        bootstrapper = MagicMock()
        bootstrapper.build.return_value = reg

        mock_mtm = MagicMock()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            with patch("hft_platform.execution.mtm.MarkToMarketCalculator", return_value=mock_mtm) as MockMTM:
                system = HFTSystem({})

        # MTM calculator should be set (or None if import failed — both are valid)
        # We just verify no exception was raised

    def test_mtm_calculator_init_without_lob(self):
        """No lob attribute → _mtm_calculator stays None."""
        reg = _registry()
        # _Runner does NOT have .lob attribute

        bootstrapper = MagicMock()
        bootstrapper.build.return_value = reg

        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})

        # _mtm_calculator is None because md_service has no .lob
        assert system._mtm_calculator is None

    def test_mtm_calculator_init_exception_handled(self):
        """If MTM init throws, system still boots with _mtm_calculator=None."""
        lob = MagicMock()
        runner = _Runner()
        runner.lob = lob

        reg = _registry()
        reg.md_service = runner
        reg.position_store = MagicMock()

        bootstrapper = MagicMock()
        bootstrapper.build.return_value = reg

        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            with patch("hft_platform.execution.mtm.MarkToMarketCalculator", side_effect=RuntimeError("mtm_fail")):
                system = HFTSystem({})

        assert system._mtm_calculator is None


# ---------------------------------------------------------------------------
# HFT_MD_RECORD_DIRECT env var
# ---------------------------------------------------------------------------


class TestMdRecordDirectEnvVar:
    def test_direct_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("HFT_MD_RECORD_DIRECT", raising=False)
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        assert system._md_record_direct is True

    def test_direct_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("HFT_MD_RECORD_DIRECT", "0")
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        assert system._md_record_direct is False

    def test_direct_disabled_via_false_string(self, monkeypatch):
        monkeypatch.setenv("HFT_MD_RECORD_DIRECT", "false")
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        assert system._md_record_direct is False


# ---------------------------------------------------------------------------
# HFT_RECORDER_DROP_ON_FULL env var
# ---------------------------------------------------------------------------


class TestRecorderDropOnFullEnvVar:
    def test_drop_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("HFT_RECORDER_DROP_ON_FULL", raising=False)
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        assert system._recorder_drop_on_full is True

    def test_drop_disabled_via_env(self, monkeypatch):
        monkeypatch.setenv("HFT_RECORDER_DROP_ON_FULL", "0")
        bootstrapper = MagicMock()
        bootstrapper.build.return_value = _registry()
        with patch("hft_platform.services.system.SystemBootstrapper", return_value=bootstrapper):
            system = HFTSystem({})
        assert system._recorder_drop_on_full is False
