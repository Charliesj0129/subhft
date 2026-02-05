"""Unit tests for lock-free shared memory ring buffer.

Tests cover: basic operations, full/empty states, wrap-around,
cursor overflow, concurrent producer/consumer, and lifecycle.
"""

import multiprocessing
import struct
import sys
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.ipc.shm_ring_buffer import (
    HEADER_SIZE,
    SLOT_SIZE,
    ShmRingBuffer,
    _read,
    _write,
)


def _unique_name() -> str:
    """Generate unique shared memory name for test isolation."""
    return f"test_ring_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def ring_buffer():
    """Create a ring buffer and clean up after test."""
    name = _unique_name()
    rb = ShmRingBuffer(name=name, capacity=8, create=True)
    yield rb
    rb.unlink()
    rb.close()


# ---------------------------------------------------------------------------
# Basic Operations
# ---------------------------------------------------------------------------
class TestBasicOperations:
    def test_write_read_single_message(self, ring_buffer):
        """Write then read a single message."""
        msg = b"hello world" + b"\x00" * (SLOT_SIZE - 11)
        assert ring_buffer.write(msg) is True
        result = ring_buffer.read()
        assert result is not None
        assert result[:11] == b"hello world"

    def test_write_returns_data_bytes(self, ring_buffer):
        """Write small data and verify read returns padded SLOT_SIZE bytes."""
        msg = b"test"
        assert ring_buffer.write(msg) is True
        result = ring_buffer.read()
        assert len(result) == SLOT_SIZE
        assert result[:4] == b"test"

    def test_write_truncates_long_messages(self, ring_buffer):
        """Messages longer than SLOT_SIZE are truncated."""
        long_msg = b"x" * (SLOT_SIZE + 100)
        assert ring_buffer.write(long_msg) is True
        result = ring_buffer.read()
        assert len(result) == SLOT_SIZE
        assert result == b"x" * SLOT_SIZE

    def test_multiple_write_read_fifo(self, ring_buffer):
        """Multiple writes and reads maintain FIFO order."""
        msgs = [f"msg_{i}".encode().ljust(SLOT_SIZE, b"\x00") for i in range(5)]
        for msg in msgs:
            assert ring_buffer.write(msg) is True

        for i, _ in enumerate(msgs):
            result = ring_buffer.read()
            assert result is not None
            assert result.rstrip(b"\x00") == f"msg_{i}".encode()


# ---------------------------------------------------------------------------
# Full Buffer
# ---------------------------------------------------------------------------
class TestFullBuffer:
    def test_write_to_full_buffer_returns_false(self, ring_buffer):
        """When buffer is full, write returns False."""
        # Fill the buffer (capacity=8)
        for i in range(8):
            assert ring_buffer.write(f"msg{i}".encode()) is True

        # Next write should fail
        assert ring_buffer.write(b"overflow") is False

    def test_write_after_read_succeeds(self, ring_buffer):
        """After reading from full buffer, write succeeds."""
        # Fill buffer
        for i in range(8):
            ring_buffer.write(f"msg{i}".encode())

        # Read one
        result = ring_buffer.read()
        assert result is not None

        # Now write should succeed
        assert ring_buffer.write(b"new_msg") is True


# ---------------------------------------------------------------------------
# Empty Buffer
# ---------------------------------------------------------------------------
class TestEmptyBuffer:
    def test_read_from_empty_returns_none(self, ring_buffer):
        """Reading from empty buffer returns None."""
        result = ring_buffer.read()
        assert result is None

    def test_multiple_reads_from_empty(self, ring_buffer):
        """Multiple reads from empty buffer all return None."""
        for _ in range(5):
            assert ring_buffer.read() is None


