import asyncio
import collections
import os
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any, Dict, TypeAlias, TypeGuard, cast

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.core.rate_limiter import PerSymbolRateLimiter, PerSymbolRateResult, RateLimiter
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.protocol import BrokerOrderCodec
from hft_platform.gateway.dedup import IdempotencyStore
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.order.circuit_breaker import CircuitBreaker, StrategyCircuitBreakerManager
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason, get_dlq
from hft_platform.order.shadow import ShadowOrderSink

logger = get_logger("order_adapter")

_PENDING_SENTINEL = object()
_TERMINAL_BEFORE_REGISTERED = object()
_GUARD_TIMEOUT = object()

# Operations that mutate broker state and must not be retried if the
# timed-out attempt might still succeed in the thread pool.
_MUTATING_OPS: frozenset[str] = frozenset({"place_order", "update_order"})


class _TimeoutCancelled(Exception):
    """Raised inside the thread-pool wrapper when the attempt was cancelled."""


TypedOrderCommandFrame: TypeAlias = tuple[
    str,  # marker: typed_order_cmd_v1
    int,  # cmd_id
    int,  # deadline_ns
    int,  # storm_guard_state
    int,  # created_ns
    Any,  # typed_intent_frame
]


def _is_typed_order_cmd_frame(obj: Any) -> TypeGuard[TypedOrderCommandFrame]:
    return isinstance(obj, tuple) and len(obj) >= 6 and obj[0] == "typed_order_cmd_v1"


def _get_trace_sampler() -> Any | None:
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler

        return get_trace_sampler()
    except ImportError as exc:
        logger.debug("operation_fallback", error=str(exc))
        return None


