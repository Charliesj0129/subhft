# DEPRECATED: External consumers should use ShioajiClientFacade from
# hft_platform.feed_adapter.shioaji.facade instead of importing from this module.
# This module is retained for internal use by the shioaji/ sub-package runtimes.
import datetime as dt
import os
import queue
import re
import threading
import time
from collections import deque
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, List

import yaml
from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji import router as _router
from hft_platform.feed_adapter.shioaji.signatures import detect_crash_signature
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.rate_limiter import RateLimiter

try:
    import shioaji as sj
except Exception:  # pragma: no cover - fallback when library absent
    sj = None

_fcntl: ModuleType | None
try:
    import fcntl as _fcntl
except Exception:  # pragma: no cover - non-posix fallback
    _fcntl = None
fcntl: ModuleType | None = _fcntl

logger = get_logger("feed_adapter")

# Backward-compatible exports for existing tests/bench harnesses.
CLIENT_REGISTRY = _router.CLIENT_REGISTRY
CLIENT_REGISTRY_LOCK = _router.CLIENT_REGISTRY_LOCK
CLIENT_REGISTRY_BY_CODE = _router.CLIENT_REGISTRY_BY_CODE
CLIENT_REGISTRY_SNAPSHOT = _router.CLIENT_REGISTRY_SNAPSHOT
CLIENT_REGISTRY_BY_CODE_SNAPSHOT = _router.CLIENT_REGISTRY_BY_CODE_SNAPSHOT
CLIENT_REGISTRY_WILDCARD_SNAPSHOT = _router.CLIENT_REGISTRY_WILDCARD_SNAPSHOT
CLIENT_DISPATCH_SNAPSHOT = _router.CLIENT_DISPATCH_SNAPSHOT
CLIENT_DISPATCH_BY_CODE_SNAPSHOT = _router.CLIENT_DISPATCH_BY_CODE_SNAPSHOT
CLIENT_DISPATCH_WILDCARD_SNAPSHOT = _router.CLIENT_DISPATCH_WILDCARD_SNAPSHOT
TOPIC_CODE_CACHE = _router.TOPIC_CODE_CACHE
_ROUTE_MISS_STRICT = _router._ROUTE_MISS_STRICT
_ROUTE_MISS_FALLBACK_MODE = _router._ROUTE_MISS_FALLBACK_MODE
_ROUTE_MISS_LOG_EVERY = _router._ROUTE_MISS_LOG_EVERY
_ROUTE_MISS_COUNT = _router._ROUTE_MISS_COUNT
_record_route_metric = _router._record_route_metric
_extract_code_from_topic = _router._extract_code_from_topic
_registry_snapshot = _router._registry_snapshot


def _sync_router_route_globals() -> None:
    _router._ROUTE_MISS_STRICT = bool(_ROUTE_MISS_STRICT)
    _router._ROUTE_MISS_FALLBACK_MODE = str(_ROUTE_MISS_FALLBACK_MODE)
    _router._ROUTE_MISS_LOG_EVERY = int(_ROUTE_MISS_LOG_EVERY)
    _router._ROUTE_MISS_COUNT = int(_ROUTE_MISS_COUNT)
    _router._record_route_metric = _record_route_metric


def _registry_register(client: Any) -> None:
    _router._registry_register(client)


def _registry_rebind_codes(client: Any, codes: list[str]) -> None:
    _router._registry_rebind_codes(client, codes)


def _registry_unregister(client: Any) -> None:
    _router._registry_unregister(client)


def dispatch_tick_cb(*args, **kwargs):
    global _ROUTE_MISS_COUNT
    _sync_router_route_globals()
    _router.dispatch_tick_cb(*args, **kwargs)
    _ROUTE_MISS_COUNT = _router._ROUTE_MISS_COUNT


