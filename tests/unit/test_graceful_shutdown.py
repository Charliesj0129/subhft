"""Tests for graceful shutdown broker logout (WU-01)."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest


def _make_system_with_mocks():
    """Create a minimal HFTSystem with mocked dependencies."""
    with (
        patch("hft_platform.services.system.SystemBootstrapper") as mock_boot,
        patch("hft_platform.services.system.configure_logging"),
    ):
        registry = MagicMock()
        registry.bus = MagicMock()
        registry.raw_queue = asyncio.Queue()
        registry.raw_exec_queue = asyncio.Queue()
        registry.risk_queue = asyncio.Queue()
        registry.order_queue = asyncio.Queue()
        registry.recorder_queue = asyncio.Queue()
        registry.position_store = MagicMock()
        registry.order_id_map = {}
        registry.storm_guard = MagicMock()
        registry.md_client = MagicMock()
        registry.order_client = MagicMock()
        registry.client = MagicMock()
        registry.symbol_metadata = {}
        registry.price_scale_provider = MagicMock()
        registry.md_service = MagicMock()
        registry.order_adapter = MagicMock()
        registry.execution_gateway = MagicMock()
        registry.exec_service = MagicMock()
        registry.risk_engine = MagicMock()
        registry.recon_service = MagicMock()
        registry.strategy_runner = MagicMock()
        registry.recorder = MagicMock()
        registry.gateway_service = None
        mock_boot.return_value.build.return_value = registry

        from hft_platform.services.system import HFTSystem

        system = HFTSystem({})
        return system


class TestGracefulShutdownBrokerLogout:
    """WU-01: Verify broker logout during shutdown."""

    def test_stop_calls_broker_close_with_logout(self) -> None:
        system = _make_system_with_mocks()
        md_client = system.md_client
        order_client = system.order_client

        system.stop()

        md_client.close.assert_called_with(logout=True)
        order_client.close.assert_called_with(logout=True)

    @pytest.mark.asyncio
    async def test_stop_async_calls_broker_close_with_logout(self) -> None:
        system = _make_system_with_mocks()
        md_client = system.md_client
        order_client = system.order_client

        await system.stop_async()

        md_client.close.assert_called_with(logout=True)
        order_client.close.assert_called_with(logout=True)

    def test_stop_handles_broker_close_exception(self) -> None:
        system = _make_system_with_mocks()
        system.md_client.close.side_effect = RuntimeError("connection lost")

        # Should not raise
        system.stop()

        # order_client should still be called
        system.order_client.close.assert_called_with(logout=True)

    def test_stop_handles_missing_close_method(self) -> None:
        system = _make_system_with_mocks()
        del system.md_client.close  # Remove close method

        # Should not raise
        system.stop()

        # order_client should still be called
        system.order_client.close.assert_called_with(logout=True)

    def test_teardown_bootstrap_calls_broker_close_fallback(self) -> None:
        system = _make_system_with_mocks()
        md_client = system.md_client
        order_client = system.order_client

        # Reset the torn-down flag (stop() sets it via _teardown_bootstrap)
        system._bootstrap_torn_down = False
        md_client.close.reset_mock()
        order_client.close.reset_mock()

        system._teardown_bootstrap()

        md_client.close.assert_called_once_with(logout=True)
        order_client.close.assert_called_once_with(logout=True)

    def test_teardown_bootstrap_idempotent(self) -> None:
        system = _make_system_with_mocks()
        md_client = system.md_client

        # First call tears down and sets flag
        system._teardown_bootstrap()
        call_count_after_first = md_client.close.call_count
        assert call_count_after_first > 0

        # Second call should be a no-op due to _bootstrap_torn_down flag
        system._teardown_bootstrap()
        assert md_client.close.call_count == call_count_after_first
