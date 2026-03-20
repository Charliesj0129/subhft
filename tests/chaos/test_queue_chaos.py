"""Chaos tests for queue and ring buffer event bus.

Tests overflow semantics, concurrent producers/consumers, back-pressure,
graceful degradation, and edge cases under load.
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_bus_env(monkeypatch):
    """Force Python-mode bus and patch MetricsRegistry for all tests."""
    monkeypatch.setenv("HFT_RUST_ACCEL", "0")
    monkeypatch.setenv("HFT_BUS_RUST", "0")
    monkeypatch.setenv("HFT_BUS_MODE", "python")
    monkeypatch.setenv("HFT_BUS_TYPED_TICK_RING", "0")
    monkeypatch.setenv("HFT_BUS_TYPED_BOOK_RINGS", "0")


@pytest.fixture()
def mock_metrics():
    m = MagicMock()
    with patch("hft_platform.engine.event_bus.MetricsRegistry.get", return_value=m):
        yield m


def _make_bus(size: int = 64, mock_metrics_fixture=None):
    """Create a fresh RingBufferBus with Python fallback (Rust disabled)."""
    from unittest.mock import patch

    with (
        patch("hft_platform.engine.event_bus._RUST_ENABLED", False),
        patch("hft_platform.engine.event_bus._USE_RUST_BUS", False),
    ):
        from hft_platform.engine.event_bus import RingBufferBus

        bus = RingBufferBus(size=size)
    return bus


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.chaos
class TestQueueChaos:
    """Chaos tests for asyncio.Queue and RingBufferBus."""

    # 1. put_nowait drop semantics -------------------------------------------
    @pytest.mark.asyncio
    async def test_put_nowait_drop_semantics(self):
        """put_nowait on full queue raises QueueFull (drop semantics)."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=3)
        for i in range(3):
            q.put_nowait(f"item-{i}")

        with pytest.raises(asyncio.QueueFull):
            q.put_nowait("overflow")

        assert q.qsize() == 3

    # 2. Bounded queue fill to maxsize ---------------------------------------
    @pytest.mark.asyncio
    async def test_bounded_queue_fill_to_maxsize(self):
        """Fill queue exactly to maxsize; no exception until maxsize+1."""
        for maxsize in (1, 5, 100):
            q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
            for i in range(maxsize):
                q.put_nowait(i)
            assert q.qsize() == maxsize
            with pytest.raises(asyncio.QueueFull):
                q.put_nowait(maxsize)

    # 3. Queue drain rate (producer faster than consumer) --------------------
    @pytest.mark.asyncio
    async def test_queue_drain_rate(self):
        """Producer fills faster than consumer drains; queue stays bounded."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=10)
        produced = 0
        dropped = 0

        async def _producer():
            nonlocal produced, dropped
            for i in range(200):
                try:
                    q.put_nowait(i)
                    produced += 1
                except asyncio.QueueFull:
                    dropped += 1
                await asyncio.sleep(0)

        consumed = 0

        async def _consumer():
            nonlocal consumed
            while consumed < 200 - dropped and not q.empty():
                try:
                    q.get_nowait()
                    consumed += 1
                except asyncio.QueueEmpty:
                    break
                # Simulate slow consumer
                await asyncio.sleep(0.001)

        await asyncio.gather(_producer(), _consumer())
        assert q.qsize() <= 10
        assert produced + dropped == 200

    # 4. Multiple producers (5 threads) --------------------------------------
    def test_multiple_producers_threads(self, mock_metrics):
        """5 threads publish to RingBufferBus concurrently."""
        bus = _make_bus(size=256)
        barrier = threading.Barrier(5)
        errors: list[Exception] = []

        def _produce(tid: int):
            try:
                barrier.wait(timeout=5)
                for i in range(100):
                    bus.publish_nowait(("tick", f"SYM-{tid}", i, 1, 1, False, False, 0))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_produce, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        # 5 * 100 = 500 publishes; cursor should reflect that
        assert bus.cursor == 499

    # 5. Task cancellation under full queue ----------------------------------
    @pytest.mark.asyncio
    async def test_task_cancellation_under_full_queue(self):
        """Cancelled put() task does not corrupt queue state."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)

        async def _blocked_put():
            await q.put(3)  # will block

        task = asyncio.create_task(_blocked_put())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # Queue still functional
        val = q.get_nowait()
        assert val == 1
        q.put_nowait(99)
        assert q.qsize() == 2

    # 6. Ring buffer overflow (oldest overwritten) ---------------------------
    def test_ring_buffer_overflow_overwrites_oldest(self, mock_metrics):
        """Publishing beyond ring size overwrites oldest slots."""
        bus = _make_bus(size=4)
        for i in range(10):
            bus.publish_nowait(("event", i))

        assert bus.cursor == 9
        # Slot 0 should now hold event written at cursor=8 (8 % 4 == 0)
        assert bus.buffer is not None
        assert bus.buffer[0] == ("event", 8)
        assert bus.buffer[1] == ("event", 9)

    # 7. Ring buffer read consistency after overflow -------------------------
    @pytest.mark.asyncio
    async def test_ring_buffer_read_consistency_after_overflow(self, mock_metrics):
        """Consumer that lags behind still reads consistent (newer) data."""
        bus = _make_bus(size=8)

        # Publish 20 events (overflows 8-slot buffer)
        for i in range(20):
            bus.publish_nowait(("ev", i))

        # Consumer starting from 0 should skip to recent window
        events_read: list[int] = []
        count = 0
        async for event in bus.consume(start_cursor=0):
            events_read.append(event[1])
            count += 1
            if count >= 8:
                break

        # Should only see events from the recent window (12..19)
        assert all(v >= 12 for v in events_read)

    # 8. Back-pressure signal (QueueFull) ------------------------------------
    @pytest.mark.asyncio
    async def test_back_pressure_signal(self):
        """QueueFull exception signals back-pressure to producer."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
        full_signals = 0
        for i in range(20):
            try:
                q.put_nowait(f"msg-{i}")
            except asyncio.QueueFull:
                full_signals += 1
        assert full_signals == 15
        assert q.qsize() == 5

    # 9. Queue depth tracking under load -------------------------------------
    @pytest.mark.asyncio
    async def test_queue_depth_tracking_under_load(self):
        """qsize() remains accurate during concurrent put/get."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=50)
        max_depth = 0

        async def _producer():
            nonlocal max_depth
            for i in range(100):
                try:
                    q.put_nowait(i)
                    depth = q.qsize()
                    if depth > max_depth:
                        max_depth = depth
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(0)

        async def _consumer():
            for _ in range(100):
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                await asyncio.sleep(0.001)

        await asyncio.gather(_producer(), _consumer())
        assert 0 <= max_depth <= 50

    # 10. Concurrent get/put (5 producers + 5 consumers) --------------------
    @pytest.mark.asyncio
    async def test_concurrent_get_put(self):
        """5 async producers + 5 async consumers on bounded queue."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=20)
        produced: list[int] = []
        consumed: list[int] = []
        p_lock = asyncio.Lock()
        c_lock = asyncio.Lock()

        async def _producer(base: int):
            for i in range(20):
                val = base * 100 + i
                await q.put(val)
                async with p_lock:
                    produced.append(val)

        async def _consumer():
            for _ in range(20):
                val = await asyncio.wait_for(q.get(), timeout=5)
                async with c_lock:
                    consumed.append(val)

        producers = [_producer(i) for i in range(5)]
        consumers = [_consumer() for _ in range(5)]
        await asyncio.gather(*producers, *consumers)

        assert len(produced) == 100
        assert len(consumed) == 100
        assert set(produced) == set(consumed)

    # 11. Queue clear under load ---------------------------------------------
    @pytest.mark.asyncio
    async def test_queue_clear_under_load(self):
        """Draining a queue while producer is active does not raise."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=10)
        stop = asyncio.Event()

        async def _producer():
            i = 0
            while not stop.is_set():
                try:
                    q.put_nowait(i)
                    i += 1
                except asyncio.QueueFull:
                    pass
                await asyncio.sleep(0)

        async def _drain():
            for _ in range(50):
                while not q.empty():
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                await asyncio.sleep(0.001)

        prod_task = asyncio.create_task(_producer())
        await _drain()
        stop.set()
        await asyncio.sleep(0.01)
        prod_task.cancel()
        try:
            await prod_task
        except asyncio.CancelledError:
            pass

        # Drain remaining items so we can verify queue is still functional
        while not q.empty():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        q.put_nowait(999)
        assert q.get_nowait() == 999

    # 12. Queue maxsize=1 edge case ------------------------------------------
    @pytest.mark.asyncio
    async def test_queue_maxsize_one(self):
        """Queue with maxsize=1 works correctly under rapid put/get."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        values: list[int] = []

        for i in range(100):
            q.put_nowait(i)
            val = q.get_nowait()
            values.append(val)

        assert values == list(range(100))

    # 13. Get timeout on empty queue -----------------------------------------
    @pytest.mark.asyncio
    async def test_get_timeout_on_empty_queue(self):
        """get() with timeout on empty queue raises TimeoutError."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=5)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)

    # 14. Graceful degradation (drop-on-full, no crash) ----------------------
    @pytest.mark.asyncio
    async def test_graceful_degradation_drop_on_full(self):
        """Drop-on-full pattern: put_nowait catches QueueFull, never crashes."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=5)
        delivered = 0
        dropped = 0

        for i in range(1000):
            try:
                q.put_nowait(i)
                delivered += 1
            except asyncio.QueueFull:
                dropped += 1

        assert delivered == 5
        assert dropped == 995
        assert q.qsize() == 5
        # Queue still works
        val = q.get_nowait()
        assert val == 0
