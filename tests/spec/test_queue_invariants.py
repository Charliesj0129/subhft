"""Queue overflow invariant spec tests.

Verifies bounded queue contracts required by HFT platform architecture governance
(rules 20-data-flow.md, 25-architecture-governance.md):
- "New hot-path stage must use bounded queues"
- "QueueFull behavior must be explicit: drop, degrade, or block"
- "Recording MUST NEVER block the hot path. Use put_nowait() with drop policy."

All tests use asyncio.Queue directly -- no full system bootstrap required.
"""

from __future__ import annotations

import asyncio

import pytest

# ---------------------------------------------------------------------------
# 1. Bounded queue rejects overflow
# ---------------------------------------------------------------------------


class TestBoundedQueueRejectsOverflow:
    """asyncio.Queue(maxsize=N) raises QueueFull on N+1th put_nowait."""

    def test_put_nowait_raises_at_capacity(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=3)
        for i in range(3):
            q.put_nowait(i)
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(99)

    def test_exactly_at_boundary(self) -> None:
        """Nth item succeeds, N+1th fails."""
        maxsize = 7
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        for i in range(maxsize):
            q.put_nowait(i)  # should not raise
        assert q.qsize() == maxsize
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(maxsize)


# ---------------------------------------------------------------------------
# 2. Recorder queue drop policy
# ---------------------------------------------------------------------------


class TestRecorderQueueDropPolicy:
    """When recorder queue is full, the platform drops events (never blocks).

    This mirrors the pattern in MarketDataService._record_direct_event():
    try: queue.put_nowait(item) except QueueFull: dropped += 1
    """

    def test_drop_on_full_does_not_block(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)
        dropped = False
        try:
            q.put_nowait(3)
        except asyncio.QueueFull:
            dropped = True
        assert dropped, "Expected QueueFull when queue is at capacity"
        assert q.qsize() == 2

    def test_drop_preserves_existing_items(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        q.put_nowait(10)
        q.put_nowait(20)
        try:
            q.put_nowait(30)
        except asyncio.QueueFull:
            pass
        assert q.get_nowait() == 10
        assert q.get_nowait() == 20

    def test_drop_counter_pattern(self) -> None:
        """Simulates the platform's drop-counting pattern."""
        maxsize = 4
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        dropped_count = 0
        for i in range(20):
            try:
                q.put_nowait(i)
            except asyncio.QueueFull:
                dropped_count += 1
        assert dropped_count == 16  # 20 - maxsize
        assert q.qsize() == maxsize


# ---------------------------------------------------------------------------
# 3. Queue depth never exceeds maxsize
# ---------------------------------------------------------------------------


class TestQueueDepthNeverExceedsMaxsize:
    """After N puts and M gets (M < N), depth = min(N-M, maxsize)."""

    def test_depth_after_partial_drain(self) -> None:
        maxsize = 5
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        n_puts = 10
        m_gets = 3
        for i in range(n_puts):
            try:
                q.put_nowait(i)
            except asyncio.QueueFull:
                pass
        for _ in range(m_gets):
            q.get_nowait()
        expected_depth = min(n_puts - m_gets, maxsize)
        # Since only maxsize items were accepted, depth = maxsize - m_gets
        assert q.qsize() == maxsize - m_gets
        assert q.qsize() <= maxsize

    def test_depth_never_exceeds_declared_limit(self) -> None:
        maxsize = 10
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        for i in range(100):
            try:
                q.put_nowait(i)
            except asyncio.QueueFull:
                pass
        assert q.qsize() <= maxsize

    @pytest.mark.asyncio
    async def test_concurrent_producers_respect_maxsize(self) -> None:
        maxsize = 5
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)

        async def producer(start: int) -> int:
            dropped = 0
            for i in range(20):
                try:
                    q.put_nowait(start + i)
                except asyncio.QueueFull:
                    dropped += 1
                await asyncio.sleep(0)
            return dropped

        tasks = [asyncio.create_task(producer(i * 100)) for i in range(5)]
        results = await asyncio.gather(*tasks)
        assert q.qsize() <= maxsize
        assert sum(results) > 0, "Some items should have been dropped"


# ---------------------------------------------------------------------------
# 4. put_nowait is non-blocking
# ---------------------------------------------------------------------------


class TestPutNowaitIsNonblocking:
    """put_nowait returns immediately without awaiting."""

    @pytest.mark.asyncio
    async def test_put_nowait_returns_immediately(self) -> None:
        """put_nowait on a non-full queue completes within one event loop tick."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=10)
        completed = False

        async def timed_put() -> None:
            nonlocal completed
            q.put_nowait(42)
            completed = True

        # Run in the same event loop iteration
        await timed_put()
        assert completed, "put_nowait should complete synchronously"
        assert q.qsize() == 1

    @pytest.mark.asyncio
    async def test_put_nowait_on_full_raises_immediately(self) -> None:
        """put_nowait on a full queue raises QueueFull without blocking."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=1)
        q.put_nowait(1)
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(2)
        # Control reaches here immediately -- no await needed

    def test_put_nowait_is_not_coroutine(self) -> None:
        """put_nowait is a regular function, not a coroutine."""
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=5)
        result = q.put_nowait(1)
        assert result is None, "put_nowait should return None (not a coroutine)"
        assert not asyncio.iscoroutinefunction(q.put_nowait)


