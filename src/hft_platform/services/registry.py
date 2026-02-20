from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from hft_platform.core.pricing import PriceScaleProvider
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.gateway import ExecutionGateway
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.execution.router import ExecutionRouter
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.market_data import MarketDataService
from hft_platform.strategy.runner import StrategyRunner


@dataclass(slots=True)
class ServiceRegistry:
    settings: Dict[str, Any]
    bus: RingBufferBus
    raw_queue: asyncio.Queue[Any]
    raw_exec_queue: asyncio.Queue[Any]
    risk_queue: asyncio.Queue[Any]
    order_queue: asyncio.Queue[Any]
    recorder_queue: asyncio.Queue[Any]
    position_store: PositionStore
    order_id_map: Dict[str, str]
    storm_guard: StormGuard
    symbol_metadata: SymbolMetadata
    price_scale_provider: PriceScaleProvider
    md_client: ShioajiClient
    order_client: ShioajiClient
    client: ShioajiClient
    md_service: MarketDataService
    order_adapter: OrderAdapter
    execution_gateway: ExecutionGateway
    exec_service: ExecutionRouter
    risk_engine: RiskEngine
    recon_service: ReconciliationService
    strategy_runner: StrategyRunner
    recorder: RecorderService
    # CE-M2: GatewayService wiring (appended at end for slots safety)
    gateway_service: Optional[Any] = field(default=None)
    intent_channel: Optional[Any] = field(default=None)
