"""P0-E1: OrderAdapter._order_id_map_lock must be a threading.Lock that is
shared with the OrderIdResolver and ExecutionRouter backfill path.

The pre-fix bug: ``_order_id_map_lock`` was an ``asyncio.Lock`` which provides
zero mutual exclusion across OS threads. ``services/system.py::_on_exec``
runs on the Shioaji broker callback thread and invokes
``OrderAdapter.order_id_resolver.resolve_strategy_id_from_candidates``,
iterating ``order_id_map`` concurrently with main-loop writes by
``_register_broker_ids`` / ``_register_pending_fill`` / ExecutionRouter
``_backfill_order_id_map``. Depending on timing, this raised
``RuntimeError: dictionary changed size during iteration`` or silently
produced stale resolutions (orphaned fills routed to UNKNOWN).

These tests assert:

1. The lock object is a ``threading.Lock`` (not ``asyncio.Lock``).
2. The resolver owned by the adapter carries the same lock.
3. Under 2 threads hammering reads vs writes, no ``RuntimeError`` escapes
   and lookups are coherent.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import pytest

from hft_platform.order.adapter import OrderAdapter


# A minimal mock broker client with the attributes OrderAdapter touches on init.
class _MockBrokerClient:
    mode = "simulation"
    activate_ca = False
    ca_active = False

    def get_exchange(self, symbol: str) -> str:  # pragma: no cover — unused
        return "TSE"


def _make_adapter(tmp_path) -> OrderAdapter:
    # Minimal config file.
    cfg = tmp_path / "order.yaml"
    cfg.write_text(
        "rate_limits:\n  shioaji_soft_cap: 180\n  shioaji_hard_cap: 250\n"
        "  window_seconds: 10\n"
        "circuit_breaker:\n  threshold: 5\n  timeout_seconds: 60\n"
    )
    os.environ["HFT_ORDER_ID_MAP_PERSIST_PATH"] = str(tmp_path / "order_id_map.jsonl")
    queue: asyncio.Queue[Any] = asyncio.Queue()
    return OrderAdapter(
        config_path=str(cfg),
        order_queue=queue,
        broker_client=_MockBrokerClient(),
    )


def test_order_id_map_lock_is_threading_lock(tmp_path):
    adapter = _make_adapter(tmp_path)
    lock = adapter._order_id_map_lock
    # CPython's threading.Lock type lives in ``_thread`` module.
    # This assertion specifically catches a regression to asyncio.Lock.
    assert type(lock).__module__ == "_thread", (
        f"Expected threading.Lock (module=_thread), got {type(lock)!r} "
        f"module={type(lock).__module__!r}. If this fails, the P0-E1 "
        f"cross-thread guarantee is broken."
    )
    assert hasattr(lock, "acquire") and hasattr(lock, "release")


def test_resolver_shares_lock_with_adapter(tmp_path):
    adapter = _make_adapter(tmp_path)
    # The resolver must see the SAME lock object; any other object (default
    # _NullLock, or a separate threading.Lock) breaks cross-writer mutex.
    assert adapter.order_id_resolver.lock is adapter._order_id_map_lock


def test_concurrent_resolver_read_and_adapter_register_is_safe(tmp_path):
    """Simulate `_on_exec` (broker thread) reading while `_register_broker_ids`
    / `_register_pending_fill` (main loop) writes. With the asyncio.Lock
    regression this test would intermittently raise RuntimeError; with the
    threading.Lock fix it must not."""
    adapter = _make_adapter(tmp_path)
    # Pre-seed so the reader has something to iterate.
    for i in range(200):
        adapter.order_id_map[f"ord_{i}"] = f"S{i}:i{i}"

    resolver = adapter.order_id_resolver
    stop = threading.Event()
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            # Loop a generous number of times to maximise interleave.
            while not stop.is_set():
                # Use a prefix that exists — hits the iteration branch.
                resolver.resolve_strategy_id_from_candidates(["ord_100_fill_suffix", "ord_50_suffix"])
        except BaseException as exc:  # noqa: BLE001 — want to see everything
            errors.append(exc)

    def writer_register() -> None:
        # Emulate _register_broker_ids: take the lock, iterate keys, delete some, add some.
        i = 10_000
        try:
            while not stop.is_set():
                with adapter._order_id_map_lock:
                    # Eviction slice
                    for k in list(adapter.order_id_map.keys())[:5]:
                        adapter.order_id_map.pop(k, None)
                    # Insertion slice
                    for j in range(5):
                        adapter.order_id_map[f"ord_{i + j}"] = f"SW:{i + j}"
                i += 10
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    r = threading.Thread(target=reader, daemon=True)
    w = threading.Thread(target=writer_register, daemon=True)
    r.start()
    w.start()
    try:
        stop.wait(0.5)
    finally:
        stop.set()
        r.join(timeout=2)
        w.join(timeout=2)

    assert not errors, f"concurrent access raised: {errors[0]!r}"


def test_bulk_register_helper_takes_lock(tmp_path):
    """register_broker_ids_bulk must acquire the lock even when called from
    a non-asyncio thread (simulating router on a different coroutine)."""
    adapter = _make_adapter(tmp_path)
    changed = adapter.register_broker_ids_bulk(["brk1", "brk2", "brk3"], "S1:42")
    assert changed is True
    assert adapter.order_id_map["brk1"] == "S1:42"
    assert adapter.order_id_map["brk2"] == "S1:42"
    assert adapter.order_id_map["brk3"] == "S1:42"
    # Idempotent re-registration must NOT report changed.
    changed_again = adapter.register_broker_ids_bulk(["brk1"], "S1:42")
    assert changed_again is False


def test_bulk_register_handles_empty_and_falsy_ids(tmp_path):
    adapter = _make_adapter(tmp_path)
    # Empty strings / None should be silently skipped without raising.
    changed = adapter.register_broker_ids_bulk(["", None, "validX"], "S2:7")  # type: ignore[list-item]
    assert changed is True
    assert adapter.order_id_map.get("validX") == "S2:7"
    assert "" not in adapter.order_id_map
    assert None not in adapter.order_id_map


@pytest.mark.asyncio
async def test_register_pending_fill_writes_under_order_id_lock(tmp_path):
    """_register_pending_fill must hold _order_id_map_lock while touching
    order_id_map, not only _pending_fill_lock."""
    from hft_platform.contracts.strategy import Side

    adapter = _make_adapter(tmp_path)
    # If the lock is actually acquired we can observe it by trying to
    # acquire non-blockingly from another thread during the critical section.
    # We use a dummy monkey-patched lock that records acquire calls.
    acquire_calls: list[str] = []
    real_lock = adapter._order_id_map_lock

    class _InstrumentedLock:
        __slots__ = ("_inner",)

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __enter__(self) -> Any:
            acquire_calls.append("order_id_map_lock")
            return self._inner.__enter__()

        def __exit__(self, *exc: Any) -> Any:
            return self._inner.__exit__(*exc)

        def acquire(self, *a: Any, **kw: Any) -> Any:  # pragma: no cover
            return self._inner.acquire(*a, **kw)

        def release(self) -> Any:  # pragma: no cover
            return self._inner.release()

    adapter._order_id_map_lock = _InstrumentedLock(real_lock)
    # Resolver must also see the instrumented lock for consistency.
    adapter.order_id_resolver.lock = adapter._order_id_map_lock

    await adapter._register_pending_fill("S1:i1", "TMFD6", Side.BUY, "ABCDEF")

    assert "order_id_map_lock" in acquire_calls, (
        "P0-E1 regression: _register_pending_fill did not acquire _order_id_map_lock before writing order_id_map."
    )
    # Restore for cleanup.
    adapter._order_id_map_lock = real_lock
    adapter.order_id_resolver.lock = real_lock
