"""D5: PositionStore.snapshot_positions() must be atomic under _fill_lock."""

from __future__ import annotations

import threading
import time


def test_snapshot_positions_exists():
    """PositionStore must expose a snapshot_positions() method."""
    from hft_platform.execution.positions import Position, PositionStore

    ps = PositionStore.__new__(PositionStore)
    pos_a = Position(account_id="A", strategy_id="S", symbol="a", net_qty=1)
    pos_b = Position(account_id="A", strategy_id="S", symbol="b", net_qty=2)
    ps.positions = {"a": pos_a, "b": pos_b}
    ps._fill_lock = threading.Lock()
    result = ps.snapshot_positions()
    assert result["a"].net_qty == 1
    assert result["b"].net_qty == 2
    assert result is not ps.positions


def test_snapshot_holds_lock_during_copy():
    """snapshot_positions must hold _fill_lock during dict copy."""

    from hft_platform.execution.positions import Position, PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps.positions = {f"k{i}": Position(account_id="A", strategy_id="S", symbol=f"k{i}", net_qty=i) for i in range(100)}

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
    from hft_platform.execution.positions import Position, PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps._fill_lock = threading.Lock()
    ps.positions = {f"k{i}": Position(account_id="A", strategy_id="S", symbol=f"k{i}", net_qty=0) for i in range(50)}

    errors: list[str] = []
    stop = threading.Event()

    def _mutator():
        gen = 1
        while not stop.is_set():
            with ps._fill_lock:
                for pos in ps.positions.values():
                    pos.net_qty = gen
            gen += 1
            time.sleep(0.0001)

    def _reader():
        for _ in range(200):
            snap = ps.snapshot_positions()
            vals = {p.net_qty for p in snap.values()}
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


def test_snapshot_deep_copies_position_objects():
    """Snapshot must return independent Position copies, not shared references.

    If snapshot_positions() returns shallow-copied references, a concurrent fill
    can mutate fields (net_qty, avg_price_scaled) on the same object while the
    checkpoint writer is serializing it — producing torn/inconsistent state.
    """
    from hft_platform.execution.positions import Position, PositionStore

    ps = PositionStore.__new__(PositionStore)
    ps._fill_lock = threading.Lock()
    orig = Position(account_id="A", strategy_id="S", symbol="2330", net_qty=10, avg_price_scaled=500_0000)
    ps.positions = {"A:S:2330": orig}

    snap = ps.snapshot_positions()
    snap_pos = snap["A:S:2330"]

    # Snapshot must be a DIFFERENT object
    assert snap_pos is not orig

    # Mutate original (simulating concurrent fill)
    orig.net_qty = 20
    orig.avg_price_scaled = 600_0000

    # Snapshot copy must be unaffected
    assert snap_pos.net_qty == 10
    assert snap_pos.avg_price_scaled == 500_0000
