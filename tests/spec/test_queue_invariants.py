"""Queue overflow invariant tests.

Verifies bounded queue contracts that the HFT platform relies on:
- put_nowait raises QueueFull at capacity
- recorder-style drop policy is non-blocking
- queue depth never exceeds maxsize
"""

from __future__ import annotations

import asyncio

import pytest


class TestBoundedQueueFull:
    """put_nowait raises QueueFull at capacity."""

    def test_put_nowait_raises_at_capacity(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=3)
        for i in range(3):
            q.put_nowait(i)
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait(99)

    def test_depth_equals_maxsize_at_capacity(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=5)
        for i in range(5):
            q.put_nowait(i)
        assert q.qsize() == 5

    def test_depth_never_exceeds_maxsize(self) -> None:
        maxsize = 10
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=maxsize)
        for i in range(100):
            try:
                q.put_nowait(i)
            except asyncio.QueueFull:
                pass
        assert q.qsize() <= maxsize


class TestRecorderDropPolicy:
    """Recorder queue uses put_nowait with silent drop on full."""

    def test_drop_on_full_does_not_block(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=2)
        q.put_nowait(1)
        q.put_nowait(2)
        # Simulate recorder drop policy
        dropped = False
        try:
            q.put_nowait(3)
        except asyncio.QueueFull:
            dropped = True
        assert dropped
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


class TestQueueDepthInvariant:
    """Queue depth never exceeds declared maxsize."""

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
        await asyncio.gather(*tasks)
        assert q.qsize() <= maxsize

    def test_get_reduces_depth(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=3)
        q.put_nowait(1)
        q.put_nowait(2)
        q.put_nowait(3)
        assert q.qsize() == 3
        q.get_nowait()
        assert q.qsize() == 2
        # Can add again
        q.put_nowait(4)
        assert q.qsize() == 3


class TestUnboundedQueueComparison:
    """Unbounded queue (maxsize=0) has no limit -- documents the risk."""

    def test_unbounded_accepts_any_amount(self) -> None:
        q: asyncio.Queue[int] = asyncio.Queue(maxsize=0)
        for i in range(1000):
            q.put_nowait(i)
        assert q.qsize() == 1000
