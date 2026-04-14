import asyncio
import collections
import inspect
import os
import tempfile
from collections.abc import Coroutine
from typing import Any, Callable, Dict, Optional, Union

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.recorder.wal import WALWriter

logger = get_logger("execution.router")


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
        recorder_queue: Optional[asyncio.Queue] = None,
        symbol_metadata: Optional[Any] = None,
        price_scale_provider: Optional[Any] = None,
        wal_writer: Optional[WALWriter] = None,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self._order_id_map = order_id_map
        self.normalizer = ExecutionNormalizer(raw_queue, order_id_map)
        self.position_store = position_store
        self.terminal_handler = terminal_handler
        self._risk_engine = risk_engine
        self._overflow_buf = overflow_buf
        self._cmd_created_ns_map: Dict[str, int] = cmd_created_ns_map if cmd_created_ns_map is not None else {}
        self._cmd_tca_map: Dict[str, tuple[int, int]] = cmd_tca_map if cmd_tca_map is not None else {}
        self.running = False
        self.metrics = MetricsRegistry.get()
        self._dlq_retry_interval = int(os.getenv("HFT_DLQ_RETRY_INTERVAL", "100"))  # Retry DLQ every N events processed
        # Fill deduplication: prevent double-counting on broker reconnect (bounded FIFO dict)
        self._fill_dedup_max_size: int = int(os.environ.get("HFT_FILL_DEDUP_MAX_SIZE", "10000"))
        self._seen_fill_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        self._fill_dedup_persist_path: str = os.environ.get(
            "HFT_FILL_DEDUP_PERSIST_PATH", ".state/fill_dedup_window.jsonl"
        )
        self._fill_dedup_persist_interval_s: float = float(os.environ.get("HFT_FILL_DEDUP_PERSIST_INTERVAL_S", "1.0"))
        self._fill_dedup_last_persist_s: float = 0.0  # noqa: monotonic timestamp
        self._load_fill_dedup()
        self._events_since_dlq_retry = 0
        self._recorder_queue: Optional[asyncio.Queue] = recorder_queue
        self._symbol_metadata = symbol_metadata
        self._price_codec: Optional[PriceCodec] = (
            PriceCodec(price_scale_provider) if price_scale_provider is not None else None
        )
        self._wal_writer: Optional[WALWriter] = wal_writer

    def set_risk_engine(self, risk_engine: object) -> None:
        """Set or replace the risk engine reference (late-bind from bootstrap)."""
        self._risk_engine = risk_engine

    def set_overflow_buf(self, buf: collections.deque) -> None:
        """Set or replace the overflow buffer (late-bind from system supervisor)."""
        self._overflow_buf = buf

    def _load_fill_dedup(self) -> None:
        """Load fill dedup window from disk on startup (restart-safe dedup)."""
        path = self._fill_dedup_persist_path
        if not os.path.exists(path):
            return
        try:
            import orjson

            loaded = 0
            with open(path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        key = orjson.loads(raw)
                        if isinstance(key, str) and key:
                            self._seen_fill_ids[key] = None
                            loaded += 1
                    except Exception:
                        continue
            # Enforce max size
            while len(self._seen_fill_ids) > self._fill_dedup_max_size:
                self._seen_fill_ids.popitem(last=False)
            logger.info("fill_dedup_loaded", count=loaded, path=path)
        except Exception as exc:
            logger.warning("fill_dedup_load_failed", error=str(exc), path=path)

    def persist_fill_dedup(self) -> None:
        """Persist fill dedup window to disk atomically (temp+fsync+rename).

        Called during graceful shutdown. Safe to call from thread pool.
        """
        path = self._fill_dedup_persist_path
        # Snapshot under CPython GIL atomicity
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
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            logger.info("fill_dedup_persisted", count=len(keys_snapshot), path=path)
        except Exception as exc:
            logger.warning("fill_dedup_persist_failed", error=str(exc), path=path)

    def _maybe_persist_fill_dedup(self, *, force: bool = False) -> None:
        """Throttle fill dedup checkpointing to bound crash-recovery loss."""
        now_s = timebase.now_ns() / 1_000_000_000
        if not force and self._fill_dedup_persist_interval_s > 0:
            if (now_s - self._fill_dedup_last_persist_s) < self._fill_dedup_persist_interval_s:
                return
        self.persist_fill_dedup()
        self._fill_dedup_last_persist_s = now_s

    def _register_fill_dedup_key(self, dedup_key: str) -> None:
        self._seen_fill_ids[dedup_key] = None
        if len(self._seen_fill_ids) > self._fill_dedup_max_size:
            self._seen_fill_ids.popitem(last=False)  # evict oldest
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
        # Gather every candidate broker ID from the payload
        _id_fields = ("id", "seqno", "seq_no", "ordno", "ord_no", "order_id")
        ids: set[str] = set()
        for src in (d, order_section, status_section):
            for key in _id_fields:
                val = src.get(key) if isinstance(src, dict) else getattr(src, key, None)
                if val:
                    ids.add(str(val))
        ids.discard("")
        if not ids:
            return
        # Find order_key from any already-registered ID
        order_key = None
        resolver = self.normalizer.order_id_resolver
        for candidate in ids:
            mapped = resolver.order_id_map.get(candidate)
            if mapped:
                order_key = resolver.normalize_order_key(mapped)
                break
        if not order_key:
            return
        # Register all extracted IDs under the same order_key
        changed = False
        for broker_id in ids:
            if broker_id not in resolver.order_id_map:
                resolver.order_id_map[broker_id] = order_key
                changed = True
        if changed:
            logger.debug(
                "order_id_map_backfilled",
                order_key=order_key,
                new_ids=[i for i in ids if i not in resolver.order_id_map or resolver.order_id_map.get(i) == order_key],
            )

    async def run(self) -> None:
        self.running = True
        logger.info("ExecutionRouter started")
        self.metrics.execution_router_alive.set(1)
        self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())
        while self.running:
            try:
                raw: RawExecEvent = await self.raw_queue.get()
                # D1: Drain overflow buffer back into main queue when space is available
                if self._overflow_buf:
                    while self._overflow_buf:
                        try:
                            self.raw_queue.put_nowait(self._overflow_buf.popleft())
                            self.metrics.exec_overflow_drained_total.inc()
                        except asyncio.QueueFull:
                            break  # Queue still full, leave remaining for next iteration
                now_ns = timebase.now_ns()
                if raw.ingest_ts_ns:
                    self.metrics.execution_router_lag_ns.observe(now_ns - raw.ingest_ts_ns)
                self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())

                if raw.topic == "order":
                    self._backfill_order_id_map(raw)
                    order_event = self.normalizer.normalize_order(raw)
                    if order_event:
                        self._publish_nowait(order_event)

                        # Direct order recording safety net: bypass RingBufferBus
                        # to prevent order events from being overwritten by tick
                        # flood before _recorder_bridge consumes them.
                        if self._recorder_queue is not None and self._symbol_metadata is not None:
                            from hft_platform.recorder.mapper import map_event_to_record  # noqa: PLC0415

                            _mapped = map_event_to_record(order_event, self._symbol_metadata, self._price_codec)
                            if _mapped:
                                _topic, _payload = _mapped
                                try:
                                    self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                                except asyncio.QueueFull:
                                    self.metrics.recorder_exec_drops_total.labels(topic="orders").inc()
                                    logger.warning("recorder_queue_full", topic="orders", event_type="order")
                                    self._wal_fallback_write(_topic, _payload)

                        # OrderStatus 3=FILLED, 4=CANCELLED, 5=FAILED
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
                        # M3/M4: normalization failed (missing account, parse error, etc.)
                        # Persist raw event data to exec overflow DLQ for later recovery.
                        self.metrics.fill_normalization_failed_total.inc()
                        self._wal_fallback_write("deal_normalization_failed", raw.data)
                        continue
                    if fill_event:
                        # Fill deduplication: prevent double-counting on broker reconnect.
                        # When fill_id is empty (broker omitted seqno), synthesize a key
                        # from fill fields so dedup still catches reconnect replays.
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
                        self._register_fill_dedup_key(_dedup_key)
                        self.metrics.fills_total.inc()
                        if fill_event.strategy_id == "UNKNOWN":
                            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

                            dlq = get_orphaned_fill_dlq()
                            dlq.add(fill_event)
                            self.metrics.orphaned_fill_total.inc()
                            logger.warning(
                                "Orphaned fill routed to DLQ",
                                symbol=fill_event.symbol,
                                order_id=fill_event.order_id,
                            )
                            continue

                        # Observe e2e order-to-fill latency (SLO-2)
                        _order_key = self._order_id_map.get(fill_event.order_id)
                        if _order_key is not None:
                            _cmd_created_ns = self._cmd_created_ns_map.get(_order_key, 0)
                            if _cmd_created_ns > 0:
                                _latency_ns = fill_event.ingest_ts_ns - _cmd_created_ns
                                if _latency_ns > 0:
                                    self.metrics.e2e_order_latency_ns.observe(_latency_ns)

                        # TCA: enrich FillEvent with decision/arrival prices
                        if _order_key is not None:
                            _tca = self._cmd_tca_map.get(_order_key)
                            if _tca is not None:
                                fill_event.decision_price = _tca[0]
                                fill_event.arrival_price = _tca[1]

                        _pre_realized = 0
                        if self._risk_engine is not None:
                            _pos_key = f"{fill_event.account_id}:{fill_event.strategy_id}:{fill_event.symbol}"
                            _pre_pos = self.position_store.positions.get(_pos_key)
                            if _pre_pos is not None:
                                _pre_realized = _pre_pos.realized_pnl_scaled

                        if hasattr(self.position_store, "on_fill_async"):
                            delta = await self.position_store.on_fill_async(fill_event)
                        else:
                            delta = self.position_store.on_fill(fill_event)

                        if self._risk_engine is not None:
                            pnl_delta = delta.realized_pnl - _pre_realized
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

                        # Direct fill recording safety net: bypass RingBufferBus to prevent
                        # fills from being overwritten by tick flood before _recorder_bridge
                        # consumes them. Recording must never block the execution path.
                        if self._recorder_queue is not None and self._symbol_metadata is not None:
                            from hft_platform.recorder.mapper import map_event_to_record  # noqa: PLC0415

                            _mapped = map_event_to_record(fill_event, self._symbol_metadata, self._price_codec)
                            if _mapped:
                                _topic, _payload = _mapped
                                try:
                                    self._recorder_queue.put_nowait({"topic": _topic, "data": _payload})
                                except asyncio.QueueFull:
                                    self.metrics.recorder_exec_drops_total.labels(topic="fills").inc()
                                    logger.warning("recorder_queue_full", topic="fills", event_type="fill")
                                    self._wal_fallback_write(_topic, _payload)

                # Periodically retry orphaned fills from DLQ
                self._events_since_dlq_retry += 1
                if self._events_since_dlq_retry >= self._dlq_retry_interval:
                    self._events_since_dlq_retry = 0
                    await self._retry_orphaned_fills()

            except asyncio.CancelledError:
                break
            except Exception as e:  # noqa: BLE001 — supervisor catch-all
                self.metrics.execution_router_errors_total.inc()
                logger.error("ExecutionRouter Error", error=str(e))
            finally:
                try:
                    self.raw_queue.task_done()
                except ValueError:
                    pass  # task_done called too many times
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
                                    await result  # DECISION-004: stop() is async, so await is safe
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
                        # Same UNKNOWN check as main loop — route to DLQ instead of
                        # creating ghost positions with strategy_id="UNKNOWN".
                        if fill_event.strategy_id == "UNKNOWN":
                            from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq  # noqa: PLC0415

                            dlq = get_orphaned_fill_dlq()
                            dlq.add(fill_event)
                            self.metrics.orphaned_fill_total.inc()
                            logger.warning(
                                "shutdown_drain_orphaned_fill_to_dlq",
                                symbol=fill_event.symbol,
                                order_id=fill_event.order_id,
                            )
                            continue
                        _dedup_key = fill_event.fill_id or _synthesize_dedup_key(fill_event)
                        if _dedup_key not in self._seen_fill_ids:
                            self._register_fill_dedup_key(_dedup_key)
                            # TCA enrichment for shutdown drain fills
                            _drain_order_key = self._order_id_map.get(fill_event.order_id)
                            if _drain_order_key is not None:
                                _drain_tca = self._cmd_tca_map.get(_drain_order_key)
                                if _drain_tca is not None:
                                    fill_event.decision_price = _drain_tca[0]
                                    fill_event.arrival_price = _drain_tca[1]
                            if hasattr(self.position_store, "on_fill"):
                                _pre_realized_sd = 0
                                if self._risk_engine is not None:
                                    _pos_key_sd = (
                                        f"{fill_event.account_id}:{fill_event.strategy_id}:{fill_event.symbol}"
                                    )
                                    _pre_pos_sd = self.position_store.positions.get(_pos_key_sd)
                                    if _pre_pos_sd is not None:
                                        _pre_realized_sd = _pre_pos_sd.realized_pnl_scaled
                                delta = self.position_store.on_fill(fill_event)
                                drained += 1
                                # Persist fill via recorder queue (mapped) or WAL fallback
                                if self._recorder_queue is not None and self._symbol_metadata is not None:
                                    from hft_platform.recorder.mapper import map_event_to_record  # noqa: PLC0415

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
                                # Notify risk engine of PnL delta using PositionDelta
                                if self._risk_engine is not None:
                                    pnl_delta_sd = delta.realized_pnl - _pre_realized_sd
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
            except Exception as exc:  # noqa: BLE001
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
        from hft_platform.execution.checkpoint import PositionCheckpointWriter  # noqa: PLC0415
        from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq  # noqa: PLC0415

        dlq = get_orphaned_fill_dlq()
        if dlq.count == 0:
            logger.info("recover_fill_gaps: DLQ empty, nothing to recover")
            return {"resolved": 0, "unresolved": 0, "skipped_dedup": 0}

        # Build checkpoint-based symbol→strategy fallback map
        ckpt_symbol_strategy: dict[str, str] = {}
        ckpt_data = PositionCheckpointWriter.load_checkpoint(checkpoint_path)
        if ckpt_data is not None:
            for key, pos_data in ckpt_data.get("positions", {}).items():
                parts = key.split(":")
                if len(parts) >= 3:
                    strategy_id = parts[1]
                    symbol = pos_data.get("symbol", parts[-1])
                    if strategy_id and strategy_id != "*":
                        ckpt_symbol_strategy[symbol] = strategy_id

        logger.info(
            "recover_fill_gaps: starting",
            dlq_count=dlq.count,
            checkpoint_strategies=len(ckpt_symbol_strategy),
        )

        def _enhanced_resolve(fill: Any) -> str:
            # Primary: use normalizer resolver chain (order_id_map + custom_field)
            from hft_platform.execution.normalizer import RawExecEvent  # noqa: PLC0415

            raw = RawExecEvent(
                topic="deal",
                data={"ordno": fill.order_id, "code": fill.symbol, "action": fill.side.name},
                ingest_ts_ns=fill.ingest_ts_ns,
            )
            resolved_id = self.normalizer._resolve_strategy_id(raw)
            if resolved_id and resolved_id != "UNKNOWN":
                return resolved_id
            # Fallback: checkpoint symbol→strategy mapping
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
            self._register_fill_dedup_key(_dedup_key)
            if hasattr(self.position_store, "on_fill"):
                self.position_store.on_fill(fill)
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
            # Use full resolver chain (order_id_map + custom_field + pending fill index)
            # instead of just order_id_resolver which only checks order_id_map.
            from hft_platform.execution.normalizer import RawExecEvent  # noqa: PLC0415

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
                # Fill deduplication: skip fills already applied via the main path
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
                self._register_fill_dedup_key(_dedup_key)
                # TCA enrichment for DLQ-resolved fills (M4)
                _order_key = self._order_id_map.get(fill.order_id)
                if _order_key is not None:
                    _tca = self._cmd_tca_map.get(_order_key)
                    if _tca is not None:
                        fill.decision_price = _tca[0]
                        fill.arrival_price = _tca[1]

                # Capture pre-fill realized PnL for incremental delta
                _pre_realized_dlq = 0
                if self._risk_engine is not None:
                    _pos_key = f"{fill.account_id}:{fill.strategy_id}:{fill.symbol}"
                    _pre_pos = self.position_store.positions.get(_pos_key)
                    if _pre_pos is not None:
                        _pre_realized_dlq = _pre_pos.realized_pnl_scaled

                if hasattr(self.position_store, "on_fill_async"):
                    delta = await self.position_store.on_fill_async(fill)
                elif hasattr(self.position_store, "on_fill"):
                    delta = self.position_store.on_fill(fill)
                else:
                    continue
                if self._risk_engine is not None and delta is not None:
                    pnl_delta_dlq = delta.realized_pnl - _pre_realized_dlq
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
                    from hft_platform.recorder.mapper import map_event_to_record  # noqa: PLC0415

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
        except Exception as wal_exc:  # noqa: BLE001
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
