import asyncio
import time

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import OrderCommand, OrderIntent, RiskDecision
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.risk.validators import MaxNotionalValidator, PriceBandValidator, StormGuardFSM

logger = get_logger("risk_engine")


class RiskEngine:
    def __init__(
        self,
        config_path: str,
        intent_queue: asyncio.Queue,
        order_queue: asyncio.Queue,
        price_scale_provider: PriceScaleProvider | None = None,
    ):
        self.config_path = config_path
        self.intent_queue = intent_queue  # Input
        self.order_queue = order_queue  # Output
        self.running = False
        self.load_config()
        self.metrics = MetricsRegistry.get()

        # Validators
        self.validators = [
            PriceBandValidator(self.config, price_scale_provider),
            MaxNotionalValidator(self.config, price_scale_provider),
        ]
        self.storm_guard = StormGuardFSM(self.config)
        self.monotomic_cmd_id = 0

    def load_config(self):
        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

    async def run(self):
        self.running = True
        logger.info("RiskEngine started")

        while self.running:
            try:
                intent: OrderIntent = await self.intent_queue.get()
                decision = self.evaluate(intent)

                if decision.approved:
                    cmd = self.create_command(decision.intent)
                    await self.order_queue.put(cmd)
                else:
                    logger.warning("Order Rejected by Risk", sid=intent.strategy_id, reason=decision.reason_code)
                    self.metrics.risk_reject_total.labels(
                        strategy=intent.strategy_id, reason=decision.reason_code
                    ).inc()
                    # In real system: Feedback to strategy via side channel

                self.intent_queue.task_done()
            except asyncio.CancelledError:
                logger.info("RiskEngine stopped")
                break
            except Exception as e:
                logger.error("RiskEngine error", error=str(e))
                self.intent_queue.task_done()

    def evaluate(self, intent: OrderIntent) -> RiskDecision:
        # 1. StormGuard Check
        ok, reason = self.storm_guard.validate(intent)
        if not ok:
            return RiskDecision(False, intent, reason)

        # 2. Hard Validators
        for v in self.validators:
            ok, reason = v.check(intent)
            if not ok:
                return RiskDecision(False, intent, reason)

        return RiskDecision(True, intent)

    def create_command(self, intent: OrderIntent) -> OrderCommand:
        self.monotomic_cmd_id += 1
        # Set 500ms deadline from now (relaxed for Python/Docker latency)
        deadline = time.time_ns() + 500_000_000

        return OrderCommand(
            cmd_id=self.monotomic_cmd_id, intent=intent, deadline_ns=deadline, storm_guard_state=self.storm_guard.state
        )
