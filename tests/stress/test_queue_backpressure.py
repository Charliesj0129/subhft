"""Queue Backpressure Stress Tests.

Tests bounded queue behavior under high load conditions.
Validates that backpressure mechanisms prevent OOM while maintaining throughput.
"""

import asyncio
import time

import pytest


@pytest.mark.stress
class TestQueueBackpressure:
    """Stress tests for bounded queue backpressure."""

    @pytest.mark.asyncio
    async def test_bounded_queue_throughput(self):
        """Measure throughput with bounded queue under contention."""
        queue_size = 1000
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)
        produced = 0
        consumed = 0
        dropped = 0

        async def producer(count: int):
            nonlocal produced, dropped
            for i in range(count):
                try:
                    queue.put_nowait(i)
                    produced += 1
                except asyncio.QueueFull:
                    dropped += 1
                # Simulate production rate
                if i % 100 == 0:
                    await asyncio.sleep(0)

        async def consumer():
            nonlocal consumed
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=0.1)
                    consumed += 1
                    queue.task_done()
                except asyncio.TimeoutError:
                    break

        start = time.monotonic()

        # Start consumer
        consumer_task = asyncio.create_task(consumer())

        # Run producer
        await producer(10000)

        # Wait for queue to drain
        await asyncio.sleep(0.5)
        consumer_task.cancel()

        elapsed = time.monotonic() - start

        print(f"Produced: {produced}, Consumed: {consumed}, Dropped: {dropped}")
        print(f"Throughput: {consumed / elapsed:.0f} items/sec")

        assert produced + dropped == 10000, "All items should be accounted for"

    @pytest.mark.asyncio
    async def test_multiple_producers_single_consumer(self):
        """Test bounded queue with multiple producers."""
        queue_size = 500
        queue: asyncio.Queue[tuple] = asyncio.Queue(maxsize=queue_size)
        stats = {"produced": 0, "dropped": 0, "consumed": 0}
        lock = asyncio.Lock()

        async def producer(producer_id: int, count: int):
            for i in range(count):
                try:
                    queue.put_nowait((producer_id, i))
                    async with lock:
                        stats["produced"] += 1
                except asyncio.QueueFull:
                    async with lock:
                        stats["dropped"] += 1
                await asyncio.sleep(0)

        async def consumer():
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=0.2)
                    async with lock:
                        stats["consumed"] += 1
                    queue.task_done()
                except asyncio.TimeoutError:
                    break

        # Start consumer
        consumer_task = asyncio.create_task(consumer())

        # Start multiple producers
        producer_tasks = [
            asyncio.create_task(producer(i, 1000))
            for i in range(5)
        ]

        await asyncio.gather(*producer_tasks)
        await asyncio.sleep(0.5)
        consumer_task.cancel()

        total_attempts = stats["produced"] + stats["dropped"]
        assert total_attempts == 5000, "All producer attempts should be accounted"
        print(f"Multi-producer stats: {stats}")

    @pytest.mark.asyncio
    async def test_batcher_under_load(self):
        """Test Batcher component under sustained load."""
        from unittest.mock import AsyncMock

        from hft_platform.recorder.batcher import BackpressurePolicy, Batcher

        # Fast mock writer
        write_count = 0
        write_rows = 0

        async def mock_write(table, data):
            nonlocal write_count, write_rows
            write_count += 1
            write_rows += len(data)

        mock_writer = AsyncMock(side_effect=mock_write)
        mock_writer.write = mock_write

        batcher = Batcher(
            table_name="stress_test",
            flush_limit=100,
            flush_interval_ms=50,
            max_buffer_size=500,
            backpressure_policy=BackpressurePolicy.DROP_OLDEST,
            writer=mock_writer,
        )

        start = time.monotonic()

        # Simulate high ingestion rate
        for i in range(10000):
            await batcher.add({"id": i, "value": f"data_{i}"})

        # Force final flush
        await batcher.force_flush()

        elapsed = time.monotonic() - start

        print(f"Batcher stress: {batcher.total_count} total, {batcher.dropped_count} dropped")
        print(f"Writes: {write_count} batches, {write_rows} rows")
        print(f"Time: {elapsed:.2f}s, Rate: {batcher.total_count / elapsed:.0f}/s")

        # Verify data integrity
        assert write_rows + batcher.dropped_count + len(batcher.buffer) == batcher.total_count


@pytest.mark.stress
class TestRecorderQueueBackpressure:
    """Stress tests for recorder queue configuration."""

    @pytest.mark.asyncio
    async def test_default_queue_sizes(self):
        """Verify default bounded queue sizes are reasonable."""
        from hft_platform.services.bootstrap import SystemBootstrapper

        # Check class constants
        assert SystemBootstrapper.DEFAULT_RAW_QUEUE_SIZE == 65536
        assert SystemBootstrapper.DEFAULT_RECORDER_QUEUE_SIZE == 16384
        assert SystemBootstrapper.DEFAULT_ORDER_QUEUE_SIZE == 2048

    @pytest.mark.asyncio
    async def test_queue_overflow_recovery(self):
        """Test system behavior when queue overflows and recovers."""
        queue_size = 100
        queue: asyncio.Queue[int] = asyncio.Queue(maxsize=queue_size)

        # Fill queue
        for i in range(queue_size):
            await queue.put(i)

        assert queue.full()

        # Overflow attempts
        overflow_count = 0
        for i in range(50):
            try:
                queue.put_nowait(i + queue_size)
            except asyncio.QueueFull:
                overflow_count += 1

        assert overflow_count == 50, "All overflow attempts should fail"

        # Drain half
        for _ in range(50):
            queue.get_nowait()

        assert not queue.full()

        # Should be able to add again
        for i in range(50):
            queue.put_nowait(i)

        assert queue.full()
