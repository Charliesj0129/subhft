"""Tests for the shared-memory monitor snapshot table."""

from __future__ import annotations

import pytest

from hft_platform.ipc import shm_snapshot
from hft_platform.ipc.shm_snapshot import ShmSnapshotReader, ShmSnapshotWriter, _symbol_hash

pytestmark = pytest.mark.skipif(shm_snapshot.fcntl is None, reason="flock is unavailable")


@pytest.fixture()
def segment(monkeypatch, tmp_path):
    monkeypatch.setattr(shm_snapshot, "_SHM_ROOT", tmp_path)
    return "monitor_snapshot"


def test_snapshot_writer_reader_roundtrip(segment):
    lob = [100, 200, 300, 400, 500, 600, 700, 800, 900]
    features = list(range(16))
    symbol_hash = _symbol_hash("2330")
    with ShmSnapshotWriter(segment, 4) as writer, ShmSnapshotReader(segment, 4) as reader:
        assert reader.read_slot(0) is None
        assert reader.global_version() == 0
        writer.publish(0, 1234567890, symbol_hash, lob, features)
        snapshot = reader.read_slot(0)
        assert snapshot is not None
        assert snapshot.ts_ns == 1234567890
        assert snapshot.symbol_hash == symbol_hash
        assert snapshot.lob_fields == tuple(lob)
        assert snapshot.features == tuple(features)
        assert snapshot.version % 2 == 0
        assert reader.read_slot(3) is None
        assert reader.global_version() == 1

        writer.publish(0, 1234567891, symbol_hash, lob, features)
        next_snapshot = reader.read_slot(0)
        assert next_snapshot is not None
        assert next_snapshot.version > snapshot.version
        assert reader.global_version() == 2


def test_snapshot_rejects_out_of_range_slot_and_field_counts(segment):
    with ShmSnapshotWriter(segment, 4) as writer, ShmSnapshotReader(segment, 4) as reader:
        with pytest.raises(IndexError):
            writer.publish(4, 1, 2, [0] * 9, [0] * 16)
        with pytest.raises(IndexError):
            reader.read_slot(-1)
        with pytest.raises(ValueError, match="lob_fields"):
            writer.publish(0, 1, 2, [0] * 8, [0] * 16)
        with pytest.raises(ValueError, match="features"):
            writer.publish(0, 1, 2, [0] * 9, [0] * 15)
        assert reader.read_slot(0) is None


def test_snapshot_keeps_slots_independent(segment):
    with ShmSnapshotWriter(segment, 4) as writer, ShmSnapshotReader(segment, 4) as reader:
        for index in range(4):
            writer.publish(index, index * 1000, index + 10, [index] * 9, [index] * 16)
        for index in range(4):
            snapshot = reader.read_slot(index)
            assert snapshot is not None
            assert snapshot.ts_ns == index * 1000
            assert snapshot.symbol_hash == index + 10
            assert snapshot.lob_fields == (index,) * 9
            assert snapshot.features == (index,) * 16


def test_snapshot_never_waits_for_a_contended_writer_slot(segment):
    lock = shm_snapshot.fcntl
    with ShmSnapshotWriter(segment, 4) as writer, ShmSnapshotReader(segment, 4) as reader:
        writer.publish(0, 1, 2, [3] * 9, [4] * 16)
        path = shm_snapshot._segment_path(segment, 4)
        with path.open("rb") as blocked:
            lock.flock(blocked, lock.LOCK_SH | lock.LOCK_NB)
            writer.publish(0, 5, 6, [7] * 9, [8] * 16)
        snapshot = reader.read_slot(0)
        assert snapshot is not None
        assert snapshot.ts_ns == 1
        assert reader.global_version() == 1


def test_snapshot_rejects_second_writer_and_mismatched_reader(segment):
    with ShmSnapshotWriter(segment, 4) as writer:
        writer.publish(0, 1, 2, [0] * 9, [0] * 16)
        with pytest.raises(BlockingIOError):
            ShmSnapshotWriter(segment, 4)
        with pytest.raises(ValueError, match="size"):
            ShmSnapshotReader(segment, 3)
        with ShmSnapshotReader(segment, 4) as reader:
            snapshot = reader.read_slot(0)
            assert snapshot is not None
            assert snapshot.ts_ns == 1


def test_snapshot_rejects_writer_lease_symlink(segment, tmp_path):
    target = tmp_path / "unrelated-file"
    target.write_text("keep")
    lease_path = shm_snapshot._segment_path(segment, 4).with_suffix(".hft-py-v1.writer")
    lease_path.symlink_to(target)
    with pytest.raises(OSError):
        ShmSnapshotWriter(segment, 4)
    assert target.read_text() == "keep"


def test_snapshot_reattaches_with_new_version_after_writer_restarts(segment):
    with ShmSnapshotWriter(segment, 4) as writer, ShmSnapshotReader(segment, 4) as reader:
        writer.publish(0, 1, 2, [0] * 9, [0] * 16)
        before = reader.read_slot(0)
        assert before is not None
        writer.close()
        assert reader.read_slot(0) is None
        with ShmSnapshotWriter(segment, 4) as restarted:
            restarted.publish(0, 2, 2, [0] * 9, [0] * 16)
            after = reader.read_slot(0)
            assert after is not None
            assert after.ts_ns == 2
            assert after.version > before.version


def test_snapshot_open_rejects_stale_file_without_owner(segment):
    with ShmSnapshotWriter(segment, 4):
        path = shm_snapshot._segment_path(segment, 4)
        saved = path.read_bytes()
    assert not path.exists()

    path.write_bytes(saved)
    with pytest.raises(FileNotFoundError):
        ShmSnapshotReader(segment, 4)
