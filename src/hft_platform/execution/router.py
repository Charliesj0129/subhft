import asyncio
import collections
import inspect
from collections.abc import Coroutine
from typing import Any, Callable, Dict, Optional, Union

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("execution.router")


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
        self._dlq_retry_interval = 100  # Retry DLQ every 100 events processed
        self._events_since_dlq_retry = 0
        self._recorder_queue: Optional[asyncio.Queue] = recorder_queue
        self._symbol_metadata = symbol_metadata
        self._price_codec: Optional[PriceCodec] = (
            PriceCodec(price_scale_provider) if price_scale_provider is not None else None
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
                    if fill_event:
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

    async def _retry_orphaned_fills(self) -> None:
        from hft_platform.execution.fill_dlq import get_orphaned_fill_dlq

        dlq = get_orphaned_fill_dlq()
        if dlq.count == 0:
            return

        def _resolve(fill: Any) -> str:
            return self.normalizer.order_id_resolver.resolve_strategy_id(fill.order_id)

        resolved, still_orphaned = dlq.retry(_resolve)
        if resolved:
            logger.info(
                "DLQ retry resolved fills",
                count=len(resolved),
                remaining=len(still_orphaned),
            )
            for fill in resolved:
                # TCA enrichment for DLQ-resolved fills (M4)
                _order_key = self._order_id_map.get(fill.order_id)
                if _order_key is not None:
                    _tca = self._cmd_tca_map.get(_order_key)
                    if _tca is not None:
                        fill.decision_price = _tca[0]
                        fill.arrival_price = _tca[1]

                if hasattr(self.position_store, "on_fill_async"):
                    delta = await self.position_store.on_fill_async(fill)
                elif hasattr(self.position_store, "on_fill"):
                    delta = self.position_store.on_fill(fill)
                else:
                    continue
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
            _dlq_metric = getattr(self.metrics, "dlq_retry_resolved_total", None)
            if _dlq_metric is not None:
                _dlq_metric.inc(len(resolved))

    def _publish_nowait(self, event: Any) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        _create_task_with_error_handling(
            self.bus.publish(event),
            name=f"bus_publish:{type(event).__name__}",
        )
