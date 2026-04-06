from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Dict, Optional

if TYPE_CHECKING:
    from hft_platform.feed_adapter.protocol import BrokerOrderCodec

from structlog import get_logger

from hft_platform.core import timebase
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
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.ops.platform_inputs import PlatformDegradeInputs
from hft_platform.order.adapter import OrderAdapter
from hft_platform.recorder.worker import RecorderService
from hft_platform.risk.engine import RiskEngine
from hft_platform.risk.storm_guard import StormGuard
from hft_platform.services.market_data import MarketDataService
from hft_platform.services.registry import ServiceRegistry
from hft_platform.strategy.runner import StrategyRunner

logger = get_logger("bootstrap")

_VALID_BROKERS = frozenset({"shioaji", "fubon"})
_VALID_RUNTIME_ROLES = frozenset({"engine", "maintenance", "monitor", "wal_loader"})
_FEED_ALLOWED_ROLES = frozenset({"engine"})


def _env_float(name: str, default: float, min_value: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        value = default
    return max(min_value, value)


def _env_int(name: str, default: int, min_value: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        value = default
    return max(min_value, value)


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


def validate_order_mode_safety() -> None:
    """Reject dangerous mode combinations and log warnings for live trading."""
    hft_mode = os.getenv("HFT_MODE", "sim").strip().lower()
    # Normalize "real" → "live" (legacy alias)
    if hft_mode == "real":
        hft_mode = "live"
        os.environ["HFT_MODE"] = "live"
        logger.warning("normalized_mode", original="real", normalized="live")
    order_mode = os.getenv("HFT_ORDER_MODE", "sim").strip().lower()
    if order_mode == "real":
        order_mode = "live"
        os.environ["HFT_ORDER_MODE"] = "live"

    if order_mode in {"live", "real"}:
        if hft_mode not in {"real", "live"}:
            logger.critical(
                "FATAL: HFT_ORDER_MODE=live requires HFT_MODE=real or live",
                hft_mode=hft_mode,
                order_mode=order_mode,
            )
            raise SystemExit(
                "HFT_ORDER_MODE=live with HFT_MODE=sim is invalid. Set HFT_MODE=real to enable live orders."
            )
        confirm = os.getenv("HFT_LIVE_CONFIRM", "").strip().lower()
        if confirm != "yes-i-know":
            logger.critical(
                "LIVE_MODE_BLOCKED: Set HFT_LIVE_CONFIRM=yes-i-know to confirm live trading",
                order_mode=order_mode,
            )
            raise SystemExit(1)
        logger.warning("live_mode_confirmed", order_mode=order_mode)
        logger.critical(
            "LIVE ORDER MODE ACTIVE — real money orders will be placed",
            hft_mode=hft_mode,
            order_mode=order_mode,
        )


def _is_shadow_enabled_by_config(settings: dict[str, Any] | None = None) -> bool:
    """Check if shadow mode is enabled via YAML config (shadow.enabled: true)."""
    if settings is not None:
        return bool(settings.get("shadow", {}).get("enabled", False))
    return False


def validate_shadow_lock(settings: dict[str, Any] | None = None) -> None:
    """Dual-lock: refuse startup if shadow mode + live order mode are both active.

    Shadow mode can be enabled by EITHER:
    - HFT_ORDER_SHADOW_MODE=1 env var
    - shadow.enabled: true in YAML config (e.g. config/env/shadow/main.yaml)

    Combining shadow with HFT_ORDER_MODE=live/real is contradictory and dangerous.
    """
    shadow = os.getenv("HFT_ORDER_SHADOW_MODE", "0") == "1" or _is_shadow_enabled_by_config(settings)
    order_mode = os.getenv("HFT_ORDER_MODE", "sim").strip().lower()
    if shadow and order_mode in {"live", "real"}:
        logger.critical(
            "FATAL: shadow mode cannot be combined with HFT_ORDER_MODE=live/real",
            order_mode=order_mode,
            shadow_env=os.getenv("HFT_ORDER_SHADOW_MODE", "0"),
            shadow_yaml=_is_shadow_enabled_by_config(settings),
        )
        raise SystemExit(1)


def log_shadow_config_summary(settings: dict[str, Any] | None = None) -> None:
    """Log all shadow-relevant config at startup for debugging."""
    shadow_yaml = _is_shadow_enabled_by_config(settings)
    shadow_env = os.getenv("HFT_ORDER_SHADOW_MODE", "0")
    shadow_effective = shadow_env == "1" or shadow_yaml
    logger.info(
        "shadow_config_summary",
        shadow_mode=shadow_env,
        shadow_yaml_enabled=shadow_yaml,
        shadow_effective=shadow_effective,
        gateway_enabled=os.getenv("HFT_GATEWAY_ENABLED", "0"),
        order_mode=os.getenv("HFT_ORDER_MODE", "sim"),
        auto_recovery_enabled=os.getenv("HFT_PLATFORM_AUTO_RECOVERY_ENABLED", "1"),
    )


class SystemBootstrapper:
    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self.settings = settings if settings is not None else {}
        self._lease_refresh_running: bool = False
        self._lease_refresh_thread: Any | None = None
        self._last_role: str = "engine"

    def _get_runtime_role(self) -> str:
        raw = os.getenv("HFT_RUNTIME_ROLE", "engine").strip().lower().replace("-", "_")
        if raw not in _VALID_RUNTIME_ROLES:
            logger.warning("Unknown HFT_RUNTIME_ROLE; defaulting to 'engine'", role=raw)
            return "engine"
        return raw

    def build_platform_degrade_inputs(
        self,
        *,
        md_service: Any,
        recorder: Any,
        raw_queue: asyncio.Queue[Any],
        raw_exec_queue: asyncio.Queue[Any],
        recorder_queue: asyncio.Queue[Any],
        risk_queue: asyncio.Queue[Any],
        order_queue: asyncio.Queue[Any],
    ) -> PlatformDegradeInputs:
        metrics = None
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            metrics = MetricsRegistry.get()
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))

        inputs = PlatformDegradeInputs(
            md_service=md_service,
            recorder=recorder,
            raw_queue=raw_queue,
            raw_exec_queue=raw_exec_queue,
            recorder_queue=recorder_queue,
            risk_queue=risk_queue,
            order_queue=order_queue,
            metrics=metrics,
        )
        inputs.configure_thresholds(
            feed_gap_threshold_s=_env_float("HFT_PLATFORM_REDUCE_ONLY_FEED_GAP_S", 120.0, min_value=1.0),
            reconnect_pending_threshold_s=_env_float(
                "HFT_PLATFORM_REDUCE_ONLY_RECONNECT_PENDING_S",
                60.0,
                min_value=1.0,
            ),
            reconnect_flap_budget=_env_int("HFT_PLATFORM_REDUCE_ONLY_RECONNECT_FLAP_BUDGET", 5, min_value=0),
            queue_depth_threshold=_env_int("HFT_PLATFORM_REDUCE_ONLY_QUEUE_DEPTH", 5000, min_value=1),
            rss_threshold_mb=_env_int("HFT_PLATFORM_REDUCE_ONLY_RSS_MB", 2048, min_value=1),
            wal_backlog_files_threshold=_env_int("HFT_PLATFORM_REDUCE_ONLY_WAL_BACKLOG_FILES", 200, min_value=1),
        )
        return inputs

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
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
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
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
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
                except ImportError:
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
            raise ValueError(f"Unknown HFT_BROKER={broker_id!r}; valid options: {sorted(_VALID_BROKERS)}")
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
        from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade  # lazy import

        num_conns = int(os.getenv("HFT_QUOTE_CONNECTIONS", "1"))
        if num_conns > 1:
            from hft_platform.feed_adapter.shioaji.quote_connection_pool import QuoteConnectionPool

            pool = QuoteConnectionPool(symbols_path, base_shioaji_cfg, num_conns)
            pool.create_facades()
            # Order client needs the full symbol list for contract resolution
            # but does NOT subscribe to quotes, so the per-connection subscription
            # limit does not apply.  Temporarily raise it for the order facade.
            order_cfg = dict(order_cfg)
            order_cfg["max_subscriptions"] = num_conns * 200
            return pool, ShioajiClientFacade(symbols_path, order_cfg)
        return ShioajiClientFacade(symbols_path, base_shioaji_cfg), ShioajiClientFacade(symbols_path, order_cfg)

    def build(self) -> ServiceRegistry:
        role = self._get_runtime_role()
        self.settings.setdefault("runtime_role", role)
        if role == "maintenance":
            logger.warning("Runtime role maintenance: broker feed creation is disabled", role=role)
        elif role not in _FEED_ALLOWED_ROLES:
            logger.warning("Runtime role does not create feed client", role=role)

        # Fail fast on unsafe live/sim combinations before wiring broker-facing services.
        validate_order_mode_safety()

        # B-OPS-03: Non-blocking preflight — warn if another runtime owns the session.
        lease_owned = self._check_session_ownership(role)
        self._last_role = role
        if role in _FEED_ALLOWED_ROLES and lease_owned:
            params = self._get_redis_lease_params()
            self._start_lease_refresh_thread(**params)
        elif role in _FEED_ALLOWED_ROLES:
            logger.warning("session_lease_refresh_not_started", role=role, reason="lease_not_owned")

        # Preflight safety checks
        validate_shadow_lock(self.settings)
        log_shadow_config_summary(self.settings)

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
        # Shared map for e2e order-to-fill latency tracking (SLO-2): order_key -> created_ns
        cmd_created_ns_map: Dict[str, int] = {}
        # TCA: shared map for decision/arrival price enrichment: order_key -> (decision_price, arrival_price)
        cmd_tca_map: Dict[str, tuple[int, int]] = {}
        # DriftBurst detector for StormGuard (opt-in via env var)
        drift_burst_detector = None
        if os.getenv("HFT_STORMGUARD_DRIFT_BURST_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}:
            from hft_platform.risk.drift_burst_detector import DriftBurstDetector

            db_threshold = float(os.getenv("HFT_STORMGUARD_DRIFT_BURST_THRESHOLD", "3.0"))
            db_window = int(os.getenv("HFT_STORMGUARD_DRIFT_BURST_WINDOW", "100"))
            drift_burst_detector = DriftBurstDetector(
                window_size=db_window,
                burst_threshold=db_threshold,
            )
            logger.info(
                "DriftBurstDetector enabled for StormGuard",
                window_size=db_window,
                burst_threshold=db_threshold,
            )

        storm_guard = StormGuard(drift_burst_detector=drift_burst_detector)

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

        # Position checkpoint writer (periodic serialization)
        from hft_platform.execution.checkpoint import PositionCheckpointWriter

        checkpoint_writer = PositionCheckpointWriter(store=position_store)

        # Startup position verifier (dual-source recovery)
        from hft_platform.execution.startup_recon import StartupPositionVerifier

        startup_verifier = StartupPositionVerifier(
            client=order_client,
            position_store=position_store,
            checkpoint_path=os.getenv("HFT_POSITION_CHECKPOINT_PATH", ".runtime/position_checkpoint.json"),
        )

        # 4. Services
        feature_engine = None
        feature_profile_registry = None
        feature_profile = None
        feature_rollout_controller = None
        feature_rollout_assignment = None
        if os.getenv("HFT_FEATURE_ENGINE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}:
            try:
                feature_profile_registry = load_feature_profile_registry()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                feature_profile_registry = None
            try:
                feature_rollout_controller = load_feature_rollout_controller()
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
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
                        except Exception as exc:
                            logger.debug("operation_fallback", error=str(exc))
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
                        except Exception as exc:
                            logger.debug("operation_fallback", error=str(exc))
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
                        except Exception as exc:
                            logger.debug("operation_fallback", error=str(exc))
                            pass
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    feature_profile = None

        md_service = MarketDataService(
            bus,
            raw_queue,
            md_client,
            symbol_metadata=symbol_metadata,
            recorder_queue=recorder_queue,
            feature_engine=feature_engine,
            storm_guard=storm_guard,
        )
        _broker_codec: BrokerOrderCodec
        if broker_id == "fubon":
            from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec

            _broker_codec = FubonOrderCodec()
        else:
            from hft_platform.feed_adapter.shioaji.order_codec import ShioajiOrderCodec

            _broker_codec = ShioajiOrderCodec()

        # TCA: mid-price lookup function for arrival_price stamping
        def _get_mid_price(symbol: str) -> int:
            book = md_service.lob.books.get(symbol)
            if book is not None and book.mid_price_x2 > 0:
                return book.mid_price_x2 // 2
            return 0

        order_adapter = OrderAdapter(
            adapter_path, order_queue, order_client, order_id_map, broker_codec=_broker_codec,
            cmd_created_ns_map=cmd_created_ns_map,
            cmd_tca_map=cmd_tca_map,
            mid_price_fn=_get_mid_price,
        )

        # Wire shadow mode from YAML config (shadow.enabled: true) into ShadowOrderSink.
        # Previously only HFT_ORDER_SHADOW_MODE env var was checked, causing a config disconnect
        # where shadow.enabled in YAML was silently ignored.
        if _is_shadow_enabled_by_config(self.settings) and not order_adapter.shadow_sink.enabled:
            order_adapter.shadow_sink.enabled = True
            logger.info("shadow_mode_enabled_via_yaml_config")

        execution_gateway = ExecutionGateway(order_adapter)
        # TCA: FeeCalculator injection into ExecutionNormalizer
        _fee_calculator = None
        try:
            from hft_platform.tca.fee_calculator import FeeCalculator

            _fee_yaml = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "config",
                "base",
                "fees",
                "futures.yaml",
            )
            if os.path.isfile(_fee_yaml):
                _fee_calculator = FeeCalculator.from_yaml(_fee_yaml)
                logger.info("fee_calculator_loaded", path=_fee_yaml)
            else:
                logger.warning("fee_calculator_yaml_not_found", path=_fee_yaml)
                # X2-H1: Missing fee config → PnL tracking excludes fees/tax
                try:
                    from hft_platform.observability.metrics import MetricsRegistry
                    _m = MetricsRegistry.get()
                    if hasattr(_m, "startup_warnings_total"):
                        _m.startup_warnings_total.labels(component="fee_calculator").inc()
                except Exception:
                    pass
        except Exception as exc:
            logger.warning("fee_calculator_init_failed", error=str(exc))

        # WAL fallback for execution events dropped by full recorder queue
        _exec_wal_writer = None
        _exec_wal_dir = os.getenv("HFT_WAL_DIR", ".wal")
        try:
            from hft_platform.recorder.wal import WALWriter as _WALWriter

            _exec_wal_writer = _WALWriter(_exec_wal_dir)
            logger.info("exec_wal_fallback_enabled", wal_dir=_exec_wal_dir)
        except Exception as exc:
            logger.warning("exec_wal_fallback_init_failed", error=str(exc))

        exec_service = ExecutionRouter(
            bus,
            raw_exec_queue,
            order_id_map,
            position_store,
            execution_gateway.on_terminal_state,
            cmd_created_ns_map=cmd_created_ns_map,
            cmd_tca_map=cmd_tca_map,
            recorder_queue=recorder_queue,
            symbol_metadata=symbol_metadata,
            price_scale_provider=price_scale_provider,
            wal_writer=_exec_wal_writer,
        )
        if _fee_calculator is not None:
            exec_service.normalizer._fee_calculator = _fee_calculator
        risk_engine = RiskEngine(
            risk_path,
            risk_queue,
            order_queue,
            price_scale_provider,
            position_provider=position_store,
            storm_guard=storm_guard,
        )
        # Late-bind risk_engine to router (created after router due to dependency order)
        exec_service._risk_engine = risk_engine
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
            gateway_policy = GatewayPolicy(storm_guard=storm_guard)
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
        # Phase 3: rejection feedback + strategy publish queues
        _rejection_queue: asyncio.Queue | None = None
        _publish_queue: asyncio.Queue | None = None
        try:
            _rejection_queue = asyncio.Queue(maxsize=256)
            _publish_queue = asyncio.Queue(maxsize=64)
        except Exception as exc:
            logger.warning("phase3_queue_init_failed", error=str(exc))

        if _rejection_queue is not None and hasattr(risk_engine, '_rejection_sink'):
            risk_engine._rejection_sink = _rejection_queue

        if _rejection_queue is not None and hasattr(strategy_runner, '_rejection_sink'):
            strategy_runner._rejection_sink = _rejection_queue

        if _rejection_queue is not None and hasattr(strategy_runner, '_rejection_queue'):
            strategy_runner._rejection_queue = _rejection_queue

        if hasattr(strategy_runner, '_storm_guard'):
            strategy_runner._storm_guard = storm_guard

        if _publish_queue is not None and hasattr(strategy_runner, '_publish_sink'):
            strategy_runner._publish_sink = lambda ch, payload: _publish_queue.put_nowait((ch, payload))

        recorder = RecorderService(recorder_queue)
        platform_degrade_inputs = self.build_platform_degrade_inputs(
            md_service=md_service,
            recorder=recorder,
            raw_queue=raw_queue,
            raw_exec_queue=raw_exec_queue,
            recorder_queue=recorder_queue,
            risk_queue=risk_queue,
            order_queue=order_queue,
        )

        # Opt-in: SessionGovernor (disabled by default)
        session_governor = None
        if os.environ.get("HFT_SESSION_GOVERNOR_ENABLED", "0") == "1":
            try:
                from hft_platform.ops.evidence import get_shared_autonomy_evidence_writer
                from hft_platform.ops.position_flattener import PositionFlattener
                from hft_platform.ops.session_governor import SessionGovernor

                position_flattener = PositionFlattener(position_store=position_store, order_adapter=order_adapter)
                session_governor = SessionGovernor(
                    evidence_writer=get_shared_autonomy_evidence_writer(),
                    position_flattener=position_flattener,
                )
                # Wire TrackGate into StrategyRunner for per-symbol session filtering
                strategy_runner.track_gate = session_governor.track_gate
                logger.info("SessionGovernor created and TrackGate wired into StrategyRunner")
            except Exception as exc:
                logger.warning("SessionGovernor creation failed", error=str(exc))
                session_governor = None

        # Opt-in: AutonomyMonitor (disabled by default)
        autonomy_monitor = None
        if os.environ.get("HFT_AUTONOMY_MONITOR_ENABLED", "0") == "1":
            try:
                from hft_platform.ops.autonomy_monitor import AutonomyMonitor
                from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller

                autonomy_monitor = AutonomyMonitor(
                    storm_guard=storm_guard,
                    platform_degrade=get_shared_platform_degrade_controller(),
                    platform_inputs=platform_degrade_inputs,
                    recon_service=recon_service,
                    broker_client=order_client,
                )
                logger.info("AutonomyMonitor created")
            except Exception as exc:
                logger.warning("AutonomyMonitor creation failed", error=str(exc))
                autonomy_monitor = None

        # Opt-in: DailyReportService (disabled by default)
        daily_report_service = None
        if os.environ.get("HFT_DAILY_REPORT_ENABLED", "0") == "1":
            try:
                from hft_platform.services.daily_report import DailyReportService

                # Get CH client from recorder writer if available
                ch_client = None
                writer = getattr(recorder, "writer", None)
                if writer is not None:
                    ch_client = getattr(writer, "ch_client", None)

                # Get or create notification dispatcher
                notification_dispatcher = None
                if session_governor is not None:
                    notification_dispatcher = getattr(session_governor, "_notification_dispatcher", None)
                if notification_dispatcher is None:
                    try:
                        from hft_platform.notifications.dispatcher import (
                            NotificationDispatcher,
                        )
                        from hft_platform.notifications.telegram import TelegramSender

                        sender = TelegramSender(enabled=True)
                        notification_dispatcher = NotificationDispatcher(sender=sender)
                    except Exception:  # noqa: BLE001
                        pass

                evidence_writer = get_shared_autonomy_evidence_writer()

                if notification_dispatcher is not None:
                    # X-H1: Late-bind dispatcher to RiskEngine so daily-loss HALT alerts fire
                    if hasattr(risk_engine, "_notification_dispatcher"):
                        risk_engine._notification_dispatcher = notification_dispatcher
                        logger.info("RiskEngine notification_dispatcher wired")

                    daily_report_service = DailyReportService(
                        ch_client=ch_client,
                        notification_dispatcher=notification_dispatcher,
                        evidence_writer=evidence_writer,
                        position_store=position_store,
                        storm_guard=storm_guard,
                    )
                    # Register phase callback on SessionGovernor
                    if session_governor is not None:
                        session_governor.register_phase_callback(daily_report_service.on_phase_transition)
                    logger.info("DailyReportService created and wired")
                else:
                    logger.warning("DailyReportService skipped: no notification dispatcher available")
            except Exception as exc:
                logger.warning("DailyReportService creation failed", error=str(exc))
                daily_report_service = None

        # Startup config snapshot (non-blocking)
        from hft_platform.ops.config_snapshot import build_snapshot, write_snapshot_to_clickhouse

        try:
            _yaml_paths = [
                "config/base/main.yaml",
                str(self.settings.get("paths", {}).get("symbols", "config/symbols.yaml")),
                str(self.settings.get("paths", {}).get("strategy_limits", "config/base/strategy_limits.yaml")),
            ]
            _snapshot = build_snapshot(yaml_paths=_yaml_paths)
            if ch_client is not None:
                asyncio.get_event_loop().create_task(write_snapshot_to_clickhouse(ch_client, _snapshot))
            else:
                logger.info("config_snapshot_fallback", **_snapshot)
        except Exception:  # noqa: BLE001
            logger.warning("config_snapshot_build_failed", exc_info=True)

        # Alertmanager → Telegram bridge (non-blocking, failure does not block trading)
        try:
            from hft_platform.notifications.alertmanager_bridge import AlertmanagerBridge

            _alert_bridge = AlertmanagerBridge()
            asyncio.get_event_loop().create_task(_alert_bridge.run())
            logger.info("alertmanager_bridge_scheduled")
        except Exception:  # noqa: BLE001
            logger.warning("alertmanager_bridge_start_failed", exc_info=True)

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
            client=order_client,
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
            platform_degrade_inputs=platform_degrade_inputs,
            gateway_service=gateway_service,
            intent_channel=intent_channel,
            session_governor=session_governor,
            autonomy_monitor=autonomy_monitor,
            daily_report_service=daily_report_service,
            checkpoint_writer=checkpoint_writer,
            startup_verifier=startup_verifier,
        )


async def wait_for_readiness(system: Any, *, timeout_s: float = 30.0) -> None:
    """Block until the system reports ready, or raise on timeout.

    This prevents the system from processing market data before all
    subsystems (risk, order, recorder) are operational.

    Parameters
    ----------
    system : HFTSystem
        The running system instance whose health is probed.
    timeout_s : float
        Maximum seconds to wait before raising ``RuntimeError``.
    """
    from hft_platform.observability.health import HealthServer

    health = HealthServer(system=system)
    deadline = time.monotonic() + timeout_s
    checks: dict[str, Any] = {}

    while time.monotonic() < deadline:
        is_ready, checks = health._check_readiness()
        if is_ready:
            logger.info(
                "startup_readiness_ok",
                checks=checks,
                elapsed_ms=round((timeout_s - (deadline - time.monotonic())) * 1000, 1),
                timestamp_ns=timebase.now_ns(),
            )
            return
        await asyncio.sleep(0.1)

    logger.error(
        "startup_readiness_timeout",
        timeout_s=timeout_s,
        checks=checks,
        timestamp_ns=timebase.now_ns(),
    )
    raise RuntimeError(f"System not ready within {timeout_s}s: {checks}")
