"""Required extension checks for the PyO3 security migration (no skip/fallback)."""

from __future__ import annotations

import gc
import importlib
import importlib.machinery
import inspect
import sys
import weakref
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture(scope="module")
def extension():
    module = importlib.import_module("hft_platform.rust_core")
    assert any(str(module.__file__).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
    return module


def test_rust_extension_import_preserves_python_signatures(extension) -> None:
    assert list(inspect.signature(extension.normalize_bidask_tuple_np).parameters) == [
        "symbol",
        "exch_ts",
        "bid_prices",
        "bid_vols",
        "ask_prices",
        "ask_vols",
        "scale",
    ]
    assert list(inspect.signature(extension.ShmRingBuffer).parameters) == ["name", "capacity", "create"]
    assert list(inspect.signature(extension.ShmSnapshotTable).parameters) == ["name", "max_symbols", "create"]


@pytest.mark.parametrize("depth", [0, 1, 5])
def test_rust_normalizer_preserves_integer_prices_and_depth(extension, depth: int) -> None:
    bids = np.array([100.15, 100.10, 100.05, 100.00, 99.95])[:depth]
    asks = np.array([100.20, 100.25, 100.30, 100.35, 100.40])[:depth]
    volumes = np.arange(1, depth + 1, dtype=np.int64)
    result = extension.normalize_bidask_tuple_np("TEST", 123, bids, volumes, asks, volumes, 10_000)
    assert len(result) == 13
    assert result[:2] == ("bidask", "TEST")
    assert result[4:6] == (123, False)
    assert type(result[5]) is bool
    assert result[2].dtype == result[3].dtype == np.dtype("int64")
    assert result[2].shape == result[3].shape == (depth, 2)
    assert result[2][:, 0].tolist() == [1_001_500, 1_001_000, 1_000_500, 1_000_000, 999_500][:depth]
    assert result[3][:, 0].tolist() == [1_002_000, 1_002_500, 1_003_000, 1_003_500, 1_004_000][:depth]
    assert all(type(value) is int for value in result[6:10])
    assert result[8:10] == (int(volumes.sum()), int(volumes.sum()))
    del bids, asks, volumes
    gc.collect()
    assert result[2].shape == (depth, 2)
    assert result[2][:, 1].tolist() == list(range(1, depth + 1))


def test_rust_numpy_output_owns_memory_after_kernel_and_inputs_drop(extension) -> None:
    kernel = extension.AlphaOFI()
    bid_p = np.array([100.0, 100.0, 101.0])
    ask_p = np.array([102.0, 102.0, 102.0])
    bid_v = np.array([10.0, 12.0, 11.0])
    ask_v = np.array([20.0, 19.0, 18.0])
    result = kernel.compute(bid_p, ask_p, bid_v, ask_v)
    del kernel, bid_p, ask_p, bid_v, ask_v
    gc.collect()
    assert result.dtype == np.dtype("float64")
    assert result.shape == (3,)
    assert result.tolist() == [0.0, 3.0, 12.0]


def test_rust_generic_ring_retains_and_releases_python_objects(extension) -> None:
    ring = extension.FastRingBuffer(1)
    array = np.array([1_001_500], dtype=np.int64)
    reference = weakref.ref(array)
    ring.set(0, array)
    assert ring.get(0) is array
    del array
    gc.collect()
    assert reference() is not None
    assert ring.get(1).tolist() == [1_001_500]
    ring.set(1, None)
    gc.collect()
    assert reference() is None
    assert ring.get(0) is None


def test_rust_bidask_ring_preserves_arrays_booleans_and_tuple_order(extension) -> None:
    ring = extension.FastBidAskRingBuffer(1)
    bids = np.array([[1_001_500, 2]], dtype=np.int64)
    asks = np.array([[1_002_000, 3]], dtype=np.int64)
    ring.set_bidask(0, "TEST", bids, asks, 123, True, True, 1_001_500, 1_002_000, 2, 3, 1_001_750.0, 500.0, -0.2)
    result = ring.get(0)
    assert result[:2] == ("bidask", "TEST")
    assert result[2] is bids and result[3] is asks
    assert result[4:] == (123, True, 1_001_500, 1_002_000, 2, 3, 1_001_750.0, 500.0, -0.2)
    assert type(result[5]) is bool


def test_rust_typed_ring_wrap_preserves_frame_layout(extension) -> None:
    ring = extension.FastTypedRingBuffer(2)
    for index in range(3):
        assert ring.publish(1, 2, 42, 100, 200, 1_001_500, 1_002_000, 3, 4, 5, 6, 0.5) == index + 1
    assert ring.get(1) is None
    assert ring.get(3) == (1, 2, 0, 42, 3, 100, 200, 1_001_500, 1_002_000, 3, 4, 5, 6, 0.5)


@pytest.mark.parametrize("capacity", [0, sys.maxsize])
def test_rust_shm_ring_rejects_invalid_capacity_before_mapping(extension, tmp_path: Path, capacity: int) -> None:
    path = tmp_path / "ring"
    with pytest.raises(ValueError, match="capacity"):
        extension.ShmRingBuffer(str(path), capacity, True)
    assert not path.exists()


@pytest.mark.parametrize("class_name", ["ShmRingBuffer", "ShmSnapshotTable"])
def test_rust_shm_rejects_short_existing_mapping(extension, tmp_path: Path, class_name: str) -> None:
    path = tmp_path / "short"
    path.write_bytes(b"short")
    with pytest.raises(ValueError, match="smaller"):
        getattr(extension, class_name)(str(path), 2, False)
    assert path.read_bytes() == b"short"


def test_rust_shm_ring_wrap_reopen_and_writer_lifetime(extension, tmp_path: Path) -> None:
    path = str(tmp_path / "ring")
    writer = extension.ShmRingBuffer(path, 2, True)
    reader = extension.ShmRingBuffer(path, 2, False)
    assert writer.write(b"a" * 64)
    assert writer.write(b"b" * 64)
    assert not writer.write(b"full")
    assert reader.read() == b"a" * 64
    assert writer.write(b"c" * 64)
    del writer
    gc.collect()
    assert reader.read() == b"b" * 64
    assert reader.read() == b"c" * 64
    assert reader.read() is None


def test_rust_snapshot_reopen_preserves_scaled_fields_and_errors(extension, tmp_path: Path) -> None:
    path = str(tmp_path / "snapshot")
    writer = extension.ShmSnapshotTable(path, 2, True)
    reader = extension.ShmSnapshotTable(path, 2, False)
    lob = [1_001_500, 1_002_000, 2_003_500, 500, 10, 20, 3, 4, 2_003_500]
    writer.write_slot(1, 123, 42, lob, list(range(16)))
    assert reader.read_slot(0) is None
    assert reader.global_version() == 1
    assert reader.read_slot(1) == (2, 123, 42, lob, list(range(16)))
    with pytest.raises(IndexError):
        writer.read_slot(2)
    with pytest.raises(ValueError, match="lob_fields"):
        writer.write_slot(0, 0, 0, [], list(range(16)))
    del writer
    gc.collect()
    assert reader.read_slot(1)[3] == lob
