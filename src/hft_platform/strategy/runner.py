import asyncio
import time
from typing import Any, List

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.strategy.registry import StrategyRegistry

logger = get_logger("strategy_runner")


class StrategyRunner:
    def __init__(
        self,
        bus,
        risk_queue: asyncio.Queue,
        lob_engine=None,
        position_store=None,
        config_path: str = "config/base/strategies.yaml",
    ):
        self.bus = bus
        self.risk_queue = risk_queue
        self.lob_engine = lob_engine
        self.position_store = position_store
        self.registry = StrategyRegistry(config_path)
        self.strategies: List[BaseStrategy] = []
        # Cache of (strategy, latency_metric, intents_metric)
        self._strat_executors = []

        self.metrics = MetricsRegistry.get()
        self.symbol_metadata = SymbolMetadata()
        self._intent_seq = 0

        # Load initial
        for strat in self.registry.instantiate():
            self.register(strat)

        self.running = False

    async def run(self):
        self.running = True
        logger.info("StrategyRunner started")
        try:
            async for event in self.bus.consume():
                await self.process_event(event)
        except asyncio.CancelledError:
            pass

    def register(self, strategy: BaseStrategy):
        self.strategies.append(strategy)

        # Cache metrics
        lat_m = self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id) if self.metrics else None
        int_m = self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id) if self.metrics else None

        self._strat_executors.append((strategy, lat_m, int_m))
        logger.info("Registered strategy", id=strategy.strategy_id)

    def _rebuild_executors(self):
        """Rebuild executor cache when strategies are replaced externally."""
        self._strat_executors = []
        for strategy in self.strategies:
            lat_m = self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id) if self.metrics else None
            int_m = self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id) if self.metrics else None
            self._strat_executors.append((strategy, lat_m, int_m))

    def _intent_factory(
        self, strategy_id, symbol, side, price, qty, tif, intent_type, target_order_id=None
    ) -> OrderIntent:
        self._intent_seq += 1
        return OrderIntent(
            intent_id=self._intent_seq,
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            target_order_id=target_order_id,
            timestamp_ns=time.time_ns(),
        )

    def _scale_price(self, symbol: str, price: float) -> int:
        scale = self.symbol_metadata.price_scale(symbol)
        return int(float(price) * scale)

    async def process_event(self, event: Any):
        # Positions snapshot
        positions = self.position_store.positions.copy() if self.position_store else {}

        target_strat_id = getattr(event, "strategy_id", None)

        # Keep executors in sync with strategy list (tests may replace list)
        if [s.strategy_id for s, _, _ in self._strat_executors] != [s.strategy_id for s in self.strategies]:
            self._rebuild_executors()

        # Use cached executors
        for strategy, lat_m, int_m in self._strat_executors:
            if not strategy.enabled:
                continue

            if target_strat_id and strategy.strategy_id != target_strat_id:
                continue

            ctx = StrategyContext(
                positions=positions,
                strategy_id=strategy.strategy_id,
                intent_factory=self._intent_factory,
                price_scaler=self._scale_price,
                lob_source=self.lob_engine.get_book_snapshot if self.lob_engine else None,
            )

            start = time.perf_counter_ns()
            try:
                intents = strategy.handle_event(ctx, event)
            except Exception as e:
                logger.error("Strategy Exception", id=strategy.strategy_id, error=str(e))
                intents = []

            duration = time.perf_counter_ns() - start

            # Direct metric use
            if lat_m:
                lat_m.observe(duration)
            if intents and int_m:
                int_m.inc(len(intents))

            if intents:
                for intent in intents:
                    self.risk_queue.put_nowait(intent)
