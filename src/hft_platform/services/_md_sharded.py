"""Symbol-sharded market-data worker threads (``HFT_MD_WORKERS``).

With ``HFT_MD_WORKERS=0`` (default) nothing here is constructed and the service keeps its single-loop
path: broker callback -> ``call_soon_threadsafe(_enqueue_raw)`` -> raw ``asyncio.Queue`` -> ``run()``
-> ``_process_raw`` on the event loop.

With N >= 1 the chain is::

    broker thread --offer()--> ShardRouter: crc32(symbol) % N
        -> worker inbox (bounded deque, drop + count on overflow)
        -> worker thread: normalize -> LOB -> feature -> L1/feature snapshot
        -> worker outbox (SPSC deque) -> one call_soon_threadsafe per batch
        -> loop drain (<= HFT_MD_COMMIT_BUDGET per turn, then call_soon yield)
        -> commit: committed view swap, feed-gap stamp, recorder, bus publish, redis

A symbol always lands on the same worker and each outbox is FIFO, so per-symbol order is kept;
cross-symbol order is not. Only the loop publishes to the bus, so ``single_writer`` stays on.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.events import BidAskEvent, TickEvent

logger = get_logger("service.market_data.sharded")

_PAUSE = object()

# Result tuple layout produced by the worker compute stage and held by the committed view.
R_SYMBOL = 0
R_EVENT = 1
R_STATS = 2
R_FEATURE_UPDATE = 3
R_L1 = 4
R_FEATURES = 5


class MDShardQuiesceTimeoutError(RuntimeError):
    """A worker did not reach the pause barrier in time."""


class MDWorkerCrashedError(RuntimeError):
    """A market-data worker thread died; the service fails closed."""


def md_workers_from_env() -> int:
    try:
        return max(0, int(os.getenv("HFT_MD_WORKERS", "0")))
    except ValueError:
        logger.error("md_workers_env_invalid", value=os.getenv("HFT_MD_WORKERS"))
        return 0


def md_commit_budget_from_env() -> int:
    try:
        return max(1, int(os.getenv("HFT_MD_COMMIT_BUDGET", "256")))
    except ValueError:
        return 256


def default_symbol_of(raw: Any) -> str:
    if isinstance(raw, dict):
        sym = raw.get("code") or raw.get("symbol")
    else:
        sym = getattr(raw, "code", None) or getattr(raw, "symbol", None)
    return sym if isinstance(sym, str) else ""


class _ShardWorker:
    __slots__ = (
        "idx",
        "capacity",
        "inbox",
        "outbox",
        "cond",
        "waiting",
        "stopping",
        "offered",
        "dropped",
        "consecutive_drops",
        "rejected",
        "paused_ack",
        "resume",
        "thread",
    )

    def __init__(self, idx: int, capacity: int) -> None:
        self.idx = idx
        self.capacity = capacity
        self.inbox: deque[Any] = deque()
        self.outbox: deque[tuple[Any, ...]] = deque()
        self.cond = threading.Condition(threading.Lock())
        self.waiting = False
        self.stopping = False
        self.offered = 0
        self.dropped = 0
        self.consecutive_drops = 0
        self.rejected = 0
        self.paused_ack = threading.Event()
        self.resume = threading.Event()
        self.thread: threading.Thread | None = None


class ShardedMDPipeline:
    """Router + N worker threads + loop-side ordered commit.

    ``compute(raw)`` runs on the owning worker and returns a result tuple or None (rejected).
    ``commit(result)`` runs on the event loop. ``on_drop(consecutive)`` runs on the offering
    thread. ``on_crash(idx, exc)`` runs on the event loop.
    """

    def __init__(
        self,
        n_workers: int,
        capacity_per_worker: int,
        commit_budget: int,
        compute: Callable[[Any], tuple[Any, ...] | None],
        commit: Callable[[tuple[Any, ...]], None],
        on_drop: Callable[[int], None] | None = None,
        on_crash: Callable[[int, BaseException], None] | None = None,
        symbol_of: Callable[[Any], str] = default_symbol_of,
        name: str = "md-shard",
    ) -> None:
        if n_workers < 1:
            raise ValueError("n_workers must be >= 1")
        self.n_workers = n_workers
        self._workers = tuple(_ShardWorker(i, max(1, capacity_per_worker)) for i in range(n_workers))
        self._budget = max(1, commit_budget)
        self._compute = compute
        self._commit = commit
        self._on_drop = on_drop
        self._on_crash = on_crash
        self._symbol_of = symbol_of
        self._name = name
        self._shard_cache: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._admitting = False
        self._wake_pending = False
        self._rr = 0
        self.committed = 0
        self.commit_errors = 0
        self.stale_drained = 0
        self.drain_turns = 0
        self.crashed: BaseException | None = None

    # -- routing (any thread) ---------------------------------------------

    def shard_of(self, symbol: str) -> int:
        idx = self._shard_cache.get(symbol)
        if idx is None:
            idx = zlib.crc32(symbol.encode("utf-8")) % self.n_workers
            self._shard_cache[symbol] = idx
        return idx

    def offer(self, raw: Any) -> bool:
        w = self._workers[self.shard_of(self._symbol_of(raw))]
        with w.cond:
            w.offered += 1
            if not self._admitting or len(w.inbox) >= w.capacity:
                w.dropped += 1
                w.consecutive_drops += 1
                consecutive = w.consecutive_drops
                accepted = False
            else:
                w.inbox.append(raw)
                w.consecutive_drops = 0
                accepted = True
                if w.waiting:
                    w.cond.notify()
        if not accepted and self._on_drop is not None:
            self._on_drop(consecutive)
        return accepted

    @property
    def admitting(self) -> bool:
        return self._admitting

    # -- lifecycle (loop thread) ------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        for w in self._workers:
            w.stopping = False
            w.thread = threading.Thread(target=self._run_worker, args=(w,), name=f"{self._name}-{w.idx}", daemon=True)
        self._admitting = self.crashed is None
        for w in self._workers:
            if w.thread is not None:
                w.thread.start()

    def stop(self, join_timeout_s: float = 2.0) -> bool:
        """Stop admission, stop and join every worker. Returns True when all joined."""
        self._admitting = False
        for w in self._workers:
            with w.cond:
                w.stopping = True
                w.cond.notify_all()
            w.resume.set()
        all_joined = True
        for w in self._workers:
            t = w.thread
            if t is not None and t.is_alive():
                t.join(join_timeout_s)
                if t.is_alive():
                    all_joined = False
                    logger.error("md_worker_join_timeout", worker=w.idx)
        return all_joined

    # -- worker thread -----------------------------------------------------

    def _run_worker(self, w: _ShardWorker) -> None:
        inbox = w.inbox
        outbox = w.outbox
        cond = w.cond
        compute = self._compute
        try:
            while True:
                if not inbox:
                    with cond:
                        while not inbox and not w.stopping:
                            w.waiting = True
                            cond.wait()
                            w.waiting = False
                if w.stopping:
                    return
                item = inbox.popleft()
                if item is _PAUSE:
                    w.paused_ack.set()
                    w.resume.wait()
                    w.resume.clear()
                    continue
                result = compute(item)
                if result is None:
                    w.rejected += 1
                    continue
                outbox.append(result)
                if not self._wake_pending:
                    self._wake_pending = True
                    self._schedule_drain_threadsafe()
        except BaseException as exc:
            self._worker_crashed(w, exc)

    def _schedule_drain_threadsafe(self) -> None:
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self.drain)
        except RuntimeError:
            # Loop closed during shutdown: nothing can commit any more.
            self._wake_pending = False

    def _worker_crashed(self, w: _ShardWorker, exc: BaseException) -> None:
        self._admitting = False
        if self.crashed is None:
            self.crashed = exc
        logger.error("md_worker_crashed", worker=w.idx, error=repr(exc))
        loop = self._loop
        cb = self._on_crash
        if loop is not None and cb is not None:
            try:
                loop.call_soon_threadsafe(cb, w.idx, exc)
            except RuntimeError:
                logger.error("md_worker_crash_unreported_loop_closed", worker=w.idx)

    # -- loop-side commit --------------------------------------------------

    def drain(self) -> None:
        """Commit at most ``commit_budget`` results, then yield via ``call_soon`` if more wait."""
        self._wake_pending = False
        self.drain_turns += 1
        budget = self._budget
        workers = self._workers
        n_workers = self.n_workers
        commit = self._commit
        start = self._rr
        self._rr = (start + 1) % n_workers
        done = 0
        for k in range(n_workers):
            out = workers[(start + k) % n_workers].outbox
            while out and done < budget:
                result = out.popleft()
                done += 1
                try:
                    commit(result)
                except Exception as exc:
                    self.commit_errors += 1
                    logger.error("md_commit_failed", error=str(exc), exc_info=True)
            if done >= budget:
                break
        self.committed += done
        for w in workers:
            if w.outbox:
                if not self._wake_pending:
                    self._wake_pending = True
                    loop = self._loop
                    if loop is not None:
                        loop.call_soon(self.drain)
                break

    def run_quiesced(self, fn: Callable[[], None], *, drain_inputs: bool, timeout_s: float = 1.0) -> int:
        """Pause every live worker at its next item, optionally drop stale inputs, run ``fn``, resume.

        Runs on the loop and blocks it until every worker acknowledges (each finishes at most one
        in-flight event). Returns the number of stale inputs dropped. Raises
        :class:`MDShardQuiesceTimeoutError` (fail closed) if a worker does not acknowledge.
        """
        live = [w for w in self._workers if w.thread is not None and w.thread.is_alive()]
        for w in live:
            w.paused_ack.clear()
            with w.cond:
                w.inbox.appendleft(_PAUSE)
                if w.waiting:
                    w.cond.notify()
        deadline_ns = timebase.now_ns() + int(timeout_s * 1e9)
        try:
            for w in live:
                remaining = max(0.0, (deadline_ns - timebase.now_ns()) / 1e9)
                if not w.paused_ack.wait(remaining):
                    raise MDShardQuiesceTimeoutError(f"md worker {w.idx} did not pause within {timeout_s}s")
            stale = 0
            if drain_inputs:
                for w in self._workers:
                    with w.cond:
                        stale += len(w.inbox)
                        w.inbox.clear()
                self.stale_drained += stale
            fn()
            return stale
        finally:
            for w in live:
                w.resume.set()

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, int]:
        offered = dropped = rejected = queued = pending = 0
        for w in self._workers:
            offered += w.offered
            dropped += w.dropped
            rejected += w.rejected
            queued += len(w.inbox)
            pending += len(w.outbox)
        return {
            "offered": offered,
            "dropped": dropped,
            "rejected": rejected,
            "queued": queued,
            "pending_commit": pending,
            "committed": self.committed,
            "stale_drained": self.stale_drained,
            "commit_errors": self.commit_errors,
        }


class CommittedMDView:
    """Per-symbol snapshot of the last committed result, read by strategies on the loop.

    Workers never write here: the loop swaps one reference per commit, so a reader sees either
    the previous or the next complete (L1, feature tuple) pair, never a half-applied book.
    """

    __slots__ = ("_snap", "_seq", "_index_by_id", "commit_seq")

    def __init__(self, feature_index_by_id: dict[str, int] | None = None) -> None:
        self._snap: dict[str, tuple[Any, ...]] = {}
        self._seq: dict[str, int] = {}
        self._index_by_id = feature_index_by_id or {}
        self.commit_seq = 0

    def publish(self, symbol: str, result: tuple[Any, ...]) -> None:
        self.commit_seq += 1
        self._snap[symbol] = result
        self._seq[symbol] = self.commit_seq

    def clear(self) -> None:
        self._snap.clear()
        self._seq.clear()

    def snapshot(self, symbol: str) -> tuple[Any, ...] | None:
        return self._snap.get(symbol)

    def seq(self, symbol: str) -> int:
        return self._seq.get(symbol, 0)

    def get_l1_scaled(self, symbol: str) -> tuple[int, int, int, int, int, int, int] | None:
        snap = self._snap.get(symbol)
        return None if snap is None else snap[R_L1]

    def get_feature_tuple(self, symbol: str) -> tuple[int | float, ...] | None:
        snap = self._snap.get(symbol)
        return None if snap is None else snap[R_FEATURES]

    def get_feature(self, symbol: str, feature_id: str) -> int | float | None:
        snap = self._snap.get(str(symbol))
        if snap is None:
            return None
        values = snap[R_FEATURES]
        idx = self._index_by_id.get(str(feature_id))
        if values is None or idx is None or idx >= len(values):
            return None
        return values[idx]


class _LoopStormGuardProxy:
    """Forwards StormGuard calls to the loop so worker threads never touch StormGuard state."""

    __slots__ = ("_target", "_loop")

    def __init__(self, target: Any, loop_getter: Callable[[], asyncio.AbstractEventLoop | None]) -> None:
        self._target = target
        self._loop = loop_getter

    def __getattr__(self, name: str) -> Callable[..., None]:
        fn = getattr(self._target, name)

        def _forward(*args: Any) -> None:
            loop = self._loop()
            if loop is None:
                fn(*args)
                return
            loop.call_soon_threadsafe(fn, *args)

        return _forward


class MarketDataShardingMixin:
    """Service-side glue: compute stage (worker), commit stage (loop), drop/crash handling."""

    # Class-level defaults so instances built without __init__ (tests, __new__) read the N=0 path.
    _md_shards: ShardedMDPipeline | None = None
    committed_view: CommittedMDView | None = None

    def _init_md_shards(self, n_workers: int) -> None:
        svc: Any = self
        self._md_shards = None
        self.committed_view = None
        self._md_crash_future: asyncio.Future[None] | None = None
        if n_workers < 1:
            return
        fe = svc.feature_engine
        index_by_id = getattr(fe, "_index_by_id", None) if fe is not None else None
        self.committed_view = CommittedMDView(dict(index_by_id) if isinstance(index_by_id, dict) else None)
        self._md_real_storm_guard = svc._storm_guard
        if svc._storm_guard is not None:
            svc._storm_guard = _LoopStormGuardProxy(svc._storm_guard, lambda: getattr(svc, "loop", None))
        capacity = max(1, int(svc._raw_queue_size) // n_workers)
        self._md_shards = ShardedMDPipeline(
            n_workers,
            capacity,
            md_commit_budget_from_env(),
            compute=self._md_shard_compute,
            commit=self._md_shard_commit,
            on_drop=self._md_shard_on_drop,
            on_crash=self._md_shard_on_crash,
        )
        logger.info("md_workers_enabled", workers=n_workers, capacity_per_worker=capacity)

    # -- worker thread -----------------------------------------------------

    def _md_shard_normalize(self, raw: Any) -> TickEvent | BidAskEvent | None:
        svc: Any = self
        if isinstance(raw, tuple) and len(raw) == 2:
            raw = raw[1]
        try:
            if isinstance(raw, dict):
                is_bid = "bid_price" in raw or "bid_volume" in raw or "ask_price" in raw
                is_tick = "close" in raw or "price" in raw
            else:
                is_bid = hasattr(raw, "bid_price") or hasattr(raw, "bid_volume") or hasattr(raw, "ask_price")
                is_tick = hasattr(raw, "close") or hasattr(raw, "price")
            normalized: Any = None
            if is_bid:
                normalized = svc.normalizer.normalize_bidask(raw)
            elif is_tick:
                normalized = svc.normalizer.normalize_tick(raw)
        except Exception as ne:
            logger.error("Normalization check failed", error=str(ne), raw_type=str(type(raw)))
            with svc._shared_state_lock:
                svc._norm_consecutive_failures += 1
                failures = svc._norm_consecutive_failures
            if svc.metrics_registry:
                svc.metrics_registry.normalize_error_total.inc()
            if failures >= svc._NORM_FAILURE_ESCALATE and svc._storm_guard is not None:
                svc._storm_guard.report_norm_failure(failures)
            return None
        if not isinstance(normalized, (TickEvent, BidAskEvent)):
            return None
        event = normalized
        if svc._norm_consecutive_failures:
            with svc._shared_state_lock:
                recovered = svc._norm_consecutive_failures >= svc._NORM_FAILURE_ESCALATE
                svc._norm_consecutive_failures = 0
            if recovered and svc._storm_guard is not None:
                svc._storm_guard.report_norm_recovery()
        if isinstance(event, TickEvent):
            if svc._feed_events_tick_child is not None:
                svc._feed_events_tick_child.inc()
        elif svc._feed_events_bidask_child is not None:
            svc._feed_events_bidask_child.inc()
        return event

    def _md_shard_compute(self, raw: Any) -> tuple[Any, ...] | None:
        svc: Any = self
        event = self._md_shard_normalize(raw)
        if event is None:
            return None
        svc._log_first_event(event)
        symbol = event.symbol
        try:
            stats = svc.lob.process_event(event)
            feature_update = svc._maybe_update_features(event, stats)
            l1 = svc.lob.get_l1_scaled(symbol)
            fe = svc.feature_engine
            features = fe.get_feature_tuple(symbol) if fe is not None else None
        except Exception as exc:
            with svc._shared_state_lock:
                svc._process_raw_error_count += 1
            if svc.metrics_registry:
                svc.metrics_registry.process_raw_error_total.inc()
            logger.error("process_raw_post_norm_error", symbol=symbol, error=str(exc), exc_info=True)
            return None
        return (symbol, event, stats, feature_update, l1, features)

    # -- loop thread -------------------------------------------------------

    def _md_shard_commit(self, result: tuple[Any, ...]) -> None:
        svc: Any = self
        event = result[R_EVENT]
        svc.last_event_ts = timebase.now_s()
        svc.last_event_mono = time.monotonic()
        svc._md_metrics_counter += 1
        if svc.metrics_registry and svc._md_metrics_counter % svc._md_metrics_sample_every == 0:
            if svc._feed_last_event_metric_child is None:
                svc._feed_last_event_metric_child = svc.metrics_registry.feed_last_event_ts.labels(source="market_data")
            svc._feed_last_event_metric_child.set(svc.last_event_ts)
        svc.metrics["count"] += 1
        view = self.committed_view
        if view is not None:
            view.publish(result[R_SYMBOL], result)
        svc._update_symbol_tick_inline(event)
        try:
            if svc._record_direct:
                svc._record_direct_event(event)
            svc._publish_events(event, result[R_STATS], result[R_FEATURE_UPDATE])
            svc._publish_to_redis(event, result[R_STATS])
        except Exception as exc:
            with svc._shared_state_lock:
                svc._process_raw_error_count += 1
            if svc.metrics_registry:
                svc.metrics_registry.process_raw_error_total.inc()
            logger.error("md_commit_error", symbol=result[R_SYMBOL], error=str(exc), exc_info=True)

    def _md_shard_on_drop(self, consecutive: int) -> None:
        loop = getattr(self, "loop", None)
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._md_shard_handle_drop, consecutive)
        except RuntimeError:
            logger.debug("md_shard_drop_unreported_loop_closed")

    def _md_shard_handle_drop(self, consecutive: int) -> None:
        svc: Any = self
        svc._raw_dropped_count += 1
        svc._raw_consecutive_drops = consecutive
        svc._escalate_raw_drop()

    def _md_shard_on_crash(self, idx: int, exc: BaseException) -> None:
        svc: Any = self
        logger.error("md_worker_crash_fail_closed", worker=idx, error=repr(exc))
        if svc.metrics_registry:
            svc.metrics_registry.process_raw_error_total.inc()
        sg = getattr(self, "_md_real_storm_guard", None)
        if sg is not None:
            try:
                sg.trigger_halt(f"md_worker_crash: worker {idx}")
            except Exception as sg_exc:
                logger.error("md_worker_crash_halt_failed", error=str(sg_exc))
        fut = self._md_crash_future
        if fut is not None and not fut.done():
            fut.set_exception(MDWorkerCrashedError(f"md worker {idx} crashed: {exc!r}"))

    def _md_shards_reconnect_reset(self, ok: bool, reset_fn: Callable[[], None]) -> int:
        """Run the reconnect reset with every worker paused, so no foreign thread races a worker."""
        pipe = self._md_shards
        if pipe is None:
            reset_fn()
            return 0
        view = self.committed_view

        def _reset() -> None:
            reset_fn()
            if view is not None:
                view.clear()

        try:
            stale = pipe.run_quiesced(_reset, drain_inputs=ok)
        except MDShardQuiesceTimeoutError as exc:
            pipe.stop(join_timeout_s=0.0)
            self._md_shard_on_crash(-1, exc)
            return 0
        if stale:
            logger.info("Drained stale md shard inputs after reconnect", count=stale)
        return stale

    async def _run_sharded(self) -> None:
        svc: Any = self
        pipe = self._md_shards
        if pipe is None:
            raise RuntimeError("_run_sharded called with HFT_MD_WORKERS=0")
        self._md_crash_future = svc.loop.create_future()
        pipe.start(svc.loop)
        logger.info("MarketDataService started", md_workers=pipe.n_workers)
        svc.lob.start_metrics_worker(svc.loop)
        monitor_task = asyncio.create_task(svc._monitor_loop())
        watchdog_task = asyncio.create_task(svc._watchdog_loop())
        try:
            await svc._connect_sequence()
            await self._md_crash_future
        finally:
            monitor_task.cancel()
            watchdog_task.cancel()
            pipe.stop()
            if hasattr(svc.lob, "stop"):
                svc.lob.stop()
