import asyncio
import threading
import time

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import OrderCommand, OrderIntent, RiskDecision
from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.observability.latency import LatencyRecorder
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
        self.latency = LatencyRecorder.get()

        # Validators
        self.validators = [
            PriceBandValidator(self.config, price_scale_provider),
            MaxNotionalValidator(self.config, price_scale_provider),
        ]
        self.storm_guard = StormGuardFSM(self.config)
        self._cmd_id_lock = threading.Lock()
        self._monotomic_cmd_id = 0

    def load_config(self):
        with open(self.config_path, "r") as f:
            self.config = yaml.safe_load(f)

    async def run(self):
        self.running = True
        logger.info("RiskEngine started")

        while self.running:
            try:
                intent: OrderIntent = await self.intent_queue.get()
                start_ns = time.perf_counter_ns()
                decision = self.evaluate(intent)
                duration = time.perf_counter_ns() - start_ns
                if self.latency:
                    self.latency.record(
                        "risk",
                        duration,
                        trace_id=intent.trace_id,
                        symbol=intent.symbol,
                        strategy_id=intent.strategy_id,
                    )

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

    @property
    def monotomic_cmd_id(self) -> int:
        """Thread-safe access to command ID counter."""
        with self._cmd_id_lock:
            return self._monotomic_cmd_id

    def _next_cmd_id(self) -> int:
        """Thread-safe increment and return of command ID."""
        with self._cmd_id_lock:
            self._monotomic_cmd_id += 1
            return self._monotomic_cmd_id

    def create_command(self, intent: OrderIntent) -> OrderCommand:
        cmd_id = self._next_cmd_id()
        # Set 500ms deadline from now (relaxed for Python/Docker latency)
        deadline = time.time_ns() + 500_000_000

        return OrderCommand(
            cmd_id=cmd_id,
            intent=intent,
            deadline_ns=deadline,
            storm_guard_state=self.storm_guard.state,
            created_ns=time.time_ns(),
        )
