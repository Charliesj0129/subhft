import asyncio
import time
from typing import Callable, Dict

from structlog import get_logger

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("execution.router")


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
        terminal_handler: Callable[[str, str], None],
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
        self.metrics.execution_router_heartbeat_ts.set(time.time())
        while self.running:
            try:
                raw: RawExecEvent = await self.raw_queue.get()
                now_ns = time.time_ns()
                if raw.ingest_ts_ns:
                    self.metrics.execution_router_lag_ns.observe(now_ns - raw.ingest_ts_ns)
                self.metrics.execution_router_heartbeat_ts.set(time.time())

                if raw.topic == "order":
                    norm = self.normalizer.normalize_order(raw)
                    if norm:
                        self._publish_nowait(norm)
                        # OrderStatus 3=FILLED, 4=CANCELLED, 5=FAILED
                        if int(norm.status) >= 3:
                            handler = self.terminal_handler
                            if callable(handler):
                                handler(norm.strategy_id, norm.order_id)
                            elif hasattr(handler, "on_terminal_state"):
                                handler.on_terminal_state(norm.strategy_id, norm.order_id)

                elif raw.topic == "deal":
                    norm = self.normalizer.normalize_fill(raw)
                    if norm:
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
        asyncio.create_task(self.bus.publish(event))
