"""Tests for ShmSnapshotTable (Phase 1: seqlock mmap snapshot table)."""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture()
def _cleanup_shm():
    """Remove /dev/shm test segments after each test."""
    names: list[str] = []
    yield names
    for name in names:
        path = f"/dev/shm/{name}"
        if os.path.exists(path):
            os.unlink(path)


def _unique_name() -> str:
    return f"test_snap_{os.getpid()}_{int(time.monotonic_ns())}"


class TestShmSnapshotRustRoundtrip:
    """Direct Rust ShmSnapshotTable roundtrip tests."""

    def test_write_read_roundtrip(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotTable(name, 4, True)
        reader = ShmSnapshotTable(name, 4, False)

        lob = [100, 200, 300, 400, 500, 600, 700, 800, 900]
        feat = list(range(16))

        writer.write_slot(0, 1234567890, 42, lob, feat)

        result = reader.read_slot(0)
        assert result is not None
        version, ts_ns, sym_hash, r_lob, r_feat = result
        assert ts_ns == 1234567890
        assert sym_hash == 42
        assert r_lob == lob
        assert r_feat == feat
        assert version % 2 == 0  # even = stable

    def test_unwritten_slot_returns_none(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        table = ShmSnapshotTable(name, 8, True)
        assert table.read_slot(0) is None
        assert table.read_slot(7) is None

    def test_slot_out_of_range(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        table = ShmSnapshotTable(name, 4, True)
        with pytest.raises(IndexError):
            table.write_slot(4, 0, 0, [0] * 9, [0] * 16)
        with pytest.raises(IndexError):
            table.read_slot(4)

    def test_version_monotonicity(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotTable(name, 2, True)
        reader = ShmSnapshotTable(name, 2, False)

        lob = [0] * 9
        feat = [0] * 16

        versions: list[int] = []
        for i in range(10):
            writer.write_slot(0, i, 1, lob, feat)
            result = reader.read_slot(0)
            assert result is not None
            versions.append(result[0])

        # Each write bumps version by 2 (odd → even)
        for i in range(1, len(versions)):
            assert versions[i] > versions[i - 1]

    def test_multi_slot_independent(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotTable(name, 4, True)
        reader = ShmSnapshotTable(name, 4, False)

        for i in range(4):
            lob = [i * 100 + j for j in range(9)]
            feat = [i * 1000 + j for j in range(16)]
            writer.write_slot(i, i * 1000, i + 10, lob, feat)

        for i in range(4):
            result = reader.read_slot(i)
            assert result is not None
            _, ts_ns, sym_hash, r_lob, r_feat = result
            assert ts_ns == i * 1000
            assert sym_hash == i + 10
            assert r_lob == [i * 100 + j for j in range(9)]
            assert r_feat == [i * 1000 + j for j in range(16)]

    def test_global_version_increments(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotTable(name, 2, True)
        reader = ShmSnapshotTable(name, 2, False)

        gv0 = reader.global_version()
        writer.write_slot(0, 1, 1, [0] * 9, [0] * 16)
        gv1 = reader.global_version()
        writer.write_slot(1, 2, 2, [0] * 9, [0] * 16)
        gv2 = reader.global_version()

        assert gv1 > gv0
        assert gv2 > gv1

    def test_wrong_field_count_rejected(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.rust_core import ShmSnapshotTable

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotTable(name, 2, True)

        with pytest.raises(ValueError, match="lob_fields"):
            writer.write_slot(0, 0, 0, [0] * 8, [0] * 16)

        with pytest.raises(ValueError, match="features"):
            writer.write_slot(0, 0, 0, [0] * 9, [0] * 15)


class TestShmSnapshotPythonWrapper:
    """Python-level ShmSnapshotWriter/Reader tests."""

    def test_python_wrapper_roundtrip(self, _cleanup_shm: list[str]) -> None:
        from hft_platform.ipc.shm_snapshot import ShmSnapshotReader, ShmSnapshotWriter, _symbol_hash

        name = _unique_name()
        _cleanup_shm.append(name)

        writer = ShmSnapshotWriter(name, max_symbols=4)
        reader = ShmSnapshotReader(name, max_symbols=4)

        sym_hash = _symbol_hash("2330")
        lob = [1000, 2000, 3000, 100, 500, 600, 10, 20, 3100]
        feat = list(range(100, 116))

        writer.publish(0, 9999, sym_hash, lob, feat)
        slot = reader.read_slot(0)

        assert slot is not None
        assert slot.ts_ns == 9999
        assert slot.symbol_hash == sym_hash
        assert slot.lob_fields == tuple(lob)
        assert slot.features == tuple(feat)
        assert slot.version % 2 == 0
