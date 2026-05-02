"""H5: OrderIdResolver prefix-iteration must tolerate concurrent mutation.

Root cause: `resolve_order_key_candidate` iterates `self.order_id_map.items()`
from the broker callback thread while the asyncio event loop concurrently
inserts/deletes entries (under a separate asyncio.Lock). CPython raises
``RuntimeError: dictionary changed size during iteration`` in this case,
or silently yields stale snapshots depending on timing. The observable
symptom is a fill routed to ``UNKNOWN`` strategy, and occasional
exceptions in `system._on_exec`.

Fix: iterate a snapshot (``tuple(items())``) so the broker thread cannot
observe a half-mutated dict.
"""

from __future__ import annotations

import threading

from hft_platform.core.order_ids import OrderIdResolver


def test_resolve_candidate_survives_concurrent_mutation():
    """Exercise a representative race. 1000 iterations balance determinism
    against signal — without the snapshot fix the reader thread almost
    always raises at least once on most machines."""
    order_map: dict[str, str] = {f"ord_{i}": f"S{i}:i{i}" for i in range(500)}
    resolver = OrderIdResolver(order_id_map=order_map)

    stop = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            while not stop.is_set():
                # Use a prefix that exists so the iteration path is
                # actually entered (not short-circuited by the direct get).
                resolver.resolve_order_key_candidate("ord_100_fill_suffix")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        i = 1000
        while not stop.is_set():
            key = f"ord_{i}"
            order_map[key] = f"SW:i{i}"
            i += 1
            if i % 17 == 0:
                # Random deletion to force dict resizing.
                victim = f"ord_{i - 500}"
                order_map.pop(victim, None)

    r = threading.Thread(target=reader, daemon=True)
    w = threading.Thread(target=writer, daemon=True)
    r.start()
    w.start()
    try:
        # Let them thrash briefly then stop.
        stop.wait(0.5)
    finally:
        stop.set()
        r.join(timeout=1)
        w.join(timeout=1)

    assert not errors, f"reader thread raised: {errors[0]!r}"
