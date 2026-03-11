"""Tests for FastTypedRingBuffer + MdEventFrame (WU-2).

These tests will pass once the coordinator registers the modules in lib.rs
and the Rust extension is rebuilt.
"""

from __future__ import annotations

import pytest

rust_core = pytest.importorskip("hft_platform.rust_core")


def _has_typed_ring() -> bool:
    return hasattr(rust_core, "FastTypedRingBuffer")


pytestmark = pytest.mark.skipif(
    not _has_typed_ring(),
    reason="FastTypedRingBuffer not yet registered in lib.rs",
)


class TestFastTypedRingBufferConstruction:
    """Test 1: Construct with capacity, verify capacity()."""

    def test_capacity_matches(self) -> None:
        ring = rust_core.FastTypedRingBuffer(64)
        assert ring.capacity() == 64

    def test_minimum_capacity_is_one(self) -> None:
        ring = rust_core.FastTypedRingBuffer(0)
        assert ring.capacity() >= 1

    def test_initial_cursor(self) -> None:
        ring = rust_core.FastTypedRingBuffer(8)
        # cursor starts at 1 (seq=0 means "no data")
        assert ring.cursor() == 1


class TestPublishAndGet:
    """Tests 2-3: Publish frames and retrieve by seq."""

    @staticmethod
    def _publish_sample(
        ring: object,
        kind: int = 1,
        flags: int = 2,
        symbol_id: int = 42,
        exch_ts_ns: int = 1_000_000,
        local_ts_ns: int = 2_000_000,
        price0: int = 100_0000,
        price1: int = 200_0000,
        qty0: int = 10,
        qty1: int = 20,
        aux0: int = 30,
        aux1: int = 40,
        ratio0: float = 0.5,
    ) -> int:
        return ring.publish(
            kind, flags, symbol_id,
            exch_ts_ns, local_ts_ns,
            price0, price1, qty0, qty1, aux0, aux1, ratio0,
        )

    def test_publish_single_and_get(self) -> None:
        ring = rust_core.FastTypedRingBuffer(8)
        seq = self._publish_sample(ring)
        assert seq == 1

        frame = ring.get(seq)
        assert frame is not None
        # Unpack 14-element tuple
        (
            kind, flags, reserved, symbol_id, ret_seq,
            exch_ts_ns, local_ts_ns,
            price0, price1, qty0, qty1, aux0, aux1, ratio0,
        ) = frame
        assert kind == 1
        assert flags == 2
        assert reserved == 0
        assert symbol_id == 42
        assert ret_seq == 1
        assert exch_ts_ns == 1_000_000
        assert local_ts_ns == 2_000_000
        assert price0 == 100_0000
        assert price1 == 200_0000
        assert qty0 == 10
        assert qty1 == 20
        assert aux0 == 30
        assert aux1 == 40
        assert abs(ratio0 - 0.5) < 1e-12

    def test_publish_multiple_and_get_each(self) -> None:
        ring = rust_core.FastTypedRingBuffer(16)
        seqs = []
        for i in range(5):
            seq = ring.publish(
                i + 1, 0, i, i * 100, i * 200,
                i * 1000, i * 2000, i, i, i, i, float(i),
            )
            seqs.append(seq)

        for i, seq in enumerate(seqs):
            frame = ring.get(seq)
            assert frame is not None
            assert frame[0] == i + 1  # kind
            assert frame[3] == i      # symbol_id


class TestCursorAdvancement:
    """Test 4: Cursor advances correctly."""

    def test_cursor_advances(self) -> None:
        ring = rust_core.FastTypedRingBuffer(8)
        assert ring.cursor() == 1
        ring.publish(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)
        assert ring.cursor() == 2
        ring.publish(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)
        assert ring.cursor() == 3


class TestOverwrite:
    """Test 5: Old frames get overwritten after capacity exceeded."""

    def test_old_frames_return_none(self) -> None:
        cap = 4
        ring = rust_core.FastTypedRingBuffer(cap)
        # Publish cap + 2 frames so seq 1 and 2 are overwritten
        for i in range(cap + 2):
            ring.publish(1, 0, i, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)

        # seq 1 and 2 should be gone
        assert ring.get(1) is None
        assert ring.get(2) is None
        # seq 3..6 should be present
        for seq in range(3, cap + 3):
            assert ring.get(seq) is not None, f"seq {seq} should be present"


class TestInvalidSeq:
    """Test 6: get() with invalid seq returns None."""

    def test_seq_zero_returns_none(self) -> None:
        ring = rust_core.FastTypedRingBuffer(8)
        assert ring.get(0) is None

    def test_too_old_returns_none(self) -> None:
        ring = rust_core.FastTypedRingBuffer(4)
        for _ in range(10):
            ring.publish(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)
        assert ring.get(1) is None

    def test_too_new_returns_none(self) -> None:
        ring = rust_core.FastTypedRingBuffer(8)
        ring.publish(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)
        # seq 2 has not been published yet
        assert ring.get(2) is None
        # far future
        assert ring.get(999) is None


class TestMdEventFrameSize:
    """Test 7: MdEventFrame is exactly 128 bytes.

    This is verified at compile-time in Rust via const assertion.
    Here we verify the published tuple has the expected 14 fields.
    """

    def test_tuple_has_14_fields(self) -> None:
        ring = rust_core.FastTypedRingBuffer(4)
        seq = ring.publish(1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0)
        frame = ring.get(seq)
        assert frame is not None
        assert len(frame) == 14


class TestBulkPublish:
    """Test 8: Publish 1000 frames rapidly, verify last N retrievable."""

    def test_bulk_1000(self) -> None:
        cap = 128
        ring = rust_core.FastTypedRingBuffer(cap)
        total = 1000
        for i in range(1, total + 1):
            ring.publish(
                1, 0, i, i * 10, i * 20,
                i * 100, i * 200, i, i, i, i, float(i),
            )

        assert ring.cursor() == total + 1

        # Last `cap` frames should be retrievable
        first_valid = total + 1 - cap
        for seq in range(first_valid, total + 1):
            frame = ring.get(seq)
            assert frame is not None, f"seq {seq} should be present"
            assert frame[4] == seq  # seq field in tuple

        # Frames before first_valid should be gone
        assert ring.get(first_valid - 1) is None
