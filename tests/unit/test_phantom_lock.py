"""P0-E2: _phantom_order_keys / _phantom_intents must be serialised by
``_phantom_lock`` so the over-capacity eviction cannot raise
``RuntimeError: dictionary changed size during iteration`` when a peer
task (``resolve_phantom_fill``, ``clear_phantom_candidate``,
``release_stale_phantom_pendings``) mutates the dict concurrently.

Pre-fix code:

    self._phantom_order_keys[phantom_key] = (time.monotonic(), ...)
    if len(self._phantom_order_keys) > self._phantom_order_max:
        cutoff = time.monotonic() - 3600.0
        _stale = [k for k, v in self._phantom_order_keys.items() if v[0] <= cutoff]
        for _sk in _stale:
            del self._phantom_order_keys[_sk]

The list comprehension iterates the live dict. If ``clear_phantom_candidate``
pops a key on the main loop between coroutine steps (same loop,
interleaved task), CPython would in general raise. Even if it didn't,
``del self._phantom_order_keys[_sk]`` on a key already popped raises
``KeyError``. Both failure modes are fixed by the lock.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.order.adapter import OrderAdapter


class _MockBrokerClient:
    mode = "simulation"
    activate_ca = False
    ca_active = False

    def get_exchange(self, symbol: str) -> str:  # pragma: no cover — unused
        return "TSE"


def _make_adapter(tmp_path) -> OrderAdapter:
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


def _intent(strategy_id: str, intent_id: int, symbol: str = "TMFD6") -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=500_0000,
        qty=1,
        tif=TIF.ROD,
    )


def test_phantom_lock_is_threading_lock(tmp_path):
    adapter = _make_adapter(tmp_path)
    lock = adapter._phantom_lock
    assert type(lock).__module__ == "_thread"


def test_phantom_insert_and_capacity_evict_under_concurrent_clear(tmp_path):
    """Simulate the race: writer inserts new phantoms + triggers capacity
    eviction; peer thread concurrently calls clear_phantom_candidate on
    random keys. Without the lock, the eviction's list-comprehension or
    ``del`` could raise. Must complete without exception."""
    adapter = _make_adapter(tmp_path)
    # Shrink the bound so eviction fires frequently.
    adapter._phantom_order_max = 50

    stop = threading.Event()
    errors: list[BaseException] = []

    # Pre-seed with some entries so clearer has targets.
    import time as _time

    old_ts = _time.monotonic() - 7200.0  # stale — eligible for eviction
    with adapter._phantom_lock:
        for i in range(40):
            adapter._phantom_order_keys[f"Spre:{i}"] = (old_ts, "TMFD6")
            adapter._phantom_intents[f"Spre:{i}"] = _intent("Spre", i)

    def writer() -> None:
        try:
            i = 0
            while not stop.is_set():
                intent = _intent("Swrite", i)
                # Directly simulate the _call_api phantom-insert path.
                phantom_key = f"{intent.strategy_id}:{intent.intent_id}"
                with adapter._phantom_lock:
                    adapter._phantom_order_keys[phantom_key] = (
                        _time.monotonic(),
                        intent.symbol,
                    )
                    adapter._phantom_intents[phantom_key] = intent
                    if len(adapter._phantom_order_keys) > adapter._phantom_order_max:
                        cutoff = _time.monotonic() - 3600.0
                        _stale = [
                            k
                            for k, v in adapter._phantom_order_keys.items()
                            if v[0] <= cutoff
                        ]
                        for _sk in _stale:
                            adapter._phantom_order_keys.pop(_sk, None)
                            adapter._phantom_intents.pop(_sk, None)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def clearer() -> None:
        try:
            i = 0
            while not stop.is_set():
                adapter.clear_phantom_candidate(f"Spre:{i % 40}")
                adapter.clear_phantom_candidate(f"Swrite:{i}")
                adapter.get_phantom_candidates()  # snapshot read
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    w = threading.Thread(target=writer, daemon=True)
    c = threading.Thread(target=clearer, daemon=True)
    w.start()
    c.start()
    try:
        stop.wait(0.5)
    finally:
        stop.set()
        w.join(timeout=2)
        c.join(timeout=2)

    assert not errors, f"concurrent phantom access raised: {errors[0]!r}"


@pytest.mark.asyncio
async def test_release_stale_phantoms_is_reentry_safe(tmp_path):
    """release_stale_phantom_pendings takes the lock, drains expired entries
    into a local buffer, then emits feedback outside the lock. Running this
    twice concurrently on the same loop must not double-release or raise."""
    adapter = _make_adapter(tmp_path)
    import time as _time

    # Seed an expired phantom.
    with adapter._phantom_lock:
        adapter._phantom_order_keys["SX:1"] = (_time.monotonic() - 1000.0, "TMFD6")
        adapter._phantom_intents["SX:1"] = _intent("SX", 1)

    # Install a no-op rejection sink so _send_dispatch_rejection works.
    adapter._rejection_sink = asyncio.Queue()

    r1_task = asyncio.create_task(adapter.release_stale_phantom_pendings(ttl_s=0.5))
    r2_task = asyncio.create_task(adapter.release_stale_phantom_pendings(ttl_s=0.5))
    r1, r2 = await asyncio.gather(r1_task, r2_task)

    # At most one of the two concurrent invocations should report the release.
    # The other must see an empty phantom dict (entry already popped under lock).
    assert (r1, r2) in {(1, 0), (0, 1)}, (
        f"expected single release, got r1={r1} r2={r2}"
    )
    assert "SX:1" not in adapter._phantom_order_keys
    assert "SX:1" not in adapter._phantom_intents


def test_get_phantom_candidates_snapshot_is_consistent(tmp_path):
    """get_phantom_candidates must return a frozenset — that's a CPython
    atomic construction when the dict is not mutating, but under concurrent
    mutation we need the lock to avoid RuntimeError during iteration."""
    adapter = _make_adapter(tmp_path)
    import time as _time

    stop = threading.Event()
    errors: list[BaseException] = []

    def mutator() -> None:
        try:
            i = 0
            while not stop.is_set():
                with adapter._phantom_lock:
                    adapter._phantom_order_keys[f"M:{i}"] = (_time.monotonic(), "X")
                    if len(adapter._phantom_order_keys) > 100:
                        # Drop half
                        for k in list(adapter._phantom_order_keys.keys())[:50]:
                            adapter._phantom_order_keys.pop(k, None)
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                _ = adapter.get_phantom_candidates()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    m = threading.Thread(target=mutator, daemon=True)
    r = threading.Thread(target=reader, daemon=True)
    m.start()
    r.start()
    try:
        stop.wait(0.3)
    finally:
        stop.set()
        m.join(timeout=2)
        r.join(timeout=2)

    assert not errors, f"concurrent phantom read raised: {errors[0]!r}"
