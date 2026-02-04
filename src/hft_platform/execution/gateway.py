import time
from typing import Any

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.adapter import OrderAdapter

logger = get_logger("execution.gateway")


class ExecutionGateway:
    """
    Outbound execution gateway (OrderAdapter wrapper).
    Keeps execution IO isolated from routing/normalization.
    """

    def __init__(self, adapter: OrderAdapter):
        self.adapter = adapter
        self.running = False
        self.metrics = MetricsRegistry.get()

    async def run(self) -> None:
        self.running = True
        logger.info("ExecutionGateway started")
        self.metrics.execution_gateway_alive.set(1)
        self.metrics.execution_gateway_heartbeat_ts.set(time.time())
        try:
            await self.adapter.run()
        except Exception:
            self.metrics.execution_gateway_errors_total.inc()
            raise
        finally:
            self.metrics.execution_gateway_alive.set(0)

    def stop(self) -> None:
        self.running = False
        self.adapter.running = False

    def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        self.adapter.on_terminal_state(strategy_id, order_id)

    async def execute(self, cmd: Any) -> None:
        await self.adapter.execute(cmd)
