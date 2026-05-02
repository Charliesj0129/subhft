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
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.market_data import MarketDataService
from hft_platform.strategy.runner import StrategyRunner

# TODO: replace with BrokerClientProtocol once WU-1 merges
BrokerClient = Any


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
    broker_id: str
    account_id: str
    md_client: BrokerClient
    order_client: BrokerClient
    client: BrokerClient
    md_service: MarketDataService
    feature_engine: Optional[Any]
    order_adapter: OrderAdapter
    execution_gateway: ExecutionGateway
    exec_service: ExecutionRouter
    risk_engine: RiskEngine
    recon_service: ReconciliationService
    strategy_runner: StrategyRunner
    recorder: RecorderService
    feature_profile_registry: Optional[Any] = field(default=None)
    feature_profile: Optional[Any] = field(default=None)
    feature_rollout_controller: Optional[Any] = field(default=None)
    feature_rollout_assignment: Optional[Any] = field(default=None)
    # CE-M2: GatewayService wiring (appended at end for slots safety)
    gateway_service: Optional[Any] = field(default=None)
    intent_channel: Optional[Any] = field(default=None)
    strategy_governor: Optional[Any] = field(default=None)
    platform_degrade_controller: Optional[Any] = field(default=None)
    platform_degrade_inputs: Optional[Any] = field(default=None)
    evidence_writer: Optional[Any] = field(default=None)
    manual_rearm_service: Optional[Any] = field(default=None)
    session_governor: Optional[Any] = field(default=None)
    autonomy_monitor: Optional[Any] = field(default=None)
    daily_report_service: Optional[Any] = field(default=None)
    position_stuck_monitor: Optional[Any] = field(default=None)
    checkpoint_writer: Optional[Any] = field(default=None)
    startup_verifier: Optional[Any] = field(default=None)
    startup_fill_reconciler: Optional[Any] = field(default=None)
    # Coroutines scheduled by HFTSystem.run() once the engine loop is running.
    # build() must NOT call asyncio.get_event_loop() (deprecated in Python 3.12+);
    # collect coroutines here and let the system create tasks post-loop-bind.
    deferred_tasks: list[Any] = field(default_factory=list)
