"""Tests for WAL fallback escalation in ExecutionRouter (I-09)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.execution.router import ExecutionRouter


def _make_router(
    wal_writer: object | None = None,
    recorder_queue: asyncio.Queue | None = None,
) -> ExecutionRouter:
    """Build a minimal ExecutionRouter for WAL fallback tests."""
    bus = MagicMock()
    raw_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    order_id_map: dict[str, str] = {}
    position_store = MagicMock()
    terminal_handler = MagicMock()

    return ExecutionRouter(
        bus=bus,
        raw_queue=raw_queue,
        order_id_map=order_id_map,
        position_store=position_store,
        terminal_handler=terminal_handler,
        recorder_queue=recorder_queue,
        wal_writer=wal_writer,
    )


class TestWalFallbackEscalation:
    """When _wal_writer is None the fallback must log CRITICAL and inc metric."""

    def test_critical_log_when_wal_writer_none(self) -> None:
        router = _make_router(wal_writer=None)
        payload = SimpleNamespace(symbol="2330")

        with patch("hft_platform.execution.router.logger") as mock_logger:
            router._wal_fallback_write("fills", payload)

        mock_logger.critical.assert_called_once()
        call_args = mock_logger.critical.call_args
        assert call_args[0][0] == "fill_data_loss"
        assert call_args[1]["event_type"] == "fills"
        assert call_args[1]["symbol"] == "2330"
        assert call_args[1]["reason"] == "wal_writer_none_and_recorder_full"

    def test_metric_incremented_when_wal_writer_none(self) -> None:
        router = _make_router(wal_writer=None)
        payload = SimpleNamespace(symbol="TXFD6")

        initial = router.metrics.exec_fill_data_loss_total._value.get()
        router._wal_fallback_write("fills", payload)
        after = router.metrics.exec_fill_data_loss_total._value.get()

        assert after == initial + 1

    def test_normal_wal_write_when_writer_exists(self) -> None:
        """When WAL writer is available, normal write proceeds — no CRITICAL log."""
        mock_wal = MagicMock()
        # Make write() return a coroutine so ensure_future works
        mock_wal.write = AsyncMock()

        router = _make_router(wal_writer=mock_wal)
        payload = SimpleNamespace(symbol="2330")

        initial = router.metrics.exec_fill_data_loss_total._value.get()

        with patch("hft_platform.execution.router.logger") as mock_logger:
            # Need a running event loop for ensure_future
            loop = asyncio.new_event_loop()
            try:
                loop.run_until_complete(
                    asyncio.ensure_future(_run_wal_fallback(router, "fills", payload), loop=loop)
                )
            finally:
                loop.close()

        mock_logger.critical.assert_not_called()
        after = router.metrics.exec_fill_data_loss_total._value.get()
        assert after == initial

    def test_critical_log_with_none_payload(self) -> None:
        """Handles None payload gracefully (symbol=None)."""
        router = _make_router(wal_writer=None)

        with patch("hft_platform.execution.router.logger") as mock_logger:
            router._wal_fallback_write("fills", None)

        mock_logger.critical.assert_called_once()
        assert mock_logger.critical.call_args[1]["symbol"] is None


async def _run_wal_fallback(
    router: ExecutionRouter, topic: str, payload: object
) -> None:
    router._wal_fallback_write(topic, payload)
