"""D1: Exec queue overflow must route to buffer, not silently drop fills."""

from __future__ import annotations

import asyncio
import collections
from unittest.mock import MagicMock


class TestSafeEnqueueExec:
    def test_normal_enqueue_goes_to_queue(self):
        queue = asyncio.Queue(maxsize=10)
        overflow_buf = collections.deque()
        event = MagicMock()
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            overflow_buf.append(event)
        assert queue.qsize() == 1
        assert len(overflow_buf) == 0

    def test_overflow_routes_to_buffer(self):
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())  # fill it
        overflow_buf = collections.deque()
        overflow_max = 4096
        metrics = MagicMock()
        storm_guard = MagicMock()

        event = MagicMock()
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if len(overflow_buf) >= overflow_max:
                metrics.exec_overflow_evicted_total.inc()
                storm_guard.trigger_halt("exec_overflow_buf_exhausted")
            else:
                overflow_buf.append(event)
                metrics.exec_queue_overflow_total.inc()

        assert len(overflow_buf) == 1
        metrics.exec_queue_overflow_total.inc.assert_called_once()

    def test_buffer_full_triggers_halt_and_evicts(self):
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())
        overflow_buf = collections.deque()
        overflow_max = 2
        overflow_buf.append(MagicMock())
        overflow_buf.append(MagicMock())
        metrics = MagicMock()
        storm_guard = MagicMock()

        event = MagicMock()
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            if len(overflow_buf) >= overflow_max:
                metrics.exec_overflow_evicted_total.inc()
                storm_guard.trigger_halt("exec_overflow_buf_exhausted")
            else:
                overflow_buf.append(event)

        assert len(overflow_buf) == 2  # unchanged
        metrics.exec_overflow_evicted_total.inc.assert_called_once()
        storm_guard.trigger_halt.assert_called_once_with("exec_overflow_buf_exhausted")

    def test_halt_after_3_overflows(self):
        queue = asyncio.Queue(maxsize=1)
        queue.put_nowait(MagicMock())
        overflow_buf = collections.deque()
        overflow_max = 4096
        overflow_counter = 0
        storm_guard = MagicMock()

        for _ in range(3):
            event = MagicMock()
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                overflow_buf.append(event)
                overflow_counter += 1
                if overflow_counter >= 3:
                    storm_guard.trigger_halt("exec_queue_overflow_repeated")

        storm_guard.trigger_halt.assert_called_once_with("exec_queue_overflow_repeated")

    def test_overflow_drain_back_to_queue(self):
        """Overflow events drain back to main queue when space available."""
        queue = asyncio.Queue(maxsize=10)
        overflow_buf = collections.deque()
        overflow_buf.append("event1")
        overflow_buf.append("event2")
        metrics = MagicMock()

        while overflow_buf:
            try:
                queue.put_nowait(overflow_buf.popleft())
                metrics.exec_overflow_drained_total.inc()
            except asyncio.QueueFull:
                break

        assert queue.qsize() == 2
        assert len(overflow_buf) == 0
        assert metrics.exec_overflow_drained_total.inc.call_count == 2
