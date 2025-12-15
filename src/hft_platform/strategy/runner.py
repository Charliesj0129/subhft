import asyncio
import time
from typing import Any, Dict, List

from structlog import get_logger

from hft_platform.strategy.base import StrategyContext, BaseStrategy
from hft_platform.strategy.registry import StrategyRegistry
from hft_platform.contracts.strategy import OrderIntent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.metrics import MetricsRegistry

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
        self.strategies: List[BaseStrategy] = self.registry.instantiate()
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.symbol_metadata = SymbolMetadata()
        self._intent_seq = 0

    def reload(self):
        """Reload strategy configs from disk."""
        self.registry.load()
        self.strategies = self.registry.instantiate()

    def register(self, strategy: BaseStrategy):
        self.strategies.append(strategy)
        logger.info("Registered strategy", id=strategy.strategy_id)

    async def run(self):
        self.running = True
        logger.info("StrategyRunner started", strategies=len(self.strategies))
        try:
            async for event in self.bus.consume():
                await self.process_event(event)
        except asyncio.CancelledError:
            logger.info("StrategyRunner stopped")

    def _next_intent_id(self) -> int:
        self._intent_seq += 1
        return self._intent_seq

    def _scale_price(self, symbol: str, price: float) -> int:
        scale = self.symbol_metadata.price_scale(symbol)
        try:
            return int(float(price) * scale)
        except (TypeError, ValueError):
            return 0

    def _intent_factory(
        self,
        *,
        strategy_id: str,
        symbol: str,
        side,
        price: int,
        qty: int,
        tif,
        intent_type,
        target_order_id=None,
    ) -> OrderIntent:
        return OrderIntent(
            intent_id=self._next_intent_id(),
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

    async def process_event(self, event: Any):
        if isinstance(event, dict):
            symbol = event.get("symbol")
        else:
            symbol = getattr(event, "symbol", None)
        lob_data = self.lob_engine.get_book_snapshot(symbol) if symbol and self.lob_engine else None
        positions = self.position_store.positions.copy() if self.position_store else {}
        features = {}
        if self.lob_engine and symbol:
            try:
                features = {symbol: self.lob_engine.get_features(symbol)}
            except Exception:
                features = {}

        target_strategy_id = event.get("strategy_id") if isinstance(event, dict) else getattr(event, "strategy_id", None)
        
        for strategy in self.strategies:
            if not strategy.enabled:
                continue
                
            # Private Event Dispatch (Fills, Acks, Rejects)
            if target_strategy_id:
                if strategy.strategy_id != target_strategy_id:
                    continue
            
            # Broadcast Dispatch (Market Data) - Subscribe Check
            elif strategy.symbols and symbol and symbol not in strategy.symbols:
                continue

            # Filter/Normalize positions: 'acc:strat:sym' -> 'sym'
            # Assuming PositionStore (MemoryPositionStore) uses keys like f"{account}:{strategy}:{symbol}"
            # We strip the prefix to give the strategy a clean {symbol: qty} view
            strat_positions = {}
            if positions: 
                # Heuristic: Match strategy_id in key
                # Or just pass all? No, BaseStrategy.position(sym) expects plain symbol key
                # If PositionStore has no partition, it might be just "symbol".
                # But bug report says "acc:strategy:symbol".
                prefix_hint = f":{strategy.strategy_id}:"
                for k, v in positions.items():
                    if prefix_hint in k:
                        # Extract symbol (suffix)
                        parts = k.split(":")
                        if len(parts) >= 3:
                            sym = parts[-1]
                            strat_positions[sym] = v
                    elif ":" not in k: # Legacy/Simple keys
                        strat_positions[k] = v

            ctx = StrategyContext(
                lob=self.lob_engine, # Pass full engine to allow .get_l1()
                positions=strat_positions,
                storm_guard_state=0,
                strategy_id=strategy.strategy_id,
                intent_factory=self._intent_factory,
                price_scaler=self._scale_price,
                features=features,
            )

            start = time.perf_counter_ns()
            try:
            try:
                # Dispatch based on Event Type
                topic = event.get("topic") if isinstance(event, dict) else getattr(event, "topic", None)
                
                # Market Data (Dict)
                if isinstance(event, dict) and topic in ["market_data", "Tick", "BidAsk", "Snapshot"]:
                    intents = strategy.on_book(ctx, event)
                
                # Timer
                elif isinstance(event, dict) and event.get("type") == "TimerTick":
                    # Assuming strategies support on_timer or on_book handles it
                    # BaseStrategy might not have on_timer, so we pass to on_book or skip
                    # For now, pass to on_book as generic event
                    intents = strategy.on_book(ctx, event)
                
                # Execution Events (Dataclasses or Dicts with topic=deal/order)
                else:
                    # Try to detect execution types
                    # If event is FillEvent or topic=="deal"
                    if topic == "deal" or getattr(event, "__class__", "").endswith("FillEvent"):
                         if hasattr(strategy, "on_fill"):
                             intents = strategy.on_fill(ctx, event)
                         else:
                             intents = []
                    elif topic == "order" or getattr(event, "__class__", "").endswith("OrderEvent"):
                         if hasattr(strategy, "on_order"):
                             intents = strategy.on_order(ctx, event)
                         else:
                             intents = []
                    else:
                        # Fallback for unknown events - pass to on_book if it's robust? 
                        # Or skip to avoid crashing
                        # User says on_book crashes on non-dict.
                        # So we only pass dicts to on_book.
                        if isinstance(event, dict):
                             intents = strategy.on_book(ctx, event)
                        else:
                             intents = []

            except Exception as exc:
                logger.error("Strategy failed", id=strategy.strategy_id, error=str(exc))
                strategy.enabled = False
                continue

            duration = time.perf_counter_ns() - start
            self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id).observe(duration)

            budget_ns = getattr(strategy, "budget_us", 200) * 1_000
            if duration > budget_ns:
                logger.warning("Strategy budget exceeded", id=strategy.strategy_id, latency_ns=duration)

            if intents:
                self._handle_intents(strategy, intents)

    def _handle_intents(self, strategy: BaseStrategy, intents: List[OrderIntent]):
        valid_intents: List[OrderIntent] = []
        for intent in intents:
            if self._validate_intent(intent):
                valid_intents.append(intent)
        if not valid_intents:
            return

        self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id).inc(len(valid_intents))
        for intent in valid_intents:
            try:
                self.risk_queue.put_nowait(intent)
            except asyncio.QueueFull:
                logger.error("Risk queue full, dropping intent", id=strategy.strategy_id, symbol=intent.symbol)

    def _validate_intent(self, intent: OrderIntent) -> bool:
        if not intent.symbol:
            logger.warning("Intent missing symbol")
            return False
        if intent.qty <= 0:
            logger.warning("Intent invalid qty", symbol=intent.symbol)
            return False
        if intent.price < 0:
            logger.warning("Intent invalid price", symbol=intent.symbol)
            return False
        return True
