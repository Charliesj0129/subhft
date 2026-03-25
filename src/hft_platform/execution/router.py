import asyncio
import inspect
from collections.abc import Coroutine
from typing import Any, Callable, Dict, Optional, Union

from structlog import get_logger

from hft_platform.core import timebase
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
        order_adapter: Optional[object] = None,
        fee_calculator: Optional[object] = None,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self._order_id_map = order_id_map
        self.normalizer = ExecutionNormalizer(
            raw_queue,
            order_id_map,
            fee_calculator=fee_calculator,
        )
        self.position_store = position_store
        self.terminal_handler = terminal_handler
        self._risk_engine = risk_engine
        self._order_adapter = order_adapter
        self._fee_calculator = fee_calculator
        self.running = False
        self.metrics = MetricsRegistry.get()

    async def run(self) -> None:
        self.running = True
        logger.info("ExecutionRouter started")
        self.metrics.execution_router_alive.set(1)
        self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())
        while self.running:
            try:
                raw: RawExecEvent = await self.raw_queue.get()
                now_ns = timebase.now_ns()
                if raw.ingest_ts_ns:
                    self.metrics.execution_router_lag_ns.observe(now_ns - raw.ingest_ts_ns)
                self.metrics.execution_router_heartbeat_ts.set(timebase.now_s())

                if raw.topic == "order":
                    order_event = self.normalizer.normalize_order(raw)
                    if order_event:
                        self._publish_nowait(order_event)
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
                    order_cmd = None
                    if self._order_adapter is not None:
                        order_key = ""
                        if hasattr(raw, "order_id"):
                            order_key = self._order_id_map.get(raw.order_id, "")
                        if not order_key:
                            for attr in ("order_id", "id", "ordno", "seqno"):
                                val = getattr(raw, attr, None) or (
                                    raw.data.get(attr) if isinstance(getattr(raw, "data", None), dict) else None
                                )
                                if val and str(val) in self._order_id_map:
                                    order_key = self._order_id_map[str(val)]
                                    break
                        if order_key:
                            order_cmd = self._order_adapter.get_inflight(order_key)
                    fill_event = self.normalizer.normalize_fill(raw, order_cmd)
                    if fill_event:
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

    def _publish_nowait(self, event: Any) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        _create_task_with_error_handling(
            self.bus.publish(event),
            name=f"bus_publish:{type(event).__name__}",
        )
