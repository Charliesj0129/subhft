"""Tests for recorder_bridge short-circuit when all direct recording is enabled."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _make_system(
    md_record_direct: bool = True,
    fill_record_direct: bool = True,
    order_record_direct: bool = True,
):
    """Return a minimal HFTSystem instance with mocked dependencies."""
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
            sys_obj._md_record_direct = md_record_direct
            sys_obj._fill_record_direct = fill_record_direct
            sys_obj._order_record_direct = order_record_direct
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
            sys_obj._recorder_bridge_drops = 0

            return sys_obj


class TestRecorderBridgeShortCircuit:
    """Verify that _recorder_bridge is skipped when all direct flags are True."""

    def test_iter_supervised_services_excludes_bridge_when_all_direct(self):
        """_iter_supervised_services should NOT include recorder_bridge when all direct flags are True."""
        sys_obj = _make_system(md_record_direct=True, fill_record_direct=True, order_record_direct=True)
        services = sys_obj._iter_supervised_services()
        names = [s[0] for s in services]
        assert "recorder_bridge" not in names

    @pytest.mark.parametrize(
        "md, fill, order",
        [
            (False, True, True),
            (True, False, True),
            (True, True, False),
            (False, False, True),
            (False, False, False),
        ],
    )
    def test_iter_supervised_services_includes_bridge_when_any_direct_disabled(self, md: bool, fill: bool, order: bool):
        """_iter_supervised_services should include recorder_bridge when any direct flag is False."""
        sys_obj = _make_system(md_record_direct=md, fill_record_direct=fill, order_record_direct=order)
        services = sys_obj._iter_supervised_services()
        names = [s[0] for s in services]
        assert "recorder_bridge" in names

    @pytest.mark.asyncio
    async def test_recorder_bridge_early_return_when_all_direct(self):
        """_recorder_bridge coroutine should return immediately when all direct flags are True."""
        sys_obj = _make_system(md_record_direct=True, fill_record_direct=True, order_record_direct=True)
        # The bus.consume / consume_batch should never be called since we return early.
        sys_obj.bus.consume = MagicMock()
        sys_obj.bus.consume_batch = MagicMock()

        await sys_obj._recorder_bridge()

        sys_obj.bus.consume.assert_not_called()
        sys_obj.bus.consume_batch.assert_not_called()

    @pytest.mark.asyncio
    async def test_recorder_bridge_runs_when_md_direct_disabled(self):
        """_recorder_bridge coroutine should proceed past early return when md_record_direct=False."""
        sys_obj = _make_system(md_record_direct=False, fill_record_direct=True, order_record_direct=True)

        # Return an empty async iterator so the coroutine terminates cleanly.
        async def _empty_gen():
            return
            yield  # pragma: no cover — makes this an async generator

        sys_obj.bus.consume = MagicMock(return_value=_empty_gen())

        with patch("hft_platform.services.system.PriceCodec"):
            await sys_obj._recorder_bridge()

        sys_obj.bus.consume.assert_called_once()