# ---------------------------------------------------------------------------
# Wrap Around
# ---------------------------------------------------------------------------
class TestWrapAround:
    def test_cursor_wrap_around(self, ring_buffer):
        """Test that cursors wrap correctly at capacity boundary."""
        # Write and read more than capacity to force wrap
        for cycle in range(3):
            for i in range(8):
                msg = f"c{cycle}m{i}".encode()
                assert ring_buffer.write(msg) is True

            for i in range(8):
                result = ring_buffer.read()
                assert result is not None
                assert result.rstrip(b"\x00") == f"c{cycle}m{i}".encode()

    def test_partial_wrap(self, ring_buffer):
        """Test wrap when write/read cursors are misaligned."""
        # Write 5, read 3, write 5 more (forces wrap)
        for i in range(5):
            ring_buffer.write(f"batch1_{i}".encode())

        for i in range(3):
            result = ring_buffer.read()
            assert result.rstrip(b"\x00") == f"batch1_{i}".encode()

        # Now write cursor is at 5, read cursor at 3
        # Write 3 more (fills to 8-3=5 slots available, but we have 5-3=2 used)
        for i in range(6):  # Write 6 more
            ring_buffer.write(f"batch2_{i}".encode())

        # Read remaining: 2 from batch1, 6 from batch2
        for i in range(3, 5):
            result = ring_buffer.read()
            assert result.rstrip(b"\x00") == f"batch1_{i}".encode()

        for i in range(6):
            result = ring_buffer.read()
            assert result.rstrip(b"\x00") == f"batch2_{i}".encode()


# ---------------------------------------------------------------------------
# Cursor Overflow (near int64 max)
# ---------------------------------------------------------------------------
class TestCursorOverflow:
    def test_large_cursor_values(self):
        """Test behavior with cursor values near int64 max."""
        name = _unique_name()
        capacity = 8
        rb = ShmRingBuffer(name=name, capacity=capacity, create=True)
        try:
            # Manually set cursors to large values near int64 boundary
            large_val = (2**62) - 10  # Large but safe from overflow
            rb.header_array[0] = large_val  # write_cursor
            rb.header_array[1] = large_val  # read_cursor

            # Should still work for a few operations (up to capacity)
            test_count = capacity - 1
            for i in range(test_count):
                assert rb.write(f"msg{i}".encode()) is True

            for i in range(test_count):
                result = rb.read()
                assert result is not None
                assert result.rstrip(b"\x00") == f"msg{i}".encode()
        finally:
            rb.unlink()
            rb.close()


# ---------------------------------------------------------------------------
# Concurrent Producer/Consumer
# ---------------------------------------------------------------------------
def _producer(name: str, count: int, capacity: int):
    """Producer process that writes messages."""
    rb = ShmRingBuffer(name=name, capacity=capacity, create=False)
    written = 0
    retries = 0
    max_retries = count * 100

    while written < count and retries < max_retries:
        msg = struct.pack("!I", written) + b"\x00" * (SLOT_SIZE - 4)
        if rb.write(msg):
            written += 1
        else:
            retries += 1
            time.sleep(0.0001)

    rb.close()
    return written


def _consumer(name: str, count: int, capacity: int):
    """Consumer process that reads messages."""
    rb = ShmRingBuffer(name=name, capacity=capacity, create=False)
    received = []
    retries = 0
    max_retries = count * 100

    while len(received) < count and retries < max_retries:
        result = rb.read()
        if result is not None:
            seq = struct.unpack("!I", result[:4])[0]
            received.append(seq)
        else:
            retries += 1
            time.sleep(0.0001)

    rb.close()
    return received


class TestConcurrentAccess:
    @pytest.mark.timeout(10)
    def test_single_producer_single_consumer(self):
        """SPSC stress test with multiprocessing."""
        name = _unique_name()
        capacity = 16
        message_count = 100

        rb = ShmRingBuffer(name=name, capacity=capacity, create=True)
        try:
            with multiprocessing.Pool(2) as pool:
                prod_result = pool.apply_async(_producer, (name, message_count, capacity))
                cons_result = pool.apply_async(_consumer, (name, message_count, capacity))

                written = prod_result.get(timeout=8)
                received = cons_result.get(timeout=8)

            # Verify all messages sent and received (order preserved)
            assert written == message_count
            assert len(received) == message_count
            assert received == list(range(message_count))
        finally:
            rb.unlink()
            rb.close()


