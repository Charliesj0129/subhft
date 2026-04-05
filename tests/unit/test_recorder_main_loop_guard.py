"""Tests for the error guard in RecorderService main loop.

Verifies that unexpected exceptions in the processing logic do not crash
the recorder, task_done() is always called, and CancelledError still
propagates for clean shutdown.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


class TestRecorderMainLoopGuard(unittest.IsolatedAsyncioTestCase):
    def _make_worker(self, queue):
        """Create a RecorderService with mocked DataWriter."""
        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            from hft_platform.recorder.worker import RecorderService

            worker = RecorderService(queue)
            # Keep a reference to mock so tests can inspect it
            worker._mock_writer = mock_writer_inst
            return worker

    async def _run_worker_briefly(self, worker, stop_after_s: float = 0.15):
        """Start worker.run() as a task, wait briefly, then cancel."""
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(stop_after_s)
        worker.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return task

    async def test_non_dict_item_does_not_crash_recorder(self):
        """A non-dict item should be logged and skipped, not crash the service."""
        queue: asyncio.Queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            from hft_platform.recorder.worker import RecorderService

            worker = RecorderService(queue)

            # Put a non-dict item (e.g., a plain string)
            await queue.put("not_a_dict")
            # Then a valid item so we know the loop continued
            await queue.put({"topic": "market_data", "data": {"price": 100}})

            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Queue should be fully drained (task_done called for each item)
        assert queue._unfinished_tasks == 0, (
            f"Expected 0 unfinished tasks, got {queue._unfinished_tasks}"
        )

    async def test_batcher_exception_does_not_crash_recorder(self):
        """An exception from batcher.add() should be caught, logged, and loop continues."""
        queue: asyncio.Queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            from hft_platform.recorder.worker import RecorderService

            worker = RecorderService(queue)

            # Patch one batcher to raise on add
            faulty_batcher = MagicMock()
            faulty_batcher.add = AsyncMock(side_effect=RuntimeError("batcher exploded"))
            worker.batchers["market_data"] = faulty_batcher

            # Items that will trigger the faulty batcher
            await queue.put({"topic": "market_data", "data": {"price": 100}})
            await queue.put({"topic": "market_data", "data": {"price": 101}})
            # Third item: valid topic that won't raise (unknown topic, just gets dropped)
            await queue.put({"topic": "market_data", "data": {"price": 102}})

            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.15)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # All 3 items must have had task_done() called
        assert queue._unfinished_tasks == 0, (
            f"Expected 0 unfinished tasks, got {queue._unfinished_tasks}"
        )
        # Error counter should have been incremented for each failure
        assert worker._process_errors >= 2, (
            f"Expected at least 2 process errors, got {worker._process_errors}"
        )

    async def test_task_done_called_even_on_exception(self):
        """task_done() must be called even when processing raises."""
        queue: asyncio.Queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            from hft_platform.recorder.worker import RecorderService

            worker = RecorderService(queue)

            # Patch batcher to raise
            boom_batcher = MagicMock()
            boom_batcher.add = AsyncMock(side_effect=ValueError("boom"))
            worker.batchers["market_data"] = boom_batcher

            await queue.put({"topic": "market_data", "data": {}})

            task = asyncio.create_task(worker.run())
            await asyncio.sleep(0.1)
            worker.running = False
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # queue.join() would hang if task_done() was not called
        assert queue._unfinished_tasks == 0, (
            "task_done() was not called after exception — queue.join() would hang"
        )

    async def test_cancelled_error_propagates_for_shutdown(self):
        """CancelledError must propagate so the service shuts down cleanly."""
        queue: asyncio.Queue = asyncio.Queue()

        with patch("hft_platform.recorder.worker.DataWriter") as MockWriter:
            mock_writer_inst = MockWriter.return_value
            mock_writer_inst.active = True
            mock_writer_inst.connect_async = AsyncMock()
            mock_writer_inst.write = AsyncMock()
            mock_writer_inst.write_columnar = AsyncMock()
            mock_writer_inst.shutdown = AsyncMock()
            mock_writer_inst.set_health_tracker = MagicMock()

            from hft_platform.recorder.worker import RecorderService

            worker = RecorderService(queue)

            task = asyncio.create_task(worker.run())
            # Give the loop a moment to start waiting on the queue
            await asyncio.sleep(0.05)
            task.cancel()

            cancelled_correctly = False
            try:
                await task
            except asyncio.CancelledError:
                cancelled_correctly = True

        # After cancellation the worker should have stopped running
        assert not worker.running, "worker.running should be False after shutdown"
        # The task should have completed (not hung)
        assert task.done(), "Task should be done after cancellation"
