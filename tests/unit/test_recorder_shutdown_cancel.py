"""Unit tests for _shutdown_flush CancelledError handling in RecorderWorker.

Covers:
- Normal shutdown: all batchers flushed, no skipped log
- CancelledError mid-flush: logs which batchers were skipped and which were flushed
- CancelledError is re-raised after logging
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hft_platform.recorder.worker import RecorderService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_worker() -> RecorderService:
    """Create a minimal RecorderService with mocked dependencies."""
    worker = RecorderService.__new__(RecorderService)
    worker.batchers = {}
    worker.writer = AsyncMock()
    worker.writer.shutdown = AsyncMock()
    return worker


def _make_batcher(name: str, raises: Exception | None = None) -> MagicMock:
    batcher = MagicMock()
    if raises is not None:
        batcher.force_flush = AsyncMock(side_effect=raises)
    else:
        batcher.force_flush = AsyncMock()
    batcher.__repr__ = lambda self: name  # noqa: ARG005
    return batcher


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShutdownFlushNormal:
    """Normal path: all batchers flush successfully."""

    @pytest.mark.asyncio
    async def test_all_batchers_flushed(self) -> None:
        worker = _make_worker()
        worker.batchers = {
            "ticks": _make_batcher("ticks"),
            "orders": _make_batcher("orders"),
        }

        with patch("hft_platform.recorder.worker.logger") as mock_log:
            await worker._shutdown_flush()

        worker.batchers["ticks"].force_flush.assert_awaited_once()
        worker.batchers["orders"].force_flush.assert_awaited_once()
        worker.writer.shutdown.assert_awaited_once()

        # No error/warning about skipped batchers
        for log_call in mock_log.error.call_args_list:
            assert "recorder_shutdown_batchers_skipped" not in log_call.args

    @pytest.mark.asyncio
    async def test_batcher_flush_exception_does_not_abort(self) -> None:
        worker = _make_worker()
        worker.batchers = {
            "ticks": _make_batcher("ticks", raises=RuntimeError("flush failed")),
            "orders": _make_batcher("orders"),
        }

        with patch("hft_platform.recorder.worker.logger") as mock_log:
            await worker._shutdown_flush()

        # Both batchers attempted; second one still flushed
        worker.batchers["ticks"].force_flush.assert_awaited_once()
        worker.batchers["orders"].force_flush.assert_awaited_once()
        mock_log.warning.assert_called_once()
        assert mock_log.warning.call_args.args[0] == "recorder_batcher_flush_error"


class TestShutdownFlushCancelledError:
    """CancelledError path: logs skipped batchers and re-raises."""

    @pytest.mark.asyncio
    async def test_cancelled_mid_flush_logs_skipped(self) -> None:
        worker = _make_worker()
        worker.batchers = {
            "ticks": _make_batcher("ticks"),
            "orders": _make_batcher("orders", raises=asyncio.CancelledError()),
            "fills": _make_batcher("fills"),
        }

        with patch("hft_platform.recorder.worker.logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await worker._shutdown_flush()

        # "ticks" was flushed before cancellation, "orders" and "fills" were not
        mock_log.error.assert_called_once()
        call_kwargs = mock_log.error.call_args
        assert call_kwargs.args[0] == "recorder_shutdown_batchers_skipped"
        assert "ticks" in call_kwargs.kwargs["flushed"]
        assert "orders" in call_kwargs.kwargs["skipped"]
        assert "fills" in call_kwargs.kwargs["skipped"]

    @pytest.mark.asyncio
    async def test_cancelled_on_first_batcher_logs_all_as_skipped(self) -> None:
        worker = _make_worker()
        worker.batchers = {
            "ticks": _make_batcher("ticks", raises=asyncio.CancelledError()),
            "orders": _make_batcher("orders"),
        }

        with patch("hft_platform.recorder.worker.logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await worker._shutdown_flush()

        call_kwargs = mock_log.error.call_args
        assert call_kwargs.args[0] == "recorder_shutdown_batchers_skipped"
        # Nothing flushed, both in skipped
        assert call_kwargs.kwargs["flushed"] == []
        assert "ticks" in call_kwargs.kwargs["skipped"]
        assert "orders" in call_kwargs.kwargs["skipped"]

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self) -> None:
        worker = _make_worker()
        worker.batchers = {
            "ticks": _make_batcher("ticks", raises=asyncio.CancelledError()),
        }

        with patch("hft_platform.recorder.worker.logger"):
            with pytest.raises(asyncio.CancelledError):
                await worker._shutdown_flush()

    @pytest.mark.asyncio
    async def test_no_error_log_when_all_flushed_before_cancel(self) -> None:
        """If CancelledError is raised but no batchers were skipped, no error is logged."""
        worker = _make_worker()
        # Simulate CancelledError raised after all batchers are done
        # by having no batchers but patching the outer try to raise
        worker.batchers = {}

        # Patch writer.shutdown to raise CancelledError to simulate
        # cancellation after batcher loop
        worker.writer.shutdown = AsyncMock(side_effect=asyncio.CancelledError())

        with patch("hft_platform.recorder.worker.logger") as mock_log:
            with pytest.raises(asyncio.CancelledError):
                await worker._shutdown_flush()

        # No skipped batchers error should be logged (skipped list is empty)
        for log_call in mock_log.error.call_args_list:
            assert "recorder_shutdown_batchers_skipped" not in (log_call.args or [])