# ---------------------------------------------------------------------------
# Shared Memory Lifecycle
# ---------------------------------------------------------------------------
class TestSharedMemoryLifecycle:
    def test_create_and_attach(self):
        """Test creating shm and attaching from another instance."""
        name = _unique_name()

        # Create
        rb1 = ShmRingBuffer(name=name, capacity=8, create=True)
        rb1.write(b"from_creator")

        # Attach (create=False)
        rb2 = ShmRingBuffer(name=name, capacity=8, create=False)
        result = rb2.read()
        assert result is not None
        assert result.rstrip(b"\x00") == b"from_creator"

        rb2.close()
        rb1.unlink()
        rb1.close()

    def test_create_existing_attaches(self):
        """Creating with existing name should attach instead of error."""
        name = _unique_name()

        rb1 = ShmRingBuffer(name=name, capacity=8, create=True)
        rb1.write(b"original")

        # Create=True but already exists -> attaches
        rb2 = ShmRingBuffer(name=name, capacity=8, create=True)
        result = rb2.read()
        assert result is not None
        assert result.rstrip(b"\x00") == b"original"

        rb2.close()
        rb1.unlink()
        rb1.close()

    def test_unlink_removes_shm(self):
        """Unlink removes shared memory."""
        name = _unique_name()
        rb = ShmRingBuffer(name=name, capacity=8, create=True)
        rb.write(b"test")
        rb.unlink()
        rb.close()

        # Trying to attach should fail
        with pytest.raises(FileNotFoundError):
            ShmRingBuffer(name=name, capacity=8, create=False)


# ---------------------------------------------------------------------------
# Numba JIT Function Tests
# ---------------------------------------------------------------------------
class TestNumbaFunctions:
    def test_write_function_direct(self):
        """Test _write numba function directly."""
        buf = np.zeros(4 * SLOT_SIZE, dtype=np.uint8)
        header = np.array([0, 0], dtype=np.int64)
        data = np.frombuffer(b"test" + b"\x00" * (SLOT_SIZE - 4), dtype=np.uint8)

        result = _write(buf, header, 4, data)
        assert result is True
        assert header[0] == 1  # write_cursor incremented

    def test_read_function_direct(self):
        """Test _read numba function directly."""
        buf = np.zeros(4 * SLOT_SIZE, dtype=np.uint8)
        header = np.array([1, 0], dtype=np.int64)  # 1 message available

        # Manually put data in slot 0
        buf[0:4] = np.frombuffer(b"read", dtype=np.uint8)

        out = np.zeros(SLOT_SIZE, dtype=np.uint8)
        result = _read(buf, header, 4, out)

        assert result is True
        assert header[1] == 1  # read_cursor incremented
        assert bytes(out[:4]) == b"read"

    def test_write_full_returns_false(self):
        """_write returns False when buffer is full."""
        buf = np.zeros(2 * SLOT_SIZE, dtype=np.uint8)
        header = np.array([2, 0], dtype=np.int64)  # write=2, read=0, capacity=2 -> full
        data = np.frombuffer(b"test".ljust(SLOT_SIZE, b"\x00"), dtype=np.uint8)

        result = _write(buf, header, 2, data)
        assert result is False

    def test_read_empty_returns_false(self):
        """_read returns False when buffer is empty."""
        buf = np.zeros(2 * SLOT_SIZE, dtype=np.uint8)
        header = np.array([0, 0], dtype=np.int64)  # write=read=0 -> empty

        out = np.zeros(SLOT_SIZE, dtype=np.uint8)
        result = _read(buf, header, 2, out)
        assert result is False


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_zero_length_message(self, ring_buffer):
        """Writing empty bytes should work."""
        assert ring_buffer.write(b"") is True
        result = ring_buffer.read()
        assert result is not None
        assert result == b"\x00" * SLOT_SIZE

    def test_binary_data_preservation(self, ring_buffer):
        """Binary data with all byte values preserved."""
        binary_msg = bytes(range(SLOT_SIZE))
        assert ring_buffer.write(binary_msg) is True
        result = ring_buffer.read()
        assert result == binary_msg

    def test_header_size_alignment(self):
        """Verify header is properly aligned."""
        assert HEADER_SIZE >= 64  # At least one cache line
        assert HEADER_SIZE % 64 == 0  # Aligned to cache line
