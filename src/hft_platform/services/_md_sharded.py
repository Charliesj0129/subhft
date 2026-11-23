"""Symbol-sharded market-data worker threads (``HFT_MD_WORKERS``).

With ``HFT_MD_WORKERS=0`` (default) nothing here is constructed and the service keeps its single-loop
path: broker callback -> ``call_soon_threadsafe(_enqueue_raw)`` -> raw ``asyncio.Queue`` -> ``run()``
-> ``_process_raw`` on the event loop.

With N >= 1 the chain is::

    broker thread --offer()--> crc32(symbol) % N (bounded shard cache)
        -> worker inbox (bounded deque; full -> drop + per-worker counter, polled by the loop)
        -> worker thread: normalize -> LOB -> feature -> (L1, feature tuple, feature meta, epoch)
        -> worker outbox (SPSC deque) -> one call_soon_threadsafe per batch
        -> loop drain (<= HFT_MD_COMMIT_BUDGET per turn, then call_soon yield)
        -> stale-epoch results dropped -> committed view swap, feed-gap stamp, recorder, bus, redis

LOB/feature resets are queued as commands in each owning worker's inbox (ordered with events);
results produced before a reset carry an older epoch and are discarded at commit. A symbol always
lands on the same worker and each outbox is FIFO, so per-symbol order is kept; cross-symbol order is
not. Only the loop publishes to the bus, so ``single_writer`` stays on.
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
from hft_platform.events import BidAskEvent, LOBStatsEvent, TickEvent

logger = get_logger("service.market_data.sharded")

_RESET_MARKER = object()

# Result tuple layout produced by the worker compute stage and held by the committed view.
R_SYMBOL = 0
R_EVENT = 1
R_STATS = 2
R_FEATURE_UPDATE = 3
R_L1 = 4
R_FEATURES = 5
R_FEATURE_META = 6  # (seq, source_ts_ns, local_ts_ns, quality_flags, last_update_ns) or None
R_EPOCH = 7

_SHARD_CACHE_MAX_DEFAULT = 4096


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


class _ResetCmd:
    """Reset command queued in a worker inbox, ordered with the events around it."""

    __slots__ = ("symbols", "epoch")

    def __init__(self, symbols: frozenset[str] | None, epoch: int) -> None:
        self.symbols = symbols
        self.epoch = epoch


class ShardWorker:
    """One worker's queues and worker-local state. Fields under 'worker-only' are written only by its thread."""

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
        "floor",
        "thread",
        # worker-only
        "epoch",
        "seen",
        "rejected",
        "stale_skipped",
        "resets_applied",
        "norm_failures",
        "feature_failures",
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
        self.floor = 0  # written by requesters under cond; results with epoch < floor are stale
        self.thread: threading.Thread | None = None
        self.epoch = 0
        self.seen: set[str] = set()
        self.rejected = 0
        self.stale_skipped = 0
        self.resets_applied = 0
        self.norm_failures = 0
        self.feature_failures = 0


