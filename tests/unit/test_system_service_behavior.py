"""Behavior tests for services/system.py."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_system():
    """Return an HFTSystem instance with all dependencies mocked out."""
    with patch("hft_platform.services.system.configure_logging"):
        with patch("hft_platform.services.system.SystemBootstrapper") as MockBS:
            mock_reg = MagicMock()
            mock_reg.bus = MagicMock()
            mock_reg.raw_queue = asyncio.Queue()
            mock_reg.raw_exec_queue = asyncio.Queue()
            mock_reg.risk_queue = asyncio.Queue()
            mock_reg.order_queue = asyncio.Queue()
            mock_reg.recorder_queue = asyncio.Queue()
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
            sys_obj.running = False
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
            sys_obj.checkpoint_writer = None
            sys_obj.daily_report_service = None

            return sys_obj


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


def test_env_float_valid(monkeypatch):
    from hft_platform.services.system import HFTSystem

    monkeypatch.setenv("HFT_TEST_FLOAT_VAR", "3.5")
    result = HFTSystem._env_float("HFT_TEST_FLOAT_VAR", 1.0, min_value=0.1)
    assert result == 3.5


def test_env_float_default(monkeypatch):
    from hft_platform.services.system import HFTSystem

    monkeypatch.delenv("HFT_TEST_FLOAT_VAR_X", raising=False)
    result = HFTSystem._env_float("HFT_TEST_FLOAT_VAR_X", 2.5, min_value=0.1)
    assert result == 2.5


def test_env_float_below_min(monkeypatch):
    from hft_platform.services.system import HFTSystem

    monkeypatch.setenv("HFT_TEST_FLOAT_VAR2", "0.001")
    result = HFTSystem._env_float("HFT_TEST_FLOAT_VAR2", 1.0, min_value=0.5)
    assert result == 0.5


def test_env_float_invalid(monkeypatch):
    from hft_platform.services.system import HFTSystem

    monkeypatch.setenv("HFT_TEST_FLOAT_VAR3", "not_a_float")
    result = HFTSystem._env_float("HFT_TEST_FLOAT_VAR3", 7.0, min_value=0.1)
    assert result == 7.0


def test_get_max_feed_gap_s_no_fn():
    from hft_platform.services.system import HFTSystem

    md = SimpleNamespace()
    assert HFTSystem._get_max_feed_gap_s(md) == 0.0


def test_get_max_feed_gap_s_with_fn():
    from hft_platform.services.system import HFTSystem

    md = SimpleNamespace(get_max_feed_gap_s=lambda: 5.0)
    result = HFTSystem._get_max_feed_gap_s(md)
    assert result == 5.0


def test_get_max_feed_gap_s_outside_reconnect_window():
    from hft_platform.services.system import HFTSystem

    md = SimpleNamespace(
        get_max_feed_gap_s=lambda: 5.0,
        within_reconnect_window=lambda: False,
    )
    assert HFTSystem._get_max_feed_gap_s(md) == 0.0


def test_get_feed_gaps_by_symbol_no_fn():
    from hft_platform.services.system import HFTSystem

    md = SimpleNamespace()
    assert HFTSystem._get_feed_gaps_by_symbol(md) == {}


def test_get_feed_gaps_by_symbol_with_fn():
    from hft_platform.services.system import HFTSystem

    md = SimpleNamespace(get_feed_gaps_by_symbol=lambda: {"TSMC": 2.0})
    result = HFTSystem._get_feed_gaps_by_symbol(md)
    assert result["TSMC"] == 2.0


def test_get_drawdown_pct_no_fn():
    from hft_platform.services.system import HFTSystem

    ps = SimpleNamespace()
    assert HFTSystem._get_drawdown_pct(ps, {}) == 0.0


def test_get_drawdown_pct_with_fn():
    from hft_platform.services.system import HFTSystem

    ps = SimpleNamespace(get_drawdown_pct=lambda: 0.05)
    assert HFTSystem._get_drawdown_pct(ps, {}) == 0.05


def test_get_drawdown_pct_from_total_pnl():
    from hft_platform.services.system import HFTSystem

    ps = SimpleNamespace(total_pnl=-100_000)
    settings = {"base_capital": 1_000_000}
    result = HFTSystem._get_drawdown_pct(ps, settings)
    assert result == pytest.approx(-0.1)


def test_get_drawdown_pct_positive_pnl():
    from hft_platform.services.system import HFTSystem

    ps = SimpleNamespace(total_pnl=50_000)
    assert HFTSystem._get_drawdown_pct(ps, {}) == 0.0


def test_set_service_running_has_attr():
    from hft_platform.services.system import HFTSystem

    svc = SimpleNamespace(running=True)
    HFTSystem._set_service_running(svc, False)
    assert svc.running is False


def test_set_service_running_no_attr():
    from hft_platform.services.system import HFTSystem

    svc = SimpleNamespace()
    HFTSystem._set_service_running(svc, False)  # Should not raise
    assert not hasattr(svc, "running")


# ---------------------------------------------------------------------------
# Instance methods
# ---------------------------------------------------------------------------


def test_close_broker_client_has_close():
    sys_obj = _make_system()
    mock_client = MagicMock()
    sys_obj.my_client = mock_client
    sys_obj._close_broker_client("my_client")
    mock_client.close.assert_called_once_with(logout=True)


def test_close_broker_client_close_raises():
    sys_obj = _make_system()
    mock_client = MagicMock()
    mock_client.close.side_effect = RuntimeError("logout failed")
    sys_obj.my_client = mock_client
    sys_obj._close_broker_client("my_client")  # Should not raise
    mock_client.close.assert_called_once_with(logout=True)


def test_close_broker_client_no_close():
    sys_obj = _make_system()
    sys_obj.no_close_client = SimpleNamespace()
    sys_obj._close_broker_client("no_close_client")  # Should not raise
    assert hasattr(sys_obj, "no_close_client")


def test_close_broker_client_missing():
    sys_obj = _make_system()
    sys_obj._close_broker_client("nonexistent_client")  # Should not raise
    assert not hasattr(sys_obj, "nonexistent_client")


def test_on_sighup_success():
    sys_obj = _make_system()
    sys_obj.risk_engine = MagicMock()
    sys_obj._on_sighup()
    sys_obj.risk_engine.reload_config.assert_called_once()


def test_on_sighup_raises():
    sys_obj = _make_system()
    sys_obj.risk_engine = MagicMock()
    sys_obj.risk_engine.reload_config.side_effect = RuntimeError("oops")
    sys_obj._on_sighup()  # Should not raise
    sys_obj.risk_engine.reload_config.assert_called_once()


def test_teardown_bootstrap_first_call():
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = False
    sys_obj.md_client = MagicMock()
    sys_obj.order_client = MagicMock()
    sys_obj.bootstrapper = MagicMock()
    sys_obj._teardown_bootstrap()
    assert sys_obj._bootstrap_torn_down is True
    sys_obj.bootstrapper.teardown.assert_called_once()


def test_teardown_bootstrap_idempotent():
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = True
    sys_obj.bootstrapper = MagicMock()
    sys_obj._teardown_bootstrap()
    sys_obj.bootstrapper.teardown.assert_not_called()


def test_teardown_bootstrap_teardown_raises():
    sys_obj = _make_system()
    sys_obj._bootstrap_torn_down = False
    sys_obj.md_client = MagicMock()
    sys_obj.order_client = MagicMock()
    sys_obj.bootstrapper = MagicMock()
    sys_obj.bootstrapper.teardown.side_effect = RuntimeError("fail")
    sys_obj._teardown_bootstrap()  # Should not raise
    assert sys_obj._bootstrap_torn_down is True


@pytest.mark.asyncio
async def test_run_early_exception_does_not_trip_gc_cleanup():
    sys_obj = _make_system()
    sys_obj.session_governor = SimpleNamespace(start=AsyncMock(side_effect=RuntimeError("boom")))
    sys_obj.autonomy_monitor = None
    sys_obj.startup_verifier = None
    sys_obj.checkpoint_writer = None
    sys_obj.stop = MagicMock()

    from hft_platform.services.system import HFTSystem

    with pytest.raises(RuntimeError, match="boom"):
        await HFTSystem.run(sys_obj)

    sys_obj.stop.assert_called_once_with()


def test_iter_supervised_services_no_gateway():
    sys_obj = _make_system()
    sys_obj.gateway_service = None
    services = sys_obj._iter_supervised_services()
    names = [s[0] for s in services]
    assert "risk" in names
    assert "gateway" not in names


def test_iter_supervised_services_with_gateway():
    sys_obj = _make_system()
    sys_obj.gateway_service = MagicMock()
    services = sys_obj._iter_supervised_services()
    names = [s[0] for s in services]
    assert "gateway" in names
    assert "risk" not in names


def test_reset_restart_backoff_if_healthy_task_running():
    sys_obj = _make_system()
    sys_obj._task_restart_attempts["md"] = 3
    sys_obj._task_restart_until_s["md"] = 9999.0
    task = MagicMock()
    task.done.return_value = False
    sys_obj._reset_restart_backoff_if_healthy("md", task)
    assert "md" not in sys_obj._task_restart_attempts


def test_reset_restart_backoff_if_healthy_task_done():
    sys_obj = _make_system()
    sys_obj._task_restart_attempts["md"] = 3
    task = MagicMock()
    task.done.return_value = True
    sys_obj._reset_restart_backoff_if_healthy("md", task)
    assert sys_obj._task_restart_attempts.get("md", 3) == 3


def test_reset_restart_backoff_none_task():
    sys_obj = _make_system()
    sys_obj._reset_restart_backoff_if_healthy("md", None)  # Should not raise
    assert sys_obj._task_restart_attempts == {}


# ---------------------------------------------------------------------------
# _recorder_bridge tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorder_bridge_cancelled():
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj._md_record_direct = False

    call_count = 0

    async def _fake_get():
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return {"topic": "tick", "data": {}}

    sys_obj.raw_queue.get = _fake_get
    sys_obj.recorder_queue.put_nowait = MagicMock()

    async def _cancelling_gen():
        raise asyncio.CancelledError
        yield  # pragma: no cover

    sys_obj.bus.consume.return_value = _cancelling_gen()

    from hft_platform.services.system import HFTSystem

    bridge_coro = HFTSystem._recorder_bridge(sys_obj)
    try:
        await asyncio.wait_for(bridge_coro, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass

    # Bridge consumed the bus — cancellation was handled without propagating
    sys_obj.bus.consume.assert_called_once()


@pytest.mark.asyncio
async def test_recorder_bridge_drop_increments_prometheus_counter():
    """recorder_bridge_drops_total is incremented when recorder_queue is full."""
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj._md_record_direct = False
    sys_obj._fill_record_direct = False
    sys_obj._order_record_direct = False
    sys_obj._recorder_bridge_drops = 0
    sys_obj._recorder_drop_on_full = True

    # Use a queue with maxsize=0 (no capacity) so every put_nowait raises QueueFull
    sys_obj.recorder_queue = asyncio.Queue(maxsize=1)
    # Pre-fill so it's full
    sys_obj.recorder_queue.put_nowait({"topic": "dummy", "data": {}})

    # Produce one event then cancel
    from hft_platform.events import TickEvent

    tick = MagicMock(spec=TickEvent)
    tick.symbol = "TEST"

    events_yielded = 0

    async def _one_event_gen():
        nonlocal events_yielded
        events_yielded += 1
        yield tick
        raise asyncio.CancelledError

    sys_obj.bus.consume.return_value = _one_event_gen()

    mock_metrics = MagicMock()
    mock_counter = MagicMock()
    mock_metrics.recorder_bridge_drops_total.labels.return_value = mock_counter

    with patch(
        "hft_platform.observability.metrics.MetricsRegistry"
    ) as MockMR:
        MockMR.get.return_value = mock_metrics

        # map_event_to_record must return a (topic, payload) tuple
        with patch(
            "hft_platform.recorder.mapper.map_event_to_record",
            return_value=("tick", {"price": 100}),
        ):
            from hft_platform.services.system import HFTSystem

            try:
                await asyncio.wait_for(HFTSystem._recorder_bridge(sys_obj), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass

    # The Prometheus counter should have been incremented for the drop
    mock_metrics.recorder_bridge_drops_total.labels.assert_called_once_with(topic="tick")
    mock_counter.inc.assert_called_once()
    assert sys_obj._recorder_bridge_drops == 1


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop():
    sys_obj = _make_system()
    sys_obj.running = True
    sys_obj.tasks = {}
    sys_obj.stop()
    assert sys_obj.running is False


@pytest.mark.asyncio
async def test_cleanup_tasks():
    sys_obj = _make_system()
    sys_obj.running = False
    sys_obj._teardown_bootstrap = MagicMock()

    async def _long():
        await asyncio.sleep(0.05)

    loop = asyncio.get_event_loop()
    t = loop.create_task(_long())
    sys_obj.tasks = {"md": t}

    from hft_platform.services.system import HFTSystem

    await HFTSystem._cleanup_tasks(sys_obj)
    assert len(sys_obj.tasks) == 0
