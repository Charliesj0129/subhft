import asyncio
import inspect
from typing import Callable, Dict, Optional, Union

from hft_platform.core import timebase
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry
from structlog import get_logger

logger = get_logger("execution.router")


def _create_task_with_error_handling(coro, name: Optional[str] = None) -> asyncio.Task:
    """Create an asyncio task with proper exception handling to prevent silent failures.

    Args:
        coro: The coroutine to run as a task.
        name: Optional name for the task for logging purposes.

    Returns:
        The created task with exception callback attached.
    """
    task = asyncio.create_task(coro, name=name)

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
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self.normalizer = ExecutionNormalizer(raw_queue, order_id_map)
        self.position_store = position_store
        self.terminal_handler = terminal_handler
        self.running = False
        self.metrics = MetricsRegistry.get()

    async def run(self):
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
                    norm = self.normalizer.normalize_order(raw)
                    if norm:
                        self._publish_nowait(norm)
                        # OrderStatus 3=FILLED, 4=CANCELLED, 5=FAILED
                        if int(norm.status) >= 3:
                            handler = self.terminal_handler
                            if callable(handler):
                                result = handler(norm.strategy_id, norm.order_id)
                                if inspect.iscoroutine(result):
                                    _create_task_with_error_handling(
                                        result,
                                        name=f"terminal_handler:{norm.strategy_id}:{norm.order_id}",
                                    )
                            elif hasattr(handler, "on_terminal_state"):
                                method = handler.on_terminal_state
                                result = method(norm.strategy_id, norm.order_id)
                                if inspect.iscoroutine(result):
                                    _create_task_with_error_handling(
                                        result,
                                        name=f"terminal_state:{norm.strategy_id}:{norm.order_id}",
                                    )

                elif raw.topic == "deal":
                    norm = self.normalizer.normalize_fill(raw)
                    if norm:
                        # Use async version to avoid blocking event loop with Rust FFI lock
                        if hasattr(self.position_store, "on_fill_async"):
                            delta = await self.position_store.on_fill_async(norm)
                        else:
                            delta = self.position_store.on_fill(norm)
                        publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
                        if publish_many_nowait:
                            publish_many_nowait([delta, norm])
                        else:
                            self._publish_nowait(delta)
                            self._publish_nowait(norm)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.execution_router_errors_total.inc()
                logger.error("ExecutionRouter Error", error=str(e))
            finally:
                try:
                    self.raw_queue.task_done()
                except Exception:
                    pass
        self.metrics.execution_router_alive.set(0)

    def _publish_nowait(self, event) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        _create_task_with_error_handling(
            self.bus.publish(event),
            name=f"bus_publish:{type(event).__name__}",
        )
