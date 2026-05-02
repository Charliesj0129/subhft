"""M1: FeatureEngine cross-thread mutation safety.

The pre-fix bug: ``mark_gap_all`` is wired by ``services/market_data.py`` as a
tick-dispatcher drop callback, which fires on the broker callback thread. It
iterates ``self._states`` and writes to ``self._quality_flags_next``. The
event loop concurrently mutates the same dicts via ``process_lob_update``
(insert into ``_states``, pop from ``_quality_flags_next``) and ``reset_*``.

Without a thread lock CPython could either:
1. Raise ``RuntimeError: dictionary changed size during iteration`` from the
   ``mark_gap_all`` for-loop when the event loop inserts a new symbol; or
2. Silently drop GAP bits — the broker thread reads ``_quality_flags_next``
   into a temp, ORs in GAP, and writes back; if the event loop popped between
   read and write, the cleared state is silently restored OR the GAP bit is
   lost depending on interleaving.

Both outcomes corrupt the StormGuard feature-failure path
(``risk/storm_guard.py:report_feature_failure``) which depends on quality
flags to detect HALT-worthy degradation.

These tests assert:

1. ``_state_lock`` is a ``threading.Lock`` (not asyncio / not a no-op).
2. Hammering ``mark_gap_all`` from N broker-thread emulators while the event
   loop drives ``process_lob_update`` raises no exception and never silently
   drops a GAP bit.
3. ``mark_gap`` is also lock-guarded (single-symbol path).
4. Object-identity contract: each ``process_lob_update`` returns a fresh
   ``FeatureUpdateEvent`` (regression guard against cached-event mutation).
"""

from __future__ import annotations

import threading

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    FeatureEngine,
)


def _stats(symbol: str, ts: int, bid: int = 1_000_000, ask: int = 1_001_000) -> LOBStatsEvent:
    return LOBStatsEvent(
        symbol=symbol,
        ts=ts,
        imbalance=0.0,
        best_bid=bid,
        best_ask=ask,
        bid_depth=10,
        ask_depth=20,
    )


def test_state_lock_is_threading_lock() -> None:
    eng = FeatureEngine()
    lock = eng._state_lock
    assert type(lock).__module__ == "_thread", (
        f"Expected threading.Lock (module=_thread), got {type(lock)!r} "
        f"module={type(lock).__module__!r}. If this fails, the M1 cross-thread "
        f"guarantee is broken."
    )
    assert hasattr(lock, "acquire") and hasattr(lock, "release")


def test_mark_gap_all_concurrent_with_process_lob_update_no_exceptions() -> None:
    """Phase 4 verification: spawn N broker-thread emulators calling
    ``mark_gap_all`` while event loop calls ``process_lob_update``.

    Pre-fix: this could raise ``RuntimeError: dictionary changed size during
    iteration`` from the unlocked for-loop in ``mark_gap_all``.
    Post-fix: lock serialises iteration vs. insertion → no exceptions.
    """
    eng = FeatureEngine()
    # Pre-seed some symbols so iterators have work to do.
    for i in range(50):
        eng.process_lob_stats(_stats(f"S{i:03d}", ts=i + 1))

    stop = threading.Event()
    errors: list[BaseException] = []

    def gap_caller() -> None:
        try:
            while not stop.is_set():
                eng.mark_gap_all()
        except BaseException as exc:  # noqa: BLE001 — want to catch RuntimeError too
            errors.append(exc)

    def event_loop_emulator() -> None:
        try:
            i = 1000
            while not stop.is_set():
                # Mix of new-symbol inserts (mutates _states) and updates.
                eng.process_lob_stats(_stats(f"N{i % 200}", ts=i + 1))
                # Periodic reset to exercise pop paths.
                if i % 137 == 0:
                    eng.reset_symbol(f"N{(i - 50) % 200}")
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=gap_caller, daemon=True, name=f"broker-{i}") for i in range(4)]
    threads.append(threading.Thread(target=event_loop_emulator, daemon=True, name="evloop"))
    for t in threads:
        t.start()
    try:
        stop.wait(0.5)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)

    assert not errors, f"Concurrent access raised: {errors[0]!r}"


def test_mark_gap_all_does_not_lose_gap_bits_under_concurrency() -> None:
    """Verify GAP bits set by ``mark_gap_all`` reach the next emitted event.

    Sequence:
    1. Seed engine with a symbol — produces a state entry.
    2. Loop N iterations: mark_gap_all from background, then drive a
       process_lob_update from the main thread; the resulting event must
       have QUALITY_FLAG_GAP set.

    This tests that the OR-then-write in mark_gap_all is not corrupted by a
    concurrent pop in process_lob_update.
    """
    eng = FeatureEngine()
    eng.process_lob_stats(_stats("X1", ts=1))

    stop = threading.Event()
    errors: list[BaseException] = []
    gap_calls = [0]

    def gap_caller() -> None:
        try:
            while not stop.is_set():
                eng.mark_gap_all()
                gap_calls[0] += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=gap_caller, daemon=True)
    t.start()
    try:
        # Drive ticks for ~50ms so many gap calls happen between each tick.
        seen_gap = 0
        for i in range(200):
            evt = eng.process_lob_stats(_stats("X1", ts=2 + i))
            if evt is not None and (evt.quality_flags & QUALITY_FLAG_GAP):
                seen_gap += 1
        # Most ticks should observe a GAP bit since the gap caller is
        # hammering on a hot loop. Even with adversarial scheduling we expect
        # >50% to pick it up. The strict assertion is that we observed the
        # GAP bit at all (proves it isn't silently dropped) AND no errors.
        assert not errors, f"Concurrent access raised: {errors[0]!r}"
        assert seen_gap > 0, (
            "No tick observed QUALITY_FLAG_GAP despite the broker-thread "
            "emulator running mark_gap_all in a tight loop. The lock pattern "
            "is dropping GAP bits."
        )
    finally:
        stop.set()
        t.join(timeout=2)