class ShioajiClient:
    def __init__(self, config_path: str | None = None, shioaji_config: dict[str, Any] | None = None):
        self.MAX_SUBSCRIPTIONS = 200
        self.contracts_timeout = int(os.getenv("SHIOAJI_CONTRACTS_TIMEOUT", "10000"))
        self.fetch_contract = os.getenv("SHIOAJI_FETCH_CONTRACT", "1") != "0"
        self.subscribe_trade = os.getenv("SHIOAJI_SUBSCRIBE_TRADE", "1") != "0"
        self.allow_symbol_fallback = os.getenv("HFT_ALLOW_SYMBOL_FALLBACK") == "1"
        self.allow_synthetic_contracts = os.getenv("HFT_ALLOW_SYNTHETIC_CONTRACTS") == "1"
        self.index_exchange = os.getenv("HFT_INDEX_EXCHANGE", "TSE").upper()
        self.resubscribe_cooldown = float(os.getenv("HFT_RESUBSCRIBE_COOLDOWN", "1.5"))
        self.shioaji_config = shioaji_config or {}

        def _as_bool(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        if "activate_ca" in self.shioaji_config:
            self.activate_ca = _as_bool(self.shioaji_config.get("activate_ca"))
        else:
            self.activate_ca = os.getenv("SHIOAJI_ACTIVATE_CA", "0") == "1" or os.getenv("HFT_ACTIVATE_CA", "0") == "1"
        self.ca_path = self.shioaji_config.get("ca_path") or os.getenv("SHIOAJI_CA_PATH") or os.getenv("CA_CERT_PATH")
        ca_password = (
            self.shioaji_config.get("ca_password") or os.getenv("SHIOAJI_CA_PASSWORD") or os.getenv("CA_PASSWORD")
        )
        if not ca_password:
            env_key = self.shioaji_config.get("ca_password_env")
            if env_key:
                ca_password = os.getenv(str(env_key))
        self.ca_password = ca_password

        if config_path is None:
            config_path = os.getenv("SYMBOLS_CONFIG")
            if not config_path:
                if os.path.exists("config/symbols.yaml"):
                    config_path = "config/symbols.yaml"
                else:
                    config_path = "config/base/symbols.yaml"

        sim_override = self.shioaji_config.get("simulation") if "simulation" in self.shioaji_config else None
        if sim_override is None:
            is_sim = os.getenv("HFT_MODE", "real") == "sim"
        else:
            is_sim = _as_bool(sim_override)
        if sj:
            self.api = sj.Shioaji(simulation=is_sim)
        else:
            self.api = None
        self.config_path = config_path
        self.symbols: List[Dict[str, Any]] = []
        self._load_config()
        self.subscribed_count = 0
        self.subscribed_codes: set[str] = set()
        self.tick_callback: Callable[..., Any] | None = None
        self._quote_dispatch_async = os.getenv("HFT_SHIOAJI_QUOTE_DISPATCH_THREAD", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            self._quote_dispatch_queue_size = max(1, int(os.getenv("HFT_SHIOAJI_QUOTE_CB_QUEUE_SIZE", "8192")))
        except ValueError:
            self._quote_dispatch_queue_size = 8192
        try:
            self._quote_dispatch_batch_max = max(1, int(os.getenv("HFT_SHIOAJI_QUOTE_CB_BATCH_MAX", "32")))
        except ValueError:
            self._quote_dispatch_batch_max = 32
        try:
            self._quote_dispatch_metrics_every = max(1, int(os.getenv("HFT_SHIOAJI_QUOTE_CB_METRICS_EVERY", "128")))
        except ValueError:
            self._quote_dispatch_metrics_every = 128
        self._quote_dispatch_queue: queue.Queue[tuple[tuple[Any, ...], dict[str, Any]] | None] | None = None
        self._quote_dispatch_thread: threading.Thread | None = None
        self._quote_dispatch_running = False
        self._quote_dispatch_dropped = 0
        self._quote_dispatch_enqueued = 0
        self._quote_dispatch_processed = 0
        self._callbacks_registered = False
        self._pending_quote_resubscribe = False
        self._pending_quote_ts = 0.0
        self._pending_quote_relogining = False
        self._pending_quote_relogin_thread: threading.Thread | None = None
        self._last_quote_event_ts = 0.0
        self._callbacks_retrying = False
        self._callbacks_retry_thread: threading.Thread | None = None
        self.logged_in = False
        self.mode = "simulation" if (is_sim or self.api is None) else "real"
        if self.mode == "simulation":
            self.activate_ca = False
        self.ca_active = False
        self._reconnect_lock = threading.Lock()
        self._callback_register_lock = threading.Lock()
        self._last_reconnect_ts = 0.0
        self._reconnect_backoff_s = float(os.getenv("HFT_RECONNECT_BACKOFF_S", "30"))
        self._reconnect_backoff_max_s = float(os.getenv("HFT_RECONNECT_BACKOFF_MAX_S", "600"))
        self._login_timeout_s = float(os.getenv("HFT_SHIOAJI_LOGIN_TIMEOUT_S", "20"))
        self._reconnect_timeout_s = float(os.getenv("HFT_SHIOAJI_RECONNECT_TIMEOUT_S", "45"))
        self._reconnect_subscribe_timeout_s = float(os.getenv("HFT_SHIOAJI_RECONNECT_SUBSCRIBE_TIMEOUT_S", "30"))
        try:
            self._login_retry_max = max(0, int(os.getenv("HFT_SHIOAJI_LOGIN_RETRY_MAX", "1")))
        except ValueError:
            self._login_retry_max = 1
        self._receive_window: int | None = int(os.environ.get("HFT_SHIOAJI_RECEIVE_WINDOW", "0")) or None
        self._last_login_error: str | None = None
        self._last_reconnect_error: str | None = None
        self.metrics = MetricsRegistry.get()
        self._api_cache: dict[str, tuple[float, Any]] = {}
        self._api_cache_lock = threading.Lock()
        self._api_cache_max_size = int(os.getenv("HFT_API_CACHE_MAX_SIZE", "1000"))
        self._positions_cache_ttl_s = float(os.getenv("HFT_POSITIONS_CACHE_TTL_S", "1.5"))
        self._usage_cache_ttl_s = float(os.getenv("HFT_USAGE_CACHE_TTL_S", "5"))
        self._account_cache_ttl_s = float(os.getenv("HFT_ACCOUNT_CACHE_TTL_S", "5"))
        self._margin_cache_ttl_s = float(os.getenv("HFT_MARGIN_CACHE_TTL_S", "5"))
        self._profit_cache_ttl_s = float(os.getenv("HFT_PROFIT_CACHE_TTL_S", "10"))
        self._positions_detail_cache_ttl_s = float(os.getenv("HFT_POSITION_DETAIL_CACHE_TTL_S", "10"))
        self._api_last_latency_ms: dict[str, float] = {}
        self._quote_force_relogin_s = float(os.getenv("HFT_QUOTE_FORCE_RELOGIN_S", "15"))
        self._quote_flap_window_s = float(os.getenv("HFT_QUOTE_FLAP_WINDOW_S", "60"))
        self._quote_flap_threshold = int(os.getenv("HFT_QUOTE_FLAP_THRESHOLD", "5"))
        self._quote_flap_cooldown_s = float(os.getenv("HFT_QUOTE_FLAP_COOLDOWN_S", "300"))
        self._quote_flap_events: deque[float] = deque()
        self._last_quote_flap_relogin_ts = 0.0
        self._quote_version_mode = os.getenv("HFT_QUOTE_VERSION", "auto").strip().lower()
        if self._quote_version_mode not in {"v0", "v1", "auto"}:
            self._quote_version_mode = "auto"
        self._quote_version_strict = os.getenv("HFT_QUOTE_VERSION_STRICT", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._quote_version = "v1" if self._quote_version_mode in {"v1", "auto"} else "v0"
        self._quote_schema_guard = os.getenv("HFT_QUOTE_SCHEMA_GUARD", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._quote_schema_guard_strict = os.getenv("HFT_QUOTE_SCHEMA_GUARD_STRICT", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            self._quote_schema_mismatch_log_every = max(1, int(os.getenv("HFT_QUOTE_SCHEMA_MISMATCH_LOG_EVERY", "100")))
        except ValueError:
            self._quote_schema_mismatch_log_every = 100
        self._quote_schema_mismatch_count = 0
        self._quote_schema_mismatch_metric_cache: dict[tuple[str, str], Any] = {}
        self._last_quote_data_ts = 0.0
        self._first_quote_seen = False
        self._quote_watchdog_thread: threading.Thread | None = None
        self._quote_watchdog_running = False
        self._quote_watchdog_interval_s = float(os.getenv("HFT_QUOTE_WATCHDOG_S", "5"))
        self._quote_no_data_s = float(os.getenv("HFT_QUOTE_NO_DATA_S", "30"))
        self._quote_watchdog_skip_off_hours = _as_bool(os.getenv("HFT_QUOTE_WATCHDOG_SKIP_OFF_HOURS", "1"))
        self._quote_off_hours_log_interval_s = float(os.getenv("HFT_QUOTE_OFF_HOURS_LOG_INTERVAL_S", "60"))
        self._last_quote_off_hours_log_ts = 0.0
        self._quote_pending_stall_warn_s = float(os.getenv("HFT_QUOTE_PENDING_STALL_WARN_S", "120"))
        self._quote_pending_stall_reported = False
        self._event_callback_registered = False
        # Keep a strong reference to event callback to avoid SDK-side weakref GC issues.
        self._event_callback_fn = self._on_quote_event
        self._event_callback_retrying = False
        self._event_callback_retry_thread: threading.Thread | None = None
        self._event_callback_retry_s = float(os.getenv("HFT_QUOTE_EVENT_RETRY_S", "5"))
        self._pending_quote_reason: str | None = None
        self._resubscribe_scheduled = False
        self._resubscribe_thread: threading.Thread | None = None
        self._resubscribe_delay_s = float(os.getenv("HFT_RESUBSCRIBE_DELAY_S", "0.5"))
        self._api_rate_limiter = RateLimiter(
            soft_cap=int(os.getenv("HFT_SHIOAJI_API_SOFT_CAP", "20")),
            hard_cap=int(os.getenv("HFT_SHIOAJI_API_HARD_CAP", "25")),
            window_s=int(os.getenv("HFT_SHIOAJI_API_WINDOW_S", "5")),
        )

        # Session refresh configuration (C3)
        self._session_refresh_interval_s = float(os.getenv("HFT_SESSION_REFRESH_S", "86400"))  # 24 hours
        self._last_session_refresh_ts = 0.0
        self._session_refresh_thread: threading.Thread | None = None
        self._session_refresh_running = False
        self._session_refresh_check_interval_s = 3600.0  # Check every hour

        # Holiday-aware session refresh (O4)
        self._session_refresh_holiday_aware = os.getenv("HFT_SESSION_REFRESH_HOLIDAY_AWARE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # Post-refresh health check (O5)
        self._session_refresh_verify_timeout_s = float(os.getenv("HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S", "10.0"))

        # Market open grace period (C4)
        self._market_open_grace_s = float(os.getenv("HFT_MARKET_OPEN_GRACE_S", "60"))  # 60 seconds
        self._market_open_grace_active = False

        # C2: Failed subscription tracking + retry thread
        self._failed_sub_symbols: list[Dict[str, Any]] = []
        self._sub_retry_running = False
        self._sub_retry_thread: threading.Thread | None = None
        self._contract_retry_s = float(os.getenv("HFT_CONTRACT_RETRY_S", "60"))

        # C3: Contract cache refresh thread
        self._contract_refresh_s = float(os.getenv("HFT_CONTRACT_REFRESH_S", "86400"))
        self._contract_cache_path = os.getenv("HFT_CONTRACT_CACHE_PATH", "config/contracts.json")
        self._contract_refresh_running = False
        self._contract_refresh_thread: threading.Thread | None = None
        self._contract_refresh_lock = threading.Lock()
        self._contract_refresh_version = 0
        self._contract_refresh_last_diff: dict[str, Any] = {}
        self._contract_refresh_last_status: dict[str, Any] = {}
        self._contract_refresh_status_path = os.getenv(
            "HFT_CONTRACT_REFRESH_STATUS_PATH", "outputs/contract_refresh_status.json"
        )
        self._contract_refresh_resubscribe_policy = (
            os.getenv("HFT_CONTRACT_REFRESH_RESUBSCRIBE_POLICY", "none").strip().lower() or "none"
        )
        self._session_lock_enabled = _as_bool(os.getenv("HFT_SHIOAJI_SESSION_LOCK_ENABLED", "1"))
        lock_id_raw = (
            os.getenv("SHIOAJI_ACCOUNT") or os.getenv("SHIOAJI_PERSON_ID") or os.getenv("SHIOAJI_API_KEY") or "default"
        )
        lock_id = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(lock_id_raw).strip())[:64] or "default"
        lock_dir = os.getenv("HFT_SHIOAJI_SESSION_LOCK_DIR", ".wal/.locks")
        self._session_lock_path = str(Path(lock_dir) / f"shioaji_session_{lock_id}.lock")
        self._session_lock_fd: Any | None = None

        # C2: Session policy interface — quote-side code must use this, not call
        # self.reconnect() directly, to keep session and quote runtimes decoupled.
        # Initialized lazily (after self is fully constructed) by ShioajiClientFacade
        # or by calling _init_session_policy(). This avoids a circular import at
        # module-load time while still providing the Protocol interface.
        self._session_policy: Any | None = None

        # C3: Quote event handler — centralises pending state transitions.
        # When set (by ShioajiClientFacade), _mark_quote_pending and
        # _clear_quote_pending delegate to this handler so the state machine
        # is owned by QuoteEventHandler rather than scattered across the client.
        self._quote_event_handler: Any | None = None

        # C4: Domain runtimes/gateways (contracts/account/order/session/quote).
        from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway
        from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime
        from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway
        from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime
        from hft_platform.feed_adapter.shioaji.session_runtime import SessionRuntime
        from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

        self._contracts_runtime = ContractsRuntime(self)
        self._account_gateway = AccountGateway(self)
        self._order_gateway = OrderGateway(self)
        self._session_runtime = SessionRuntime(self)
        self._quote_runtime = QuoteRuntime(self)
        self._subscription_manager = SubscriptionManager(self)
        # Wire decoupled interfaces (SessionPolicy + QuoteEventHandler).
        self._session_policy = self._session_runtime
        self._quote_event_handler = self._quote_runtime._event_handler

        # Register self globally (callback routing + strong ref)
        _registry_register(self)
        self._refresh_quote_routes()
        logger.info("Registered ShioajiClient in Global Registry")

    def _init_session_policy(self) -> None:
        """Ensure session_policy is wired (no-op: already set in __init__)."""
        if self._session_policy is None:
            self._session_policy = self._session_runtime

    def _init_domain_modules(self) -> None:
        """Lazily create contracts/order/account delegates.

        Some tests construct ``ShioajiClient`` via ``__new__`` and bypass
        ``__init__``. Keep wrappers resilient by rebuilding delegates on demand.
        """
        if getattr(self, "_contracts_runtime", None) is None:
            from hft_platform.feed_adapter.shioaji.contracts_runtime import ContractsRuntime

            self._contracts_runtime = ContractsRuntime(self)
        if getattr(self, "_account_gateway", None) is None:
            from hft_platform.feed_adapter.shioaji.account_gateway import AccountGateway

            self._account_gateway = AccountGateway(self)
        if getattr(self, "_order_gateway", None) is None:
            from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway

            self._order_gateway = OrderGateway(self)

    def _init_quote_runtime(self) -> None:
        """Lazily create quote runtime delegate for __new__-constructed tests."""
        if getattr(self, "_quote_runtime", None) is None:
            from hft_platform.feed_adapter.shioaji.quote_runtime import QuoteRuntime

            self._quote_runtime = QuoteRuntime(self)
        if getattr(self, "_quote_event_handler", None) is None:
            self._quote_event_handler = self._quote_runtime._event_handler

    def _init_subscription_manager(self) -> None:
        """Lazily create subscription manager for __new__-constructed tests."""
        if getattr(self, "_subscription_manager", None) is None:
            from hft_platform.feed_adapter.shioaji.subscription_manager import SubscriptionManager

            self._subscription_manager = SubscriptionManager(self)

    def _contracts(self):
        self._init_domain_modules()
        return self._contracts_runtime

    def _accounts(self):
        self._init_domain_modules()
        return self._account_gateway

    def _orders(self):
        self._init_domain_modules()
        return self._order_gateway

    def _quotes(self):
        self._init_quote_runtime()
        return self._quote_runtime

    def _subscriptions(self):
        self._init_subscription_manager()
        return self._subscription_manager

    def _request_reconnect_via_policy(self, reason: str, force: bool = True) -> bool:
        """Route a reconnect intent through the SessionPolicy interface.

        Falls back to direct self.reconnect() when policy is not yet
        initialized (e.g., in unit tests that construct ShioajiClient directly).
        """
        if self._session_policy is not None:
            try:
                return bool(self._session_policy.request_reconnect(reason=reason, force=force))
            except Exception:
                return False
        # Fallback: direct call (only in legacy/test contexts)
        return bool(self.reconnect(reason=reason, force=force))

    def _record_api_latency(self, op: str, start_ns: int, ok: bool = True) -> None:
        if not self.metrics:
            return
        now_ns = time.perf_counter_ns()
        try:
            start_ns_int = int(start_ns)
        except (TypeError, ValueError):
            start_ns_int = now_ns
        latency_ms = max(0.0, (now_ns - start_ns_int) / 1e6)
        op_label = self._sanitize_metric_label(op, fallback="unknown")
        result = "ok" if bool(ok) else "error"
        self.metrics.shioaji_api_latency_ms.labels(op=op_label, result=result).observe(latency_ms)
        last = self._api_last_latency_ms.get(op_label)
        if last is not None:
            jitter = abs(latency_ms - last)
            self.metrics.shioaji_api_jitter_ms.labels(op=op_label).set(jitter)
            if hasattr(self.metrics, "shioaji_api_jitter_ms_hist"):
                self.metrics.shioaji_api_jitter_ms_hist.labels(op=op_label).observe(jitter)
        self._api_last_latency_ms[op_label] = latency_ms
        if not ok:
            self.metrics.shioaji_api_errors_total.labels(op=op_label).inc()

    @staticmethod
    def _sanitize_metric_label(value: Any, *, fallback: str) -> str:
        """Ensure Prometheus label values are always strings with stable cardinality."""
        if isinstance(value, str):
            text = value
        elif isinstance(value, bytes):
            text = value.decode("utf-8", errors="replace")
        elif isinstance(value, type):
            text = value.__name__
        else:
            name = getattr(value, "__name__", None)
            text = str(name) if name else type(value).__name__
        text = text.strip()
        if not text:
            return fallback
        if len(text) > 64:
            return text[:64]
        return text

    def _record_crash_signature(self, text: str | None, *, context: str) -> None:
        metrics = getattr(self, "metrics", None)
        if not metrics or not hasattr(metrics, "shioaji_crash_signature_total"):
            return
        signature = detect_crash_signature(text)
        if not signature:
            return
        try:
            metrics.shioaji_crash_signature_total.labels(signature=signature, context=context).inc()
        except Exception:
            return

    def _safe_call_with_timeout(
        self,
        op: str,
        fn: Callable[[], Any],
        timeout_s: float,
    ) -> tuple[bool, Any | None, Exception | None, bool]:
        """Run blocking broker SDK call with timeout in a daemon thread."""
        if timeout_s <= 0:
            try:
                return True, fn(), None, False
            except Exception as exc:
                return False, None, exc, False
        done = threading.Event()
        state: dict[str, Any] = {}

        def _worker() -> None:
            try:
                state["result"] = fn()
            except Exception as exc:  # pragma: no cover
                state["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=_worker, name=f"shioaji-{op}-guard", daemon=True)
        worker.start()
        if not done.wait(timeout=max(0.1, timeout_s)):
            return False, None, TimeoutError(f"{op} timed out after {timeout_s:.1f}s"), True
        err = state.get("error")
        if err is not None:
            return False, None, err, False
        return True, state.get("result"), None, False

    def _set_thread_alive_metric(self, thread_name: str, alive: bool) -> None:
        metrics = getattr(self, "metrics", None)
        if not metrics or not hasattr(metrics, "shioaji_thread_alive"):
            return
        try:
            metrics.shioaji_thread_alive.labels(thread=thread_name).set(1 if alive else 0)
        except Exception:
            return

    def _update_quote_pending_metrics(self) -> None:
        if not self.metrics:
            return
        age_s = 0.0
        if self._pending_quote_resubscribe and self._pending_quote_ts > 0:
            age_s = max(0.0, timebase.now_s() - self._pending_quote_ts)
        try:
            if hasattr(self.metrics, "shioaji_quote_pending_age_seconds"):
                self.metrics.shioaji_quote_pending_age_seconds.set(age_s)
            if (
                self._pending_quote_resubscribe
                and age_s >= self._quote_pending_stall_warn_s
                and not self._quote_pending_stall_reported
                and hasattr(self.metrics, "shioaji_quote_pending_stall_total")
            ):
                reason = self._sanitize_metric_label(self._pending_quote_reason or "unknown", fallback="unknown")
                self.metrics.shioaji_quote_pending_stall_total.labels(reason=reason).inc()
                self._quote_pending_stall_reported = True
                logger.warning(
                    "Pending quote resubscribe appears stalled",
                    reason=self._pending_quote_reason,
                    age_s=round(age_s, 2),
                )
        except Exception:
            return

    def _ensure_session_lock(self) -> bool:
        if not self._session_lock_enabled:
            return True
        if self._session_lock_fd is not None:
            return True
        lock_fd = None
        try:
            lock_path = Path(self._session_lock_path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_fd = open(lock_path, "a+", encoding="utf-8")
            if fcntl is not None:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._session_lock_fd = lock_fd
            return True
        except Exception as exc:
            if lock_fd is not None:
                try:
                    lock_fd.close()
                except Exception:
                    pass
            logger.warning(
                "Potential duplicate broker runtime detected: session lock unavailable",
                lock_path=self._session_lock_path,
                error=str(exc),
            )
            if self.metrics and hasattr(self.metrics, "shioaji_session_lock_conflicts_total"):
                try:
                    self.metrics.shioaji_session_lock_conflicts_total.inc()
                except Exception:
                    pass
            return False

    def _release_session_lock(self) -> None:
        lock_fd = getattr(self, "_session_lock_fd", None)
        if lock_fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_fd.close()
        except Exception:
            pass
        self._session_lock_fd = None

    def _cache_get(self, key: str) -> Any | None:
        now = timebase.now_s()
        with self._api_cache_lock:
            entry = self._api_cache.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._api_cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, ttl_s: float, value: Any) -> None:
        expires_at = timebase.now_s() + max(0.0, ttl_s)
        with self._api_cache_lock:
            # Evict expired entries if cache is at limit
            if len(self._api_cache) >= self._api_cache_max_size:
                now = timebase.now_s()
                expired_keys = [k for k, (exp, _) in self._api_cache.items() if now >= exp]
                for k in expired_keys:
                    del self._api_cache[k]
                # If still at limit, remove oldest entry
                if len(self._api_cache) >= self._api_cache_max_size:
                    oldest_key = min(self._api_cache.keys(), key=lambda k: self._api_cache[k][0])
                    del self._api_cache[oldest_key]
            self._api_cache[key] = (expires_at, value)

    def _rate_limit_api(self, op: str) -> bool:
        if not self._api_rate_limiter.check():
            logger.warning("API rate limit hit", op=op)
            return False
        self._api_rate_limiter.record()
        return True

    def _process_tick(self, *args, **kwargs):
        """Internal method called by global dispatcher."""
        try:
            ok_schema, schema_reason = self._validate_quote_schema(*args, **kwargs)
            if not ok_schema:
                self._handle_quote_schema_mismatch(schema_reason, *args, **kwargs)
                return
            self._last_quote_data_ts = timebase.now_s()
            if not self._first_quote_seen:
                self._first_quote_seen = True
                logger.info(
                    "First quote data received",
                    quote_version=self._quote_version,
                    quote_version_mode=self._quote_version_mode,
                )
                try:
                    if self.metrics and hasattr(self.metrics, "feed_first_quote_total"):
                        self.metrics.feed_first_quote_total.inc()
                except Exception:
                    pass
            if self._pending_quote_resubscribe:
                self._clear_quote_pending()
            if self.tick_callback:
                self.tick_callback(*args, **kwargs)
        except Exception as e:
            logger.error("Error processing tick", error=str(e))

    def _validate_quote_schema(self, *args, **kwargs) -> tuple[bool, str]:
        """Delegates to QuoteRuntime.validate_quote_schema()."""
        return self._quotes().validate_quote_schema(*args, **kwargs)

    def _handle_quote_schema_mismatch(self, reason: str, *args, **kwargs) -> None:
        self._quote_schema_mismatch_count += 1
        try:
            if self.metrics and hasattr(self.metrics, "quote_schema_mismatch_total"):
                key = ("v1", reason)
                child = self._quote_schema_mismatch_metric_cache.get(key)
                if child is None:
                    child = self.metrics.quote_schema_mismatch_total.labels(expected="v1", reason=reason)
                    self._quote_schema_mismatch_metric_cache[key] = child
                child.inc()
        except Exception:
            pass
        if self._quote_schema_mismatch_count % self._quote_schema_mismatch_log_every == 1:
            logger.error(
                "Quote schema guard rejected callback payload",
                expected_version="v1",
                reason=reason,
                arg0_type=(type(args[0]).__name__ if args else None),
                kwargs_keys=sorted(kwargs.keys())[:8],
            )

    def _enqueue_tick(self, *args, **kwargs) -> None:
        """Non-blocking callback ingress: callback thread enqueues, worker executes."""
        start_ns = time.perf_counter_ns()
        try:
            if not self._quote_dispatch_async:
                self._process_tick(*args, **kwargs)
                return
            self._start_quote_dispatch_worker()
            q = self._quote_dispatch_queue
            if q is None:
                self._process_tick(*args, **kwargs)
                return
            try:
                q.put_nowait((args, kwargs))
                self._quote_dispatch_enqueued += 1
                if self.metrics and (self._quote_dispatch_enqueued % self._quote_dispatch_metrics_every == 0):
                    try:
                        if hasattr(self.metrics, "shioaji_quote_callback_queue_depth"):
                            self.metrics.shioaji_quote_callback_queue_depth.set(q.qsize())
                    except Exception:
                        pass
            except queue.Full:
                self._quote_dispatch_dropped += 1
                if self.metrics:
                    try:
                        self.metrics.raw_queue_dropped_total.inc()
                        if hasattr(self.metrics, "shioaji_quote_callback_queue_dropped_total"):
                            self.metrics.shioaji_quote_callback_queue_dropped_total.inc()
                    except Exception:
                        pass
                if self._quote_dispatch_dropped % 100 == 1:
                    logger.warning(
                        "Quote callback queue full; dropping quote callback payload",
                        dropped_total=self._quote_dispatch_dropped,
                        maxsize=self._quote_dispatch_queue_size,
                    )
        finally:
            if self.metrics and hasattr(self.metrics, "shioaji_quote_callback_ingress_latency_ns"):
                try:
                    self.metrics.shioaji_quote_callback_ingress_latency_ns.observe(
                        max(0, time.perf_counter_ns() - start_ns)
                    )
                except Exception:
                    pass

    def _start_quote_dispatch_worker(self) -> None:
        if not self._quote_dispatch_async or self._quote_dispatch_running:
            return
        self._quote_dispatch_queue = queue.Queue(maxsize=self._quote_dispatch_queue_size)
        q = self._quote_dispatch_queue
        if q is None:
            return
        self._quote_dispatch_running = True
        batch_max = self._quote_dispatch_batch_max

        def _worker() -> None:
            while self._quote_dispatch_running:
                try:
                    item = q.get(timeout=0.5)
                except queue.Empty:
                    continue
                if item is None:
                    continue
                batch_count = 0

                def _process_item(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
                    try:
                        self._process_tick(*args, **kwargs)
                    except Exception as exc:
                        logger.error("Quote dispatch worker error", error=str(exc))

                args, kwargs = item
                _process_item(args, kwargs)
                batch_count += 1
                while batch_count < batch_max and self._quote_dispatch_running:
                    try:
                        nxt = q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        continue
                    n_args, n_kwargs = nxt
                    _process_item(n_args, n_kwargs)
                    batch_count += 1
                self._quote_dispatch_processed += batch_count
                if self.metrics and (self._quote_dispatch_processed % self._quote_dispatch_metrics_every == 0):
                    try:
                        if hasattr(self.metrics, "shioaji_quote_callback_queue_depth"):
                            self.metrics.shioaji_quote_callback_queue_depth.set(q.qsize())
                    except Exception:
                        pass

        self._quote_dispatch_thread = threading.Thread(
            target=_worker,
            name="shioaji-quote-dispatch",
            daemon=True,
        )
        self._quote_dispatch_thread.start()

    def _stop_quote_dispatch_worker(self, join_timeout_s: float = 1.0) -> None:
        if not self._quote_dispatch_running:
            return
        self._quote_dispatch_running = False
        q = self._quote_dispatch_queue
        if q is not None:
            try:
                q.put_nowait(None)
            except Exception:
                pass
        t = self._quote_dispatch_thread
        if t and t.is_alive():
            t.join(timeout=max(0.0, float(join_timeout_s)))
        self._quote_dispatch_thread = None
        self._quote_dispatch_queue = None

    def _refresh_quote_routes(self) -> None:
        codes: list[str] = []
        for sym in self.symbols:
            if isinstance(sym, dict):
                code = sym.get("code")
            else:
                code = None
            if code:
                codes.append(str(code))
        subscribed_codes = getattr(self, "subscribed_codes", None)
        if subscribed_codes:
            codes.extend(str(c) for c in subscribed_codes)
        _registry_rebind_codes(self, codes)

    def _load_config(self):
        with open(self.config_path, "r") as f:
            data = yaml.safe_load(f) or {}
            self.symbols = data.get("symbols", [])
            if len(self.symbols) > self.MAX_SUBSCRIPTIONS:
                if os.getenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS") == "1":
                    logger.warning(
                        "Symbol list exceeds limit",
                        limit=self.MAX_SUBSCRIPTIONS,
                        count=len(self.symbols),
                    )
                    self.symbols = self.symbols[: self.MAX_SUBSCRIPTIONS]
                else:
                    raise ValueError(f"Symbol list exceeds limit ({len(self.symbols)} > {self.MAX_SUBSCRIPTIONS}).")

        # Build map
        self.code_exchange_map = {s["code"]: s["exchange"] for s in self.symbols if s.get("code") and s.get("exchange")}
        self._refresh_quote_routes()

    def login(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        person_id: str | None = None,
        ca_passwd: str | None = None,
        contracts_cb=None,
    ):
        """Login to Shioaji broker. Delegates to SessionRuntime.login_with_retry()."""
        return self._session_runtime.login_with_retry(
            api_key=api_key,
            secret_key=secret_key,
            person_id=person_id,
            ca_passwd=ca_passwd,
            contracts_cb=contracts_cb,
        )

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]):
        """Delegates to SubscriptionManager.set_execution_callbacks()."""
        self._subscriptions().set_execution_callbacks(on_order=on_order, on_deal=on_deal)

    def _ensure_contracts(self) -> None:
        if not self.api or not hasattr(self.api, "fetch_contracts"):
            return
        try:
            start_ns = time.perf_counter_ns()
            self.api.fetch_contracts(contract_download=True)
            self._record_api_latency("fetch_contracts", start_ns, ok=True)
        except Exception as exc:
            self._record_api_latency("fetch_contracts", start_ns, ok=False)
            logger.warning("Contract fetch failed", error=str(exc))

    def _maybe_activate_ca(self) -> None:
        if not self.api or not self.activate_ca:
            return
        if self.mode == "simulation":
            return
        if not self.ca_path or not self.ca_password:
            logger.warning("CA activation requested but missing ca_path/ca_password")
            return
        try:
            self.api.activate_ca(ca_path=self.ca_path, ca_passwd=self.ca_password)
            self.ca_active = True
            logger.info("CA activated")
        except Exception as exc:
            logger.error("CA activation failed", error=str(exc))

    def _quote_api(self):
        api = self.api
        if not api:
            return None
        quote = getattr(api, "quote", None)
        if quote is None:
            return None
        return quote

    def subscribe_basket(self, cb: Callable[..., Any]):
        """Delegates to SubscriptionManager.subscribe_basket()."""
        self._subscriptions().subscribe_basket(cb)

    def _ensure_callbacks(self, cb: Callable[..., Any]) -> None:
        if not self.api:
            return
        if self._callbacks_registered and self._event_callback_registered:
            return
        self._register_callbacks(cb)
        if not self._callbacks_registered:
            self._start_callback_retry(cb)
        if not self._event_callback_registered:
            self._start_event_callback_retry()

    def _register_callbacks(self, cb: Callable[..., Any]) -> bool:
        if not self.api:
            return False
        with self._callback_register_lock:
            if self._callbacks_registered and self._event_callback_registered:
                return True
            ok_quote = True
            ok_event = True
            try:
                # Use global dispatcher to avoid weak-ref issues with bound methods.
                ok_quote = self._register_quote_callbacks()
            except Exception as e:
                logger.error("Quote callback registration failed", error=str(e))
                ok_quote = False

            ok_event = self._register_event_callback()

            self._callbacks_registered = ok_quote
            self._event_callback_registered = ok_event

            if ok_quote:
                logger.info(
                    "Quote callbacks registered",
                    quote_version=self._quote_version,
                    quote_version_mode=self._quote_version_mode,
                )
            else:
                logger.warning(
                    "Quote callbacks not registered",
                    quote_version=self._quote_version,
                    quote_version_mode=self._quote_version_mode,
                )

            if ok_event:
                logger.info("Quote event callback registered")
            else:
                logger.warning("Quote event callback not registered")

            return ok_quote and ok_event

    def _register_quote_callbacks(self) -> bool:
        """Delegates to QuoteRuntime.register_quote_callbacks()."""
        return self._quotes().register_quote_callbacks()

    def _register_event_callback(self) -> bool:
        quote_api = self._quote_api()
        if quote_api is None:
            logger.warning("Quote API unavailable; event callback registration deferred")
            return False
        try:
            quote_api.set_event_callback(self._event_callback_fn)
            return True
        except Exception as exc:
            self._record_crash_signature(str(exc), context="register_event_callback")
            logger.warning("Failed quote event callback registration", error=str(exc))
            return False

    def _get_quote_version(self):
        if not sj or not hasattr(sj.constant, "QuoteVersion"):
            return None
        if self._quote_version == "v0" and not self._supports_quote_v0():
            if self._supports_quote_v1():
                return sj.constant.QuoteVersion.v1
            return None
        return sj.constant.QuoteVersion.v0 if self._quote_version == "v0" else sj.constant.QuoteVersion.v1

    def _start_session_refresh_thread(self) -> None:
        """Delegates to SessionRuntime.start_session_refresh_thread()."""
        self._session_runtime.start_session_refresh_thread()

    def _do_session_refresh(self) -> bool:
        """Delegates to SessionRuntime.do_session_refresh()."""
        return self._session_runtime.do_session_refresh()

    def _verify_quotes_flowing(self, timeout_s: float | None = None) -> bool:
        """Verify quotes are flowing after refresh (O5).

        Waits for new quote data to arrive within timeout period.

        Args:
            timeout_s: Timeout in seconds (default: HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S)

        Returns:
            True if new quote data received within timeout
        """
        if not self.logged_in or not self.subscribed_count:
            # No subscriptions to verify
            return True

        if timeout_s is None:
            timeout_s = self._session_refresh_verify_timeout_s

        start_ts = self._last_quote_data_ts
        deadline = timebase.now_s() + timeout_s

        logger.debug(
            "Verifying quotes flowing",
            timeout_s=timeout_s,
            subscribed_count=self.subscribed_count,
        )

        while timebase.now_s() < deadline:
            if self._last_quote_data_ts > start_ts:
                logger.debug(
                    "Quotes flowing verified",
                    elapsed_s=round(timebase.now_s() - (deadline - timeout_s), 2),
                )
                return True
            time.sleep(0.5)

        logger.warning(
            "Quote verification timeout",
            timeout_s=timeout_s,
            subscribed_count=self.subscribed_count,
        )
        return False

    def _is_trading_hours(self) -> bool:
        """Return True if currently within TWSE trading hours."""
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.now(calendar._tz)
            return calendar.is_trading_hours(now_dt)
        except Exception:
            # Conservative fallback: weekdays 09:00-13:30 Asia/Taipei.
            now_dt = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            if now_dt.weekday() >= 5:
                return False
            minute = now_dt.hour * 60 + now_dt.minute
            return (9 * 60) <= minute <= (13 * 60 + 30)

    def _allow_quote_recovery(self, reason: str) -> bool:
        return self._quotes().allow_quote_recovery(reason)

    def _is_market_open_grace_period(self) -> bool:
        return self._quotes().is_market_open_grace_period()

    def _start_quote_watchdog(self) -> None:
        """Delegates to QuoteRuntime.start_quote_watchdog()."""
        self._quotes().start_quote_watchdog()

    def _start_callback_retry(self, cb: Callable[..., Any]) -> None:
        self._quotes().start_callback_retry(cb)

    def _start_event_callback_retry(self) -> None:
        self._quotes().start_event_callback_retry()

    def _schedule_force_relogin(self) -> None:
        self._quotes().schedule_force_relogin()

    def _start_forced_relogin(self, reason: str) -> None:
        self._quotes().start_forced_relogin(reason)

    def _note_quote_flap(self, now: float) -> None:
        self._quotes().note_quote_flap(now)

    def _supports_quote_v0(self) -> bool:
        return self._quotes().supports_quote_v0()

    def _supports_quote_v1(self) -> bool:
        return self._quotes().supports_quote_v1()

    def _mark_quote_pending(self, reason: str) -> None:
        self._quotes().mark_quote_pending(reason)

    def _clear_quote_pending(self) -> None:
        self._quotes().clear_quote_pending()

    def _schedule_resubscribe(self, reason: str) -> None:
        self._quotes().schedule_resubscribe(reason)

    def _on_quote_event(self, resp_code: int, event_code: int, info: str, event: str) -> None:
        self._quotes().on_quote_event(resp_code, event_code, info, event)

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if not self.api:
            return False
        now = timebase.now_s()
        cooldown = float(os.getenv("HFT_RECONNECT_COOLDOWN", "30"))
        if not force and now - self._last_reconnect_ts < max(cooldown, self._reconnect_backoff_s):
            return False
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            self._last_reconnect_ts = now
            self._last_reconnect_error = None
            logger.warning("Reconnecting Shioaji", reason=reason, force=force)
            ok_logout, _, err_logout, _ = self._safe_call_with_timeout(
                "logout",
                lambda: self.api.logout(),
                self._reconnect_timeout_s,
            )
            if not ok_logout:
                logger.warning("Logout failed during reconnect", error=str(err_logout))

            self.logged_in = False
            self._callbacks_registered = False
            self._clear_quote_pending()
            self.subscribed_codes = set()
            self.subscribed_count = 0
            self._refresh_quote_routes()

            login_ok = bool(self.login())
            if not login_ok or not self.logged_in:
                self._last_reconnect_error = self._last_login_error or "login_failed"
                if self.metrics:
                    self.metrics.feed_reconnect_total.labels(result="fail").inc()
                self._reconnect_backoff_s = min(self._reconnect_backoff_s * 2.0, self._reconnect_backoff_max_s)
                return False

            subscribe_ok = True
            callback = self.tick_callback
            if callback is not None:
                try:
                    self._ensure_callbacks(callback)
                    if not self._callbacks_registered:
                        subscribe_ok = False
                        self._last_reconnect_error = "callbacks_not_registered"
                    else:
                        ok_sub, _, err_sub, timed_out_sub = self._safe_call_with_timeout(
                            "subscribe_basket",
                            lambda: self.subscribe_basket(callback),
                            self._reconnect_subscribe_timeout_s,
                        )
                        if not ok_sub:
                            subscribe_ok = False
                            self._last_reconnect_error = str(err_sub) if err_sub is not None else "subscribe_failed"
                            if self.metrics and timed_out_sub:
                                try:
                                    self.metrics.feed_reconnect_timeout_total.labels(reason="subscribe").inc()
                                except Exception:
                                    pass
                            logger.error(
                                "Subscribe basket failed after reconnect",
                                timeout=timed_out_sub,
                                error=self._last_reconnect_error,
                            )
                except Exception as exc:
                    subscribe_ok = False
                    self._last_reconnect_error = str(exc)
                    logger.error("Callback/subscribe failed after reconnect login", error=str(exc))

            ok = self.logged_in and subscribe_ok
            if self.metrics:
                self.metrics.feed_reconnect_total.labels(result="ok" if ok else "fail").inc()
            if ok:
                self._reconnect_backoff_s = float(os.getenv("HFT_RECONNECT_BACKOFF_S", "30"))
                return True

            self._reconnect_backoff_s = min(self._reconnect_backoff_s * 2.0, self._reconnect_backoff_max_s)
            return False
        except Exception as exc:
            self._last_reconnect_error = str(exc)
            logger.error("Reconnect failed unexpectedly", reason=reason, error=str(exc))
            if self.metrics:
                self.metrics.feed_reconnect_total.labels(result="exception").inc()
                try:
                    self.metrics.feed_reconnect_exception_total.labels(
                        reason=reason or "unknown",
                        exception_type=type(exc).__name__,
                    ).inc()
                except Exception:
                    pass
            self._reconnect_backoff_s = min(self._reconnect_backoff_s * 2.0, self._reconnect_backoff_max_s)
            return False
        finally:
            self._reconnect_lock.release()

    def _resubscribe_all(self) -> None:
        """Delegates to SubscriptionManager._resubscribe_all()."""
        self._subscriptions()._resubscribe_all()

    def resubscribe(self) -> bool:
        """Delegates to SubscriptionManager.resubscribe()."""
        return self._subscriptions().resubscribe()

    def _subscribe_symbol(self, sym: Dict[str, Any], cb: Callable[..., Any]) -> bool:
        """Delegates to SubscriptionManager._subscribe_symbol()."""
        return self._subscriptions()._subscribe_symbol(sym, cb)

    def _unsubscribe_symbol(self, sym: Dict[str, Any]) -> None:
        """Delegates to SubscriptionManager._unsubscribe_symbol()."""
        self._subscriptions()._unsubscribe_symbol(sym)

    def reload_symbols(self) -> None:
        self._contracts().reload_symbols()

    # --- C2: Failed subscription retry thread ---

    def _start_sub_retry_thread(self, cb: Callable[..., Any]) -> None:
        self._quotes().start_sub_retry_thread(cb)

    # --- C3: Contract cache refresh thread ---

    def _is_contract_cache_stale(self) -> bool:
        return self._contracts().is_contract_cache_stale()

    def _write_contract_refresh_status(self, *, result: str, error: str | None = None) -> None:
        self._contracts().write_refresh_status(result=result, error=error)

    def get_contract_refresh_status(self) -> dict[str, Any]:
        return self._contracts().refresh_status()

    def _refresh_contracts_and_symbols(self) -> None:
        self._contracts().refresh_contracts_and_symbols()

    def _preflight_contracts(self) -> None:
        self._contracts().preflight_contracts()

    def _start_contract_refresh_thread(self) -> None:
        self._contracts().start_contract_refresh_thread()

    def close(self, logout: bool = False) -> None:
        """Best-effort client teardown for tests/services (registry cleanup + worker stop)."""
        self._quote_watchdog_running = False
        self._session_refresh_running = False
        self._callbacks_retrying = False
        self._event_callback_retrying = False
        self._resubscribe_scheduled = False
        self._pending_quote_relogining = False
        self._sub_retry_running = False
        self._contract_refresh_running = False
        self.tick_callback = None
        self._stop_quote_dispatch_worker(join_timeout_s=1.0)
        for t in (
            getattr(self, "_session_refresh_thread", None),
            getattr(self, "_quote_watchdog_thread", None),
            getattr(self, "_contract_refresh_thread", None),
        ):
            if t and t.is_alive():
                t.join(timeout=0.2)
        _registry_unregister(self)
        if logout and self.api:
            try:
                self.api.logout()
            except Exception as exc:
                logger.warning("Logout failed during close", error=str(exc))
        self._release_session_lock()
        for name in (
            "quote_watchdog",
            "callback_retry",
            "event_callback_retry",
            "quote_relogin",
            "force_relogin",
            "session_refresh",
            "sub_retry",
            "contract_refresh",
        ):
            self._set_thread_alive_metric(name, False)
        self._write_contract_refresh_status(result="closed")

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    def validate_symbols(self) -> list[str]:
        return self._contracts().validate_symbols()

    def _wrapped_tick_cb(self, *args, **kwargs):
        """Persistent callback wrapper."""
        try:
            if hasattr(self, "tick_callback") and self.tick_callback:
                self.tick_callback(*args, **kwargs)
        except Exception as e:
            logger.error(f"Callback error: {e}")

    def _get_contract(
        self,
        exchange: str,
        code: str,
        product_type: str | None = None,
        allow_synthetic: bool = False,
    ):
        return self._contracts()._get_contract(
            exchange, code, product_type=product_type, allow_synthetic=allow_synthetic
        )

    def get_exchange(self, code: str) -> str | None:
        return self._contracts().get_exchange(code)

    def get_usage(self):
        return self._accounts().get_usage()

    def get_positions(self) -> List[Any]:
        return self._accounts().get_positions()

    def fetch_snapshots(self):
        return self._accounts().fetch_snapshots()

    # get_positions was defined earlier. Removing duplicate.

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str,
        tif: str,
        custom_field: str | None = None,
        product_type: str | None = None,
        order_cond: str | None = None,
        order_lot: str | None = None,
        oc_type: str | None = None,
        account: Any | None = None,
        price_type: str | None = None,
    ):
        return self._orders().place_order(
            contract_code=contract_code,
            exchange=exchange,
            action=action,
            price=price,
            qty=qty,
            order_type=order_type,
            tif=tif,
            custom_field=custom_field,
            product_type=product_type,
            order_cond=order_cond,
            order_lot=order_lot,
            oc_type=oc_type,
            account=account,
            price_type=price_type,
        )

    def cancel_order(self, trade):
        return self._orders().cancel_order(trade)

    def update_order(self, trade, price: float | None = None, qty: int | None = None):
        return self._orders().update_order(trade=trade, price=price, qty=qty)
