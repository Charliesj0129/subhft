import asyncio
import os
import re
import time
from decimal import Decimal
from typing import Any, List

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.latency import LatencyRecorder
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
        symbol_metadata: SymbolMetadata | None = None,
    ):
        self.bus = bus
        self.risk_queue = risk_queue
        self.lob_engine = lob_engine
        self.position_store = position_store
        cfg_path = os.getenv("HFT_STRATEGY_CONFIG") or config_path
        self.registry = StrategyRegistry(cfg_path)
        self.strategies: List[BaseStrategy] = []
        # Cache of (strategy, latency_metric, intents_metric)
        self._strat_executors: list[tuple[BaseStrategy, Any, Any]] = []

        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self.symbol_metadata = symbol_metadata or SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.symbol_metadata))
        self._intent_seq = 0
        self._positions_cache: dict = {}
        self._positions_dirty = True
        self._current_source_ts_ns = 0
        self._current_trace_id = ""

        # Load initial
        for strat in self.registry.instantiate():
            self.register(strat)

        self.running = False

    async def run(self):
        self.running = True
        logger.info("StrategyRunner started")
        try:
            batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
            if batch_size > 1:
                async for batch in self.bus.consume_batch(batch_size):
                    for event in batch:
                        await self.process_event(event)
            else:
                async for event in self.bus.consume():
                    await self.process_event(event)
        except asyncio.CancelledError:
            pass

    def register(self, strategy: BaseStrategy):
        self.strategies.append(strategy)
        self._resolve_strategy_symbols(strategy)

        # Cache metrics
        lat_m = self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id) if self.metrics else None
        int_m = self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id) if self.metrics else None

        self._strat_executors.append((strategy, lat_m, int_m))
        logger.info("Registered strategy", id=strategy.strategy_id)

    def _resolve_strategy_symbols(self, strategy: BaseStrategy) -> None:
        resolved = set()
        used_tag = False
        raw_symbols = getattr(strategy, "symbols", None) or []
        if isinstance(raw_symbols, (set, list, tuple)):
            candidates = list(raw_symbols)
        else:
            candidates = [raw_symbols]

        for item in candidates:
            if not item:
                continue
            if isinstance(item, str) and item.lower().startswith("tag:"):
                used_tag = True
                tag_str = item[4:]
                tags = [t for t in re.split(r"[|,]", tag_str) if t]
                resolved.update(self.symbol_metadata.symbols_for_tags(tags))
            else:
                resolved.add(str(item))

        raw_tags = getattr(strategy, "symbol_tags", None) or []
        if raw_tags:
            used_tag = True
            resolved.update(self.symbol_metadata.symbols_for_tags(raw_tags))

        if resolved or used_tag:
            strategy.symbols = set(resolved)

    def _rebuild_executors(self):
        """Rebuild executor cache when strategies are replaced externally."""
        self._strat_executors = []
        for strategy in self.strategies:
            lat_m = self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id) if self.metrics else None
            int_m = self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id) if self.metrics else None
            self._strat_executors.append((strategy, lat_m, int_m))

    def _intent_factory(
        self,
        strategy_id,
        symbol,
        side,
        price,
        qty,
        tif,
        intent_type,
        target_order_id=None,
        source_ts_ns: int | None = None,
        trace_id: str | None = None,
    ) -> OrderIntent:
        self._intent_seq += 1
        if source_ts_ns is None:
            source_ts_ns = self._current_source_ts_ns
        if trace_id is None:
            trace_id = self._current_trace_id
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
            timestamp_ns=timebase.now_ns(),
            source_ts_ns=int(source_ts_ns or 0),
            trace_id=str(trace_id or ""),
        )

    def _scale_price(self, symbol: str, price: int | Decimal) -> int:
        return self.price_codec.scale(symbol, price)

    def _build_positions_by_strategy(self):
        if not self.position_store:
            return {}
        raw = getattr(self.position_store, "positions", None)
        if not isinstance(raw, dict):
            return {}

        positions_by_strategy = {}
        fallback = {}

        for key, value in raw.items():
            if hasattr(value, "strategy_id") and hasattr(value, "symbol") and hasattr(value, "net_qty"):
                positions_by_strategy.setdefault(value.strategy_id, {})[value.symbol] = value.net_qty
                continue

            if isinstance(key, str) and ":" in key:
                parts = key.split(":")
                if len(parts) >= 3:
                    strat_id = parts[1]
                    symbol = parts[2]
                    net_qty = value.net_qty if hasattr(value, "net_qty") else value
                    positions_by_strategy.setdefault(strat_id, {})[symbol] = net_qty
                    continue

            fallback[key] = value.net_qty if hasattr(value, "net_qty") else value

        if fallback:
            positions_by_strategy["*"] = fallback

        return positions_by_strategy

    def invalidate_positions(self):
        self._positions_dirty = True

    async def process_event(self, event: Any):
        source_ts_ns, trace_id = self._extract_event_trace(event)
        self._current_source_ts_ns = source_ts_ns
        self._current_trace_id = trace_id
        # Invalidate on position delta events
        if hasattr(event, "delta_source"):
            self._positions_dirty = True

        if self._positions_dirty:
            self._positions_cache = self._build_positions_by_strategy()
            self._positions_dirty = False
        positions_by_strategy = self._positions_cache

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

            positions = positions_by_strategy.get(strategy.strategy_id) or positions_by_strategy.get("*", {})

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
            if self.latency:
                self.latency.record(
                    "strategy",
                    duration,
                    trace_id=trace_id,
                    symbol=getattr(event, "symbol", ""),
                    strategy_id=strategy.strategy_id,
                )

            if intents:
                for intent in intents:
                    self.risk_queue.put_nowait(intent)

    def _extract_event_trace(self, event: Any) -> tuple[int, str]:
        source_ts_ns = 0
        trace_id = ""
        meta = getattr(event, "meta", None)
        if meta is not None:
            source_ts_ns = int(getattr(meta, "local_ts", 0) or getattr(meta, "source_ts", 0) or 0)
            seq = getattr(meta, "seq", None)
            topic = getattr(meta, "topic", "event")
            if seq is not None:
                trace_id = f"{topic}:{seq}"
        elif hasattr(event, "ts"):
            try:
                source_ts_ns = int(getattr(event, "ts") or 0)
            except Exception:
                source_ts_ns = 0
        if not source_ts_ns:
            source_ts_ns = timebase.now_ns()
        return source_ts_ns, trace_id