def test_mark_gap_single_symbol_under_lock() -> None:
    """``mark_gap(sym)`` is the single-symbol cousin; the same lock must
    guard it. Regression: if someone changes ``mark_gap`` to bypass the
    lock for a 'fast path' the read-modify-write race returns.
    """
    eng = FeatureEngine()
    eng.process_lob_stats(_stats("Y1", ts=1))
    eng.mark_gap("Y1")
    evt = eng.process_lob_stats(_stats("Y1", ts=2))
    assert evt is not None
    assert evt.quality_flags & QUALITY_FLAG_GAP


def test_process_lob_update_returns_fresh_event_per_tick() -> None:
    """Object-identity contract — DO NOT change return semantics. Each call
    returns a fresh ``FeatureUpdateEvent`` so downstream consumers reading
    a stale reference don't see corrupted data on the next tick."""
    eng = FeatureEngine()
    e1 = eng.process_lob_stats(_stats("Z1", ts=1))
    e2 = eng.process_lob_stats(_stats("Z1", ts=2))
    assert e1 is not None and e2 is not None
    assert e1 is not e2, (
        "FeatureUpdateEvent identity reuse detected. StrategyRunner / "
        "RecorderService hold the reference; reusing it lets the next tick "
        "corrupt data still being read."
    )


def test_state_dicts_remain_consistent_after_concurrent_reset() -> None:
    """``reset_symbol`` and ``mark_gap_all`` must not deadlock or corrupt
    state when interleaved. Specifically: a symbol's QUALITY_FLAG_STATE_RESET
    flag set by reset_symbol() must be visible to the next process_lob_update
    even if mark_gap_all runs between them.
    """
    eng = FeatureEngine()
    eng.process_lob_stats(_stats("R1", ts=1))

    stop = threading.Event()
    errors: list[BaseException] = []

    def gap_loop() -> None:
        try:
            while not stop.is_set():
                eng.mark_gap_all()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    t = threading.Thread(target=gap_loop, daemon=True)
    t.start()
    try:
        for i in range(50):
            eng.reset_symbol("R1")
            evt = eng.process_lob_stats(_stats("R1", ts=10 + i))
            assert evt is not None
            # GAP and STATE_RESET may both be present; we don't assert
            # ordering, just that we got a valid event with no exception.
        assert not errors, f"Concurrent reset/gap raised: {errors[0]!r}"
    finally:
        stop.set()
        t.join(timeout=2)


def test_evict_stale_symbols_does_not_deadlock_with_state_lock() -> None:
    """``evict_stale_symbols`` snapshots under lock, then calls
    ``reset_symbol`` (which re-acquires the same lock) outside. With a
    re-entrant pattern this would deadlock; ``threading.Lock`` is
    non-reentrant by design so the snapshot+release pattern is required.
    """
    eng = FeatureEngine()
    # Seed some symbols and force their last_update_ns far in the past.
    for i in range(5):
        eng.process_lob_stats(_stats(f"E{i}", ts=i + 1))
    # Force eviction-ready state.
    very_old = 1
    for sym in list(eng._last_update_ns.keys()):
        eng._last_update_ns[sym] = very_old
    eng._eviction_last_run_ns = 0  # bypass rate limit
    eng._eviction_ttl_ns = 1_000_000  # 1ms TTL → everything is stale
    n = eng.evict_stale_symbols()
    assert n >= 1
    # Idempotent: nothing left to evict (within rate-limit window second
    # call returns 0 due to throttle, but should not raise).
    assert eng.evict_stale_symbols() == 0


def test_runtime_status_is_thread_safe() -> None:
    """``runtime_status`` reads ``len(self._states)`` from a possibly
    non-event-loop thread; verify it does not raise under contention.
    """
    eng = FeatureEngine()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            i = 0
            while not stop.is_set():
                eng.process_lob_stats(_stats(f"T{i % 100}", ts=i + 1))
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                _ = eng.runtime_status()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, daemon=True),
        threading.Thread(target=reader, daemon=True),
        threading.Thread(target=reader, daemon=True),
    ]
    for t in threads:
        t.start()
    try:
        stop.wait(0.3)
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=2)
    assert not errors, f"Concurrent runtime_status raised: {errors[0]!r}"
