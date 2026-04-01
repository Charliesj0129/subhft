"""Coverage tests for services/system.py — targeting uncovered branches."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Shared helper — matches the pattern in test_system_service_behavior.py
# ---------------------------------------------------------------------------


def _make_system():
    """Return an HFTSystem instance with all dependencies mocked out."""
    with patch("hft_platform.services.system.configure_logging"):
        with patch("hft_platform.services.system.SystemBootstrapper") as MockBS:
            mock_reg = MagicMock()
            mock_reg.bus = MagicMock()
            mock_reg.raw_queue = asyncio.Queue(maxsize=100)
            mock_reg.raw_exec_queue = asyncio.Queue(maxsize=2)
            mock_reg.risk_queue = asyncio.Queue()
            mock_reg.order_queue = asyncio.Queue()
            mock_reg.recorder_queue = asyncio.Queue(maxsize=50)
            mock_reg.position_store = MagicMock()
            mock_reg.order_id_map = {}
            mock_reg.storm_guard = MagicMock()
            mock_reg.md_client = MagicMock()
            mock_reg.order_client = MagicMock()
            mock_reg.client = MagicMock()
            mock_reg.symbol_metadata = MagicMock()
            mock_reg.price_scale_provider = MagicMock()
            mock_reg.md_service = MagicMock()
            mock_reg.order_adapter = MagicMock()
            mock_reg.execution_gateway = MagicMock()
            mock_reg.exec_service = MagicMock()
            mock_reg.risk_engine = MagicMock()
            mock_reg.recon_service = MagicMock()
            mock_reg.strategy_runner = MagicMock()
            mock_reg.recorder = MagicMock()
            mock_reg.gateway_service = None
            MockBS.return_value.build.return_value = mock_reg

            from hft_platform.services.system import HFTSystem

            sys_obj = HFTSystem.__new__(HFTSystem)
            sys_obj.settings = {}
            sys_obj.running = True
            sys_obj._recorder_seen_tick = False
            sys_obj._recorder_seen_bidask = False
            sys_obj._md_record_direct = True
            sys_obj.bootstrapper = MagicMock()
            sys_obj.registry = mock_reg
            sys_obj.bus = mock_reg.bus
            sys_obj.raw_queue = mock_reg.raw_queue
            sys_obj.raw_exec_queue = mock_reg.raw_exec_queue
            sys_obj.risk_queue = mock_reg.risk_queue
            sys_obj.order_queue = mock_reg.order_queue
            sys_obj.recorder_queue = mock_reg.recorder_queue
            sys_obj.position_store = mock_reg.position_store
            sys_obj.order_id_map = {}
            sys_obj.storm_guard = mock_reg.storm_guard
            sys_obj.md_client = mock_reg.md_client
            sys_obj.order_client = mock_reg.order_client
            sys_obj.client = mock_reg.client
            sys_obj.symbol_metadata = mock_reg.symbol_metadata
            sys_obj.price_scale_provider = mock_reg.price_scale_provider
            sys_obj.md_service = mock_reg.md_service
            sys_obj.order_adapter = mock_reg.order_adapter
            sys_obj.execution_gateway = mock_reg.execution_gateway
            sys_obj.exec_service = mock_reg.exec_service
            sys_obj.risk_engine = mock_reg.risk_engine
            sys_obj.recon_service = mock_reg.recon_service
            sys_obj.strategy_runner = mock_reg.strategy_runner
            sys_obj.recorder = mock_reg.recorder
            sys_obj.gateway_service = None
            sys_obj.evidence_writer = MagicMock()
            sys_obj.platform_degrade_controller = MagicMock()
            sys_obj.platform_degrade_inputs = MagicMock()
            sys_obj.tasks = {}
            sys_obj._recorder_drop_on_full = True
            sys_obj._bootstrap_torn_down = False
            sys_obj._task_restart_attempts = {}
            sys_obj._task_restart_until_s = {}
            sys_obj._task_restart_base_delay_s = 1.0
            sys_obj._task_restart_max_delay_s = 30.0
            sys_obj._queue_log_every_s = 30.0
            sys_obj._last_queue_log_s = 0.0
            sys_obj._mtm_calculator = None
            sys_obj.session_hook_manager = MagicMock()
            sys_obj.session_hook_manager.enabled = False
            sys_obj.health_server = MagicMock()
            sys_obj.autonomy_monitor = None
            sys_obj.session_governor = None
            sys_obj.checkpoint_writer = None
            sys_obj.daily_report_service = None
            sys_obj._exec_overflow_buf = []
            sys_obj._exec_overflow_counter = 0
            sys_obj._exec_overflow_evicted = 0
            sys_obj._EXEC_OVERFLOW_MAX = 50
            sys_obj.loop = None

            return sys_obj


# ---------------------------------------------------------------------------
# _safe_enqueue_exec — three paths
# ---------------------------------------------------------------------------


def test_safe_enqueue_exec_normal_path():
    """Event enqueued into raw_exec_queue without overflow."""
    sys_obj = _make_system()
    sys_obj.raw_exec_queue = asyncio.Queue(maxsize=10)
    event = SimpleNamespace(topic="fill")

    mock_metrics = MagicMock()
    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._safe_enqueue_exec(event)

    assert sys_obj.raw_exec_queue.qsize() == 1
    assert sys_obj._exec_overflow_counter == 0


def test_safe_enqueue_exec_overflow_append():
    """Queue full → event appended to overflow buffer."""
    sys_obj = _make_system()
    # Fill the exec queue to capacity
    sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
    sys_obj.raw_exec_queue.put_nowait(SimpleNamespace(topic="fill"))

    event = SimpleNamespace(topic="fill2")
    mock_metrics = MagicMock()
    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._safe_enqueue_exec(event)

    assert len(sys_obj._exec_overflow_buf) == 1
    assert sys_obj._exec_overflow_counter == 1
    assert sys_obj.storm_guard.trigger_halt.call_count == 0


def test_safe_enqueue_exec_overflow_halt_on_repeated():
    """After 3 overflow events, storm_guard.trigger_halt is called."""
    sys_obj = _make_system()
    sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
    sys_obj.raw_exec_queue.put_nowait(SimpleNamespace(topic="seed"))
    sys_obj._exec_overflow_counter = 2  # two previous overflows

    event = SimpleNamespace(topic="fill3")
    mock_metrics = MagicMock()
    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._safe_enqueue_exec(event)

    sys_obj.storm_guard.trigger_halt.assert_called_once_with("exec_queue_overflow_repeated")


def test_safe_enqueue_exec_overflow_buf_full():
    """Overflow buffer exhausted → event evicted and HALT triggered."""
    sys_obj = _make_system()
    sys_obj.raw_exec_queue = asyncio.Queue(maxsize=1)
    sys_obj.raw_exec_queue.put_nowait(SimpleNamespace(topic="seed"))
    # Fill overflow buffer to max
    sys_obj._exec_overflow_buf = ["x"] * sys_obj._EXEC_OVERFLOW_MAX

    event = SimpleNamespace(topic="overflow_event")
    mock_metrics = MagicMock()
    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._safe_enqueue_exec(event)

    sys_obj.storm_guard.trigger_halt.assert_called_once_with("exec_overflow_buf_exhausted")
    assert sys_obj._exec_overflow_evicted == 1


# ---------------------------------------------------------------------------
# _on_exec
# ---------------------------------------------------------------------------


def test_on_exec_when_running_schedules_call():
    """_on_exec schedules work on loop when running=True."""
    sys_obj = _make_system()
    loop = MagicMock()
    sys_obj.loop = loop
    sys_obj.running = True

    with patch("hft_platform.services.system.timebase") as mock_tb:
        mock_tb.now_ns.return_value = 12345
        with patch("hft_platform.services.system.RawExecEvent", create=True) as _:
            # Just confirm call_soon_threadsafe is called
            sys_obj._on_exec("fill", {"price": 100})

    loop.call_soon_threadsafe.assert_called_once()


def test_on_exec_when_not_running_skips():
    """_on_exec does nothing when running=False."""
    sys_obj = _make_system()
    loop = MagicMock()
    sys_obj.loop = loop
    sys_obj.running = False

    sys_obj._on_exec("fill", {"price": 100})

    loop.call_soon_threadsafe.assert_not_called()


def test_on_exec_no_loop_skips():
    """_on_exec does nothing when loop is None."""
    sys_obj = _make_system()
    sys_obj.loop = None
    sys_obj.running = True

    # Should not raise
    sys_obj._on_exec("fill", {"price": 100})
    assert sys_obj.loop is None


# ---------------------------------------------------------------------------
# _close_broker_client
# ---------------------------------------------------------------------------


def test_close_broker_client_calls_close():
    """_close_broker_client calls close(logout=True) on the client."""
    sys_obj = _make_system()
    mock_client = MagicMock()
    sys_obj.test_client = mock_client

    sys_obj._close_broker_client("test_client")

    mock_client.close.assert_called_once_with(logout=True)


def test_close_broker_client_no_close_attr():
    """_close_broker_client silently skips clients without close method."""
    sys_obj = _make_system()
    client_no_close = SimpleNamespace()  # no close attribute
    sys_obj.test_client = client_no_close

    # Should not raise
    sys_obj._close_broker_client("test_client")
    assert not hasattr(client_no_close, "close")


def test_close_broker_client_exception_is_swallowed():
    """_close_broker_client logs warning on exception, does not raise."""
    sys_obj = _make_system()
    mock_client = MagicMock()
    mock_client.close.side_effect = RuntimeError("logout failed")
    sys_obj.test_client = mock_client

    # Should not raise
    sys_obj._close_broker_client("test_client")
    mock_client.close.assert_called_once()


def test_close_broker_client_none_client():
    """_close_broker_client silently skips None client."""
    sys_obj = _make_system()
    sys_obj.missing_client = None

    sys_obj._close_broker_client("missing_client")
    assert sys_obj.missing_client is None


# ---------------------------------------------------------------------------
# _on_sighup
# ---------------------------------------------------------------------------


def test_on_sighup_reloads_config():
    """_on_sighup calls risk_engine.reload_config()."""
    sys_obj = _make_system()
    sys_obj.risk_engine.reload_config = MagicMock()

    sys_obj._on_sighup()

    sys_obj.risk_engine.reload_config.assert_called_once()


def test_on_sighup_exception_is_logged():
    """_on_sighup swallows exceptions from reload_config."""
    sys_obj = _make_system()
    sys_obj.risk_engine.reload_config = MagicMock(side_effect=RuntimeError("reload error"))

    # Should not raise
    sys_obj._on_sighup()

    sys_obj.risk_engine.reload_config.assert_called_once()


# ---------------------------------------------------------------------------
# _teardown_bootstrap
# ---------------------------------------------------------------------------


def test_teardown_bootstrap_calls_teardown():
    """_teardown_bootstrap calls bootstrapper.teardown() and closes clients."""
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = False
    sys_obj.bootstrapper.teardown = MagicMock()

    sys_obj._teardown_bootstrap()

    sys_obj.bootstrapper.teardown.assert_called_once()
    assert sys_obj._bootstrap_torn_down is True


def test_teardown_bootstrap_idempotent():
    """_teardown_bootstrap is a no-op when already torn down."""
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = True
    sys_obj.bootstrapper.teardown = MagicMock()

    sys_obj._teardown_bootstrap()

    sys_obj.bootstrapper.teardown.assert_not_called()


def test_teardown_bootstrap_exception_is_swallowed():
    """_teardown_bootstrap swallows exceptions from bootstrapper.teardown()."""
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = False
    sys_obj.bootstrapper.teardown = MagicMock(side_effect=RuntimeError("teardown failed"))

    # Should not raise
    sys_obj._teardown_bootstrap()

    assert sys_obj._bootstrap_torn_down is True


# ---------------------------------------------------------------------------
# _update_platform_degrade_state
# ---------------------------------------------------------------------------


def test_update_platform_degrade_state_with_reasons():
    """_update_platform_degrade_state calls enter_reduce_only for each reason."""
    sys_obj = _make_system()
    sys_obj.platform_degrade_inputs.reduce_only_reasons.return_value = ["feed_gap", "queue_depth"]
    sys_obj.platform_degrade_controller.enter_reduce_only = MagicMock()
    sys_obj.platform_degrade_controller.check_auto_recovery = MagicMock()

    with patch("hft_platform.services.system.timebase") as mock_tb:
        mock_tb.now_ns.return_value = 1000
        sys_obj._update_platform_degrade_state()

    assert sys_obj.platform_degrade_controller.enter_reduce_only.call_count == 2
    sys_obj.platform_degrade_controller.check_auto_recovery.assert_called_once()


def test_update_platform_degrade_state_no_reasons():
    """_update_platform_degrade_state calls check_auto_recovery even with no reasons."""
    sys_obj = _make_system()
    sys_obj.platform_degrade_inputs.reduce_only_reasons.return_value = []
    sys_obj.platform_degrade_controller.check_auto_recovery = MagicMock()

    with patch("hft_platform.services.system.timebase") as mock_tb:
        mock_tb.now_ns.return_value = 1000
        sys_obj._update_platform_degrade_state()

    sys_obj.platform_degrade_controller.check_auto_recovery.assert_called_once()


def test_update_platform_degrade_state_no_controller():
    """_update_platform_degrade_state returns early when no controller set."""
    sys_obj = _make_system()
    sys_obj.platform_degrade_controller = None

    # Should not raise
    sys_obj._update_platform_degrade_state()
    assert sys_obj.platform_degrade_controller is None


def test_update_platform_degrade_state_no_inputs():
    """_update_platform_degrade_state returns early when no inputs set."""
    sys_obj = _make_system()
    sys_obj.platform_degrade_inputs = None

    # Should not raise
    sys_obj._update_platform_degrade_state()
    assert sys_obj.platform_degrade_inputs is None


# ---------------------------------------------------------------------------
# _start_service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_service_exec_router_sets_metric():
    """_start_service sets execution_router_alive=1 for exec_router."""
    sys_obj = _make_system()
    sys_obj.tasks = {}

    mock_metrics = MagicMock()

    async def _dummy():
        pass

    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._start_service("exec_router", _dummy())

    mock_metrics.execution_router_alive.set.assert_called_once_with(1)
    # Clean up the created task
    if "exec_router" in sys_obj.tasks and sys_obj.tasks["exec_router"]:
        sys_obj.tasks["exec_router"].cancel()
        try:
            await asyncio.gather(sys_obj.tasks["exec_router"], return_exceptions=True)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_start_service_exec_gateway_sets_metric():
    """_start_service sets execution_gateway_alive=1 for exec_gateway."""
    sys_obj = _make_system()
    sys_obj.tasks = {}

    mock_metrics = MagicMock()

    async def _dummy():
        pass

    with patch("hft_platform.observability.metrics.MetricsRegistry") as MockMR:
        MockMR.get.return_value = mock_metrics
        sys_obj._start_service("exec_gateway", _dummy())

    mock_metrics.execution_gateway_alive.set.assert_called_once_with(1)
    if "exec_gateway" in sys_obj.tasks and sys_obj.tasks["exec_gateway"]:
        sys_obj.tasks["exec_gateway"].cancel()
        try:
            await asyncio.gather(sys_obj.tasks["exec_gateway"], return_exceptions=True)
        except Exception:
            pass


@pytest.mark.asyncio
async def test_start_service_generic():
    """_start_service creates a task for a non-special service name."""
    sys_obj = _make_system()
    sys_obj.tasks = {}

    async def _dummy():
        pass

    sys_obj._start_service("recorder", _dummy())

    assert "recorder" in sys_obj.tasks
    if sys_obj.tasks["recorder"]:
        sys_obj.tasks["recorder"].cancel()
        try:
            await asyncio.gather(sys_obj.tasks["recorder"], return_exceptions=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _pnl_snapshot_exporter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pnl_snapshot_exporter_one_iteration(monkeypatch):
    """_pnl_snapshot_exporter exports one row per position then stops."""
    sys_obj = _make_system()
    sys_obj.running = True

    pos = SimpleNamespace(
        account_id="acct1",
        strategy_id="strat1",
        symbol="2330",
        net_qty=100,
        avg_price_scaled=500000,
        realized_pnl_scaled=1000,
        fees_scaled=50,
    )
    sys_obj.position_store.total_pnl = 1000
    sys_obj.position_store._peak_equity_scaled = 999000
    sys_obj.position_store.get_drawdown_pct.return_value = 0.5
    sys_obj.position_store.positions = {"2330": pos}
    sys_obj.recorder_queue = asyncio.Queue(maxsize=100)

    monkeypatch.setenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "0.01")

    call_count = 0

    async def _mock_sleep(s):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sys_obj.running = False

    with patch("hft_platform.services.system.timebase") as mock_tb:
        mock_tb.now_ns.return_value = 12345
        with patch("asyncio.sleep", side_effect=_mock_sleep):
            await sys_obj._pnl_snapshot_exporter()

    # Should have queued one row
    assert sys_obj.recorder_queue.qsize() >= 1
    item = sys_obj.recorder_queue.get_nowait()
    assert item["topic"] == "pnl_snapshots"
    assert item["data"]["symbol"] == "2330"


@pytest.mark.asyncio
async def test_pnl_snapshot_exporter_queue_full_silent(monkeypatch):
    """_pnl_snapshot_exporter silently drops row when recorder_queue is full."""
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj.recorder_queue = asyncio.Queue(maxsize=1)
    sys_obj.recorder_queue.put_nowait({"topic": "other", "data": {}})  # fill it

    pos = SimpleNamespace(
        account_id="acct1",
        strategy_id="strat1",
        symbol="2330",
        net_qty=0,
        avg_price_scaled=0,
        realized_pnl_scaled=0,
        fees_scaled=0,
    )
    sys_obj.position_store.total_pnl = 0
    sys_obj.position_store._peak_equity_scaled = 0
    sys_obj.position_store.get_drawdown_pct.return_value = 0.0
    sys_obj.position_store.positions = {"2330": pos}

    monkeypatch.setenv("HFT_PNL_SNAPSHOT_INTERVAL_S", "0.01")
    call_count = 0

    async def _mock_sleep(s):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            sys_obj.running = False

    with patch("hft_platform.services.system.timebase") as mock_tb:
        mock_tb.now_ns.return_value = 12345
        with patch("asyncio.sleep", side_effect=_mock_sleep):
            # Should not raise even with full queue
            await sys_obj._pnl_snapshot_exporter()
    # Queue remains at capacity with only the pre-filled item (pnl row was dropped)
    assert sys_obj.recorder_queue.qsize() == 1


# ---------------------------------------------------------------------------
# stop_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_async_cancels_tasks_and_calls_teardown():
    """stop_async cancels running tasks and calls teardown."""
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj._bootstrap_torn_down = False

    # Create a real async task that blocks indefinitely
    async def _blocker():
        await asyncio.sleep(9999)

    task = asyncio.create_task(_blocker())
    sys_obj.tasks = {"blocker": task}

    # Make evidence_writer.record_transition a regular MagicMock
    sys_obj.evidence_writer.record_transition = MagicMock()

    await sys_obj.stop_async()

    assert sys_obj.running is False
    assert sys_obj._bootstrap_torn_down is True
    sys_obj.evidence_writer.record_transition.assert_called_once()


@pytest.mark.asyncio
async def test_stop_async_stops_optional_services():
    """stop_async calls stop() on autonomy_monitor and session_governor when present."""
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj.tasks = {}
    sys_obj.autonomy_monitor = MagicMock()
    sys_obj.autonomy_monitor.stop = AsyncMock()
    sys_obj.session_governor = MagicMock()
    sys_obj.session_governor.stop = AsyncMock()
    sys_obj.evidence_writer.record_transition = MagicMock()

    await sys_obj.stop_async()

    assert sys_obj.running is False
    sys_obj.autonomy_monitor.stop.assert_awaited_once()
    sys_obj.session_governor.stop.assert_awaited_once()
