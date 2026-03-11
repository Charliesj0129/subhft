from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from typing import Any, Dict, Optional

from structlog import get_logger

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
from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade

_VALID_BROKERS = frozenset({"shioaji", "fubon"})
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.market_data import MarketDataService
from hft_platform.services.registry import ServiceRegistry
from hft_platform.strategy.runner import StrategyRunner

logger = get_logger("bootstrap")

_VALID_RUNTIME_ROLES = frozenset({"engine", "maintenance", "monitor", "wal_loader"})
_FEED_ALLOWED_ROLES = frozenset({"engine"})


def _encode_resp(*parts: str) -> bytes:
    """Encode a RESP command for Redis."""
    payload = [f"*{len(parts)}\r\n".encode("utf-8")]
    for part in parts:
        raw = str(part).encode("utf-8")
        payload.append(f"${len(raw)}\r\n".encode("utf-8"))
        payload.append(raw + b"\r\n")
    return b"".join(payload)


def _read_resp(stream) -> str | int | None:
    """Read a single RESP response from a binary stream."""
    prefix = stream.read(1)
    if not prefix:
        raise RuntimeError("empty redis response")
    if prefix == b"+":
        return stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace")
    if prefix == b":":
        return int(stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace"))
    if prefix == b"$":
        size = int(stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace"))
        if size < 0:
            return None
        payload = stream.read(size)
        stream.read(2)
        return payload.decode("utf-8", errors="replace")
    if prefix == b"-":
        err = stream.readline().rstrip(b"\r\n").decode("utf-8", errors="replace")
        raise RuntimeError(f"redis error: {err}")
    raise RuntimeError(f"unsupported redis response prefix: {prefix!r}")


class _RoleGuardedNoopClient:
    """Non-trading broker client used for non-engine runtime roles."""

    __slots__ = ("runtime_role", "api", "logged_in", "tick_callback")

    def __init__(self, runtime_role: str) -> None:
        self.runtime_role = runtime_role
        self.api = None
        self.logged_in = False
        self.tick_callback = None

    def login(self, *args, **kwargs) -> bool:
        logger.warning("Broker login blocked by runtime role guard", role=self.runtime_role)
        self.logged_in = False
        return False

    def reconnect(self, *args, **kwargs) -> bool:
        logger.warning("Broker reconnect blocked by runtime role guard", role=self.runtime_role)
        return False

    def subscribe_basket(self, cb) -> None:
        self.tick_callback = cb
        logger.warning("Feed subscription blocked by runtime role guard", role=self.runtime_role)

    def fetch_snapshots(self) -> list[Any]:
        return []

    def reload_symbols(self) -> None:
        return None

    def get_exchange(self, symbol: str) -> str:
        return ""

    def resubscribe(self) -> bool:
        return False

    def set_execution_callbacks(self, on_order, on_deal) -> None:
        return None

    def place_order(self, *args, **kwargs) -> dict[str, Any]:
        logger.warning("Order placement blocked by runtime role guard", role=self.runtime_role)
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def cancel_order(self, trade: Any) -> dict[str, Any]:
        logger.warning("Order cancel blocked by runtime role guard", role=self.runtime_role)
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def update_order(self, trade: Any, price: float | None = None, qty: int | None = None) -> dict[str, Any]:
        logger.warning("Order update blocked by runtime role guard", role=self.runtime_role)
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def get_positions(self) -> list[Any]:
        return []

    def get_account_balance(self, account: Any = None) -> dict[str, Any]:
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def get_margin(self, account: Any = None) -> dict[str, Any]:
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def list_position_detail(self, account: Any = None) -> list[Any]:
        return []

    def list_profit_loss(
        self,
        account: Any = None,
        begin_date: str | None = None,
        end_date: str | None = None,
    ) -> list[Any]:
        return []

    def validate_symbols(self) -> list[str]:
        return []

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return {"status": "blocked", "reason": f"runtime_role:{self.runtime_role}"}

    def close(self, logout: bool = False) -> None:
        self.logged_in = False

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)


class SystemBootstrapper:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings or {}
        self._lease_refresh_running: bool = False
        self._lease_refresh_thread: Any | None = None
        self._last_role: str = "engine"

    def _get_runtime_role(self) -> str:
        raw = os.getenv("HFT_RUNTIME_ROLE", "engine").strip().lower().replace("-", "_")
        if raw not in _VALID_RUNTIME_ROLES:
            logger.warning("Unknown HFT_RUNTIME_ROLE; defaulting to 'engine'", role=raw)
            return "engine"
        return raw

    def _get_redis_lease_params(self) -> dict:
        """Return Redis session lease connection parameters from environment."""
        port_raw = os.getenv("HFT_REDIS_PORT")
        if port_raw is None:
            port_raw = os.getenv("REDIS_PORT", "6379")
        return {
            "host": os.getenv("HFT_REDIS_HOST") or os.getenv("REDIS_HOST", "redis"),
            "port": int(port_raw),
            "password": os.getenv("HFT_REDIS_PASSWORD") or os.getenv("REDIS_PASSWORD") or os.getenv("REDIS_PASS") or "",
            "key": os.getenv("HFT_FEED_SESSION_OWNER_KEY", "feed:session:owner"),
            "owner_id": os.getenv("HFT_RUNTIME_INSTANCE_ID") or f"{os.getenv('HOSTNAME', 'unknown')}:{os.getpid()}",
            "ttl_s": max(30, int(os.getenv("HFT_FEED_SESSION_OWNER_TTL_S", "300"))),
            "timeout_s": float(os.getenv("HFT_FEED_SESSION_PREFLIGHT_TIMEOUT_S", "0.5")),
        }

    @staticmethod
    def _read_int_resp(value: str | int | None, default: int = -2) -> int:
        try:
            return int(value)  # type: ignore[arg-type]
        except Exception:
            return int(default)

    @staticmethod
    def _lease_is_stale(ttl_s: int, takeover_ttl_s: int) -> bool:
        # Redis TTL semantics:
        # -2 = key missing, -1 = key exists without expire.
        if ttl_s in (-2, -1):
            return True
        return takeover_ttl_s > 0 and ttl_s <= takeover_ttl_s

    @staticmethod
    def _record_lease_metric(op: str, result: str) -> None:
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            m = MetricsRegistry.get()
            if hasattr(m, "feed_session_lease_ops_total"):
                m.feed_session_lease_ops_total.labels(op=op, result=result).inc()
        except Exception:
            return

    def _check_session_ownership(self, role: str) -> bool:
        """Non-blocking preflight: warn if another runtime already holds the feed session.

        Checks Redis key ``feed:session:owner``. If set to a different instance
        identifier, logs CRITICAL and increments ``feed_session_conflict_total``.
        Failure to reach Redis is swallowed so a Redis outage never blocks startup.
        """
        if role not in _FEED_ALLOWED_ROLES:
            return False
        params = self._get_redis_lease_params()
        host = params["host"]
        port = params["port"]
        password = params["password"]
        key = params["key"]
        owner_id = params["owner_id"]
        timeout_s = params["timeout_s"]
        ttl_s = params["ttl_s"]
        stale_takeover_ttl_s = max(0, int(os.getenv("HFT_FEED_SESSION_STALE_TAKEOVER_TTL_S", "0")))

        try:
            with socket.create_connection((host, port), timeout=timeout_s) as sock:
                sock.settimeout(timeout_s)
                stream = sock.makefile("rb")

                def _command(*parts: str) -> str | int | None:
                    sock.sendall(_encode_resp(*parts))
                    return _read_resp(stream)

                if password:
                    _command("AUTH", password)

                owner = _command("GET", key)
                owner_str = str(owner or "").strip()
                if not owner_str or owner_str == owner_id:
                    _command("SETEX", key, str(ttl_s), owner_id)
                    self._record_lease_metric("preflight", "acquired" if not owner_str else "refreshed")
                    return True

                ttl_remaining = self._read_int_resp(_command("TTL", key), default=-2)
                if self._lease_is_stale(ttl_remaining, stale_takeover_ttl_s):
                    current_owner = str(_command("GET", key) or "").strip()
                    if current_owner == owner_str:
                        _command("DEL", key)
                        owner_after_cleanup = str(_command("GET", key) or "").strip()
                        if not owner_after_cleanup:
                            _command("SETEX", key, str(ttl_s), owner_id)
                            logger.warning(
                                "feed_session_stale_owner_cleaned",
                                role=role,
                                stale_owner=owner_str,
                                my_id=owner_id,
                                ttl_remaining_s=ttl_remaining,
                            )
                            self._record_lease_metric("stale_cleanup", "ok")
                            self._record_lease_metric("preflight", "acquired")
                            return True
                    self._record_lease_metric("stale_cleanup", "failed")

                logger.critical(
                    "feed_session_conflict: another runtime already holds the broker session",
                    role=role,
                    owner=owner_str,
                    my_id=owner_id,
                    ttl_remaining_s=ttl_remaining,
                )
                try:
                    from hft_platform.observability.metrics import MetricsRegistry

                    m = MetricsRegistry.get()
                    if hasattr(m, "feed_session_conflict_total"):
                        m.feed_session_conflict_total.labels(role=role).inc()
                except Exception:
                    pass
                self._record_lease_metric("preflight", "conflict")
                return False
        except Exception as exc:
            logger.debug("session_ownership_preflight_skipped", role=role, reason=str(exc))
            self._record_lease_metric("preflight", "error")
            return False

    def _start_lease_refresh_thread(
        self,
        host: str,
        port: int,
        password: str,
        key: str,
        owner_id: str,
        ttl_s: int,
        timeout_s: float,
    ) -> None:
        """Daemon thread: refresh lease only when key is still owned by this runtime."""
        interval_s = max(15, ttl_s // 2)

        def _refresh_loop() -> None:
            remaining = float(interval_s)
            while self._lease_refresh_running:
                time.sleep(0.1)
                remaining -= 0.1
                if remaining > 0:
                    continue
                remaining = float(interval_s)
                try:
                    with socket.create_connection((host, port), timeout=timeout_s) as sock:
                        sock.settimeout(timeout_s)
                        stream = sock.makefile("rb")

                        def _command(*parts: str) -> str | int | None:
                            sock.sendall(_encode_resp(*parts))
                            return _read_resp(stream)

                        if password:
                            _command("AUTH", password)
                        owner = str(_command("GET", key) or "").strip()
                        if owner and owner != owner_id:
                            ttl_remaining = self._read_int_resp(_command("TTL", key), default=-2)
                            logger.warning(
                                "session_lease_refresh_skipped_not_owner",
                                key=key,
                                owner=owner,
                                my_id=owner_id,
                                ttl_remaining_s=ttl_remaining,
                            )
                            self._record_lease_metric("refresh", "lost_owner")
                            continue

                        if not owner:
                            logger.warning("session_lease_reacquire", key=key, my_id=owner_id)
                            self._record_lease_metric("refresh", "reacquired")

                        _command("SETEX", key, str(ttl_s), owner_id)
                        logger.debug("session_lease_refreshed", key=key, ttl_s=ttl_s)
                        self._record_lease_metric("refresh", "ok")
                except Exception as exc:
                    logger.warning("session_lease_refresh_failed", reason=str(exc))
                    self._record_lease_metric("refresh", "error")

        self._lease_refresh_running = True
        self._lease_refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="bootstrap-lease-refresh",
            daemon=True,
        )
        self._lease_refresh_thread.start()

    def _stop_lease_refresh_thread(self) -> None:
        self._lease_refresh_running = False
        thread = self._lease_refresh_thread
        if thread and thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                logger.warning("session_lease_refresh_thread_still_alive")
        self._lease_refresh_thread = None

    def teardown(self) -> None:
        """Stop lease refresh thread and release Redis session lease on clean shutdown."""
        self._stop_lease_refresh_thread()
        if self._last_role not in _FEED_ALLOWED_ROLES:
            return
        params = self._get_redis_lease_params()
        try:
            with socket.create_connection((params["host"], params["port"]), timeout=params["timeout_s"]) as sock:
                sock.settimeout(params["timeout_s"])
                stream = sock.makefile("rb")

                def _command(*parts: str) -> str | int | None:
                    sock.sendall(_encode_resp(*parts))
                    return _read_resp(stream)

                if params["password"]:
                    _command("AUTH", params["password"])
                owner = _command("GET", params["key"])
                owner_str = str(owner or "").strip()
                if owner_str and owner_str != params["owner_id"]:
                    logger.warning(
                        "session_lease_release_skipped_not_owner",
                        key=params["key"],
                        owner=owner_str,
                        my_id=params["owner_id"],
                    )
                    self._record_lease_metric("teardown", "skip_not_owner")
                    return
                _command("DEL", params["key"])
                logger.info("session_lease_released", key=params["key"])
                self._record_lease_metric("teardown", "released")
        except Exception as exc:
            logger.debug("session_lease_release_skipped", reason=str(exc))
            self._record_lease_metric("teardown", "error")

    # Default bounded queue sizes to prevent unbounded memory growth
    # These can be overridden via environment variables
    DEFAULT_RAW_QUEUE_SIZE = 65536  # Market data ingestion
    DEFAULT_RAW_EXEC_QUEUE_SIZE = 8192  # Execution events
    DEFAULT_RISK_QUEUE_SIZE = 4096  # Risk engine queue
    DEFAULT_ORDER_QUEUE_SIZE = 2048  # Order dispatch queue
    DEFAULT_RECORDER_QUEUE_SIZE = 16384  # Recorder/persistence queue

    @staticmethod
    def _resolve_broker_id() -> str:
        """Read HFT_BROKER env var and validate against known broker IDs."""
        broker_id = os.environ.get("HFT_BROKER", "shioaji").strip().lower()
        if broker_id not in _VALID_BROKERS:
            raise ValueError(
                f"Unknown HFT_BROKER={broker_id!r}; valid options: {sorted(_VALID_BROKERS)}"
            )
        return broker_id

    def _build_broker_clients(
        self,
        role: str,
        symbols_path: str,
        base_shioaji_cfg: dict[str, Any],
        broker_id: str,
    ) -> tuple[Any, Any]:
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

        if role not in _FEED_ALLOWED_ROLES:
            logger.warning("Using role-guarded no-op broker clients", role=role)
            return _RoleGuardedNoopClient(role), _RoleGuardedNoopClient(role)

        if broker_id == "fubon":
            from hft_platform.feed_adapter.fubon.facade import FubonClientFacade  # lazy import

            logger.info("Instantiating Fubon broker clients", broker_id=broker_id)
            return FubonClientFacade(symbols_path, base_shioaji_cfg), FubonClientFacade(symbols_path, order_cfg)

        # Default: shioaji
        return ShioajiClientFacade(symbols_path, base_shioaji_cfg), ShioajiClientFacade(symbols_path, order_cfg)

    def build(self) -> ServiceRegistry:
        role = self._get_runtime_role()
        self.settings.setdefault("runtime_role", role)
        if role == "maintenance":
            logger.warning("Runtime role maintenance: broker feed creation is disabled", role=role)
        elif role not in _FEED_ALLOWED_ROLES:
            logger.warning("Runtime role does not create feed client", role=role)

        # B-OPS-03: Non-blocking preflight — warn if another runtime owns the session.
        lease_owned = self._check_session_ownership(role)
        self._last_role = role
        if role in _FEED_ALLOWED_ROLES and lease_owned:
            params = self._get_redis_lease_params()
            self._start_lease_refresh_thread(**params)
        elif role in _FEED_ALLOWED_ROLES:
            logger.warning("session_lease_refresh_not_started", role=role, reason="lease_not_owned")

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

        broker_id = self._resolve_broker_id()
        base_shioaji_cfg = dict(self.settings.get("shioaji", {}))
        md_client, order_client = self._build_broker_clients(role, symbols_path, base_shioaji_cfg, broker_id)

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
            broker_id=broker_id,
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
