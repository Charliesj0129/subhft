import asyncio
import collections
import dataclasses
import inspect
import os
import tempfile
import threading
import time
from collections.abc import Coroutine
from typing import Any, Callable, Dict, Optional, Union

from structlog import get_logger

from hft_platform.contracts.constants import MANUAL_STRATEGY_ID
from hft_platform.contracts.execution import OrderStatus
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.recorder.wal import WALWriter

logger = get_logger("execution.router")

_FIRST_TERMINAL_STATUS = int(OrderStatus.FILLED)


def _net_pnl_delta(gross_realized_delta: int, fill: Any) -> int:
    """Realized PnL change a fill causes, net of what the fill cost.

    ``Position.realized_pnl_scaled`` is gross: fees accumulate separately in
    ``fees_scaled``. The daily-loss stop judges money, so it is fed the delta
    minus ``fill.fee`` (the all-in charge, x10000 NTD, tax included). An
    opening fill realizes nothing yet still costs its fee.
    """
    fee = getattr(fill, "fee", 0)
    return gross_realized_delta - (fee if isinstance(fee, int) else 0)


def _synthesize_dedup_key(fill: Any) -> str:
    """Synthesize a dedup key from fill fields when fill_id is empty.

    Used to prevent duplicate processing of reconnect-replayed fills
    that lack a broker sequence number.
    """
    return f"{fill.symbol}|{fill.order_id}|{fill.side}|{fill.price}|{fill.qty}|{fill.match_ts_ns}"


