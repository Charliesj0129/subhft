from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, Optional

from hft_platform.core.pricing import SymbolMetadataPriceScaleProvider
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.execution.gateway import ExecutionGateway
from hft_platform.execution.positions import PositionStore
from hft_platform.execution.reconciliation import ReconciliationService
from hft_platform.execution.router import ExecutionRouter
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.profile import load_feature_profile_registry
from hft_platform.feature.rollout import load_feature_rollout_controller
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.shioaji_client import ShioajiClient
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.market_data import MarketDataService
from hft_platform.services.registry import ServiceRegistry
from hft_platform.strategy.runner import StrategyRunner


class SystemBootstrapper:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}

    # Default bounded queue sizes to prevent unbounded memory growth
    # These can be overridden via environment variables
    DEFAULT_RAW_QUEUE_SIZE = 65536  # Market data ingestion
    DEFAULT_RAW_EXEC_QUEUE_SIZE = 8192  # Execution events
    DEFAULT_RISK_QUEUE_SIZE = 4096  # Risk engine queue
    DEFAULT_ORDER_QUEUE_SIZE = 2048  # Order dispatch queue
    DEFAULT_RECORDER_QUEUE_SIZE = 16384  # Recorder/persistence queue

    def build(self) -> ServiceRegistry:
        # 1. Infrastructure
        # Note: StormGuard is created below, so we set it after creation
        bus = RingBufferBus()

        # Bounded queues with sensible defaults (prevents OOM under load)
        # CRITICAL: Always enforce minimum bounds to prevent unbounded memory growth
        # Setting size=0 via env var is blocked - use defaults instead
        MIN_QUEUE_SIZE = 1024  # Minimum enforced size for safety

        def get_queue_size(env_key: str, default: int) -> int:
            """Get bounded queue size from env, enforcing minimum."""
            return max(MIN_QUEUE_SIZE, int(os.getenv(env_key, str(default))))

        raw_queue_size = get_queue_size("HFT_RAW_QUEUE_SIZE", self.DEFAULT_RAW_QUEUE_SIZE)
        raw_exec_queue_size = get_queue_size("HFT_RAW_EXEC_QUEUE_SIZE", self.DEFAULT_RAW_EXEC_QUEUE_SIZE)
        risk_queue_size = get_queue_size("HFT_RISK_QUEUE_SIZE", self.DEFAULT_RISK_QUEUE_SIZE)
        order_queue_size = get_queue_size("HFT_ORDER_QUEUE_SIZE", self.DEFAULT_ORDER_QUEUE_SIZE)
        recorder_queue_size = get_queue_size("HFT_RECORDER_QUEUE_SIZE", self.DEFAULT_RECORDER_QUEUE_SIZE)

        # All queues are now guaranteed to be bounded
        raw_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=raw_queue_size)
        raw_exec_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=raw_exec_queue_size)
        risk_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=risk_queue_size)
        order_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=order_queue_size)
        recorder_queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=recorder_queue_size)

        LatencyRecorder.get().configure(recorder_queue)

        # 2. Shared State
        position_store = PositionStore()
        order_id_map: Dict[str, str] = {}
        storm_guard = StormGuard()

        # Wire StormGuard to EventBus for overflow HALT triggering
        bus.set_storm_guard(storm_guard)

        # 3. Config Paths
        paths = self.settings.get("paths", {})
        symbols_path = os.getenv("SYMBOLS_CONFIG", paths.get("symbols", "config/symbols.yaml"))
        os.environ.setdefault("SYMBOLS_CONFIG", symbols_path)
        risk_path = paths.get("strategy_limits", "config/base/strategy_limits.yaml")
        adapter_path = paths.get("order_adapter", "config/base/order_adapter.yaml")

        symbol_metadata = SymbolMetadata(symbols_path)
        price_scale_provider = SymbolMetadataPriceScaleProvider(symbol_metadata)

        base_shioaji_cfg = dict(self.settings.get("shioaji", {}))
        md_client = ShioajiClient(symbols_path, base_shioaji_cfg)

        order_cfg = dict(base_shioaji_cfg)
        order_mode = os.getenv("HFT_ORDER_MODE", "").strip().lower()
        order_sim_flag = os.getenv("HFT_ORDER_SIMULATION")
        order_no_ca = os.getenv("HFT_ORDER_NO_CA", "0").lower() in {"1", "true", "yes", "on"}
        if order_mode:
            order_cfg["simulation"] = order_mode in {"sim", "simulation", "paper"}
        elif order_sim_flag is not None:
            order_cfg["simulation"] = order_sim_flag.lower() in {"1", "true", "yes", "on", "sim"}
        if order_no_ca or order_cfg.get("simulation") is True:
            order_cfg["activate_ca"] = False

        order_client = ShioajiClient(symbols_path, order_cfg)

        # 4. Services
        feature_engine = None
        feature_profile_registry = None
        feature_profile = None
        feature_rollout_controller = None
        feature_rollout_assignment = None
        if os.getenv("HFT_FEATURE_ENGINE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}:
            try:
                feature_profile_registry = load_feature_profile_registry()
            except Exception:
                feature_profile_registry = None
            try:
                feature_rollout_controller = load_feature_rollout_controller()
            except Exception:
                feature_rollout_controller = None
            feature_engine = FeatureEngine()
            if feature_profile_registry is not None:
                try:
                    if feature_rollout_controller is not None:
                        feature_rollout_assignment = feature_rollout_controller.get(feature_engine.feature_set_id())
                    override_profile_id = (
                        feature_rollout_controller.resolve_profile_id(feature_engine.feature_set_id())
                        if feature_rollout_controller is not None
                        else None
                    )
                    if feature_rollout_assignment is not None and str(feature_rollout_assignment.state) == "disabled":
                        feature_profile = None
                    elif override_profile_id:
                        try:
                            feature_profile = feature_profile_registry.get(override_profile_id)
                        except Exception:
                            feature_profile = None
                    else:
                        feature_profile = feature_profile_registry.get_active_for_set(feature_engine.feature_set_id())
                    if feature_profile is not None:
                        feature_engine.apply_profile(feature_profile)
                        try:
                            from hft_platform.observability.metrics import MetricsRegistry

                            m = MetricsRegistry.get()
                            if hasattr(m, "feature_profile_activations_total"):
                                action = "shadow" if feature_profile.state == "shadow" else "activate"
                                m.feature_profile_activations_total.labels(
                                    feature_set=feature_profile.feature_set_id,
                                    profile_id=feature_profile.profile_id,
                                    action=action,
                                ).inc()
                            if hasattr(m, "feature_profile_rollout_state"):
                                state_map = {"disabled": 0, "shadow": 1, "active": 2}
                                rollout_state = (
                                    feature_rollout_assignment.state
                                    if feature_rollout_assignment is not None
                                    else feature_profile.state
                                )
                                m.feature_profile_rollout_state.labels(
                                    feature_set=feature_profile.feature_set_id,
                                    profile_id=feature_profile.profile_id,
                                ).set(float(state_map.get(str(rollout_state), 0)))
                        except Exception:
                            pass
                    elif feature_rollout_assignment is not None:
                        try:
                            from hft_platform.observability.metrics import MetricsRegistry

                            m = MetricsRegistry.get()
                            if hasattr(m, "feature_profile_rollout_state"):
                                state_map = {"disabled": 0, "shadow": 1, "active": 2}
                                m.feature_profile_rollout_state.labels(
                                    feature_set=feature_rollout_assignment.feature_set_id,
                                    profile_id=str(feature_rollout_assignment.active_profile_id or ""),
                                ).set(float(state_map.get(str(feature_rollout_assignment.state), 0)))
                        except Exception:
                            pass
                except Exception:
                    feature_profile = None

        md_service = MarketDataService(
            bus,
            raw_queue,
            md_client,
            symbol_metadata=symbol_metadata,
            recorder_queue=recorder_queue,
            feature_engine=feature_engine,
        )
        order_adapter = OrderAdapter(adapter_path, order_queue, order_client, order_id_map)
        execution_gateway = ExecutionGateway(order_adapter)
        exec_service = ExecutionRouter(
            bus,
            raw_exec_queue,
            order_id_map,
            position_store,
            execution_gateway.on_terminal_state,
        )
        risk_engine = RiskEngine(risk_path, risk_queue, order_queue, price_scale_provider)
        recon_service = ReconciliationService(order_client, position_store, self.settings, storm_guard)

        # CE-M2: GatewayService wiring
        gateway_service = None
        intent_channel = None
        _gateway_enabled = os.getenv("HFT_GATEWAY_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        if _gateway_enabled:
            from hft_platform.gateway.channel import LocalIntentChannel
            from hft_platform.gateway.dedup import IdempotencyStore
            from hft_platform.gateway.exposure import ExposureStore
            from hft_platform.gateway.policy import GatewayPolicy
            from hft_platform.gateway.service import GatewayService

            intent_channel = LocalIntentChannel(maxsize=risk_queue_size)
            exposure_store = ExposureStore()
            dedup_store = IdempotencyStore()
            dedup_store.load()
            gateway_policy = GatewayPolicy()
            gateway_service = GatewayService(
                channel=intent_channel,
                risk_engine=risk_engine,
                order_adapter=order_adapter,
                exposure_store=exposure_store,
                dedup_store=dedup_store,
                storm_guard=storm_guard,
                policy=gateway_policy,
            )

        # StrategyRunner: use intent_channel when gateway enabled, else risk_queue
        _runner_queue = intent_channel if _gateway_enabled and intent_channel is not None else risk_queue
        strategy_runner = StrategyRunner(
            bus=bus,
            risk_queue=_runner_queue,
            lob_engine=md_service.lob,
            feature_engine=feature_engine,
            position_store=position_store,
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
            md_client=md_client,
            order_client=order_client,
            client=md_client,
            md_service=md_service,
            feature_engine=feature_engine,
            feature_profile_registry=feature_profile_registry,
            feature_profile=feature_profile,
            feature_rollout_controller=feature_rollout_controller,
            feature_rollout_assignment=feature_rollout_assignment,
            order_adapter=order_adapter,
            execution_gateway=execution_gateway,
            exec_service=exec_service,
            risk_engine=risk_engine,
            recon_service=recon_service,
            strategy_runner=strategy_runner,
            recorder=recorder,
            gateway_service=gateway_service,
            intent_channel=intent_channel,
        )