# ---------------------------------------------------------------------------
# 5. Bounded queue FIFO order
# ---------------------------------------------------------------------------


class TestBoundedQueueFifoOrder:
    """Items come out in insertion order."""

    def test_fifo_order_basic(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=5)
        items = [10, 20, 30, 40, 50]
        for item in items:
            q.put_nowait(item)
        retrieved = [q.get_nowait() for _ in range(5)]
        assert retrieved == items

    def test_fifo_order_after_partial_drain_and_refill(self) -> None:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=3)
        q.put_nowait("a")
        q.put_nowait("b")
        q.put_nowait("c")
        # Drain two
        assert q.get_nowait() == "a"
        assert q.get_nowait() == "b"
        # Refill
        q.put_nowait("d")
        q.put_nowait("e")
        # Remaining: c, d, e
        assert q.get_nowait() == "c"
        assert q.get_nowait() == "d"
        assert q.get_nowait() == "e"


# ---------------------------------------------------------------------------
# 6. Raw queue (market data ingestion) is always bounded
# ---------------------------------------------------------------------------


class TestRawQueueBounded:
    """The raw_queue used in market data ingestion must be bounded.

    Bootstrap creates it as: asyncio.Queue(maxsize=raw_queue_size)
    where raw_queue_size defaults to DEFAULT_RAW_QUEUE_SIZE = 65536.
    """

    def test_raw_queue_default_is_bounded(self) -> None:
        """Verify the documented default raw queue size is positive (bounded)."""
        from hft_platform.services.bootstrap import BootstrapService

        default = BootstrapService.DEFAULT_RAW_QUEUE_SIZE
        assert default > 0, "Raw queue must have a positive maxsize (bounded)"
        assert default == 65536, "Expected default raw queue size of 65536"

    def test_bounded_queue_with_raw_queue_size(self) -> None:
        """A queue created with the default raw queue size rejects overflow."""

        # Use a small queue to avoid allocating 65k items in test
        maxsize = 8
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        for i in range(maxsize):
            q.put_nowait(i)
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(maxsize)
        assert q.qsize() == maxsize

    def test_raw_queue_drop_policy_pattern(self) -> None:
        """Simulates _enqueue_raw pattern: put_nowait with QueueFull catch."""
        maxsize = 4
        q: asyncio.Queue[tuple] = asyncio.Queue(maxsize=maxsize)
        dropped_count = 0

        def enqueue_raw(exchange: str, msg: dict) -> None:
            nonlocal dropped_count
            try:
                q.put_nowait((exchange, msg))
            except asyncio.QueueFull:
                dropped_count += 1

        for i in range(10):
            enqueue_raw("TSE", {"price": i})

        assert q.qsize() == maxsize
        assert dropped_count == 6


# ---------------------------------------------------------------------------
# 7. Risk queue is bounded
# ---------------------------------------------------------------------------


class TestRiskQueueBounded:
    """The risk/intent queue must be bounded.

    Bootstrap creates it as: asyncio.Queue(maxsize=risk_queue_size)
    where risk_queue_size defaults to DEFAULT_RISK_QUEUE_SIZE = 4096.
    """

    def test_risk_queue_default_is_bounded(self) -> None:
        from hft_platform.services.bootstrap import BootstrapService

        default = BootstrapService.DEFAULT_RISK_QUEUE_SIZE
        assert default > 0, "Risk queue must have a positive maxsize (bounded)"
        assert default == 4096, "Expected default risk queue size of 4096"

    def test_order_queue_default_is_bounded(self) -> None:
        from hft_platform.services.bootstrap import BootstrapService

        default = BootstrapService.DEFAULT_ORDER_QUEUE_SIZE
        assert default > 0, "Order queue must have a positive maxsize (bounded)"

    def test_recorder_queue_default_is_bounded(self) -> None:
        from hft_platform.services.bootstrap import BootstrapService

        default = BootstrapService.DEFAULT_RECORDER_QUEUE_SIZE
        assert default > 0, "Recorder queue must have a positive maxsize (bounded)"
        assert default == 16384, "Expected default recorder queue size of 16384"

    def test_all_platform_queues_have_positive_maxsize(self) -> None:
        """All queue defaults in BootstrapService must be positive (bounded)."""
        from hft_platform.services.bootstrap import BootstrapService

        queue_attrs = [
            attr for attr in dir(BootstrapService) if attr.startswith("DEFAULT_") and attr.endswith("_QUEUE_SIZE")
        ]
        assert len(queue_attrs) >= 3, "Expected at least 3 queue size defaults"
        for attr in queue_attrs:
            value = getattr(BootstrapService, attr)
            assert isinstance(value, int), f"{attr} should be int, got {type(value)}"
            assert value > 0, f"{attr} must be positive (bounded), got {value}"