def _create_task_with_error_handling(coro: Coroutine[Any, Any, Any], name: Optional[str] = None) -> asyncio.Task[Any]:
    """Create an asyncio task with proper exception handling to prevent silent failures.

    Args:
        coro: The coroutine to run as a task.
        name: Optional name for the task for logging purposes.

    Returns:
        The created task with exception callback attached.
    """
    task: asyncio.Task[Any] = asyncio.create_task(coro, name=name)

    def _on_task_done(t: asyncio.Task) -> None:
        try:
            exc = t.exception()
            if exc is not None:
                logger.error(
                    "Background task failed",
                    task_name=t.get_name(),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
        except asyncio.CancelledError:
            pass
        except asyncio.InvalidStateError:
            pass

    task.add_done_callback(_on_task_done)
    return task


class ExecutionRouter:
    """
    Handles inbound execution reports (fills/order updates).
    Updates PositionStore and publishes to Bus.
    """

    def __init__(
        self,
        bus: RingBufferBus,
        raw_queue: asyncio.Queue,
        order_id_map: Dict[str, str],
        position_store: PositionStore,
        terminal_handler: Union[Callable[[str, str], None], object],
        risk_engine: Optional[object] = None,
        overflow_buf: Optional[collections.deque] = None,
        cmd_created_ns_map: Optional[Dict[str, int]] = None,
        cmd_tca_map: Optional[Dict[str, tuple[int, int]]] = None,
        cmd_trace_id_map: Optional[Dict[str, str]] = None,
        recorder_queue: Optional[asyncio.Queue] = None,
        symbol_metadata: Optional[Any] = None,
        price_scale_provider: Optional[Any] = None,
        wal_writer: Optional[WALWriter] = None,
        default_account_id_provider: Optional[Callable[[], str]] = None,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self._order_id_map = order_id_map
        self.normalizer = ExecutionNormalizer(
            raw_queue,
            order_id_map,
            default_account_id_provider=default_account_id_provider,
        )
        self.position_store = position_store
        self.terminal_handler = terminal_handler
        self._risk_engine = risk_engine
        self._overflow_buf = overflow_buf
        self._cmd_created_ns_map: Dict[str, int] = cmd_created_ns_map if cmd_created_ns_map is not None else {}
        self._cmd_tca_map: Dict[str, tuple[int, int]] = cmd_tca_map if cmd_tca_map is not None else {}
        self._cmd_trace_id_map: Dict[str, str] = cmd_trace_id_map if cmd_trace_id_map is not None else {}
        self.running = False
        self.metrics = MetricsRegistry.get()
        self._dlq_retry_interval = int(os.getenv("HFT_DLQ_RETRY_INTERVAL", "100"))
        self._fill_dedup_max_size: int = int(os.environ.get("HFT_FILL_DEDUP_MAX_SIZE", "10000"))
        self._seen_fill_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._fill_dedup_persist_path: str = os.environ.get(
            "HFT_FILL_DEDUP_PERSIST_PATH", ".state/fill_dedup_window.jsonl"
        )
        self._fill_dedup_persist_interval_s: float = float(os.environ.get("HFT_FILL_DEDUP_PERSIST_INTERVAL_S", "1.0"))
        self._fill_dedup_last_persist_seconds: float = float("-inf")
        self._fill_dedup_io_lock = threading.Lock()
        self._fill_dedup_persist_futures: set[asyncio.Future[None]] = set()
        self._fill_dedup_trailing_handle: asyncio.TimerHandle | None = None
        self._load_fill_dedup()
        self._events_since_dlq_retry = 0
        self._recorder_queue: Optional[asyncio.Queue] = recorder_queue
        self._symbol_metadata = symbol_metadata
        self._price_codec: Optional[PriceCodec] = (
            PriceCodec(price_scale_provider) if price_scale_provider is not None else None
        )
        self._wal_writer: Optional[WALWriter] = wal_writer
        self._phantom_resolver: Optional[Callable[[Any], Optional[str]]] = None
        self._fill_tracker: Optional[Any] = None

    def set_fill_tracker(self, tracker: Any) -> None:
        """Inject the object that finishes orders on fills.

        Needs ``async on_order_fill(order_key, qty) -> bool`` and
        ``filled_qty_for(order_key) -> int`` (``OrderAdapter`` provides both).
        """
        self._fill_tracker = tracker

    def _apply_fill_ledger(self, order_event: Any) -> None:
        """Give an order status event the filled quantity the broker did not send.

        Without this ``remaining_qty`` equals the order quantity on every cancel
        or reject that follows a fill, and the strategy releases the pending slot
        a second time. Only ever raises ``filled_qty``: a broker that does send
        fills is left alone.
        """
        tracker = self._fill_tracker
        key = getattr(order_event, "client_order_id", "")
        if tracker is None or not key:
            return
        cum = tracker.filled_qty_for(key)
        if cum > order_event.filled_qty:
            order_event.filled_qty = cum
            order_event.remaining_qty = max(0, order_event.submitted_qty - cum)

    def _release_slot_once(self, order_event: Any) -> None:
        """Make a repeat terminal status release nothing.

        The strategy gives its pending slot back by ``remaining_qty`` on every
        CANCELLED / FAILED. An order that already ended -- cancelled, filled,
        given up by the TTL sweep -- and then gets another terminal (the broker
        rejecting a cancel retry) would free the slot a second time, which by then
        may belong to a newer quote. The tracker answers once per event, before
        it records this one, so the first terminal is never touched: a live
        FAILED for insufficient margin still releases. Status is left alone so
        the strategy still clears its cancel-in-flight bookkeeping.
        """
        tracker = self._fill_tracker
        key = getattr(order_event, "client_order_id", "")
        if tracker is None or not key or int(order_event.status) < _FIRST_TERMINAL_STATUS:
            return
        note = getattr(tracker, "note_terminal", None)
        if not callable(note) or note(key) is not True:
            return
        order_event.remaining_qty = 0
        try:
            self.metrics.order_repeat_terminal_total.labels(status=OrderStatus(order_event.status).name).inc()
        except Exception:
            pass

    def set_risk_engine(self, risk_engine: object) -> None:
        """Set or replace the risk engine reference (late-bind from bootstrap)."""
        self._risk_engine = risk_engine

    def set_phantom_resolver(self, resolver: Callable[[Any], Optional[str]]) -> None:
        """Inject phantom order resolver for orphaned fill auto-reconciliation.

        The resolver takes a FillEvent and returns a strategy_id if the fill
        matches a known phantom order candidate (dispatch failed but order may
        have reached broker). Returns None if no match.
        """
        self._phantom_resolver = resolver

    def set_overflow_buf(self, buf: collections.deque) -> None:
        """Set or replace the overflow buffer (late-bind from system supervisor)."""
        self._overflow_buf = buf

    def _load_fill_dedup(self) -> None:
        """Load fill dedup window from disk on startup (restart-safe dedup).

        A damaged line leaves a hole in the window, so a replayed fill with that
        key would be booked twice. Skips are therefore counted and logged, split
        by where they sit (``tail`` = the last line, ``mid`` = one with good lines
        after it). The writer is temp+fsync+rename, so neither is an expected
        crash artefact: any skip is filesystem-level damage. The last line is the
        newest key, the one most likely to be replayed, so it is not downgraded.
        Dedup behaviour itself is unchanged.
        """
        self._fill_dedup_load_tail_skipped = 0
        self._fill_dedup_load_mid_skipped = 0
        path = self._fill_dedup_persist_path
        if not os.path.exists(path):
            return
        try:
            import orjson

            loaded = 0
            pending_bad = False
            try:
                with open(path, "rb") as f:
                    for raw in f:
                        raw = raw.strip()
                        if not raw:
                            continue
                        if pending_bad:
                            self._fill_dedup_load_mid_skipped += 1
                            pending_bad = False
                        try:
                            key = orjson.loads(raw)
                        except orjson.JSONDecodeError:
                            key = None
                        if isinstance(key, str) and key:
                            self._seen_fill_ids[key] = None
                            loaded += 1
                        else:
                            pending_bad = True
            finally:
                if pending_bad:
                    self._fill_dedup_load_tail_skipped += 1
            while len(self._seen_fill_ids) > self._fill_dedup_max_size:
                self._seen_fill_ids.popitem(last=False)
            if self._fill_dedup_load_mid_skipped or self._fill_dedup_load_tail_skipped:
                logger.error(
                    "fill_dedup_load_corrupt_lines",
                    mid_skipped=self._fill_dedup_load_mid_skipped,
                    tail_skipped=self._fill_dedup_load_tail_skipped,
                    loaded=loaded,
                    path=path,
                )
            logger.info("fill_dedup_loaded", count=loaded, path=path)
        except Exception as exc:
            logger.warning("fill_dedup_load_failed", error=str(exc), path=path)

    def persist_fill_dedup(self) -> None:
        """Persist fill dedup window to disk atomically (temp+fsync+rename).

        Called during graceful shutdown. Safe to call from thread pool.

        Serialised on ``_fill_dedup_io_lock``, which covers the snapshot as well
        as the write: ``_maybe_persist_fill_dedup`` hands this to the default
        executor, whose pool has many workers, so two checkpoints can run at
        once and the rename that lands last wins. Snapshotting inside the lock
        makes the last writer necessarily the one holding the newest window.
        """
        with self._fill_dedup_io_lock:
            self._persist_fill_dedup_locked()

    def _persist_fill_dedup_locked(self) -> None:
        path = self._fill_dedup_persist_path
        keys_snapshot = list(self._seen_fill_ids.keys())
        try:
            import orjson

            persist_dir = os.path.dirname(path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    for key in keys_snapshot:
                        f.write(orjson.dumps(key) + b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            logger.info("fill_dedup_persisted", count=len(keys_snapshot), path=path)
        except Exception as exc:
            logger.warning("fill_dedup_persist_failed", error=str(exc), path=path)

    def _maybe_persist_fill_dedup(self, *, force: bool = False) -> None:
        """Throttle fill dedup checkpointing to bound crash-recovery loss.

        Offloads the synchronous fsync to the default thread pool executor.
        This ran inline on the event loop, once per second for as long as fills
        kept arriving, which is a blocking write on the loop that also carries
        market data and order dispatch (Constitution Law 3). The order_id_map
        twin was offloaded for exactly this reason; this one was not.
        """
        now_s = time.monotonic()
        if not force and self._fill_dedup_persist_interval_s > 0:
            if (now_s - self._fill_dedup_last_persist_seconds) < self._fill_dedup_persist_interval_s:
                self._schedule_trailing_fill_dedup_persist(now_s)
                return
        self._cancel_trailing_fill_dedup_persist()
        self._fill_dedup_last_persist_seconds = now_s
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.persist_fill_dedup()
            return
        future = loop.run_in_executor(None, self.persist_fill_dedup)
        self._fill_dedup_persist_futures.add(future)
        future.add_done_callback(self._fill_dedup_persist_futures.discard)

    def _schedule_trailing_fill_dedup_persist(self, now_seconds: float) -> None:
        """Arm a one-shot flush for the end of the current throttle window."""
        if self._fill_dedup_trailing_handle is not None:
            return
        delay = max(0.0, self._fill_dedup_persist_interval_s - (now_seconds - self._fill_dedup_last_persist_seconds))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._fill_dedup_trailing_handle = loop.call_later(delay, self._run_trailing_fill_dedup_persist)

    def _run_trailing_fill_dedup_persist(self) -> None:
        self._fill_dedup_trailing_handle = None
        self._maybe_persist_fill_dedup(force=True)

    def _cancel_trailing_fill_dedup_persist(self) -> None:
        handle = self._fill_dedup_trailing_handle
        if handle is not None:
            handle.cancel()
            self._fill_dedup_trailing_handle = None

    async def flush_fill_dedup(self) -> None:
        """Wait for every dedup checkpoint already scheduled to reach disk."""
        pending = [f for f in tuple(self._fill_dedup_persist_futures) if not f.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

    def _realized_before(self, fill: Any) -> int:
        """Realized PnL the fill's position carries before it, the baseline of its delta.

        ``PositionStore.realized_before_fill`` includes the recovered lot a
        first fill is seeded from; reading ``positions`` alone booked that
        lot's history as today's PnL (Codex review of B0-B6, 2026-10-24).
        The fallback serves test doubles. Read under ``_fill_lock`` (Wave 3,
        2026-04-25) so a concurrent ``on_fill_async`` cannot tear it.
        """
        store = self.position_store
        if isinstance(store, PositionStore):
            return store.realized_before_fill(fill)
        pos_key = f"{fill.account_id}:{fill.strategy_id}:{fill.symbol}"
        fill_lock = getattr(store, "_fill_lock", None)
        if fill_lock is not None:
            with fill_lock:
                pos = store.positions.get(pos_key)
        else:
            pos = store.positions.get(pos_key)
        return pos.realized_pnl_scaled if pos is not None else 0

    def _register_fill_dedup_key(self, dedup_key: str) -> None:
        self._seen_fill_ids[dedup_key] = None
        if len(self._seen_fill_ids) > self._fill_dedup_max_size:
            self._seen_fill_ids.popitem(last=False)
        self._maybe_persist_fill_dedup()

    def _backfill_order_id_map(self, raw: RawExecEvent) -> None:
        """Extract broker IDs from order callback and backfill order_id_map.

        Shioaji's place_order() returns a Trade object with empty ordno/seqno.
        These fields are only populated in the subsequent order callback.
        This method extracts ALL broker IDs from the order callback payload,
        finds the order_key via any already-registered ID (e.g. ``order.id``),
        and registers the remaining IDs so deal callbacks can resolve strategy_id.
        """
        d = raw.data
        if isinstance(d, dict) and "payload" in d:
            d = d.get("payload", d)
        if not isinstance(d, dict):
            return
        order_section = d.get("order", {}) if isinstance(d.get("order"), dict) else {}
        status_section = d.get("status", {}) if isinstance(d.get("status"), dict) else {}
        _id_fields = ("id", "seqno", "seq_no", "ordno", "ord_no", "order_id")
        strong_names = set(self.normalizer.field_map.order_id_keys())
        strong_ids: list[str] = []
        weak_ids: list[str] = []
        for src in (d, order_section, status_section):
            for key in _id_fields:
                val = src.get(key) if isinstance(src, dict) else getattr(src, key, None)
                if val and str(val) not in strong_ids and str(val) not in weak_ids:
                    (strong_ids if key in strong_names else weak_ids).append(str(val))
        if not strong_ids and not weak_ids:
            return
        order_key = None
        resolver = self.normalizer.order_id_resolver
        for candidate in (*strong_ids, *weak_ids):
            mapped = resolver.order_id_map.get(candidate)
            if mapped:
                resolved = resolver.normalize_order_key(mapped)
                if resolved and (candidate in strong_ids or resolver.key_is_live(resolved)):
                    order_key = resolved
                    break
        if not order_key:
            return
        changed = False
        newly_mapped: list[str] = []
        with resolver.lock:
            for broker_id in (*strong_ids, *weak_ids):
                current = resolver.order_id_map.get(broker_id)
                stale = (
                    current is not None
                    and broker_id in weak_ids
                    and resolver.normalize_order_key(current) != order_key
                    and not resolver.key_is_live(resolver.normalize_order_key(current))
                )
                if current is None or stale:
                    resolver.order_id_map[broker_id] = order_key
                    logger.debug(
                        "order_id_map_set",
                        token=broker_id,
                        order_key=order_key,
                        source="execution_router_backfill",
                    )
                    changed = True
                    newly_mapped.append(broker_id)
        if changed:
            logger.debug(
                "order_id_map_backfilled",
                order_key=order_key,
                new_ids=newly_mapped,
            )

    def _resolve_order_key_for_fill(self, fill: Any) -> str | None:
        client_order_id = str(getattr(fill, "client_order_id", "") or "")
        if client_order_id:
            return client_order_id
        order_id = str(getattr(fill, "order_id", "") or "")
        if not order_id:
            return None
        return self.normalizer.order_id_resolver.resolve_order_key_candidate(order_id)

    async def run(self) -> None:
        self.running = True
        logger.info("ExecutionRouter started")
        self.metrics.execution_router_alive.set(1)
        self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())
        while self.running:
            try:
                raw: RawExecEvent = await self.raw_queue.get()
                if self._overflow_buf:
                    while self._overflow_buf:
                        try:
                            self.raw_queue.put_nowait(self._overflow_buf.popleft())
                            self.metrics.exec_overflow_drained_total.inc()
                        except asyncio.QueueFull:
                            break
                now_ns = timebase.now_ns()
                if raw.ingest_ts_ns:
                    self.metrics.execution_router_lag_ns.observe(now_ns - raw.ingest_ts_ns)
                self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())

                if raw.topic == "order":
                    self._backfill_order_id_map(raw)
                    order_event = self.normalizer.normalize_order(raw)
                    if order_event:
                        self._apply_fill_ledger(order_event)
                        self._release_slot_once(order_event)
                        self._publish_nowait(order_event)

                        if self._recorder_queue is not None and self._symbol_metadata is not None:
                            from hft_platform.recorder.mapper import map_event_to_record

                            _mapped = map_event_to_record(order_event, self._symbol_metadata, self._price_codec)
                            if _mapped:
                                _topic, _payload = _mapped
                                try:
                                    self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                                except asyncio.QueueFull:
                                    self.metrics.recorder_exec_drops_total.labels(topic="orders").inc()
                                    logger.warning("recorder_queue_full", topic="orders", event_type="order")
                                    self._wal_fallback_write(_topic, _payload)

                        if int(order_event.status) >= 3:
                            handler = self.terminal_handler
                            if callable(handler):
                                result = handler(order_event.strategy_id, order_event.order_id)
                                if inspect.iscoroutine(result):
                                    _create_task_with_error_handling(
                                        result,
                                        name=f"terminal_handler:{order_event.strategy_id}:{order_event.order_id}",
                                    )
                            elif hasattr(handler, "on_terminal_state"):
                                method = handler.on_terminal_state
                                result = method(order_event.strategy_id, order_event.order_id)
                                if inspect.iscoroutine(result):
                                    _create_task_with_error_handling(
                                        result,
                                        name=f"terminal_state:{order_event.strategy_id}:{order_event.order_id}",
                                    )

                elif raw.topic == "deal":
                    fill_event = self.normalizer.normalize_fill(raw)
                    if fill_event is None:
                        self.metrics.fill_normalization_failed_total.inc()
                        self._wal_fallback_write("deal_normalization_failed", raw.data)
                        continue
                    if fill_event:
                        _dedup_key = fill_event.fill_id or _synthesize_dedup_key(fill_event)
                        if _dedup_key in self._seen_fill_ids:
                            self.metrics.duplicate_fill_total.inc()
                            logger.warning(
                                "duplicate_fill_skipped",
                                fill_id=fill_event.fill_id,
                                dedup_key=_dedup_key,
                                symbol=fill_event.symbol,
                            )
                            continue
                        self.metrics.fills_total.inc()
                        if fill_event.strategy_id == "UNKNOWN":
                            _phantom_resolved = False
                            if self._phantom_resolver is not None:
                                try:
                                    _phantom_strat = self._phantom_resolver(fill_event)
                                    if _phantom_strat and _phantom_strat != "UNKNOWN":
                                        fill_event = dataclasses.replace(fill_event, strategy_id=_phantom_strat)
                                        _phantom_resolved = True
                                        _phantom_metric = getattr(self.metrics, "phantom_fill_reconciled_total", None)
                                        if _phantom_metric is not None:
                                            _phantom_metric.inc()
                                        logger.warning(
                                            "orphaned_fill_phantom_reconciled",
                                            symbol=fill_event.symbol,
                                            order_id=fill_event.order_id,
                                            resolved_strategy=_phantom_strat,
                                        )
                                except Exception as _phantom_exc:
                                    logger.warning(
                                        "phantom_resolver_error",
                                        error=str(_phantom_exc),
                                        symbol=fill_event.symbol,
                                    )
                            if not _phantom_resolved:
                                from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

                                dlq = get_orphaned_fill_dlq()
                                dlq.add(fill_event)
                                self.metrics.orphaned_fill_total.inc()
                                logger.warning(
                                    "Orphaned fill routed to DLQ",
                                    symbol=fill_event.symbol,
                                    order_id=fill_event.order_id,
                                )
                                if self._recorder_queue is not None and self._symbol_metadata is not None:
                                    from hft_platform.recorder.mapper import map_event_to_record

                                    _orphan_mapped = map_event_to_record(
                                        fill_event, self._symbol_metadata, self._price_codec
                                    )
                                    if _orphan_mapped:
                                        _o_topic, _o_payload = _orphan_mapped
                                        try:
                                            self._recorder_queue.put_nowait({"topic": _o_topic, "data": _o_payload})
                                        except asyncio.QueueFull:
                                            self.metrics.recorder_exec_drops_total.labels(topic="fills").inc()
                                            self._wal_fallback_write(_o_topic, _o_payload)
                                continue

                        _order_key = self._resolve_order_key_for_fill(fill_event)
                        if _order_key is not None:
                            _cmd_created_ns = self._cmd_created_ns_map.get(_order_key, 0)
                            if _cmd_created_ns > 0:
                                _latency_ns = fill_event.ingest_ts_ns - _cmd_created_ns
                                if _latency_ns > 0:
                                    self.metrics.e2e_order_latency_ns.observe(_latency_ns)

                        if _order_key is not None:
                            fill_event.client_order_id = _order_key
                            _tca = self._cmd_tca_map.get(_order_key)
                            if _tca is not None:
                                fill_event.decision_price = _tca[0]
                                fill_event.arrival_price = _tca[1]
                            _trace = self._cmd_trace_id_map.get(_order_key, "")
                            if _trace:
                                fill_event.trace_id = _trace

                        _pre_realized = self._realized_before(fill_event) if self._risk_engine is not None else 0

                        if hasattr(self.position_store, "on_fill_async"):
                            delta = await self.position_store.on_fill_async(fill_event)
                        else:
                            delta = self.position_store.on_fill(fill_event)
                        self._register_fill_dedup_key(_dedup_key)

                        if self._risk_engine is not None:
                            pnl_delta = _net_pnl_delta(delta.realized_pnl - _pre_realized, fill_event)
                            if pnl_delta != 0:
                                notify = getattr(self._risk_engine, "notify_fill_pnl", None)
                                if callable(notify):
                                    notify(fill_event.strategy_id, pnl_delta)

                        publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
                        if publish_many_nowait:
                            publish_many_nowait([delta, fill_event])
                        else:
                            self._publish_nowait(delta)
                            self._publish_nowait(fill_event)

                        if self._recorder_queue is not None and self._symbol_metadata is not None:
                            from hft_platform.recorder.mapper import map_event_to_record

                            _mapped = map_event_to_record(fill_event, self._symbol_metadata, self._price_codec)
                            if _mapped:
                                _topic, _payload = _mapped
                                try:
                                    self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                                except asyncio.QueueFull:
                                    self.metrics.recorder_exec_drops_total.labels(topic="fills").inc()
                                    logger.warning("recorder_queue_full", topic="fills", event_type="fill")
                                    self._wal_fallback_write(_topic, _payload)

                        if _order_key is not None and self._fill_tracker is not None:
                            try:
                                await self._fill_tracker.on_order_fill(_order_key, int(fill_event.qty))
                            except Exception as _ft_exc:
                                logger.warning("fill_tracker_error", error=str(_ft_exc), order_key=_order_key)

                self._events_since_dlq_retry += 1
                if self._events_since_dlq_retry >= self._dlq_retry_interval:
                    self._events_since_dlq_retry = 0
                    await self._retry_orphaned_fills()

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.execution_router_errors_total.inc()
                logger.error("ExecutionRouter Error", error=str(e))
            finally:
                try:
                    self.raw_queue.task_done()
                except ValueError:
                    pass
        self.metrics.execution_router_alive.set(0)

    async def stop(self, drain_timeout_s: float = 2.0) -> int:
        """Graceful shutdown: stop accepting new events and drain remaining queue items.

        Returns the number of events drained during shutdown.
        """
        self.running = False
        drained = 0
        import asyncio as _asyncio

        deadline = timebase.now_ns() + int(drain_timeout_s * 1_000_000_000)
        while timebase.now_ns() < deadline:
            try:
                raw = self.raw_queue.get_nowait()
            except _asyncio.QueueEmpty:
                break
            try:
                if raw.topic == "order":
                    self._backfill_order_id_map(raw)
                    order_event = self.normalizer.normalize_order(raw)
                    if order_event:
                        self._publish_nowait(order_event)
                        if int(order_event.status) >= 3:
                            handler = self.terminal_handler
                            if callable(handler):
                                result = handler(order_event.strategy_id, order_event.order_id)
                                if inspect.iscoroutine(result):
                                    await result
                            elif hasattr(handler, "on_terminal_state"):
                                method = handler.on_terminal_state
                                result = method(order_event.strategy_id, order_event.order_id)
                                if inspect.iscoroutine(result):
                                    await result
                        drained += 1
                        logger.info(
                            "shutdown_drain_order",
                            order_id=order_event.order_id,
                            status=order_event.status,
                        )
                elif raw.topic == "deal":
                    fill_event = self.normalizer.normalize_fill(raw)
                    if fill_event:
                        if fill_event.strategy_id == "UNKNOWN":
                            _sd_phantom_resolved = False
                            if self._phantom_resolver is not None:
                                try:
                                    _sd_strat = self._phantom_resolver(fill_event)
                                    if _sd_strat and _sd_strat != "UNKNOWN":
                                        fill_event = dataclasses.replace(fill_event, strategy_id=_sd_strat)
                                        _sd_phantom_resolved = True
                                        _sd_metric = getattr(self.metrics, "phantom_fill_reconciled_total", None)
                                        if _sd_metric is not None:
                                            _sd_metric.inc()
                                        logger.warning(
                                            "shutdown_drain_phantom_reconciled",
                                            symbol=fill_event.symbol,
                                            order_id=fill_event.order_id,
                                            resolved_strategy=_sd_strat,
                                        )
                                except Exception:
                                    pass
                            if not _sd_phantom_resolved:
                                from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

                                dlq = get_orphaned_fill_dlq()
                                dlq.add(fill_event)
                                self.metrics.orphaned_fill_total.inc()
                                logger.warning(
                                    "shutdown_drain_orphaned_fill_to_dlq",
                                    symbol=fill_event.symbol,
                                    order_id=fill_event.order_id,
                                )
                                if self._recorder_queue is not None and self._symbol_metadata is not None:
                                    from hft_platform.recorder.mapper import map_event_to_record

                                    _sd_mapped = map_event_to_record(
                                        fill_event, self._symbol_metadata, self._price_codec
                                    )
                                    if _sd_mapped:
                                        _sd_topic, _sd_payload = _sd_mapped
                                        try:
                                            self._recorder_queue.put_nowait({"topic": _sd_topic, "data": _sd_payload})
                                        except asyncio.QueueFull:
                                            self._wal_fallback_write(_sd_topic, _sd_payload)
                                continue
                        _dedup_key = fill_event.fill_id or _synthesize_dedup_key(fill_event)
                        if _dedup_key not in self._seen_fill_ids:
                            _drain_order_key = self._order_id_map.get(fill_event.order_id)
                            if _drain_order_key is not None:
                                _drain_tca = self._cmd_tca_map.get(_drain_order_key)
                                if _drain_tca is not None:
                                    fill_event.decision_price = _drain_tca[0]
                                    fill_event.arrival_price = _drain_tca[1]
                                _drain_trace = self._cmd_trace_id_map.get(_drain_order_key, "")
                                if _drain_trace:
                                    fill_event.trace_id = _drain_trace
                            if hasattr(self.position_store, "on_fill"):
                                _pre_realized_sd = (
                                    self._realized_before(fill_event) if self._risk_engine is not None else 0
                                )
                                delta = self.position_store.on_fill(fill_event)
                                self._register_fill_dedup_key(_dedup_key)
                                drained += 1
                                if self._recorder_queue is not None and self._symbol_metadata is not None:
                                    from hft_platform.recorder.mapper import map_event_to_record

                                    _mapped = map_event_to_record(fill_event, self._symbol_metadata, self._price_codec)
                                    if _mapped:
                                        _topic, _payload = _mapped
                                        try:
                                            self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                                        except asyncio.QueueFull:
                                            self._wal_fallback_write(_topic, _payload)
                                    else:
                                        logger.warning("shutdown_drain_fill_unmappable", fill_id=fill_event.fill_id)
                                else:
                                    self._wal_fallback_write("fills", fill_event)
                                if self._risk_engine is not None:
                                    pnl_delta_sd = _net_pnl_delta(delta.realized_pnl - _pre_realized_sd, fill_event)
                                    if pnl_delta_sd != 0:
                                        notify = getattr(self._risk_engine, "notify_fill_pnl", None)
                                        if callable(notify):
                                            notify(fill_event.strategy_id, pnl_delta_sd)
                                publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
                                if publish_many_nowait:
                                    publish_many_nowait([delta, fill_event])
                                else:
                                    self._publish_nowait(delta)
                                    self._publish_nowait(fill_event)
                                logger.info("shutdown_drain_fill", fill_id=fill_event.fill_id, dedup_key=_dedup_key)
            except Exception as exc:
                logger.warning("shutdown_drain_error", error=str(exc))
            finally:
                try:
                    self.raw_queue.task_done()
                except ValueError:
                    pass
        if drained > 0:
            logger.info("shutdown_drain_complete", drained=drained)
        return drained

    async def recover_fill_gaps(
        self,
        checkpoint_path: str = ".state/position_checkpoint.json",
    ) -> dict[str, int]:
        """Cold-path fill gap recovery at startup.

        Loads persisted DLQ and retries orphaned fills using an enhanced
        resolver that falls back to checkpoint symbol→strategy mapping when
        order_id_map is empty (typical after crash).

        Returns ``{resolved, unresolved, skipped_dedup}`` counts.
        """
        from hft_platform.execution.checkpoint import PositionCheckpointWriter
        from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

        dlq = get_orphaned_fill_dlq()
        if dlq.count == 0:
            logger.info("recover_fill_gaps: DLQ empty, nothing to recover")
            return {"resolved": 0, "unresolved": 0, "skipped_dedup": 0}

        ckpt_symbol_strategy: dict[str, str] = {}
        ckpt_data = PositionCheckpointWriter.load_checkpoint(checkpoint_path)
        if ckpt_data is not None:
            for key, pos_data in ckpt_data.get("positions", {}).items():
                parts = key.split(":")
                if len(parts) >= 3:
                    strategy_id = parts[1]
                    symbol = pos_data.get("symbol", parts[-1])
                    if strategy_id and strategy_id != MANUAL_STRATEGY_ID:
                        ckpt_symbol_strategy[symbol] = strategy_id

        logger.info(
            "recover_fill_gaps: starting",
            dlq_count=dlq.count,
            checkpoint_strategies=len(ckpt_symbol_strategy),
        )

        def _enhanced_resolve(fill: Any) -> str:
            from hft_platform.execution.normalizer import RawExecEvent

            raw = RawExecEvent(
                topic="deal",
                data={"ordno": fill.order_id, "code": fill.symbol, "action": fill.side.name},
                ingest_ts_ns=fill.ingest_ts_ns,
            )
            resolved_id = self.normalizer._resolve_strategy_id(raw)
            if resolved_id and resolved_id != "UNKNOWN":
                return resolved_id
            ckpt_strat = ckpt_symbol_strategy.get(fill.symbol)
            if ckpt_strat:
                logger.info(
                    "recover_fill_gaps: checkpoint fallback",
                    symbol=fill.symbol,
                    strategy_id=ckpt_strat,
                    fill_id=fill.fill_id,
                )
                return ckpt_strat
            return "UNKNOWN"

        resolved, still_orphaned = dlq.retry(_enhanced_resolve)

        skipped_dedup = 0
        applied = 0
        for fill in resolved:
            _dedup_key = fill.fill_id or _synthesize_dedup_key(fill)
            if _dedup_key in self._seen_fill_ids:
                skipped_dedup += 1
                continue
            if hasattr(self.position_store, "on_fill"):
                self.position_store.on_fill(fill)
            self._register_fill_dedup_key(_dedup_key)
            applied += 1

        logger.info(
            "recover_fill_gaps: complete",
            resolved=len(resolved),
            applied=applied,
            skipped_dedup=skipped_dedup,
            unresolved=len(still_orphaned),
        )
        return {
            "resolved": applied,
            "unresolved": len(still_orphaned),
            "skipped_dedup": skipped_dedup,
        }

    async def _retry_orphaned_fills(self) -> None:
        from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

        dlq = get_orphaned_fill_dlq()
        if dlq.count == 0:
            return

        def _resolve(fill: Any) -> str:
            from hft_platform.execution.normalizer import RawExecEvent

            raw = RawExecEvent(
                topic="deal",
                data={"ordno": fill.order_id, "code": fill.symbol, "action": fill.side.name},
                ingest_ts_ns=fill.ingest_ts_ns,
            )
            resolved_id = self.normalizer._resolve_strategy_id(raw)
            return resolved_id

        resolved, still_orphaned = dlq.retry(_resolve)
        if resolved:
            logger.info(
                "DLQ retry resolved fills",
                count=len(resolved),
                remaining=len(still_orphaned),
            )
            for fill in resolved:
                _dedup_key = fill.fill_id or _synthesize_dedup_key(fill)
                if _dedup_key in self._seen_fill_ids:
                    self.metrics.duplicate_fill_total.inc()
                    logger.warning(
                        "duplicate_fill_skipped_dlq",
                        fill_id=fill.fill_id,
                        dedup_key=_dedup_key,
                        symbol=fill.symbol,
                    )
                    continue
                _order_key = self._resolve_order_key_for_fill(fill)
                if _order_key is not None:
                    fill.client_order_id = _order_key
                    _tca = self._cmd_tca_map.get(_order_key)
                    if _tca is not None:
                        fill.decision_price = _tca[0]
                        fill.arrival_price = _tca[1]
                    _dlq_trace = self._cmd_trace_id_map.get(_order_key, "")
                    if _dlq_trace:
                        fill.trace_id = _dlq_trace

                _pre_realized_dlq = self._realized_before(fill) if self._risk_engine is not None else 0

                if hasattr(self.position_store, "on_fill_async"):
                    delta = await self.position_store.on_fill_async(fill)
                elif hasattr(self.position_store, "on_fill"):
                    delta = self.position_store.on_fill(fill)
                else:
                    continue
                self._register_fill_dedup_key(_dedup_key)
                if self._risk_engine is not None and delta is not None:
                    pnl_delta_dlq = _net_pnl_delta(delta.realized_pnl - _pre_realized_dlq, fill)
                    if pnl_delta_dlq != 0:
                        notify = getattr(self._risk_engine, "notify_fill_pnl", None)
                        if callable(notify):
                            notify(fill.strategy_id, pnl_delta_dlq)
                publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
                if publish_many_nowait:
                    publish_many_nowait([delta, fill])
                else:
                    self._publish_nowait(delta)
                    self._publish_nowait(fill)
                if self._recorder_queue is not None and self._symbol_metadata is not None:
                    from hft_platform.recorder.mapper import map_event_to_record

                    _mapped = map_event_to_record(fill, self._symbol_metadata, self._price_codec)
                    if _mapped:
                        _topic, _payload = _mapped
                        try:
                            self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                        except asyncio.QueueFull:
                            self.metrics.recorder_exec_drops_total.labels(topic="fills").inc()
                            logger.warning("recorder_queue_full", topic="fills", event_type="fill_dlq_retry")
                            self._wal_fallback_write(_topic, _payload)
            _dlq_metric = getattr(self.metrics, "dlq_retry_resolved_total", None)
            if _dlq_metric is not None:
                _dlq_metric.inc(len(resolved))

    def _wal_fallback_write(self, topic: str, payload: Any) -> None:
        """WAL write when recorder queue is full. Logs failures via done_callback."""
        if self._wal_writer is None:
            _symbol = getattr(payload, "symbol", None) if payload is not None else None
            logger.critical(
                "fill_data_loss",
                event_type=topic,
                symbol=_symbol,
                reason="wal_writer_none_and_recorder_full",
            )
            _loss = getattr(self.metrics, "exec_fill_data_loss_total", None)
            if _loss is not None:
                _loss.inc()
            return
        try:
            _wal_fallback = getattr(self.metrics, "recorder_exec_wal_fallback_total", None)
            if _wal_fallback is not None:
                _wal_fallback.labels(topic=topic).inc()
            task = asyncio.ensure_future(self._wal_writer.write(topic, [payload]))

            def _on_wal_done(fut: asyncio.Future[Any]) -> None:
                if fut.cancelled():
                    return
                exc = fut.exception()
                if exc is not None:
                    logger.error("wal_fallback_async_failed", error=str(exc), topic=topic)
                    _fail = getattr(self.metrics, "recorder_exec_wal_fallback_failure_total", None)
                    if _fail is not None:
                        _fail.labels(topic=topic).inc()

            task.add_done_callback(_on_wal_done)
        except Exception as wal_exc:
            logger.error("wal_fallback_failed", error=str(wal_exc), topic=topic)

    def _publish_nowait(self, event: Any) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        _create_task_with_error_handling(
            self.bus.publish(event),
            name=f"bus_publish:{type(event).__name__}",
        )