class ShardedMDPipeline:
    """Router + N worker threads + loop-side ordered commit.

    ``compute(raw, worker)`` runs on the owning worker and returns a result tuple (layout ``R_*``,
    ``R_EPOCH`` = ``worker.epoch``) or None. ``commit(result)`` runs on the loop.
    ``reset(symbols)`` runs on the owning worker for the symbols it owns. ``on_reset_committed(symbols)``
    runs on the loop when that reset reaches the commit stage. ``on_drain(pipe)`` runs on the loop after
    every drain turn. ``on_crash(idx, exc)`` runs on the loop.
    """

    def __init__(
        self,
        n_workers: int,
        capacity_per_worker: int,
        commit_budget: int,
        compute: Callable[[Any, ShardWorker], tuple[Any, ...] | None],
        commit: Callable[[tuple[Any, ...]], None],
        reset: Callable[[set[str]], None] | None = None,
        on_reset_committed: Callable[[frozenset[str]], None] | None = None,
        on_drain: Callable[[], None] | None = None,
        on_crash: Callable[[int, BaseException], None] | None = None,
        symbol_of: Callable[[Any], str] = default_symbol_of,
        known_symbols: Any = (),
        shard_cache_max: int = _SHARD_CACHE_MAX_DEFAULT,
        name: str = "md-shard",
    ) -> None:
        if n_workers < 1:
            raise ValueError("n_workers must be >= 1")
        self.n_workers = n_workers
        self._workers = tuple(ShardWorker(i, max(1, capacity_per_worker)) for i in range(n_workers))
        self._budget = max(1, commit_budget)
        self._compute = compute
        self._commit = commit
        self._reset = reset
        self._on_reset_committed = on_reset_committed
        self._on_drain = on_drain
        self._on_crash = on_crash
        self._symbol_of = symbol_of
        self._name = name
        self._shard_cache_max = max(0, shard_cache_max)
        self._shard_cache: dict[str, int] = {}
        for sym in known_symbols or ():
            if len(self._shard_cache) >= self._shard_cache_max:
                break
            self._shard_cache[str(sym)] = self._hash_shard(str(sym))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._admitting = False
        self._wake_pending = False
        self._rr = 0
        self.committed = 0
        self.commit_errors = 0
        self.stale_drained = 0
        self.drain_turns = 0
        self.crashed: BaseException | None = None

    # -- routing (any thread) ---------------------------------------------

    def _hash_shard(self, symbol: str) -> int:
        return zlib.crc32(symbol.encode("utf-8")) % self.n_workers

    def shard_of(self, symbol: str) -> int:
        idx = self._shard_cache.get(symbol)
        if idx is None:
            idx = self._hash_shard(symbol)
            # Bounded: past the cap a symbol is hashed per event instead of cached.
            if len(self._shard_cache) < self._shard_cache_max:
                self._shard_cache[symbol] = idx
        return idx

    def offer(self, raw: Any) -> bool:
        """Admit one raw message (broker thread). Never blocks; a full shard drops and counts."""
        w = self._workers[self.shard_of(self._symbol_of(raw))]
        with w.cond:
            w.offered += 1
            if not self._admitting or len(w.inbox) >= w.capacity:
                w.dropped += 1
                w.consecutive_drops += 1
                return False
            w.inbox.append(raw)
            w.consecutive_drops = 0
            if w.waiting:
                w.cond.notify()
        return True

    @property
    def admitting(self) -> bool:
        return self._admitting

    def healthy(self) -> bool:
        if not self._started or self.crashed is not None or not self._admitting:
            return False
        return all(w.thread is not None and w.thread.is_alive() for w in self._workers)

    def request_reset(self, symbols: set[str] | frozenset[str] | None) -> bool:
        """Queue a LOB/feature reset on every worker, ordered after the events already queued.

        Any thread. Results produced before the reset are discarded at commit (counted in
        ``stale_drained``), and queued pre-reset inputs are skipped. Returns False when the reset
        cannot be applied (pipeline not running or a worker is dead): the caller must fail closed.
        """
        if not self.healthy():
            return False
        frozen = frozenset(symbols) if symbols is not None else None
        for w in self._workers:
            with w.cond:
                w.floor += 1
                w.inbox.append(_ResetCmd(frozen, w.floor))
                if w.waiting:
                    w.cond.notify()
        return True

    # -- lifecycle (loop thread) ------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        for w in self._workers:
            w.stopping = False
            w.thread = threading.Thread(target=self._run_worker, args=(w,), name=f"{self._name}-{w.idx}", daemon=True)
        self._started = True
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
        all_joined = True
        for w in self._workers:
            t = w.thread
            if t is not None and t.is_alive() and t is not threading.current_thread():
                t.join(join_timeout_s)
                if t.is_alive():
                    all_joined = False
                    logger.error("md_worker_join_timeout", worker=w.idx)
        self._started = False
        return all_joined

    # -- worker thread -----------------------------------------------------

    def _apply_reset(self, w: ShardWorker, cmd: _ResetCmd) -> None:
        if cmd.symbols is None:
            owned = set(w.seen)
        else:
            owned = {s for s in cmd.symbols if self.shard_of(s) == w.idx}
        if self._reset is not None and owned:
            self._reset(owned)
        w.seen.difference_update(owned)
        w.epoch = cmd.epoch
        w.resets_applied += 1
        w.outbox.append((_RESET_MARKER, frozenset(owned), None, None, None, None, None, cmd.epoch))

    def _run_worker(self, w: ShardWorker) -> None:
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
                if type(item) is _ResetCmd:
                    self._apply_reset(w, item)
                elif w.epoch < w.floor:
                    # Queued before a pending reset: its book is about to be discarded.
                    w.stale_skipped += 1
                    continue
                else:
                    result = compute(item, w)
                    if result is None:
                        w.rejected += 1
                        continue
                    sym = result[R_SYMBOL]
                    if sym not in w.seen:
                        w.seen.add(sym)
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

    def _worker_crashed(self, w: ShardWorker, exc: BaseException) -> None:
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
            w = workers[(start + k) % n_workers]
            out = w.outbox
            while out and done < budget:
                result = out.popleft()
                done += 1
                if result[R_EPOCH] < w.floor:
                    self.stale_drained += 1
                    continue
                try:
                    if result[R_SYMBOL] is _RESET_MARKER:
                        if self._on_reset_committed is not None:
                            self._on_reset_committed(result[R_EVENT])
                        continue
                    commit(result)
                    self.committed += 1
                except Exception as exc:
                    self.commit_errors += 1
                    logger.error("md_commit_failed", error=str(exc), exc_info=True)
            if done >= budget:
                break
        if self._on_drain is not None:
            self._on_drain()
        for w in workers:
            if w.outbox:
                if not self._wake_pending:
                    self._wake_pending = True
                    loop = self._loop
                    if loop is not None:
                        loop.call_soon(self.drain)
                break

    # -- introspection (loop) ---------------------------------------------

    def drop_totals(self) -> tuple[int, int]:
        """(total dropped across shards, max consecutive drops on any shard)."""
        total = 0
        worst = 0
        for w in self._workers:
            total += w.dropped
            c = w.consecutive_drops
            if c > worst:
                worst = c
        return total, worst

    def stats(self) -> dict[str, int]:
        offered = dropped = rejected = queued = pending = stale_skipped = 0
        for w in self._workers:
            offered += w.offered
            dropped += w.dropped
            rejected += w.rejected
            stale_skipped += w.stale_skipped
            # tuple(deque) is one atomic copy; iterating the live deque races the worker's popleft.
            queued += sum(1 for x in tuple(w.inbox) if type(x) is not _ResetCmd)
            pending += sum(1 for r in tuple(w.outbox) if r[R_SYMBOL] is not _RESET_MARKER)
        return {
            "offered": offered,
            "dropped": dropped,
            "rejected": rejected,
            "queued": queued,
            "pending_commit": pending,
            "committed": self.committed,
            "stale_drained": self.stale_drained + stale_skipped,
            "commit_errors": self.commit_errors,
        }


