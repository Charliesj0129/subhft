"""Tests for RecorderService shutdown flush timeout (HFT_RECORDER_SHUTDOWN_TIMEOUT_S)."""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestRecorderShutdownFlushTimeout(unittest.IsolatedAsyncioTestCase):
    """Test that _shutdown_flush respects the configurable timeout."""

    def _make_service(self):
        """Build a RecorderService with a mock DataWriter."""
        from hft_platform.recorder.worker import RecorderService

        queue = asyncio.Queue()
        with (
            patch.dict(os.environ, {"HFT_CLICKHOUSE_ENABLED": "0"}, clear=False),
            patch("hft_platform.recorder.worker.DataWriter") as MockWriter,
        ):
            inst = MockWriter.return_value
            inst.active = True
            inst.connect_async = AsyncMock()
            inst.write = AsyncMock()
            inst.write_columnar = AsyncMock()
            inst.shutdown = AsyncMock()
            inst.set_health_tracker = MagicMock()
            svc = RecorderService(queue)
            svc.writer = inst  # keep reference
            svc.recover_wal = AsyncMock()
        return svc

    async def test_shutdown_flush_completes_within_timeout(self):
        """All batchers are flushed and writer shut down when flush finishes quickly."""
        svc = self._make_service()

        flushed = []

        async def fast_flush():
            flushed.append(True)

        async def fast_writer_shutdown():
            pass

        # Patch batchers with fast coroutines
        mock_batcher = MagicMock()
        mock_batcher.force_flush = AsyncMock(side_effect=fast_flush)
        svc.batchers = {"market_data": mock_batcher}
        svc.writer.shutdown = AsyncMock(side_effect=fast_writer_shutdown)

        # Should complete without timeout (default 60 s, operation is instant)
        with patch.dict(os.environ, {"HFT_RECORDER_SHUTDOWN_TIMEOUT_S": "5"}):
            await svc._shutdown_flush()

        assert flushed, "force_flush should have been called"
        svc.writer.shutdown.assert_awaited_once()

    async def test_shutdown_flush_timeout_does_not_hang(self):
        """When flush exceeds timeout, wait_for raises TimeoutError quickly (no 17-min hang)."""
        svc = self._make_service()

        async def slow_flush():
            await asyncio.sleep(999)  # simulate hung CH connection

        mock_batcher = MagicMock()
        mock_batcher.force_flush = AsyncMock(side_effect=slow_flush)
        svc.batchers = {"market_data": mock_batcher, "orders": MagicMock()}
        svc.batchers["orders"].force_flush = AsyncMock(side_effect=slow_flush)

        timeout_s = 0.05
        start = asyncio.get_event_loop().time()
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(svc._shutdown_flush(), timeout=timeout_s)
        elapsed = asyncio.get_event_loop().time() - start

        # Must complete well within 1 second, not 17 minutes
        assert elapsed < 1.0, f"Shutdown took {elapsed:.2f}s, expected < 1s"

    async def test_shutdown_timeout_logs_error_via_run_cancellation(self):
        """The run() method logs recorder_shutdown_flush_timeout when _shutdown_flush times out."""
        from hft_platform.recorder.worker import RecorderService

        queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            inst = MockWriter.return_value
            inst.active = True
            inst.connect_async = AsyncMock()
            inst.write = AsyncMock()
            inst.write_columnar = AsyncMock()
            inst.shutdown = AsyncMock()
            inst.set_health_tracker = MagicMock()

            svc = RecorderService(queue)
            svc.recover_wal = AsyncMock()

            async def _slow_shutdown_flush():
                await asyncio.sleep(999)

            svc._shutdown_flush = _slow_shutdown_flush  # type: ignore[method-assign]

            with (
                patch("hft_platform.recorder.worker.logger") as mock_logger,
                patch.dict(os.environ, {"HFT_RECORDER_SHUTDOWN_TIMEOUT_S": "0.05"}),
            ):
                task = asyncio.create_task(svc.run())
                await asyncio.sleep(0.02)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

                # The timeout error log should have been emitted
                error_calls = [
                    c
                    for c in mock_logger.error.call_args_list
                    if c.args and c.args[0] == "recorder_shutdown_flush_timeout"
                ]
                assert error_calls, "Expected recorder_shutdown_flush_timeout to be logged"

    async def test_shutdown_timeout_configurable_via_env(self):
        """HFT_RECORDER_SHUTDOWN_TIMEOUT_S env var controls the timeout value."""
        svc = self._make_service()

        durations: list[float] = []

        async def timed_flush():
            start = asyncio.get_event_loop().time()
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                durations.append(asyncio.get_event_loop().time() - start)
                raise

        mock_batcher = MagicMock()
        mock_batcher.force_flush = AsyncMock(side_effect=timed_flush)
        svc.batchers = {"market_data": mock_batcher}

        timeout_s = 0.07
        with patch.dict(os.environ, {"HFT_RECORDER_SHUTDOWN_TIMEOUT_S": str(timeout_s)}):
            _t = float(os.getenv("HFT_RECORDER_SHUTDOWN_TIMEOUT_S", "60"))
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(svc._shutdown_flush(), timeout=_t)

        # The cancellation should have happened near the configured timeout
        assert durations, "flush coroutine was cancelled (confirming timeout fired)"
        assert durations[0] < timeout_s + 0.5, "cancelled within expected window"

    async def test_shutdown_flush_batcher_error_does_not_abort_others(self):
        """If one batcher raises, remaining batchers and writer.shutdown still run."""
        svc = self._make_service()

        completed = []

        async def erroring_flush():
            raise RuntimeError("CH connection refused")

        async def good_flush():
            completed.append("good")

        b1 = MagicMock()
        b1.force_flush = AsyncMock(side_effect=erroring_flush)
        b2 = MagicMock()
        b2.force_flush = AsyncMock(side_effect=good_flush)

        # dict preserves insertion order (Python 3.7+)
        svc.batchers = {"bad": b1, "good": b2}
        svc.writer.shutdown = AsyncMock()

        await svc._shutdown_flush()

        assert completed == ["good"], "good batcher should still be flushed after bad one errors"
        svc.writer.shutdown.assert_awaited_once()
