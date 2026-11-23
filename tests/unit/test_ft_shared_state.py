"""Concurrency regression tests for the G0 shared-state sites (free-threaded CPython 3.14t).

Each test drives one previously GIL-dependent site from several threads released together by a
barrier, with the interpreter switch interval cut to one microsecond so threads interleave as
often as possible. The functions use no pytest fixtures and no pytest import, so the same file
also runs under a plain stdlib runner on the free-threaded interpreter, where pytest is absent.

Sites covered: MarketDataNormalizer._get_scale, RingBufferBus._notify and the multi-producer
publish paths, FeatureEngine update sequence numbers, RiskEngine._next_cmd_id,
LOBEngine.get_book, and the MarketDataService per-symbol activity/error counters.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock, patch

_THREADS = 8
_JOIN_TIMEOUT_S = 60.0
_WAKE_TIMEOUT_S = 5.0


@contextmanager
def _fast_thread_switching() -> Iterator[None]:
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        yield
    finally:
        sys.setswitchinterval(previous)


def _hammer(worker: Callable[[int], None], threads: int = _THREADS) -> list[Exception]:
    """Run worker(index) on N threads released together; return the exceptions they raised."""
    barrier = threading.Barrier(threads)
    errors: list[Exception] = []

    def _run(index: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            worker(index)
        except Exception as exc:  # collected and asserted by the calling test
            errors.append(exc)

    pool = [threading.Thread(target=_run, args=(i,), daemon=True) for i in range(threads)]
    with _fast_thread_switching():
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join(timeout=_JOIN_TIMEOUT_S)
    assert not any(thread.is_alive() for thread in pool), "worker thread did not finish"
    return errors


# ---------------------------------------------------------------------------
# Site 1: MarketDataNormalizer._get_scale
# ---------------------------------------------------------------------------

_SCALES = {"S0": 100, "S1": 1_000, "S2": 100_000, "S3": 1_000_000, "ZERO": 0}


class _ScaleMetadata:
    """Symbol metadata whose price scale differs per symbol and that counts lookups."""

    DEFAULT_SCALE = 10_000

    def __init__(self) -> None:
        self.lookups = 0

    def price_scale(self, symbol: str) -> int:
        self.lookups += 1
        return _SCALES.get(symbol, self.DEFAULT_SCALE)


def _make_normalizer(metadata: Any) -> Any:
    with (
        patch("hft_platform.feed_adapter.normalizer.MetricsRegistry") as registry,
        patch("hft_platform.feed_adapter.normalizer._HAS_FUSED", False),
    ):
        registry.get.return_value = None
        from hft_platform.feed_adapter.normalizer import MarketDataNormalizer

        return MarketDataNormalizer(metadata=metadata)


def test_get_scale_never_returns_another_symbols_scale_under_thread_contention():
    norm = _make_normalizer(_ScaleMetadata())
    symbols = ("S0", "S1", "S2", "S3")
    wrong: list[tuple[str, int]] = []

    def worker(index: int) -> None:
        for i in range(4_000):
            symbol = symbols[(i + index) % len(symbols)]
            scale = norm._get_scale(symbol)
            if scale != _SCALES[symbol]:
                wrong.append((symbol, scale))

    errors = _hammer(worker)

    assert not errors
    assert not wrong, f"{len(wrong)} lookups returned a scale that belongs to another symbol, e.g. {wrong[:3]}"


def test_get_scale_serves_a_repeated_symbol_from_cache_single_thread():
    metadata = _ScaleMetadata()
    norm = _make_normalizer(metadata)

    first = norm._get_scale("S1")
    second = norm._get_scale("S1")

    assert first == second == _SCALES["S1"]
    assert metadata.lookups == 1


def test_get_scale_clamps_non_positive_scale_to_one_single_thread():
    norm = _make_normalizer(_ScaleMetadata())

    assert norm._get_scale("ZERO") == 1
    assert norm._get_scale("ZERO") == 1


# ---------------------------------------------------------------------------
# Site 2: RingBufferBus._notify and multi-producer publish
# ---------------------------------------------------------------------------


def _make_bus(*, single_writer: bool, size: int = 1 << 14) -> Any:
    with (
        patch("hft_platform.engine.event_bus._WAIT_MODE", "event"),
        patch("hft_platform.engine.event_bus._RUST_ENABLED", False),
    ):
        from hft_platform.engine.event_bus import RingBufferBus

        bus = RingBufferBus(size=size)
    bus.single_writer = single_writer
    return bus


async def _stop(task: asyncio.Task) -> None:
    """Cancel without awaiting the task itself: a task whose wakeup was lost would never finish."""
    task.cancel()
    await asyncio.wait({task}, timeout=1.0)


def _run_guarded(factory: Callable[[], Any], *, deadline_s: float = 20.0, debug: bool = False) -> Any:
    """Run an event loop on a daemon thread and fail, rather than hang, if the coroutine never ends.

    A lost wakeup leaves a consumer task that can never be cancelled cleanly, which would also hang
    asyncio.run() during its own cleanup; the daemon thread keeps such a regression a plain failure.
    """
    finished = threading.Event()
    box: dict[str, Any] = {}

    async def wrapper() -> None:
        try:
            box["value"] = await factory()
        finally:
            finished.set()

    def runner() -> None:
        try:
            asyncio.run(wrapper(), debug=debug)
        except Exception as exc:  # reported by the assert below
            box["error"] = exc

    thread = threading.Thread(target=runner, name="ft-loop", daemon=True)
    thread.start()
    assert finished.wait(deadline_s), "the event loop coroutine did not finish (lost wakeup?)"
    thread.join(timeout=5.0)
    assert "error" not in box, f"event loop raised: {box.get('error')!r}"
    return box["value"]


def _consume_from_producer_threads(kind: str) -> tuple[list[Exception], list[Any], int]:
    """Park a consumer, publish from N plain threads, return (producer errors, received, expected)."""
    per_thread = 150
    expected = _THREADS * per_thread
    bus = _make_bus(single_writer=False)
    received: list[Any] = []
    errors: list[Exception] = []

    async def main() -> None:
        done = asyncio.Event()

        async def consumer() -> None:
            if kind == "batch":
                async for batch in bus.consume_batch(16, start_cursor=-1, consumer_name="ft-batch"):
                    received.extend(batch)
                    if len(received) >= expected:
                        done.set()
                        return
            else:
                async for event in bus.consume(start_cursor=-1, consumer_name="ft-single"):
                    received.append(event)
                    if len(received) >= expected:
                        done.set()
                        return

        task = asyncio.create_task(consumer())
        while not bus._consumer_signals:  # the consumer registers its signal, then parks in the same step
            await asyncio.sleep(0)

        def produce(index: int) -> None:
            for i in range(per_thread):
                bus.publish_nowait((index, i))

        producers = threading.Thread(target=lambda: errors.extend(_hammer(produce)), daemon=True)
        producers.start()
        try:
            await asyncio.wait_for(done.wait(), timeout=_WAKE_TIMEOUT_S)
        except TimeoutError:
            pass
        await _stop(task)
        producers.join(timeout=_JOIN_TIMEOUT_S)

    # Debug mode makes a loop call made from a foreign thread without call_soon_threadsafe raise.
    _run_guarded(main, debug=True)
    return errors, received, expected


def test_notify_from_producer_threads_wakes_single_consumer_without_loop_error():
    errors, received, expected = _consume_from_producer_threads("single")

    assert not errors, f"producer thread raised: {errors[:2]}"
    assert len(received) == expected
    assert len(set(received)) == expected


def test_notify_from_producer_threads_wakes_batch_consumer_without_loop_error():
    errors, received, expected = _consume_from_producer_threads("batch")

    assert not errors, f"producer thread raised: {errors[:2]}"
    assert len(received) == expected
    assert len(set(received)) == expected


def test_publish_nowait_from_producer_thread_wakes_idle_loop_promptly():
    bus = _make_bus(single_writer=False)

    async def main() -> tuple[float, bool]:
        got = asyncio.Event()

        async def consumer() -> None:
            async for _event in bus.consume(start_cursor=-1, consumer_name="ft-idle"):
                got.set()
                return

        task = asyncio.create_task(consumer())
        while not bus._consumer_signals:
            await asyncio.sleep(0)

        def produce() -> None:
            # 30 ms (< 50 ms ceiling) lets the loop thread finish this step and block in select();
            # without it the publish can land before the loop idles and hide a lost wakeup.
            time.sleep(0.03)
            bus.publish_nowait(("evt", 1))

        started = time.monotonic()
        thread = threading.Thread(target=produce, daemon=True)
        thread.start()
        try:
            # The 3 s timer below is the only thing that can wake an idle loop that was not told
            # to wake; a prompt arrival therefore proves the notify crossed threads correctly.
            await asyncio.wait_for(got.wait(), timeout=3.0)
        except TimeoutError:
            pass
        elapsed = time.monotonic() - started
        thread.join(timeout=_JOIN_TIMEOUT_S)
        await _stop(task)
        return elapsed, got.is_set()

    elapsed, delivered = _run_guarded(main)

    assert delivered
    assert elapsed < 1.0, f"consumer woke {elapsed:.2f}s after a cross-thread publish"


def test_publish_nowait_on_loop_thread_sets_consumer_signal_synchronously():
    bus = _make_bus(single_writer=True)

    async def main() -> bool:
        task = asyncio.create_task(_first_event(bus))
        while not bus._consumer_signals:
            await asyncio.sleep(0)
        bus.publish_nowait(("evt", 1))
        flagged = bus._consumer_signals[0].is_set()  # direct path: no loop hop needed
        await asyncio.wait_for(task, timeout=_WAKE_TIMEOUT_S)
        return flagged

    assert _run_guarded(main) is True


async def _first_event(bus: Any) -> Any:
    async for event in bus.consume(start_cursor=-1, consumer_name="ft-first"):
        return event
    return None


def test_publish_nowait_assigns_distinct_slots_across_producer_threads():
    per_thread = 1_000
    total = _THREADS * per_thread
    bus = _make_bus(single_writer=False, size=1 << 14)

    def worker(index: int) -> None:
        if index % 2 == 0:
            for i in range(per_thread):
                bus.publish_nowait((index, i))
        else:
            for start in range(0, per_thread, 10):
                bus.publish_many_nowait([(index, i) for i in range(start, start + 10)])

    errors = _hammer(worker)

    assert not errors
    assert bus.cursor == total - 1
    stored = [bus.buffer[i] for i in range(total)]
    assert None not in stored
    assert len(set(stored)) == total, "two producers claimed the same sequence number"


def test_async_publish_and_thread_publish_nowait_never_share_a_sequence_number():
    per_thread = 600
    threads = 4
    total = (threads + 1) * per_thread
    bus = _make_bus(single_writer=False, size=1 << 14)

    async def main() -> list[Exception]:
        errors: list[Exception] = []

        def produce(index: int) -> None:
            for i in range(per_thread):
                bus.publish_nowait((index, i))

        pool = threading.Thread(target=lambda: errors.extend(_hammer(produce, threads)), daemon=True)
        pool.start()
        for i in range(per_thread):
            await bus.publish(("loop", i))
            await asyncio.sleep(0)
        await asyncio.get_running_loop().run_in_executor(None, pool.join, _JOIN_TIMEOUT_S)
        return errors

    errors = _run_guarded(main)

    assert not errors
    assert bus.cursor == total - 1
    stored = [bus.buffer[i] for i in range(total)]
    assert None not in stored
    assert len(set(stored)) == total, "the loop publisher and a producer thread claimed one sequence number"


# ---------------------------------------------------------------------------
# Site 3a: FeatureEngine update sequence
# ---------------------------------------------------------------------------


def test_feature_update_seq_is_unique_and_monotonic_per_thread():
    from hft_platform.events import LOBStatsEvent
    from hft_platform.feature.engine import FeatureEngine

    engine = FeatureEngine(emit_events=True)
    per_thread = 300
    seqs: list[list[int]] = [[] for _ in range(_THREADS)]

    def worker(index: int) -> None:
        symbol = f"FT{index}"  # one symbol per thread: the sharded-worker contract
        out = seqs[index]
        for i in range(1, per_thread + 1):
            stats = LOBStatsEvent(
                symbol=symbol,
                ts=i,
                imbalance=0.0,
                best_bid=1_000_000,
                best_ask=1_001_000,
                bid_depth=10,
                ask_depth=20,
            )
            update = engine.process_lob_stats(stats, local_ts_ns=i)
            out.append(update.seq)

    errors = _hammer(worker)

    assert not errors
    flat = [s for out in seqs for s in out]
    assert len(flat) == _THREADS * per_thread
    assert len(set(flat)) == len(flat), "two feature updates were stamped with the same seq"
    assert all(out == sorted(out) for out in seqs)


# ---------------------------------------------------------------------------
# Site 3b: RiskEngine._next_cmd_id
# ---------------------------------------------------------------------------


def _bare_risk_engine(lock: Any = None) -> Any:
    from hft_platform.risk.engine import RiskEngine

    engine = RiskEngine.__new__(RiskEngine)
    engine._cmd_id_lock_enabled = lock is not None
    engine._cmd_id_lock = lock
    engine._monotonic_cmd_id = 0
    return engine


def _next_cmd_ids_from_threads(engine: Any, per_thread: int) -> list[list[int]]:
    ids: list[list[int]] = [[] for _ in range(_THREADS)]

    def worker(index: int) -> None:
        out = ids[index]
        for _ in range(per_thread):
            out.append(engine._next_cmd_id())

    assert not _hammer(worker)
    return ids


def test_next_cmd_id_is_unique_and_monotonic_per_thread_without_opt_in_lock():
    engine = _bare_risk_engine()
    per_thread = 4_000

    ids = _next_cmd_ids_from_threads(engine, per_thread)

    flat = [i for out in ids for i in out]
    assert len(set(flat)) == _THREADS * per_thread
    assert all(out == sorted(out) for out in ids)
    assert engine.monotonic_cmd_id == _THREADS * per_thread


def test_next_cmd_id_is_unique_with_opt_in_lock():
    engine = _bare_risk_engine(lock=threading.Lock())
    per_thread = 2_000

    ids = _next_cmd_ids_from_threads(engine, per_thread)

    flat = [i for out in ids for i in out]
    assert len(set(flat)) == _THREADS * per_thread
    assert engine.monotonic_cmd_id == _THREADS * per_thread


def test_next_cmd_id_starts_at_one_and_counts_up_single_thread():
    engine = _bare_risk_engine()

    assert [engine._next_cmd_id() for _ in range(3)] == [1, 2, 3]
    assert engine.monotonic_cmd_id == 3


# ---------------------------------------------------------------------------
# Site 4: LOBEngine.get_book
# ---------------------------------------------------------------------------


class _SlowSymbolMetadata:
    """Symbol metadata whose lookup yields the CPU, so callers overlap inside check-then-create."""

    def __init__(self, symbols: list[str]) -> None:
        self._meta = {symbol: {} for symbol in symbols}

    @property
    def meta(self) -> dict[str, dict]:
        time.sleep(0.0005)  # widens the race window on purpose; far below the 50 ms ceiling
        return self._meta


def _make_lob() -> Any:
    from hft_platform.feed_adapter.lob_engine import LOBEngine

    return LOBEngine()


def test_get_book_creates_one_book_per_symbol_under_thread_contention():
    engine = _make_lob()
    symbols = [f"FTB{i:03d}" for i in range(24)]
    engine._symbol_metadata = _SlowSymbolMetadata(symbols)
    returned: list[list[Any]] = [[] for _ in range(_THREADS)]

    def worker(index: int) -> None:
        for symbol in symbols:
            returned[index].append((symbol, engine.get_book(symbol)))

    errors = _hammer(worker)

    assert not errors
    assert len(engine.books) == len(symbols)
    for out in returned:
        for symbol, book in out:
            assert book is engine.books[symbol], f"{symbol}: a caller was handed a BookState that was discarded"


def test_get_book_never_exceeds_symbol_cap_under_thread_contention():
    from hft_platform.feed_adapter.lob_engine import BookState

    class _SlowBookState(BookState):
        def __init__(self, symbol: str) -> None:
            time.sleep(0.0005)  # sits between the cardinality check and the insert
            super().__init__(symbol)

    engine = _make_lob()
    cap = 10
    symbols = [f"FTC{i:03d}" for i in range(40)]
    engine._max_symbols = cap

    def worker(index: int) -> None:
        # Staggered start: at any moment the threads are creating different symbols, so only a
        # check-and-insert that is atomic as a whole can hold the cap.
        for k in range(len(symbols)):
            engine.get_book(symbols[(k + index * 5) % len(symbols)])

    with (
        patch("hft_platform.feed_adapter.lob_engine.BookState", _SlowBookState),
        patch("hft_platform.feed_adapter.lob_engine.logger"),
    ):
        errors = _hammer(worker)

    assert not errors
    assert len(engine.books) == cap


def test_get_book_last_book_cache_never_returns_another_symbols_book():
    engine = _make_lob()
    symbols = ["FTD0", "FTD1", "FTD2", "FTD3"]
    for symbol in symbols:
        assert engine.get_book(symbol) is not None
    wrong: list[tuple[str, str]] = []

    def worker(index: int) -> None:
        for i in range(4_000):
            symbol = symbols[(i + index) % len(symbols)]
            book = engine.get_book(symbol)
            if book is None or book.symbol != symbol:
                wrong.append((symbol, getattr(book, "symbol", "None")))

    errors = _hammer(worker)

    assert not errors
    assert not wrong, f"{len(wrong)} lookups were handed another symbol's book, e.g. {wrong[:3]}"


def test_get_book_returns_the_same_book_on_repeat_single_thread():
    engine = _make_lob()

    first = engine.get_book("FTE0")
    second = engine.get_book("FTE0")

    assert first is not None
    assert second is first
    assert engine._last_symbol == "FTE0"


# ---------------------------------------------------------------------------
# Site 5: MarketDataService per-symbol activity and error counters
# ---------------------------------------------------------------------------


def _make_market_data_service() -> Any:
    with (
        patch("hft_platform.services.market_data.ShmSnapshotWriter"),
        patch("hft_platform.services.market_data.MarketDataNormalizer"),
        patch("hft_platform.services.market_data.LOBEngine") as lob_cls,
        patch("hft_platform.services.market_data.FeatureEngine") as feature_cls,
        patch("hft_platform.services.market_data.MetricsRegistry") as registry,
        patch("hft_platform.services.market_data.LatencyRecorder") as latency,
    ):
        lob_cls.return_value = MagicMock()
        feature_cls.return_value = MagicMock()
        registry.get.return_value = MagicMock()
        latency.get.return_value = MagicMock()
        from hft_platform.services.market_data import MarketDataService

        svc = MarketDataService(
            bus=MagicMock(),
            raw_queue=asyncio.Queue(),
            client=MagicMock(),
            recorder_queue=asyncio.Queue(),
            feature_engine=MagicMock(),
        )
    svc._symbol_tick_inline = True
    return svc


def test_symbol_activity_counts_every_event_once_under_thread_contention():
    svc = _make_market_data_service()
    baseline = svc._ACTIVE_BASELINE_EVENT_COUNT
    delivering = baseline - 1  # one short of the latch, split across threads
    threads = delivering

    for round_no in range(6):
        symbols = [f"R{round_no}S{i:03d}" for i in range(150)]
        events = [SimpleNamespace(symbol=symbol) for symbol in symbols]

        def worker(_index: int, events: list = events) -> None:
            for event in events:
                svc._update_symbol_tick_inline(event)

        assert not _hammer(worker, threads)

        for symbol in symbols:
            assert svc._event_counts.get(symbol) == delivering, f"{symbol}: an event count was lost"
            assert symbol not in svc._ever_active_symbols
        for event in events:
            svc._update_symbol_tick_inline(event)
        for symbol in symbols:
            assert symbol in svc._ever_active_symbols
            assert symbol not in svc._event_counts


def test_symbol_activity_latch_keeps_counts_reclaimed_for_latched_symbols():
    svc = _make_market_data_service()
    symbols = [f"L{i:02d}" for i in range(20)]
    events = [SimpleNamespace(symbol=symbol) for symbol in symbols]
    for _ in range(svc._ACTIVE_BASELINE_EVENT_COUNT):
        for event in events:
            svc._update_symbol_tick_inline(event)
    assert set(symbols) <= svc._ever_active_symbols

    def worker(_index: int) -> None:
        for _ in range(300):
            for event in events:
                svc._update_symbol_tick_inline(event)

    assert not _hammer(worker)

    assert svc._event_counts == {}
    assert set(symbols) <= svc._ever_active_symbols
    assert set(symbols) <= set(svc._symbol_last_tick)


class _CountingLock:
    def __init__(self) -> None:
        self._inner = threading.Lock()
        self.acquisitions = 0

    def __enter__(self) -> bool:
        self._inner.acquire()
        self.acquisitions += 1
        return True

    def __exit__(self, *_exc: object) -> None:
        self._inner.release()


def test_symbol_activity_common_path_takes_no_lock_once_latched():
    svc = _make_market_data_service()
    counting = _CountingLock()
    svc._shared_state_lock = counting
    event = SimpleNamespace(symbol="LATCHED")
    for _ in range(svc._ACTIVE_BASELINE_EVENT_COUNT):
        svc._update_symbol_tick_inline(event)
    assert "LATCHED" in svc._ever_active_symbols
    assert counting.acquisitions >= 1  # the rare, pre-latch path is the one that locks
    acquired_before = counting.acquisitions

    for _ in range(1_000):
        svc._update_symbol_tick_inline(event)

    assert counting.acquisitions == acquired_before


def test_process_raw_error_count_is_exact_under_thread_contention():
    from hft_platform.events import MetaData, TickEvent

    svc = _make_market_data_service()
    svc.metrics_registry = None
    svc.latency = None
    svc._record_direct = False
    svc.lob = MagicMock()
    svc.lob.process_event.side_effect = RuntimeError("lob failed")
    tick = TickEvent(meta=MetaData(seq=1, source_ts=0, local_ts=0), symbol="2330", price=1_000_000, volume=1)
    svc.normalizer = MagicMock()
    svc.normalizer.normalize_tick.return_value = tick
    per_thread = 300

    def worker(_index: int) -> None:
        for _ in range(per_thread):
            svc._process_raw({"code": "2330", "close": 100.0, "volume": 1})

    with patch("hft_platform.services.market_data.logger"):
        errors = _hammer(worker)

    assert not errors
    assert svc._process_raw_error_count == _THREADS * per_thread
