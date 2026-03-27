"""D5: PositionStore.snapshot_positions() must be atomic under _fill_lock."""

from __future__ import annotations

import threading
import time


def test_snapshot_positions_exists():
    """PositionStore must expose a snapshot_positions() method."""
    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps.positions = {"a": 1, "b": 2}
    ps._fill_lock = threading.Lock()
    result = ps.snapshot_positions()
    assert result == {"a": 1, "b": 2}
    assert result is not ps.positions


def test_snapshot_holds_lock_during_copy():
    """snapshot_positions must hold _fill_lock during dict copy."""

    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps.positions = {f"k{i}": i for i in range(100)}

    real_lock = threading.Lock()
    acquired_calls: list[bool] = []

    # Use a context-manager-compatible wrapper to spy on lock usage
    class SpyLock:
        def __enter__(self):
            acquired_calls.append(True)
            real_lock.acquire()
            return self

        def __exit__(self, *args):
            real_lock.release()

        def acquire(self, *a, **kw):
            acquired_calls.append(True)
            return real_lock.acquire(*a, **kw)

        def release(self):
            return real_lock.release()

    ps._fill_lock = SpyLock()
    ps.snapshot_positions()
    assert acquired_calls, "Lock was not acquired during snapshot"


def test_snapshot_returns_consistent_view():
    """Concurrent fills must not produce a torn snapshot."""
    from hft_platform.execution.positions import PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps._fill_lock = threading.Lock()
    ps.positions = {f"k{i}": 0 for i in range(50)}

    errors: list[str] = []
    stop = threading.Event()

    def _mutator():
        gen = 1
        while not stop.is_set():
            with ps._fill_lock:
                for k in ps.positions:
                    ps.positions[k] = gen
            gen += 1
            time.sleep(0.0001)

    def _reader():
        for _ in range(200):
            snap = ps.snapshot_positions()
            vals = set(snap.values())
            if len(vals) > 1:
                errors.append(f"Torn snapshot: {vals}")
            time.sleep(0.0001)

    t1 = threading.Thread(target=_mutator)
    t2 = threading.Thread(target=_reader)
    t1.start()
    t2.start()
    t2.join(timeout=5.0)
    stop.set()
    t1.join(timeout=2.0)

    assert not errors, f"Torn snapshots detected: {errors[:5]}"