class CommittedMDView:
    """Per-symbol snapshot of the last committed result, read on the loop (strategies, risk, TCA).

    Workers never write here: the loop swaps one reference per commit, so a reader sees either the
    previous or the next complete result, never a half-applied book. Dict views are built on demand
    from the committed tuple, the same allocation the live getters make today.
    """

    __slots__ = ("_snap", "_seq", "_fe", "_index_by_id", "commit_seq")

    def __init__(self, feature_engine: Any = None) -> None:
        self._snap: dict[str, tuple[Any, ...]] = {}
        self._seq: dict[str, int] = {}
        self._fe = feature_engine
        idx = getattr(feature_engine, "_index_by_id", None) if feature_engine is not None else None
        self._index_by_id: dict[str, int] = dict(idx) if isinstance(idx, dict) else {}
        self.commit_seq = 0

    def publish(self, symbol: str, result: tuple[Any, ...]) -> None:
        self.commit_seq += 1
        self._snap[symbol] = result
        self._seq[symbol] = self.commit_seq

    def clear(self, symbols: Any = None) -> None:
        if symbols is None:
            self._snap.clear()
            self._seq.clear()
            return
        for s in symbols:
            self._snap.pop(s, None)
            self._seq.pop(s, None)

    def snapshot(self, symbol: str) -> tuple[Any, ...] | None:
        return self._snap.get(symbol)

    def seq(self, symbol: str) -> int:
        return self._seq.get(symbol, 0)

    def get_l1_scaled(self, symbol: str) -> tuple[int, int, int, int, int, int, int] | None:
        snap = self._snap.get(symbol)
        return None if snap is None else snap[R_L1]

    def get_mid_price_x2(self, symbol: str) -> int:
        snap = self._snap.get(symbol)
        l1 = None if snap is None else snap[R_L1]
        return int(l1[3]) if l1 is not None else 0

    def get_book_snapshot(self, symbol: str) -> dict[str, Any] | None:
        """Same keys and values as ``LOBEngine.get_book_snapshot``, from the committed L1."""
        snap = self._snap.get(symbol)
        l1 = None if snap is None else snap[R_L1]
        if l1 is None:
            return None
        return {
            "symbol": symbol,
            "timestamp": l1[0],
            "best_bid": l1[1],
            "best_ask": l1[2],
            "mid_price": l1[3] / 2.0,
            "spread": float(l1[4]),
            "mid_price_x2": l1[3],
            "spread_scaled": l1[4],
        }

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

    def get_feature_view(self, symbol: str) -> dict[str, Any] | None:
        """Same keys as ``FeatureEngine.get_feature_view``, from the committed state."""
        snap = self._snap.get(str(symbol))
        fe = self._fe
        if snap is None or fe is None or snap[R_FEATURE_META] is None:
            return None
        seq, source_ts_ns, local_ts_ns, quality_flags, _last = snap[R_FEATURE_META]
        fset = fe._feature_set
        return {
            "symbol": str(symbol),
            "feature_set_id": fset.feature_set_id,
            "feature_profile_id": fe.active_profile_id(),
            "schema_version": int(fset.schema_version),
            "seq": int(seq),
            "source_ts_ns": int(source_ts_ns),
            "local_ts_ns": int(local_ts_ns),
            "quality_flags": int(quality_flags),
            "feature_ids": fe._feature_ids,
            "values": snap[R_FEATURES],
        }

    def last_update_ns(self, symbol: str) -> int | None:
        snap = self._snap.get(str(symbol))
        if snap is None or snap[R_FEATURE_META] is None:
            return None
        return snap[R_FEATURE_META][4]


