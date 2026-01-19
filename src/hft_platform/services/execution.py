import asyncio
from typing import Dict

from structlog import get_logger

from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.execution.positions import PositionStore
from hft_platform.order.adapter import OrderAdapter

logger = get_logger("service.execution")


class ExecutionService:
    """
    Handles Inbound Execution Reports (Fills, Order Updates).
    Updates PositionStore and publishes to Bus.
    """

    def __init__(
        self,
        bus: RingBufferBus,
        raw_queue: asyncio.Queue,
        order_id_map: Dict[str, str],
        position_store: PositionStore,
        order_adapter: OrderAdapter,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self.normalizer = ExecutionNormalizer(raw_queue, order_id_map)
        self.position_store = position_store
        self.order_adapter = order_adapter  # To notify terminal states
        self.running = False

    async def run(self):
        self.running = True
        logger.info("ExecutionService started")
        while self.running:
            try:
                raw: RawExecEvent = await self.raw_queue.get()

                if raw.topic == "order":
                    norm = self.normalizer.normalize_order(raw)
                    if norm:
                        await self.bus.publish(norm)
                        # Notify OrderAdapter of terminal state to allow cleanup
                        # OrderStatus 3=FILLED, 4=CANCELLED, 5=FAILED
                        if int(norm.status) >= 3:
                            self.order_adapter.on_terminal_state(norm.strategy_id, norm.order_id)

                elif raw.topic == "deal":
                    norm = self.normalizer.normalize_fill(raw)
                    if norm:
                        delta = self.position_store.on_fill(norm)
                        await self.bus.publish(delta)
                        await self.bus.publish(norm)

                self.raw_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("ExecService Error", error=str(e))
