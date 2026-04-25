"""M2 (2026-04-25): DataWriter._get_wal_batch_writer must serialise lazy
init under threading.Lock. Without the lock, two batchers calling the
method concurrently could each construct a WALBatchWriter — each spawning
its own ``wal-batch-timer`` daemon thread, leaking memory and producing
duplicate flush traffic.

This test launches N threads that call ``_get_wal_batch_writer`` concurrently
through a barrier and asserts:
1. All threads see the same writer instance (no racing assignments).
2. Only ONE WALBatchWriter constructor was actually invoked.
3. Only ONE ``wal-batch-timer`` daemon thread was spawned.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from hft_platform.recorder.writer import DataWriter


@pytest.fixture()
def isolated_wal_dir(tmp_path):
    """Point the writer at a tmp directory so we don't touch the real .wal/."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir()
    return str(wal_dir)


def _count_batch_timer_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "wal-batch-timer")


def test_concurrent_lazy_init_creates_single_writer(isolated_wal_dir, monkeypatch) -> None:
    """N threads racing on _get_wal_batch_writer must produce exactly one writer."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.setenv("HFT_WAL_BATCH_ENABLED", "1")

    pre_count = _count_batch_timer_threads()

    writer = DataWriter(wal_dir=isolated_wal_dir)
    n_threads = 16
    barrier = threading.Barrier(n_threads)
    results: list[Any] = [None] * n_threads
    errors: list[BaseException] = []

    construct_count = {"n": 0}

    # Wrap WALBatchWriter to count constructions. Patch BEFORE threads race.
    import hft_platform.recorder.wal as wal_mod

    real_writer_cls = wal_mod.WALBatchWriter

    class _CountingWriter(real_writer_cls):  # type: ignore[misc, valid-type]
        def __init__(self_inner, wal_dir: str) -> None:  # noqa: ANN001
            construct_count["n"] += 1
            super().__init__(wal_dir)

    monkeypatch.setattr(wal_mod, "WALBatchWriter", _CountingWriter)

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            results[idx] = writer._get_wal_batch_writer()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,), name=f"race-{i}") for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    assert not errors, f"Worker errors: {errors}"
    first = results[0]
    assert first is not None, "First worker did not produce a writer"
    for r in results:
        assert r is first, "All threads must observe the same writer instance"

    assert construct_count["n"] == 1, (
        f"Expected exactly one WALBatchWriter construction; got {construct_count['n']}"
    )

    # Stop the timer thread so we don't leak it past the test.
    if first is not None:
        first.stop()

    post_count = _count_batch_timer_threads()
    assert post_count == pre_count, (
        f"wal-batch-timer thread leaked: pre={pre_count}, post={post_count}"
    )


def test_init_lock_attribute_exists(isolated_wal_dir, monkeypatch) -> None:
    """Regression guard: the init lock must be present and a real Lock."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    writer = DataWriter(wal_dir=isolated_wal_dir)
    assert hasattr(writer, "_wal_batch_writer_init_lock")
    assert isinstance(writer._wal_batch_writer_init_lock, type(threading.Lock()))


def test_disabled_batch_writer_returns_none(isolated_wal_dir, monkeypatch) -> None:
    """When HFT_WAL_BATCH_ENABLED=0 the lazy-init must skip and return None."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    monkeypatch.setenv("HFT_WAL_BATCH_ENABLED", "0")
    writer = DataWriter(wal_dir=isolated_wal_dir)
    assert writer._get_wal_batch_writer() is None