class _LoopStormGuardProxy:
    """Forwards StormGuard calls to the loop. Given to worker threads only; svc keeps the real guard."""

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
    """Service-side glue: compute stage (worker), commit stage (loop), resets, drops, crashes."""

    # Class-level defaults so instances built without __init__ (tests, __new__) read the N=0 path.
    _md_shards: ShardedMDPipeline | None = None
    committed_view: CommittedMDView | None = None

    def _init_md_shards(self, n_workers: int) -> None:
        svc: Any = self
        self._md_shards = None
        self.committed_view = None
        self._md_crash_future: asyncio.Future[None] | None = None
        self._md_drops_seen = 0
        if n_workers < 1:
            return
        fe = svc.feature_engine
        self.committed_view = CommittedMDView(fe)
        self._md_sg_proxy = (
            _LoopStormGuardProxy(svc._storm_guard, lambda: getattr(svc, "loop", None))
            if svc._storm_guard is not None
            else None
        )
        self._md_fe_process = getattr(fe, "process_lob_update", None) if fe is not None else None
        _mr = svc.metrics_registry
        self._md_feature_error_child = (
            _mr.feature_plane_updates_total.labels(result="error", feature_set=svc._feature_set_id_cached)
            if _mr is not None and hasattr(_mr, "feature_plane_updates_total")
            else None
        )
        if svc._feature_shadow_engine is not None:
            logger.warning("md_workers_feature_shadow_parity_skipped", reason="shadow parity is single-loop only")
        client = svc.client
        known = getattr(client, "subscribed_symbols", None) or getattr(client, "symbols", None)
        if not isinstance(known, (list, tuple, set, frozenset)):
            known = ()
        capacity = max(1, int(svc._raw_queue_size) // n_workers)
        self._md_shards = ShardedMDPipeline(
            n_workers,
            capacity,
            md_commit_budget_from_env(),
            compute=self._md_shard_compute,
            commit=self._md_shard_commit,
            reset=self._md_shard_reset_owned,
            on_reset_committed=self._md_shard_reset_committed,
            on_drain=self._md_shard_poll_drops,
            on_crash=self._md_shard_on_crash,
            known_symbols=known,
        )
        # Per-facade warmup resets (QuoteConnectionPool) go to the owning workers, not the pool thread.
        if callable(getattr(type(client), "set_reset_router", None)):
            client.set_reset_router(self._md_shard_route_reset)
        logger.info("md_workers_enabled", workers=n_workers, capacity_per_worker=capacity)

    # -- worker thread -----------------------------------------------------

    def _md_shard_normalize(self, raw: Any, w: ShardWorker) -> TickEvent | BidAskEvent | None:
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
            logger.error("Normalization check failed", error=str(ne), raw_type=str(type(raw)), worker=w.idx)
            w.norm_failures += 1
            if svc.metrics_registry:
                svc.metrics_registry.normalize_error_total.inc()
            sg = self._md_sg_proxy
            if w.norm_failures >= svc._NORM_FAILURE_ESCALATE and sg is not None:
                sg.report_norm_failure(w.norm_failures)
            return None
        if not isinstance(normalized, (TickEvent, BidAskEvent)):
            return None
        if w.norm_failures:
            recovered = w.norm_failures >= svc._NORM_FAILURE_ESCALATE
            w.norm_failures = 0
            sg = self._md_sg_proxy
            if recovered and sg is not None:
                sg.report_norm_recovery()
        return normalized

    def _md_shard_features(self, event: Any, stats: Any, w: ShardWorker) -> Any:
        svc: Any = self
        fe = svc.feature_engine
        if fe is None:
            return None
        if isinstance(event, TickEvent) and event.trade_direction != 0:
            fe.on_tick(event.symbol, event.price, event.volume, event.trade_direction, event.trade_confidence)
        if stats is None or not isinstance(stats, (LOBStatsEvent, tuple)):
            return None
        meta = event.meta
        local_ts_ns = int(getattr(meta, "local_ts", 0) or 0) if meta is not None else 0
        sg = self._md_sg_proxy
        try:
            process_fn = self._md_fe_process
            if process_fn is not None:
                update = process_fn(event, stats, local_ts_ns=local_ts_ns)
            else:
                update = fe.process_lob_stats(stats, local_ts_ns=local_ts_ns)
        except Exception as exc:
            w.feature_failures += 1
            if self._md_feature_error_child is not None:
                self._md_feature_error_child.inc()
            if w.feature_failures >= svc._FEATURE_FAILURE_ESCALATE:
                logger.error("feature_engine_update_failed", reason=str(exc), worker=w.idx)
                if sg is not None:
                    sg.report_feature_failure(w.feature_failures)
            else:
                logger.warning("feature_engine_update_failed", reason=str(exc), worker=w.idx)
            return None
        if w.feature_failures:
            w.feature_failures = 0
            if sg is not None:
                sg.report_feature_recovery()
        return update

    def _md_shard_compute(self, raw: Any, w: ShardWorker) -> tuple[Any, ...] | None:
        svc: Any = self
        event = self._md_shard_normalize(raw, w)
        if event is None:
            return None
        symbol = event.symbol
        try:
            stats = svc.lob.process_event(event)
            feature_update = self._md_shard_features(event, stats, w)
            l1 = svc.lob.get_l1_scaled(symbol)
            fe = svc.feature_engine
            features = None
            fmeta = None
            if fe is not None:
                state = fe._states.get(symbol)
                if state is not None:
                    features = state.values
                    fmeta = (
                        state.seq,
                        state.source_ts_ns,
                        state.local_ts_ns,
                        state.quality_flags,
                        fe._last_update_ns.get(symbol),
                    )
        except Exception as exc:
            with svc._shared_state_lock:
                svc._process_raw_error_count += 1
            if svc.metrics_registry:
                svc.metrics_registry.process_raw_error_total.inc()
            logger.error("process_raw_post_norm_error", symbol=symbol, error=str(exc), exc_info=True)
            return None
        return (symbol, event, stats, feature_update, l1, features, fmeta, w.epoch)

    def _md_shard_reset_owned(self, symbols: set[str]) -> None:
        """Worker thread: reset the LOB/feature state of symbols this worker owns."""
        svc: Any = self
        lob = svc.lob
        if lob is not None and hasattr(lob, "reset_books_for_symbols"):
            lob.reset_books_for_symbols(symbols)
        fe = svc.feature_engine
        if fe is not None and hasattr(fe, "reset_symbols"):
            fe.reset_symbols(symbols)

    # -- any thread --------------------------------------------------------

    def _md_shard_route_reset(self, symbols: Any) -> bool:
        pipe = self._md_shards
        ok = pipe is not None and pipe.request_reset(set(symbols) if symbols is not None else None)
        if not ok:
            logger.error("md_shard_reset_not_applied", symbols=None if symbols is None else len(symbols))
        return ok

    # -- loop thread -------------------------------------------------------

    def _md_shard_reset_committed(self, symbols: frozenset[str]) -> None:
        view = self.committed_view
        if view is not None:
            view.clear(symbols)

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
        if isinstance(event, TickEvent):
            if svc._feed_events_tick_child is not None:
                svc._feed_events_tick_child.inc()
        elif svc._feed_events_bidask_child is not None:
            svc._feed_events_bidask_child.inc()
        svc._log_first_event(event)
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

    def _md_shard_poll_drops(self) -> None:
        """Loop: fold new shard drops into the raw-drop counters and escalate once per poll."""
        svc: Any = self
        pipe = self._md_shards
        if pipe is None:
            return
        total, worst = pipe.drop_totals()
        new = total - self._md_drops_seen
        svc._raw_consecutive_drops = worst
        if new <= 0:
            return
        self._md_drops_seen = total
        svc._raw_dropped_count += new
        svc._escalate_raw_drop(new)

    async def _md_shard_drop_poller(self, interval_s: float = 0.01) -> None:
        while True:
            await asyncio.sleep(interval_s)
            self._md_shard_poll_drops()

    def _md_shard_on_crash(self, idx: int, exc: BaseException) -> None:
        svc: Any = self
        logger.error("md_worker_crash_fail_closed", worker=idx, error=repr(exc))
        if svc.metrics_registry:
            svc.metrics_registry.process_raw_error_total.inc()
        sg = svc._storm_guard
        if sg is not None:
            try:
                sg.trigger_halt(f"md_worker_crash: worker {idx}")
            except Exception as sg_exc:
                logger.error("md_worker_crash_halt_failed", error=str(sg_exc))
        fut = self._md_crash_future
        if fut is not None and not fut.done():
            fut.set_exception(MDWorkerCrashedError(f"md worker {idx} crashed: {exc!r}"))

    def _md_shards_reconnect_reset(self) -> bool:
        """Route the post-reconnect LOB/feature reset to the owning workers.

        Returns False when the reset cannot be applied; the caller must not report the feed CONNECTED.
        """
        svc: Any = self
        pipe = self._md_shards
        if pipe is None:
            return False
        client = svc.client
        if hasattr(client, "_apply_pending_resets"):
            # Pool: its per-facade resets reach the workers through set_reset_router.
            client._apply_pending_resets()
            return pipe.healthy()
        if hasattr(client, "get_healthy_feed_gap_s"):
            return pipe.healthy()
        return self._md_shard_route_reset(None)

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
        drop_task = asyncio.create_task(self._md_shard_drop_poller())
        try:
            await svc._connect_sequence()
            await self._md_crash_future
        finally:
            monitor_task.cancel()
            watchdog_task.cancel()
            drop_task.cancel()
            pipe.stop()
            if hasattr(svc.lob, "stop"):
                svc.lob.stop()
