from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from hft_platform.core.pricing import SymbolMetadataPriceScaleProvider
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.execution import ExecutionService
from hft_platform.services.market_data import MarketDataService
from hft_platform.services.registry import ServiceRegistry
from hft_platform.strategy.runner import StrategyRunner


class SystemBootstrapper:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}

    def build(self) -> ServiceRegistry:
        # 1. Infrastructure
        bus = RingBufferBus()
        raw_queue_size = int(os.getenv("HFT_RAW_QUEUE_SIZE", "0"))
        raw_exec_queue_size = int(os.getenv("HFT_RAW_EXEC_QUEUE_SIZE", "0"))
        risk_queue_size = int(os.getenv("HFT_RISK_QUEUE_SIZE", "0"))
        order_queue_size = int(os.getenv("HFT_ORDER_QUEUE_SIZE", "0"))
        recorder_queue_size = int(os.getenv("HFT_RECORDER_QUEUE_SIZE", "0"))

        raw_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=raw_queue_size) if raw_queue_size > 0 else asyncio.Queue()
        raw_exec_queue: asyncio.Queue[Any] = (
            asyncio.Queue(maxsize=raw_exec_queue_size) if raw_exec_queue_size > 0 else asyncio.Queue()
        )
        risk_queue: asyncio.Queue[Any] = (
            asyncio.Queue(maxsize=risk_queue_size) if risk_queue_size > 0 else asyncio.Queue()
        )
        order_queue: asyncio.Queue[Any] = (
            asyncio.Queue(maxsize=order_queue_size) if order_queue_size > 0 else asyncio.Queue()
        )
        recorder_queue: asyncio.Queue[Any] = (
            asyncio.Queue(maxsize=recorder_queue_size) if recorder_queue_size > 0 else asyncio.Queue()
        )

        # 2. Shared State
        position_store = PositionStore()
        order_id_map: Dict[str, str] = {}
        storm_guard = StormGuard()

        # 3. Config Paths
        paths = self.settings.get("paths", {})
        symbols_path = os.getenv("SYMBOLS_CONFIG", paths.get("symbols", "config/symbols.yaml"))
        os.environ.setdefault("SYMBOLS_CONFIG", symbols_path)
        risk_path = paths.get("strategy_limits", "config/base/strategy_limits.yaml")
        adapter_path = paths.get("order_adapter", "config/base/order_adapter.yaml")

        symbol_metadata = SymbolMetadata(symbols_path)
        price_scale_provider = SymbolMetadataPriceScaleProvider(symbol_metadata)

        client = ShioajiClient(symbols_path, self.settings.get("shioaji", {}))

        # 4. Services
        md_service = MarketDataService(bus, raw_queue, client, symbol_metadata=symbol_metadata)
        order_adapter = OrderAdapter(adapter_path, order_queue, client, order_id_map)
        exec_service = ExecutionService(bus, raw_exec_queue, order_id_map, position_store, order_adapter)
        risk_engine = RiskEngine(risk_path, risk_queue, order_queue, price_scale_provider)
        recon_service = ReconciliationService(client, position_store, self.settings)
        strategy_runner = StrategyRunner(
            bus,
            risk_queue,
            md_service.lob,
            position_store,
            symbol_metadata=symbol_metadata,
        )
        recorder = RecorderService(recorder_queue)

        return ServiceRegistry(
            settings=self.settings,
            bus=bus,
            raw_queue=raw_queue,
            raw_exec_queue=raw_exec_queue,
            risk_queue=risk_queue,
            order_queue=order_queue,
            recorder_queue=recorder_queue,
            position_store=position_store,
            order_id_map=order_id_map,
            storm_guard=storm_guard,
            symbol_metadata=symbol_metadata,
            price_scale_provider=price_scale_provider,
            client=client,
            md_service=md_service,
            order_adapter=order_adapter,
            exec_service=exec_service,
            risk_engine=risk_engine,
            recon_service=recon_service,
            strategy_runner=strategy_runner,
            recorder=recorder,
        )