class OrderAdapter:
    __slots__ = (
        "config_path",
        "order_queue",
        "client",
        "_broker_codec",
        "order_id_map",
        "_order_id_map_lock",
        "_order_id_map_max_size",
        "running",
        "metrics",
        "latency",
        "_metadata",
        "price_codec",
        "live_orders",
        "_live_orders_lock",
        "rate_limiter",
        "circuit_breaker",
        "order_id_resolver",
        "_api_timeout_s",
        "_api_guard_timeout_s",
        "_api_max_inflight",
        "_api_semaphore",
        "_api_queue_max",
        "_api_queue",
        "_api_coalesce_window_s",
        "_api_pending",
        "_api_worker_task",
        "_supports_typed_command_ingress",
        "_trace_sampler",
        "_dlq",
        "per_symbol_rate_limiter",
        "strategy_cb_mgr",
        "shadow_sink",
        "_pending_order_keys",
        "_deferred_terminals",
        "_cmd_created_ns_map",
        "_cmd_tca_map",
        "_cmd_map_max_size",
        "_mid_price_fn",
        "_storm_guard",
        "_dedup_store",
        "_phantom_order_keys",
        "_phantom_order_max",
        "_audit_writer",
        "_rejection_sink",
        "__dict__",  # needed for test monkey-patching
    )

    def __init__(
        self,
        config_path: str,
        order_queue: asyncio.Queue[OrderCommand],
        broker_client: Any,
        order_id_map: Dict[str, str] | None = None,
        broker_codec: BrokerOrderCodec | None = None,
        cmd_created_ns_map: Dict[str, int] | None = None,
        cmd_tca_map: Dict[str, tuple[int, int]] | None = None,
        mid_price_fn: Callable[[str], int] | None = None,
    ) -> None:
        self.config_path = config_path
        self.order_queue = order_queue
        self.client = broker_client
        self._broker_codec = broker_codec
        # Map broker order IDs -> order_key ("strategy_id:intent_id")
        # Protected by _order_id_map_lock for concurrent access
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self._order_id_map_lock = asyncio.Lock()
        # Map order_key -> cmd.created_ns for e2e latency tracking (SLO-2)
        # Shared with ExecutionRouter for fill-side lookup
        self._cmd_created_ns_map: Dict[str, int] = cmd_created_ns_map if cmd_created_ns_map is not None else {}
        # TCA price map: order_key -> (decision_price, arrival_price) for fill enrichment
        self._cmd_tca_map: Dict[str, tuple[int, int]] = cmd_tca_map if cmd_tca_map is not None else {}
        self._mid_price_fn: Callable[[str], int] | None = mid_price_fn
        self._storm_guard: Any = None  # Set post-init to close TOCTOU gap (M1)
        self._order_id_map_max_size = int(os.getenv("HFT_ORDER_ID_MAP_MAX_SIZE", "10000"))
        self._order_id_map_persist_path: str = os.getenv(
            "HFT_ORDER_ID_MAP_PERSIST_PATH", ".state/order_id_map.jsonl"
        )
        self._load_order_id_map()
        self._cmd_map_max_size = int(os.getenv("HFT_CMD_MAP_MAX_SIZE", "10000"))
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._metadata: SymbolMetadata = SymbolMetadata()
        self.price_codec: PriceCodec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

        # State - Protected by _live_orders_lock for concurrent access
        self.live_orders: Dict[str, Any] = {}  # Map "strategy_id:intent_id" -> Trade Object or Status dict
        self._live_orders_lock = asyncio.Lock()
        self._pending_order_keys: set[str] = set()
        # Bounded deque: auto-evicts oldest entries when full (OOM protection)
        self._deferred_terminals: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=256)

        # Helpers
        self.rate_limiter = RateLimiter(soft_cap=180, hard_cap=250, window_s=10)
        self.circuit_breaker = CircuitBreaker(threshold=5, timeout_s=60)
        self.order_id_resolver = OrderIdResolver(self.order_id_map)
        self._api_timeout_s = float(os.getenv("HFT_API_TIMEOUT_S", "3.0"))  # precision-time
        self._api_guard_timeout_s = float(os.getenv("HFT_API_GUARD_TIMEOUT_S", "0.005"))  # precision-time
        self._api_max_inflight = int(os.getenv("HFT_API_MAX_INFLIGHT", "16"))
        self._api_semaphore = asyncio.Semaphore(self._api_max_inflight)
        self._api_queue_max = int(os.getenv("HFT_API_QUEUE_MAX", "1024"))
        self._api_queue: asyncio.Queue[OrderCommand | TypedOrderCommandFrame] = asyncio.Queue(
            maxsize=self._api_queue_max
        )
        self._api_coalesce_window_s = float(os.getenv("HFT_API_COALESCE_WINDOW_S", "0.005"))  # precision-time
        self._api_pending: dict[tuple, OrderCommand] = {}
        self._api_worker_task: asyncio.Task | None = None
        self._supports_typed_command_ingress = True
        self._trace_sampler = _get_trace_sampler()

        # Dead Letter Queue for rejected orders
        self._dlq: DeadLetterQueue = get_dlq()

        # Per-symbol rate limiter, per-strategy circuit breaker, shadow mode
        self.per_symbol_rate_limiter = PerSymbolRateLimiter()
        self.strategy_cb_mgr = StrategyCircuitBreakerManager()
        self.shadow_sink = ShadowOrderSink()
        self.platform_degrade_controller = get_shared_platform_degrade_controller(metrics=self.metrics)
        self.position_store: Any = None

        # Idempotency dedup for non-gateway path (avoid double-dedup when Gateway is active)
        _gateway_on = os.getenv("HFT_GATEWAY_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        self._dedup_store: IdempotencyStore | None = None if _gateway_on else IdempotencyStore(persist_enabled=False)

        # Phantom order tracking: timed-out mutating calls that may have succeeded at broker
        # R2-03: Bounded dict with TTL eviction (Architecture Governance Rule 12)
        self._phantom_order_keys: dict[str, float] = {}  # key -> monotonic timestamp
        self._phantom_order_max: int = 1000

        # Audit writer for order lifecycle logging (optional, injected post-init)
        self._audit_writer: Any = None
        # Rejection feedback sink for dispatch failures (optional, injected post-init)
        self._rejection_sink: asyncio.Queue | None = None

        self.load_config()

    @property
    def metadata(self) -> SymbolMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, value: SymbolMetadata) -> None:
        self._metadata = value
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

    def set_audit_writer(self, writer: Any) -> None:
        """Inject audit writer for order lifecycle logging."""
        self._audit_writer = writer

    def set_rejection_sink(self, sink: asyncio.Queue) -> None:
        """Inject rejection feedback queue so strategies learn about dispatch failures."""
        self._rejection_sink = sink

    def set_storm_guard(self, storm_guard: Any) -> None:
        """Inject StormGuard reference for live HALT checks (M1 gap closure)."""
        self._storm_guard = storm_guard

    def _send_dispatch_rejection(self, intent: OrderIntent, reason_code: str) -> None:
        """Non-blocking rejection feedback for dispatch failures."""
        if self._rejection_sink is None:
            return
        try:
            from hft_platform.contracts.strategy import RiskFeedback

            self._rejection_sink.put_nowait(
                RiskFeedback(
                    intent_id=intent.intent_id if hasattr(intent, "intent_id") else 0,
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    reason_code=reason_code,
                    timestamp_ns=timebase.now_ns(),
                    side=getattr(intent, "side", None),
                )
            )
        except asyncio.QueueFull:
            self.metrics.rejection_sink_overflow_total.inc()
        except Exception:
            pass  # feedback must never crash order path

    def _audit_log_order(self, order_data: dict) -> None:
        """Non-blocking audit log for order lifecycle events. Skips silently if no writer."""
        if self._audit_writer is not None:
            try:
                self._audit_writer.log_order(order_data)
            except Exception:  # noqa: BLE001
                pass  # audit must never block or crash order dispatch

    async def invalidate_live_orders(self, reason: str = "reconnect") -> int:
        """Mark all live orders as stale after broker session reset.

        After a broker reconnect, all pending orders are invalidated at the
        broker side. This clears the local tracking to prevent phantom entries.
        """
        async with self._live_orders_lock:
            count = len(self.live_orders)
            if count > 0:
                logger.warning(
                    "invalidating_live_orders_after_reconnect",
                    count=count,
                    reason=reason,
                    order_keys=list(self.live_orders.keys())[:10],
                )
                self.live_orders.clear()
                self._pending_order_keys.clear()
            return count

    async def _run_blocking_call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run a synchronous broker call off-loop without using the loop's default executor.

        A daemon thread avoids pytest-asyncio teardown hangs when timed-out broker
        calls are still unwinding in the background.
        """
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future[Any] = loop.create_future()

        def _set_result(value: Any) -> None:
            if not result_future.done():
                result_future.set_result(value)

        def _set_exception(exc: BaseException) -> None:
            if not result_future.done():
                result_future.set_exception(exc)

        def _worker() -> None:
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001
                try:
                    loop.call_soon_threadsafe(_set_exception, exc)
                except RuntimeError:
                    return
            else:
                try:
                    loop.call_soon_threadsafe(_set_result, result)
                except RuntimeError:
                    return

        threading.Thread(target=_worker, name="order-adapter-call", daemon=True).start()
        return await result_future

    def get_phantom_candidates(self) -> frozenset[str]:
        """Return a frozen copy of phantom order keys for reconciliation."""
        return frozenset(self._phantom_order_keys.keys())

    def clear_phantom_candidate(self, key: str) -> None:
        """Remove a phantom order key after reconciliation confirms resolution."""
        self._phantom_order_keys.pop(key, None)

    def load_config(self) -> None:
        with open(self.config_path, "r") as f:
            cfg = yaml.safe_load(f) or {}
            rate_cfg = cfg.get("rate_limits", {})
            self.rate_limiter.update(
                soft_cap=rate_cfg.get("shioaji_soft_cap"),
                hard_cap=rate_cfg.get("shioaji_hard_cap"),
                window_s=rate_cfg.get("window_seconds"),
            )
            cb_cfg = cfg.get("circuit_breaker", {})
            if "threshold" in cb_cfg:
                self.circuit_breaker.threshold = cb_cfg.get("threshold", self.circuit_breaker.threshold)
            if "timeout_seconds" in cb_cfg:
                self.circuit_breaker.timeout_s = cb_cfg.get("timeout_seconds", self.circuit_breaker.timeout_s)

    def _intent_to_command(self, intent: OrderIntent) -> OrderCommand:
        mono_ns = time.monotonic_ns()  # monotonic clock for deadline comparison
        created_ns = timebase.now_ns()  # epoch wall-clock for TCA / recording
        ttl_ns = int(intent.ttl_ns) if intent.ttl_ns > 0 else 5_000_000_000
        # Stamp HALT only for legitimate HaltFlattener intents (FORCE_FLAT + halt_flatten reason)
        storm_guard_state = (
            StormGuardState.HALT
            if intent.reason == "halt_flatten" and intent.intent_type == IntentType.FORCE_FLAT
            else StormGuardState.NORMAL
        )
        # TCA: arrival_price = current LOB mid-price (not decision_price)
        if self._mid_price_fn is not None:
            try:
                arrival = self._mid_price_fn(intent.symbol)
            except Exception:  # noqa: BLE001
                arrival = int(intent.decision_price)
        else:
            arrival = int(intent.decision_price)
        # Fall back to decision_price if LOB returned 0 (book not ready)
        if arrival <= 0:
            arrival = int(intent.decision_price)
        return OrderCommand(
            cmd_id=int(intent.intent_id),
            intent=intent,
            deadline_ns=mono_ns + ttl_ns,
            storm_guard_state=storm_guard_state,
            created_ns=created_ns,
            decision_price=int(intent.decision_price),
            arrival_price=arrival,
        )

    async def submit_intent(self, intent: OrderIntent) -> None:
        """Public async submission API for platform-owned flatteners.

        Only CANCEL and FORCE_FLAT intents are allowed — these are safety-critical
        operations that intentionally bypass risk evaluation.
        """
        if intent.intent_type not in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            raise ValueError(f"submit_intent only accepts CANCEL/FORCE_FLAT, got {intent.intent_type!r}")
        await self.execute(self._intent_to_command(intent))

    async def run(self) -> None:
        self.running = True
        logger.info("OrderAdapter started")
        self._api_worker_task = asyncio.create_task(self._api_worker())

        try:
            while self.running:
                # Allow exceptions to crash the task (Supervisor will handle)
                try:
                    cmd: OrderCommand = await asyncio.wait_for(
                        self.order_queue.get(),
                        timeout=1.0,  # Check running flag periodically
                    )
                except asyncio.TimeoutError:
                    continue

                # Check Deadline
                if time.monotonic_ns() > cmd.deadline_ns:
                    logger.warning("Order Timeout (Pre-dispatch)", cmd_id=cmd.cmd_id)
                    try:
                        self.metrics.order_deadline_expired_total.inc()
                    except Exception:  # noqa: BLE001
                        pass
                    self.order_queue.task_done()
                    continue

                await self.execute(cmd)
                self.order_queue.task_done()
        finally:
            # Ensure worker task is properly cancelled and awaited
            if self._api_worker_task:
                self._api_worker_task.cancel()
                try:
                    await asyncio.wait_for(self._api_worker_task, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                self._api_worker_task = None
            logger.info("OrderAdapter stopped")

    def check_rate_limit(self) -> bool:
        """Sliding window check."""
        return self.rate_limiter.check()

    def _load_order_id_map(self) -> None:
        """Load order_id_map from disk on startup (restart-safe strategy resolution)."""
        path = self._order_id_map_persist_path
        if not os.path.exists(path):
            return
        try:
            import orjson

            loaded = 0
            with open(path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = orjson.loads(raw)
                        if isinstance(obj, dict) and "k" in obj and "v" in obj:
                            self.order_id_map[str(obj["k"])] = str(obj["v"])
                            loaded += 1
                    except Exception:
                        continue
            # Enforce max size
            while len(self.order_id_map) > self._order_id_map_max_size:
                first_key = next(iter(self.order_id_map))
                del self.order_id_map[first_key]
            logger.info("order_id_map_loaded", count=loaded, path=path)
        except Exception as exc:
            logger.warning("order_id_map_load_failed", error=str(exc), path=path)

    def persist_order_id_map(self) -> None:
        """Persist order_id_map to disk atomically (temp+fsync+rename).

        Called during graceful shutdown. Safe to call from thread pool.
        """
        path = self._order_id_map_persist_path
        # Snapshot under CPython GIL atomicity
        snapshot = list(self.order_id_map.items())
        try:
            import orjson

            persist_dir = os.path.dirname(path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    for k, v in snapshot:
                        f.write(orjson.dumps({"k": k, "v": v}) + b"\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)
                raise
            logger.info("order_id_map_persisted", count=len(snapshot), path=path)
        except Exception as exc:
            logger.warning("order_id_map_persist_failed", error=str(exc), path=path)

    async def drain_and_cancel(self, timeout_s: float = 5.0) -> int:  # precision-ok
        """Drain pending orders and cancel all live orders."""
        cancelled = 0
        while not self.order_queue.empty():
            try:
                self.order_queue.get_nowait()
                self.order_queue.task_done()
            except asyncio.QueueEmpty:
                break
        # Also drain _api_queue (gateway mode dispatches directly to it)
        # Safety-order filter: preserve CANCEL/FORCE_FLAT + halt-exempt strategies
        _api_drained = 0
        _api_safety: list = []
        while not self._api_queue.empty():
            try:
                cmd = self._api_queue.get_nowait()
                self._api_queue.task_done()
                _intent = getattr(cmd, "intent", None)
                _itype = getattr(_intent, "intent_type", None) if _intent else None
                _sid = getattr(_intent, "strategy_id", None) if _intent else None
                _is_safety = _itype in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                _is_exempt = bool(_sid) and self._is_strategy_halt_exempt(_sid)
                if _is_safety or _is_exempt:
                    _api_safety.append(cmd)
                else:
                    _api_drained += 1
            except asyncio.QueueEmpty:
                break
        # Dispatch safety commands directly via _dispatch_to_api (bypasses stopped _api_worker)
        for cmd in _api_safety:
            try:
                _task = asyncio.create_task(self._dispatch_to_api(cmd))
                logger.info(
                    "halt_drain_api_safety_cmd_dispatched",
                    cmd_id=getattr(cmd, "cmd_id", "?"),
                    intent_type=str(getattr(getattr(cmd, "intent", None), "intent_type", "?")),
                )
            except Exception as exc:
                logger.critical(
                    "halt_drain_api_safety_cmd_dispatch_failed",
                    cmd_id=getattr(cmd, "cmd_id", "?"),
                    error=str(exc),
                )
        if _api_drained > 0:
            logger.warning(
                "Drained commands from _api_queue during HALT",
                count=_api_drained,
                safety_preserved=len(_api_safety),
            )
        async with self._live_orders_lock:
            live_keys = list(self.live_orders.keys())
        for key in live_keys:
            async with self._live_orders_lock:
                trade = self.live_orders.get(key)
            if trade is None or trade is _PENDING_SENTINEL or trade is _TERMINAL_BEFORE_REGISTERED:
                continue
            try:
                await asyncio.wait_for(self._run_blocking_call(self.client.cancel_order, trade), timeout=timeout_s)
                cancelled += 1
                logger.info("Drained live order", order_key=key)
            except asyncio.TimeoutError:
                logger.warning("Cancel timeout during drain", order_key=key)
            except Exception as exc:
                logger.warning("Cancel failed during drain", order_key=key, error=str(exc))
        logger.info("Order drain complete", cancelled=cancelled, total=len(live_keys))
        return cancelled

    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        async with self._live_orders_lock:
            order_key = self.order_id_resolver.resolve_order_key(strategy_id, order_id, self.live_orders)
            entry = self.live_orders.get(order_key)

            if entry is not None and entry is not _PENDING_SENTINEL:
                # Normal path — order is registered, clean up
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]
                # Clean up e2e latency tracking entry (SLO-2)
                self._cmd_created_ns_map.pop(order_key, None)
                # Clean up TCA price tracking entry
                self._cmd_tca_map.pop(order_key, None)
                return

            # Check if any order for this strategy is in-flight
            has_pending = any(k.startswith(f"{strategy_id}:") for k in self._pending_order_keys)
            if has_pending:
                if len(self._deferred_terminals) == self._deferred_terminals.maxlen:
                    self.metrics.deferred_terminal_overflow_total.inc()
                    logger.error(
                        "deferred_terminal_overflow",
                        strategy_id=strategy_id,
                        broker_order_id=order_id,
                        msg="Oldest deferred terminal silently evicted — stale live_orders risk",
                    )
                self._deferred_terminals.append((strategy_id, order_id, time.monotonic()))
                self.metrics.terminal_before_registration_total.inc()
                logger.warning(
                    "terminal_before_registration",
                    strategy_id=strategy_id,
                    broker_order_id=order_id,
                )
                return

            # No pending orders — genuine orphan or already cleaned up
            if order_key in self.live_orders:
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]

        # Also clean up rate limit window if needed? No, rate limit is distinct.

    async def _register_broker_ids(self, order_key: str, trade: Any) -> None:
        """Register broker IDs to order_key mapping with lock protection."""
        ids = set()
        id_keys = ("seq_no", "seqno", "ord_no", "ordno", "order_id", "id")

        if isinstance(trade, dict):
            for key in id_keys:
                val = trade.get(key)
                if val:
                    ids.add(val)

            order = trade.get("order")
            if isinstance(order, dict):
                for key in id_keys:
                    val = order.get(key)
                    if val:
                        ids.add(val)
            status = trade.get("status")
            if isinstance(status, dict):
                for key in ("id", "seq_no", "seqno", "ord_no", "ordno"):
                    val = status.get(key)
                    if val:
                        ids.add(val)
        else:
            for attr in id_keys:
                val = getattr(trade, attr, None)
                if val:
                    ids.add(val)

            order = getattr(trade, "order", None)
            if order:
                for attr in id_keys:
                    val = getattr(order, attr, None)
                    if val:
                        ids.add(val)
            status = getattr(trade, "status", None)
            if status:
                for attr in ("id", "seq_no", "seqno", "ord_no", "ordno"):
                    val = getattr(status, attr, None)
                    if val:
                        ids.add(val)

        async with self._order_id_map_lock:
            # Evict oldest entries if at limit — skip entries whose order_key
            # is still in live_orders to prevent orphaning active fills (M6).
            if len(self.order_id_map) >= self._order_id_map_max_size:
                evict_target = max(1, len(self.order_id_map) // 10)
                evicted = 0
                for k in list(self.order_id_map.keys()):
                    if evicted >= evict_target:
                        break
                    mapped_key = self.order_id_map[k]
                    if mapped_key not in self.live_orders:
                        del self.order_id_map[k]
                        evicted += 1
                logger.info("Evicted stale order IDs", count=evicted, remaining=len(self.order_id_map))

            for oid in ids:
                self.order_id_map[str(oid)] = order_key

    async def _drain_deferred_terminals(self, order_key: str, trade: Any) -> None:
        """Re-process deferred terminal callbacks now that broker IDs are registered."""
        now = time.monotonic()
        remaining: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=256)
        async with self._live_orders_lock:
            for sid, oid, ts in self._deferred_terminals:
                if now - ts >= 30.0:
                    logger.error(
                        "deferred_terminal_expired",
                        strategy_id=sid,
                        broker_order_id=oid,
                        age_s=round(now - ts, 1),
                    )
                    self.metrics.deferred_terminal_expired_total.inc()
                    continue
                resolved = self.order_id_resolver.resolve_order_key(sid, oid, self.live_orders)
                if resolved in self.live_orders:
                    del self.live_orders[resolved]
                    logger.info(
                        "deferred_terminal_cleanup",
                        key=resolved,
                        broker_order_id=oid,
                        defer_age_ms=int((now - ts) * 1000),
                    )
                else:
                    remaining.append((sid, oid, ts))
            self._deferred_terminals = remaining

    async def execute(self, cmd: OrderCommand) -> None:
        intent = cmd.intent

        # StormGuard HALT check — both stamped and live state (closes TOCTOU gap)
        _is_halt = cmd.storm_guard_state == StormGuardState.HALT
        if not _is_halt and self._storm_guard is not None:
            _is_halt = getattr(self._storm_guard, "state", None) == StormGuardState.HALT
        # Constitution: HALT blocks new orders but allows CANCEL/FORCE_FLAT through.
        # halt_flatten requires conjunction with FORCE_FLAT to prevent spoofing.
        _halt_exempt = (
            intent.intent_type == IntentType.CANCEL
            or intent.intent_type == IntentType.FORCE_FLAT
            or self._is_strategy_halt_exempt(intent.strategy_id)
        )
        if _is_halt and not _halt_exempt:
            await self._add_to_dlq(
                intent,
                RejectionReason.VALIDATION_ERROR,
                "StormGuard HALT",
                halt_exempt_blocked=self._is_strategy_halt_exempt(intent.strategy_id),
            )
            return

        # Safety-critical intents (CANCEL/FORCE_FLAT) bypass rate limiters and
        # circuit breakers — they must never be blocked during HALT evacuation.
        _safety_exempt = intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT)

        # Idempotency dedup check (D-01) — skip for safety-exempt and empty keys
        if not _safety_exempt and self._dedup_store is not None and intent.idempotency_key:
            existing = self._dedup_store.check_or_reserve(intent.idempotency_key)
            if existing is not None:
                logger.warning(
                    "duplicate_idempotency_key",
                    key=intent.idempotency_key,
                    strategy_id=intent.strategy_id,
                    symbol=intent.symbol,
                    prior_approved=existing.approved,
                )
                self.metrics.order_reject_total.inc()
                await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Duplicate idempotency_key")
                return

        # R2-01: Track whether we reserved a dedup slot so we can release it on
        # unexpected exception (prevents orphaned slots blocking re-submission).
        _dedup_key = (
            intent.idempotency_key
            if (not _safety_exempt and self._dedup_store is not None and intent.idempotency_key)
            else ""
        )
        try:
            # Per-symbol rate limit check (WU-06)
            if not _safety_exempt:
                ps_result = self.per_symbol_rate_limiter.check(intent.symbol)
                if ps_result == PerSymbolRateResult.HARD:
                    await self._add_to_dlq(intent, RejectionReason.RATE_LIMIT, "Per-symbol hard rate limit")
                    return

            # Per-strategy circuit breaker check (WU-09)
            if not _safety_exempt and self.strategy_cb_mgr.is_open(intent.strategy_id):
                await self._add_to_dlq(intent, RejectionReason.CIRCUIT_BREAKER, "Per-strategy circuit breaker open")
                return

            # Circuit Breaker Check
            if not _safety_exempt and self.circuit_breaker.is_open():
                logger.warning("Circuit Breaker Open - Rejecting", cmd_id=cmd.cmd_id)
                await self._add_to_dlq(intent, RejectionReason.CIRCUIT_BREAKER, "Circuit breaker open")
                return

            if not _safety_exempt and not self.check_rate_limit():
                # Rate limit exceeded
                await self._add_to_dlq(intent, RejectionReason.RATE_LIMIT, "Rate limit exceeded")
                return

            if not self._platform_degrade_allows(intent):
                self.metrics.order_reject_total.inc()
                self._emit_trace("order_reject", intent, {"reason": "platform_reduce_only", "cmd_id": int(cmd.cmd_id)})
                await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Platform is in reduce-only mode")
                return
            self._reserve_platform_reduce_only_close(intent)

            # Shadow mode intercept (WU-10)
            if self.shadow_sink.enabled:
                self.shadow_sink.intercept(intent)
                self.per_symbol_rate_limiter.record(intent.symbol)
                return

            if not self._validate_client(intent):
                self.metrics.order_reject_total.inc()
                self.circuit_breaker.record_failure()
                self._update_cb_metric()
                self.strategy_cb_mgr.record_failure(intent.strategy_id)
                await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Client validation failed")
                return

            if not self.running:
                if cmd.created_ns:
                    self._record_queue_latency(cmd)
                try:
                    ok = await self._dispatch_to_api(cmd)
                    if ok:
                        self._dedup_commit(intent.idempotency_key, True, "OK", cmd.cmd_id)
                except Exception:
                    self._dedup_commit(intent.idempotency_key, False, "dispatch_error", cmd.cmd_id)
                return
            await self._enqueue_api(cmd)
            # Dedup remains in 'reserved' state until _api_worker resolves it
            # (commit on dispatch success, release on failure/skip).
        except Exception:
            if _dedup_key:
                self._dedup_commit(_dedup_key, False, "internal_error", 0)
            raise

    def _dedup_commit(self, key: str, approved: bool, reason_code: str, cmd_id: int) -> None:
        """Commit dedup result if store is active and key is non-empty."""
        if self._dedup_store is not None and key:
            self._dedup_store.commit(key, approved, reason_code, cmd_id)

    def _dedup_release(self, key: str) -> None:
        """Release a dedup slot so the same key can be resubmitted on dispatch failure."""
        if self._dedup_store is not None and key:
            self._dedup_store.release(key)

    def _is_strategy_halt_exempt(self, strategy_id: str) -> bool:
        """Check if a strategy is halt-exempt via StormGuard."""
        sg = self._storm_guard
        if sg is None:
            return False
        is_exempt = getattr(sg, "is_halt_exempt", None)
        if callable(is_exempt):
            return is_exempt(strategy_id)
        return strategy_id in getattr(sg, "_halt_exempt_strategies", frozenset())

    async def _add_to_dlq(
        self,
        intent: OrderIntent,
        reason: RejectionReason,
        error_message: str,
        halt_exempt_blocked: bool = False,
    ) -> None:
        """Add a rejected order to the dead letter queue."""
        self._dedup_commit(intent.idempotency_key, False, error_message, 0)
        try:
            await self._dlq.add(
                order_id=str(intent.intent_id),
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                side=str(intent.side.name if hasattr(intent.side, "name") else intent.side),
                price=intent.price,
                qty=intent.qty,
                reason=reason,
                error_message=error_message,
                intent_type=str(intent.intent_type.name if hasattr(intent.intent_type, "name") else intent.intent_type),
                trace_id=getattr(intent, "trace_id", ""),
                halt_exempt_blocked=halt_exempt_blocked,
            )
        except (TypeError, ValueError, OSError) as e:
            logger.error("Failed to add to DLQ", error=str(e))

    def _validate_client(self, intent: OrderIntent) -> bool:
        if intent.intent_type in (IntentType.NEW, IntentType.FORCE_FLAT):
            return hasattr(self.client, "place_order") and hasattr(self.client, "get_exchange")
        if intent.intent_type == IntentType.CANCEL:
            return hasattr(self.client, "cancel_order")
        if intent.intent_type == IntentType.AMEND:
            return hasattr(self.client, "update_order")
        return True

    def _platform_degrade_allows(self, intent: OrderIntent) -> bool:
        # FORCE_FLAT and CANCEL are always allowed regardless of platform degrade state
        if intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT):
            return True
        controller = getattr(self, "platform_degrade_controller", None)
        if controller is None:
            return True
        if getattr(controller, "reduce_only_active", False) and intent.intent_type == IntentType.NEW:
            return self._platform_reduce_only_new_order_allowed(intent)
        return controller.allow_intent(
            intent_type=intent.intent_type,
            opens_risk=self._intent_opens_risk(intent),
        )

    def _platform_reduce_only_new_order_allowed(self, intent: OrderIntent) -> bool:
        local_capacity = self._available_close_capacity(intent.symbol, intent.side)
        controller = getattr(self, "platform_degrade_controller", None)
        reference_capacity = 0
        if controller is not None:
            reference_net = controller.reference_available_net_qty(intent.symbol)
            reference_capacity = self._close_capacity(reference_net or 0, intent.side)
        return int(intent.qty) <= max(local_capacity, reference_capacity)

    def _intent_opens_risk(self, intent: OrderIntent) -> bool:
        if intent.intent_type != IntentType.NEW:
            return False

        net_qty = self._reference_net_position_for_intent(intent)
        if net_qty == 0:
            return True
        if net_qty > 0:
            return not (intent.side == Side.SELL and intent.qty <= net_qty)
        return not (intent.side == Side.BUY and intent.qty <= abs(net_qty))

    def _reference_net_position_for_intent(self, intent: OrderIntent) -> int:
        controller = getattr(self, "platform_degrade_controller", None)
        if controller is not None:
            ref_qty = controller.reference_available_net_qty(intent.symbol)
            if ref_qty is not None:
                return int(ref_qty)
        return self._platform_net_position_for_symbol(intent.symbol)

    def _reserve_platform_reduce_only_close(self, intent: OrderIntent) -> None:
        controller = getattr(self, "platform_degrade_controller", None)
        if controller is None or not getattr(controller, "reduce_only_active", False):
            return
        if intent.intent_type != IntentType.NEW:
            return
        reference_net = controller.reference_available_net_qty(intent.symbol)
        if self._close_capacity(reference_net or 0, intent.side) <= 0:
            return
        controller.reserve_reference_close(symbol=intent.symbol, qty=int(intent.qty))

    def _available_close_capacity(self, symbol: str, side: Side) -> int:
        local_net = self._platform_net_position_for_symbol(symbol)
        pending_close = self._pending_close_qty(symbol, side)
        return max(0, self._close_capacity(local_net, side) - pending_close)

    @staticmethod
    def _close_capacity(net_qty: int, side: Side) -> int:
        if net_qty > 0 and side == Side.SELL:
            return int(net_qty)
        if net_qty < 0 and side == Side.BUY:
            return int(abs(net_qty))
        return 0

    def _pending_close_qty(self, symbol: str, side: Side) -> int:
        """Count pending close qty for symbol/side.

        Safe to iterate live_orders directly: asyncio is single-threaded and
        this method contains no await points, so no other coroutine can mutate
        live_orders during execution.
        """
        pending_qty = 0
        for trade in self.live_orders.values():
            if trade is _PENDING_SENTINEL or trade is _TERMINAL_BEFORE_REGISTERED:
                continue
            if self._live_order_symbol(trade) != symbol:
                continue
            if self._live_order_side(trade) != side:
                continue
            pending_qty += self._live_order_qty(trade)
        return pending_qty

    @staticmethod
    def _live_order_symbol(trade: Any) -> str:
        if isinstance(trade, dict):
            return str(trade.get("contract_code") or trade.get("symbol") or "")
        return str(getattr(trade, "contract_code", "") or getattr(trade, "symbol", ""))

    @staticmethod
    def _live_order_side(trade: Any) -> Side | None:
        raw = ""
        if isinstance(trade, dict):
            raw = str(trade.get("action") or trade.get("side") or "").upper()
        else:
            raw = str(getattr(trade, "action", "") or getattr(trade, "side", "")).upper()
        if raw in {"SELL", "ACTION.SELL", "1"}:
            return Side.SELL
        if raw in {"BUY", "ACTION.BUY", "0"}:
            return Side.BUY
        return None

    @staticmethod
    def _live_order_qty(trade: Any) -> int:
        if isinstance(trade, dict):
            return int(trade.get("qty", 0) or 0)
        return int(getattr(trade, "qty", 0) or 0)

    def _platform_net_position_for_symbol(self, symbol: str) -> int:
        position_store = getattr(self, "position_store", None)
        if position_store is None:
            return 0
        positions = getattr(position_store, "positions", {})
        net_qty = 0
        for pos in positions.values():
            if getattr(pos, "symbol", None) != symbol:
                continue
            net_qty += int(getattr(pos, "net_qty", 0))
        return net_qty

    def _platform_reference_price_for_symbol(self, symbol: str) -> int:
        position_store = getattr(self, "position_store", None)
        if position_store is None:
            return 0
        positions = getattr(position_store, "positions", {})
        ref_price = 0
        for pos in positions.values():
            if getattr(pos, "symbol", None) != symbol:
                continue
            ref_price = max(ref_price, int(getattr(pos, "avg_price_scaled", 0) or 0))
        return ref_price

    def _force_flat_price(self, symbol: str, close_side: Side, requested_price: int) -> int:
        if requested_price > 0:
            return int(requested_price)

        scale = int(self.metadata.price_scale(symbol))
        ref_price = self._platform_reference_price_for_symbol(symbol)
        if ref_price <= 0:
            ref_price = scale * 1000

        if close_side == Side.BUY:
            # Cap at +7% above reference to stay within exchange daily price limits (±10%).
            # Previous 2x multiplier exceeded TAIFEX limits, causing HALT evacuation failure.
            return max(ref_price + scale, int(ref_price * 1.07))
        return max(scale, int(ref_price * 0.93))

    async def _dispatch_to_api(self, cmd: OrderCommand) -> bool:
        intent = cmd.intent
        self._emit_trace(
            "order_dispatch_start", intent, {"cmd_id": int(cmd.cmd_id), "intent_type": int(intent.intent_type)}
        )
        try:
            order_key = f"{intent.strategy_id}:{intent.intent_id}"

            # Record dispatch timestamp for e2e latency tracking (SLO-2)
            if cmd.created_ns > 0:
                self._cmd_created_ns_map[order_key] = cmd.created_ns
            # TCA: store decision/arrival prices for fill enrichment (NEW only —
            # AMEND/CANCEL should not overwrite the original arrival reference point)
            if intent.intent_type == IntentType.NEW:
                arrival = int(cmd.arrival_price)
                # RiskEngine path leaves arrival_price=0; stamp from LOB mid-price
                if arrival <= 0 and self._mid_price_fn is not None:
                    try:
                        arrival = self._mid_price_fn(intent.symbol)
                    except Exception:  # noqa: BLE001
                        arrival = int(cmd.decision_price)
                if arrival <= 0:
                    arrival = int(cmd.decision_price)
                self._cmd_tca_map[order_key] = (int(cmd.decision_price), arrival)

            # Bound cmd maps — evict oldest FIFO entries, skipping live orders (M6 pattern).
            # Use _cmd_created_ns_map as the trigger since it is the superset (populated for
            # all intent types, while _cmd_tca_map is NEW-only).
            if len(self._cmd_created_ns_map) >= self._cmd_map_max_size:
                evict_target = max(1, len(self._cmd_created_ns_map) // 10)
                evicted = 0
                for k in list(self._cmd_created_ns_map.keys()):
                    if evicted >= evict_target:
                        break
                    if k not in self.live_orders:
                        del self._cmd_created_ns_map[k]
                        self._cmd_tca_map.pop(k, None)
                        evicted += 1
                if evicted:
                    logger.warning(
                        "cmd_map_eviction",
                        evicted=evicted,
                        remaining_created=len(self._cmd_created_ns_map),
                        remaining_tca=len(self._cmd_tca_map),
                    )

            if intent.intent_type == IntentType.NEW:
                if self._broker_codec is None:
                    logger.error("No broker codec configured — cannot dispatch order", symbol=intent.symbol)
                    self.metrics.order_reject_total.inc()
                    self._dedup_commit(intent.idempotency_key, False, "no_broker_codec", cmd.cmd_id)
                    await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "no_broker_codec")
                    return False
                logger.info("Placing Order", symbol=intent.symbol, price=intent.price, qty=intent.qty, side=intent.side)

                # Dynamic Exchange Lookup (prefer config metadata)
                meta = self.metadata
                meta_exchange = ""
                if hasattr(meta, "exchange"):
                    try:
                        meta_exchange = meta.exchange(intent.symbol)
                    except (KeyError, TypeError, AttributeError) as ex_err:
                        logger.warning(
                            "Metadata exchange lookup failed",
                            symbol=intent.symbol,
                            error=str(ex_err),
                        )
                        meta_exchange = ""

                client_exchange = ""
                if hasattr(self.client, "get_exchange"):
                    client_exchange = self.client.get_exchange(intent.symbol) or ""
                exchange = meta_exchange or client_exchange or "TSE"

                if not meta_exchange and not client_exchange:
                    logger.warning(
                        "Exchange unknown - using default TSE",
                        symbol=intent.symbol,
                    )

                product_type = None
                if hasattr(meta, "product_type"):
                    try:
                        product_type = meta.product_type(intent.symbol) or None
                    except (KeyError, TypeError, AttributeError) as pt_err:
                        logger.warning(
                            "Product type lookup failed",
                            symbol=intent.symbol,
                            error=str(pt_err),
                        )
                        product_type = None

                order_params: Dict[str, Any] = {}
                if hasattr(meta, "order_params"):
                    try:
                        order_params = meta.order_params(intent.symbol) or {}
                    except (KeyError, TypeError, AttributeError) as op_err:
                        logger.warning(
                            "Order params lookup failed",
                            symbol=intent.symbol,
                            error=str(op_err),
                        )
                        order_params = {}

                # Convert Side IntEnum to broker-specific string
                action_str = self._broker_codec.encode_side(intent.side)

                # De-scale price (Fixed Point -> Float limit price)
                price_float = self.price_codec.descale(intent.symbol, intent.price)

                # Shioaji custom_field limit is 6 chars
                c_field = intent.strategy_id
                if len(c_field) > 6:
                    logger.warning("StrategyID too long for custom_field", id=c_field)
                    c_field = c_field[:6]

                # TIF Mapping via broker codec
                tif_str = self._broker_codec.encode_tif(intent.tif)

                # D2: Pre-register sentinel to track in-flight order
                async with self._live_orders_lock:
                    self.live_orders[order_key] = _PENDING_SENTINEL
                    self._pending_order_keys.add(order_key)

                # Broker-specific price type encoding + validation
                _intent_pt = getattr(intent, "price_type", "LMT")
                _raw_pt = _intent_pt if _intent_pt != "LMT" else str(order_params.get("price_type", "LMT"))
                price_type = self._broker_codec.encode_price_type(_raw_pt)
                if price_type in {"MKT", "MKP"} and tif_str == "ROD":
                    logger.error(
                        "Rejecting invalid order type",
                        reason="MKT/MKP requires IOC/FOK",
                        symbol=intent.symbol,
                        price_type=price_type,
                        tif=tif_str,
                    )
                    self.metrics.order_reject_total.inc()
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    return False

                # Live safety: CA must be active when enabled
                if getattr(self.client, "mode", "") != "simulation" and getattr(self.client, "activate_ca", False):
                    if not getattr(self.client, "ca_active", False):
                        logger.error(
                            "Rejecting order: CA not active",
                            symbol=intent.symbol,
                        )
                        self.metrics.order_reject_total.inc()
                        async with self._live_orders_lock:
                            self.live_orders.pop(order_key, None)
                            self._pending_order_keys.discard(order_key)
                        return False

                trade = await self._call_api(
                    "place_order",
                    self.client.place_order,
                    contract_code=intent.symbol,
                    exchange=exchange,
                    action=action_str,
                    price=price_float,
                    qty=intent.qty,
                    order_type=tif_str,
                    tif=tif_str,
                    custom_field=c_field,
                    product_type=product_type,
                    price_type=price_type,
                    intent=intent,
                    **order_params,
                )
                if trade is None or trade is _GUARD_TIMEOUT:
                    _is_timeout = trade is _GUARD_TIMEOUT
                    _fail_reason = "api_timeout" if _is_timeout else "api_failure"
                    _dlq_reason = RejectionReason.API_TIMEOUT if _is_timeout else RejectionReason.CONNECTION_ERROR
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    self.metrics.order_reject_total.inc()
                    self._dedup_commit(intent.idempotency_key, False, _fail_reason, cmd.cmd_id)
                    await self._add_to_dlq(intent, _dlq_reason, _fail_reason)
                    return False

                self.metrics.order_actions_total.labels(type="new").inc()
                # Inject timestamp for TTL tracking
                trade_ts = timebase.now_s()
                try:
                    if isinstance(trade, dict):
                        trade["timestamp"] = trade_ts
                    else:
                        trade.timestamp = trade_ts
                except (AttributeError, TypeError) as ts_err:
                    logger.warning(
                        "Failed to set trade timestamp - TTL tracking may be affected",
                        order_key=order_key,
                        error=str(ts_err),
                    )
                    # Store timestamp externally if object is rigid
                    if isinstance(trade, dict):
                        trade["_external_timestamp"] = trade_ts
                    # For objects, we'll rely on live_orders insertion time

                # Register broker IDs BEFORE removing from pending keys so that
                # fast fill callbacks arriving during this window are still
                # deferred (DECISION-007 race fix).
                await self._register_broker_ids(order_key, trade)

                # D2: Replace sentinel with real trade and leave pending state
                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade
                    self._pending_order_keys.discard(order_key)

                # Process any deferred terminal callbacks now that IDs are mapped.
                await self._drain_deferred_terminals(order_key, trade)

                self.rate_limiter.record()
                self.per_symbol_rate_limiter.record(intent.symbol)
                self.circuit_breaker.record_success()
                self._update_cb_metric()
                self.strategy_cb_mgr.record_success(intent.strategy_id)
                self._audit_log_order({
                    "event": "dispatched",
                    "intent_type": "NEW",
                    "order_key": order_key,
                    "symbol": intent.symbol,
                    "side": str(intent.side),
                    "price": intent.price,
                    "qty": intent.qty,
                    "strategy_id": intent.strategy_id,
                    "cmd_id": int(cmd.cmd_id),
                })

            elif intent.intent_type == IntentType.FORCE_FLAT:
                if self._broker_codec is None:
                    logger.error("No broker codec configured — cannot dispatch force-flat order", symbol=intent.symbol)
                    self.metrics.order_reject_total.inc()
                    return False

                net_qty = self._platform_net_position_for_symbol(intent.symbol)
                if net_qty == 0:
                    logger.info("Force-flat no-op: already flat", symbol=intent.symbol, strategy_id=intent.strategy_id)
                    return False

                meta = self.metadata
                meta_exchange = ""
                if hasattr(meta, "exchange"):
                    try:
                        meta_exchange = meta.exchange(intent.symbol)
                    except (KeyError, TypeError, AttributeError) as ex_err:
                        logger.warning("Metadata exchange lookup failed", symbol=intent.symbol, error=str(ex_err))

                client_exchange = ""
                if hasattr(self.client, "get_exchange"):
                    client_exchange = self.client.get_exchange(intent.symbol) or ""
                exchange = meta_exchange or client_exchange or "TSE"

                product_type = None
                if hasattr(meta, "product_type"):
                    try:
                        product_type = meta.product_type(intent.symbol) or None
                    except (KeyError, TypeError, AttributeError) as pt_err:
                        logger.warning("Product type lookup failed", symbol=intent.symbol, error=str(pt_err))

                force_flat_order_params: Dict[str, Any] = {}
                if hasattr(meta, "order_params"):
                    try:
                        force_flat_order_params = meta.order_params(intent.symbol) or {}
                    except (KeyError, TypeError, AttributeError) as op_err:
                        logger.warning("Order params lookup failed", symbol=intent.symbol, error=str(op_err))

                close_side = Side.SELL if net_qty > 0 else Side.BUY
                close_qty = abs(net_qty)
                action_str = self._broker_codec.encode_side(close_side)
                tif_str = self._broker_codec.encode_tif(TIF.IOC)
                price_type = self._broker_codec.encode_price_type("LMT")
                price_scaled = self._force_flat_price(intent.symbol, close_side, intent.price)
                price_float = self.price_codec.descale(intent.symbol, price_scaled)

                c_field = intent.strategy_id
                if len(c_field) > 6:
                    logger.warning("StrategyID too long for custom_field", id=c_field)
                    c_field = ""

                order_key = f"{intent.strategy_id}:{intent.intent_id}"
                async with self._live_orders_lock:
                    self.live_orders[order_key] = _PENDING_SENTINEL
                    self._pending_order_keys.add(order_key)

                trade = await self._call_api(
                    "place_order",
                    self.client.place_order,
                    contract_code=intent.symbol,
                    exchange=exchange,
                    action=action_str,
                    price=price_float,
                    qty=close_qty,
                    order_type=tif_str,
                    tif=tif_str,
                    custom_field=c_field,
                    product_type=product_type,
                    price_type=price_type,
                    intent=intent,
                    **force_flat_order_params,
                )
                if trade is None or trade is _GUARD_TIMEOUT:
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    return False

                trade_ts = timebase.now_s()
                try:
                    if isinstance(trade, dict):
                        trade["timestamp"] = trade_ts
                    else:
                        trade.timestamp = trade_ts
                except (AttributeError, TypeError):
                    if isinstance(trade, dict):
                        trade["_external_timestamp"] = trade_ts

                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade
                    self._pending_order_keys.discard(order_key)

                await self._register_broker_ids(order_key, trade)
                await self._drain_deferred_terminals(order_key, trade)
                self.metrics.order_actions_total.labels(type="force_flat").inc()
                self.rate_limiter.record()
                self.per_symbol_rate_limiter.record(intent.symbol)
                self.circuit_breaker.record_success()
                self._update_cb_metric()
                self.strategy_cb_mgr.record_success(intent.strategy_id)
                self._audit_log_order({
                    "event": "dispatched",
                    "intent_type": "FORCE_FLAT",
                    "order_key": order_key,
                    "symbol": intent.symbol,
                    "side": str(close_side),
                    "qty": close_qty,
                    "strategy_id": intent.strategy_id,
                    "cmd_id": int(cmd.cmd_id),
                })

            elif intent.intent_type == IntentType.CANCEL:
                async with self._live_orders_lock:
                    target_key = self.order_id_resolver.resolve_order_key(
                        intent.strategy_id, intent.target_order_id, self.live_orders
                    )
                    target_trade = self.live_orders.get(target_key)

                if (
                    target_trade
                    and target_trade is not _PENDING_SENTINEL
                    and target_trade is not _TERMINAL_BEFORE_REGISTERED
                ):
                    logger.info("Canceling Order", target=target_key)
                    result = await self._call_api("cancel_order", self.client.cancel_order, target_trade, intent=intent)
                    if result is None or result is _GUARD_TIMEOUT:
                        return False
                    self.metrics.order_actions_total.labels(type="cancel").inc()
                    self.rate_limiter.record()
                    self.per_symbol_rate_limiter.record(intent.symbol)
                    self._audit_log_order({
                        "event": "dispatched",
                        "intent_type": "CANCEL",
                        "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                        "target_key": target_key,
                        "symbol": intent.symbol,
                        "strategy_id": intent.strategy_id,
                        "cmd_id": int(cmd.cmd_id),
                    })
                elif target_trade is _PENDING_SENTINEL:
                    logger.warning("Cancel target still pending", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Cancel target still pending")
                elif target_trade is _TERMINAL_BEFORE_REGISTERED:
                    logger.warning("Cancel target terminated before registered", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(
                        intent, RejectionReason.VALIDATION_ERROR, "Cancel target terminated before registered"
                    )
                else:
                    logger.warning("Cancel target not found", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Cancel target not found")

            elif intent.intent_type == IntentType.AMEND:
                async with self._live_orders_lock:
                    target_key = self.order_id_resolver.resolve_order_key(
                        intent.strategy_id, intent.target_order_id, self.live_orders
                    )
                    target_trade = self.live_orders.get(target_key)

                if (
                    target_trade
                    and target_trade is not _PENDING_SENTINEL
                    and target_trade is not _TERMINAL_BEFORE_REGISTERED
                ):
                    # Descale price
                    price_f = self.price_codec.descale(intent.symbol, intent.price)

                    logger.info("Amending Order", target=target_key, new_price=price_f)
                    result = await self._call_api(
                        "update_order",
                        self.client.update_order,
                        target_trade,
                        price=price_f,
                        intent=intent,
                    )
                    if result is None or result is _GUARD_TIMEOUT:
                        return False
                    self.metrics.order_actions_total.labels(type="amend").inc()
                    self.rate_limiter.record()
                    self.per_symbol_rate_limiter.record(intent.symbol)
                    self._audit_log_order({
                        "event": "dispatched",
                        "intent_type": "AMEND",
                        "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                        "target_key": target_key,
                        "symbol": intent.symbol,
                        "new_price": intent.price,
                        "strategy_id": intent.strategy_id,
                        "cmd_id": int(cmd.cmd_id),
                    })
                elif target_trade is _PENDING_SENTINEL:
                    logger.warning("Amend target still pending", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Amend target still pending")
                elif target_trade is _TERMINAL_BEFORE_REGISTERED:
                    logger.warning("Amend target terminated before registered", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(
                        intent, RejectionReason.VALIDATION_ERROR, "Amend target terminated before registered"
                    )
                else:
                    logger.warning("Amend target not found", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Amend target not found")

        except (OSError, TimeoutError, ConnectionError, RuntimeError) as e:
            logger.error("Broker Error", error=str(e))
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            self._update_cb_metric()
            self.strategy_cb_mgr.record_failure(intent.strategy_id)
            self._emit_trace("order_dispatch_error", intent, {"cmd_id": int(cmd.cmd_id), "error": str(e)})
            self._audit_log_order({
                "event": "dispatch_failed",
                "intent_type": str(intent.intent_type),
                "order_key": order_key,
                "symbol": intent.symbol,
                "strategy_id": intent.strategy_id,
                "cmd_id": int(cmd.cmd_id),
                "error": str(e),
            })
            # Clean up sentinel to prevent permanent slot occupation (D2 rollback)
            async with self._live_orders_lock:
                if order_key in self.live_orders and self.live_orders.get(order_key) is _PENDING_SENTINEL:
                    del self.live_orders[order_key]
                    self._pending_order_keys.discard(order_key)
            return False
        else:
            self._emit_trace("order_dispatch_ok", intent, {"cmd_id": int(cmd.cmd_id)})
        return True

    async def _enqueue_api(self, cmd: OrderCommand) -> bool:
        """Enqueue command to API worker. Returns True on success, False if DLQ'd."""
        try:
            self._api_queue.put_nowait(cmd)
            self._emit_trace("order_enqueue_api", cmd.intent, {"cmd_id": int(cmd.cmd_id)})
            return True
        except asyncio.QueueFull:
            logger.warning(
                "API queue full - routing to DLQ",
                cmd_id=cmd.cmd_id,
                strategy_id=cmd.intent.strategy_id,
                symbol=cmd.intent.symbol,
            )
            self.metrics.order_reject_total.inc()
            self._emit_trace("order_reject", cmd.intent, {"reason": "API_QUEUE_FULL", "cmd_id": int(cmd.cmd_id)})
            await self._add_to_dlq(cmd.intent, RejectionReason.RATE_LIMIT, "API queue full")
            return False

    def submit_typed_command_nowait(self, frame: TypedOrderCommandFrame) -> None:
        """Prototype typed command ingress from GatewayService (avoids early materialization)."""
        if not _is_typed_order_cmd_frame(frame):
            raise ValueError("Invalid typed order command frame")
        self._api_queue.put_nowait(frame)

    def _materialize_typed_command(self, frame: TypedOrderCommandFrame) -> OrderCommand:
        from hft_platform.gateway.channel import typed_frame_to_intent

        _, cmd_id, deadline_ns, storm_guard_state, created_ns, typed_intent_frame = frame
        intent = typed_frame_to_intent(typed_intent_frame)
        return OrderCommand(
            cmd_id=int(cmd_id),
            intent=intent,
            deadline_ns=int(deadline_ns),
            storm_guard_state=StormGuardState(int(storm_guard_state)),
            created_ns=int(created_ns),
        )

    def _coalesce_key(self, cmd: OrderCommand) -> tuple:
        intent = cmd.intent
        if intent.intent_type == IntentType.NEW:
            return ("new", intent.strategy_id, intent.symbol, intent.intent_id)
        if intent.intent_type == IntentType.CANCEL:
            return ("cancel", intent.strategy_id, intent.target_order_id)
        if intent.intent_type == IntentType.AMEND:
            return ("amend", intent.strategy_id, intent.target_order_id)
        return ("other", intent.strategy_id, intent.intent_id)

    def _store_pending(self, cmd: OrderCommand) -> None:
        intent = cmd.intent
        key = self._coalesce_key(cmd)
        if intent.intent_type == IntentType.CANCEL:
            amend_key = ("amend", intent.strategy_id, intent.target_order_id)
            superseded = self._api_pending.pop(amend_key, None)
            if superseded is not None:
                self._dedup_release(superseded.intent.idempotency_key)
            self._api_pending[key] = cmd
            return
        if intent.intent_type == IntentType.AMEND:
            cancel_key = ("cancel", intent.strategy_id, intent.target_order_id)
            if cancel_key in self._api_pending:
                self._dedup_release(intent.idempotency_key)
                return
        self._api_pending[key] = cmd

    async def _api_worker(self) -> None:
        while self.running:
            current_cmd: OrderCommand | None = None
            try:
                item = await self._api_queue.get()
            except asyncio.CancelledError:
                return
            try:
                cmd: OrderCommand = (
                    self._materialize_typed_command(item)
                    if _is_typed_order_cmd_frame(item)
                    else cast(OrderCommand, item)
                )
                current_cmd = cmd
                if cmd.created_ns:
                    self._record_queue_latency(cmd)
                self._store_pending(cmd)

                # Fast-path: skip coalesce window for urgent intent types (CANCEL,
                # FORCE_FLAT) to minimise latency on safety-critical orders.
                _urgent = cmd.intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT)
                if not _urgent and self._api_coalesce_window_s > 0:
                    start = time.monotonic()
                    while True:
                        remaining = self._api_coalesce_window_s - (time.monotonic() - start)
                        if remaining <= 0:
                            break
                        try:
                            item = await asyncio.wait_for(self._api_queue.get(), timeout=remaining)
                            cmd = (
                                self._materialize_typed_command(item)
                                if _is_typed_order_cmd_frame(item)
                                else cast(OrderCommand, item)
                            )
                            self._store_pending(cmd)
                            # Urgent command arrived — break out immediately so
                            # CANCEL/FORCE_FLAT is not delayed by coalesce window.
                            if cmd.intent.intent_type in (
                                IntentType.CANCEL,
                                IntentType.FORCE_FLAT,
                            ):
                                break
                        except asyncio.TimeoutError:
                            break

                pending = list(self._api_pending.values())
                self._api_pending.clear()
                # Prioritize urgent intents (CANCEL, FORCE_FLAT) ahead of NEWs
                # to ensure risk-reducing orders execute before risk-increasing ones.
                pending.sort(key=lambda c: 0 if c.intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT) else 1)
                for item in pending:
                    _exempt = item.intent.intent_type in (
                        IntentType.CANCEL,
                        IntentType.FORCE_FLAT,
                    ) or self._is_strategy_halt_exempt(item.intent.strategy_id)
                    _sg_halt = (
                        self._storm_guard is not None
                        and getattr(self._storm_guard, "state", None) == StormGuardState.HALT
                    )
                    if not _exempt and _sg_halt:
                        logger.warning(
                            "api_worker_halt_skip",
                            cmd_id=item.cmd_id,
                            symbol=item.intent.symbol,
                            strategy_id=item.intent.strategy_id,
                            intent_type=item.intent.intent_type.name,
                        )
                        self.metrics.order_halt_skip_total.labels(
                            strategy_id=item.intent.strategy_id,
                        ).inc()
                        self.metrics.order_reject_total.inc()
                        self._dedup_release(item.intent.idempotency_key)
                        await self._add_to_dlq(
                            item.intent,
                            RejectionReason.STORMGUARD_HALT,
                            "STORMGUARD_HALT_SKIP",
                            halt_exempt_blocked=self._is_strategy_halt_exempt(item.intent.strategy_id),
                        )
                        self._send_dispatch_rejection(item.intent, "dispatch_halt_skip")
                        continue
                    if item.deadline_ns and time.monotonic_ns() > item.deadline_ns:
                        logger.warning(
                            "api_worker_deadline_expired",
                            cmd_id=item.cmd_id,
                            symbol=item.intent.symbol,
                            strategy_id=item.intent.strategy_id,
                        )
                        self._dedup_release(item.intent.idempotency_key)
                        self.metrics.order_reject_total.inc()
                        await self._add_to_dlq(
                            item.intent,
                            RejectionReason.VALIDATION_ERROR,
                            "DEADLINE_EXPIRED",
                        )
                        self._send_dispatch_rejection(item.intent, "dispatch_deadline_expired")
                        continue
                    try:
                        ok = await self._dispatch_to_api(item)
                        if ok:
                            self._dedup_commit(
                                item.intent.idempotency_key, True, "dispatched", item.cmd_id
                            )
                    except Exception:
                        logger.error(
                            "_api_worker: dispatch failed for single order",
                            cmd_id=item.cmd_id,
                            symbol=item.intent.symbol,
                            exc_info=True,
                        )
                        # Release dedup slot so strategy can retry with same key
                        self._dedup_release(item.intent.idempotency_key)
                        self.metrics.order_reject_total.inc()
                        self.circuit_breaker.record_failure()
                        self._update_cb_metric()
                        self._send_dispatch_rejection(item.intent, "dispatch_failed")
                        self.strategy_cb_mgr.record_failure(item.intent.strategy_id)
            except Exception:
                logger.error("_api_worker: unexpected exception in dispatch loop", exc_info=True)
                self.metrics.order_reject_total.inc()
                self.circuit_breaker.record_failure()
                self._update_cb_metric()
                if current_cmd is not None:
                    self.strategy_cb_mgr.record_failure(current_cmd.intent.strategy_id)
                # Release dedup slots for orphaned commands before clearing
                for orphaned in self._api_pending.values():
                    self._dedup_release(orphaned.intent.idempotency_key)
                self._api_pending.clear()

    def _update_cb_metric(self) -> None:
        """Emit global circuit-breaker state to Prometheus (0=closed, 1=open)."""
        try:
            self.metrics.circuit_breaker_state.labels(component="order_adapter").set(
                1 if self.circuit_breaker.is_open() else 0
            )
        except Exception:  # noqa: BLE001 — metrics must never crash the hot path
            pass

    def _record_queue_latency(self, cmd: OrderCommand) -> None:
        if not self.latency:
            return
        if not cmd.created_ns:
            return
        queue_latency_ns = timebase.now_ns() - cmd.created_ns
        self.latency.record(
            "order_queue",
            queue_latency_ns,
            trace_id=cmd.intent.trace_id,
            symbol=cmd.intent.symbol,
            strategy_id=cmd.intent.strategy_id,
        )

    def _is_transient_error(self, exc: Exception) -> bool:
        """Check if error is transient and worth retrying."""
        # Connection errors
        if isinstance(exc, (ConnectionError, ConnectionResetError, ConnectionRefusedError)):
            return True
        # Timeout errors
        if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
            return True
        # Check error message for common transient patterns
        err_str = str(exc).lower()
        transient_patterns = ("econnrefused", "econnreset", "etimedout", "connection reset", "temporarily unavailable")
        return any(p in err_str for p in transient_patterns)

    async def _call_api(
        self,
        op: str,
        fn: Any,
        *args: Any,
        intent: OrderIntent | None = None,
        max_retries: int = 2,
        **kwargs: Any,
    ) -> Any | None:
        try:
            await asyncio.wait_for(self._api_semaphore.acquire(), timeout=self._api_guard_timeout_s)
        except asyncio.TimeoutError:
            logger.warning("API guard tripped", op=op)
            self.metrics.api_guard_timeout_total.inc()
            return _GUARD_TIMEOUT

        base_delay_s = 0.01  # 10ms initial backoff
        is_mutating = op in _MUTATING_OPS

        try:
            for attempt in range(max_retries + 1):
                # For mutating operations, wrap the broker call with a
                # cancellation guard so a timed-out thread cannot actually
                # execute the broker SDK call after we move on to retry.
                cancelled = threading.Event() if is_mutating else None

                def _guarded_call(
                    _fn: Any = fn,
                    _args: tuple[Any, ...] = args,
                    _kwargs: dict[str, Any] = kwargs,
                    _cancelled: threading.Event | None = cancelled,
                ) -> Any:
                    if _cancelled is not None and _cancelled.is_set():
                        raise _TimeoutCancelled()
                    return _fn(*_args, **_kwargs)

                try:
                    start_ns = time.perf_counter_ns()
                    result = await asyncio.wait_for(
                        self._run_blocking_call(_guarded_call),
                        timeout=self._api_timeout_s,
                    )
                    duration = time.perf_counter_ns() - start_ns
                    if self.latency:
                        self.latency.record(
                            f"api_{op}",
                            duration,
                            trace_id=intent.trace_id if intent else "",
                            symbol=intent.symbol if intent else "",
                            strategy_id=intent.strategy_id if intent else "",
                        )
                        if intent and intent.source_ts_ns:
                            e2e_ns = timebase.now_ns() - intent.source_ts_ns
                            self.latency.record(
                                "e2e_order",
                                e2e_ns,
                                trace_id=intent.trace_id,
                                symbol=intent.symbol,
                                strategy_id=intent.strategy_id,
                            )
                    self.circuit_breaker.record_success()
                    self._update_cb_metric()
                    if intent and intent.strategy_id:
                        self.strategy_cb_mgr.record_success(intent.strategy_id)
                    return result

                except Exception as exc:  # noqa: BLE001 — broker SDK retry
                    # Signal the in-flight thread to abort if it hasn't
                    # started the broker call yet (best-effort guard).
                    if cancelled is not None:
                        cancelled.set()

                    # Treat our internal cancellation as a timeout
                    if isinstance(exc, _TimeoutCancelled):
                        exc = asyncio.TimeoutError()

                    is_transient = self._is_transient_error(exc)

                    # For mutating ops that timed out, do NOT retry — the
                    # original thread-pool call may still complete at the
                    # broker side, and retrying would create a duplicate.
                    if is_mutating and isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                        logger.error(
                            "Mutating API call timed out, skipping retry to prevent duplicate",
                            op=op,
                            attempt=attempt + 1,
                            timeout_s=self._api_timeout_s,
                        )
                        self.metrics.order_reject_total.inc()
                        self.circuit_breaker.record_failure()
                        self._update_cb_metric()
                        if intent and intent.strategy_id:
                            self.strategy_cb_mgr.record_failure(intent.strategy_id)
                        # D-03: Track phantom order candidates for reconciliation
                        # R2-02: Use 2-part order_key format for reconciliation parity
                        if intent is not None:
                            phantom_key = f"{intent.strategy_id}:{intent.intent_id}"
                            self._phantom_order_keys[phantom_key] = time.monotonic()
                            # R2-03: Evict entries older than 1 hour when over capacity
                            if len(self._phantom_order_keys) > self._phantom_order_max:
                                cutoff = time.monotonic() - 3600.0
                                self._phantom_order_keys = {
                                    k: v for k, v in self._phantom_order_keys.items() if v > cutoff
                                }
                            logger.warning(
                                "phantom_order_candidate",
                                strategy_id=intent.strategy_id,
                                symbol=intent.symbol,
                                op=op,
                                order_key=phantom_key,
                            )
                            self.metrics.phantom_order_candidates_total.inc()
                        if intent and self.latency and intent.source_ts_ns:
                            e2e_ns = timebase.now_ns() - intent.source_ts_ns
                            self.latency.record(
                                "e2e_order",
                                e2e_ns,
                                trace_id=intent.trace_id,
                                symbol=intent.symbol,
                                strategy_id=intent.strategy_id,
                            )
                        return None

                    if is_transient and attempt < max_retries:
                        # Exponential backoff: 10ms, 20ms, 40ms...
                        delay = base_delay_s * (2**attempt)
                        logger.warning(
                            "API call failed, retrying",
                            op=op,
                            error=str(exc),
                            attempt=attempt + 1,
                            max_retries=max_retries,
                            delay_ms=delay * 1000,
                        )
                        await asyncio.sleep(delay)
                        continue

                    # Non-transient error or exhausted retries
                    logger.error(
                        "API call failed",
                        op=op,
                        error=str(exc),
                        error_type=type(exc).__name__,
                        attempts=attempt + 1,
                        is_transient=is_transient,
                    )
                    self.metrics.order_reject_total.inc()
                    self.circuit_breaker.record_failure()
                    self._update_cb_metric()
                    if intent and intent.strategy_id:
                        self.strategy_cb_mgr.record_failure(intent.strategy_id)
                    if intent and self.latency and intent.source_ts_ns:
                        e2e_ns = timebase.now_ns() - intent.source_ts_ns
                        self.latency.record(
                            "e2e_order",
                            e2e_ns,
                            trace_id=intent.trace_id,
                            symbol=intent.symbol,
                            strategy_id=intent.strategy_id,
                        )
                    return None

            # Should not reach here, but handle gracefully
            return None
        finally:
            self._api_semaphore.release()

    def _emit_trace(self, stage: str, intent: OrderIntent, payload: dict[str, Any]) -> None:
        sampler = getattr(self, "_trace_sampler", None)
        if sampler is None:
            return
        try:
            sampler.emit(
                stage=stage,
                trace_id=str(getattr(intent, "trace_id", "") or ""),
                payload={
                    "strategy_id": intent.strategy_id,
                    "symbol": intent.symbol,
                    "intent_type": int(intent.intent_type),
                    **payload,
                },
            )
        except (TypeError, ValueError, AttributeError) as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass
