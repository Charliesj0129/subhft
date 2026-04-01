import asyncio
import collections
import os
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
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.order.circuit_breaker import CircuitBreaker, StrategyCircuitBreakerManager
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason, get_dlq
from hft_platform.order.shadow import ShadowOrderSink

logger = get_logger("order_adapter")

_PENDING_SENTINEL = object()
_TERMINAL_BEFORE_REGISTERED = object()


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
        "_mid_price_fn",
        "_storm_guard",
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

        self.load_config()

    @property
    def metadata(self) -> SymbolMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, value: SymbolMetadata) -> None:
        self._metadata = value
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

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
        storm_guard_state = StormGuardState.HALT if intent.reason == "halt_flatten" else StormGuardState.NORMAL
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
        """Public async submission API for platform-owned flatteners."""
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

    async def drain_and_cancel(self, timeout_s: float = 5.0) -> int:  # precision-ok
        """Drain pending orders and cancel all live orders."""
        cancelled = 0
        while not self.order_queue.empty():
            try:
                self.order_queue.get_nowait()
                self.order_queue.task_done()
            except asyncio.QueueEmpty:
                break
        async with self._live_orders_lock:
            live_keys = list(self.live_orders.keys())
        for key in live_keys:
            async with self._live_orders_lock:
                trade = self.live_orders.get(key)
            if trade is None or trade is _PENDING_SENTINEL or trade is _TERMINAL_BEFORE_REGISTERED:
                continue
            try:
                await asyncio.wait_for(asyncio.to_thread(self.client.cancel_order, trade), timeout=timeout_s)
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
                    order_key = self.order_id_map[k]
                    if order_key not in self.live_orders:
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
        if _is_halt and intent.reason != "halt_flatten":
            await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "StormGuard HALT")
            return

        # Per-symbol rate limit check (WU-06)
        ps_result = self.per_symbol_rate_limiter.check(intent.symbol)
        if ps_result == PerSymbolRateResult.HARD:
            await self._add_to_dlq(intent, RejectionReason.RATE_LIMIT, "Per-symbol hard rate limit")
            return

        # Per-strategy circuit breaker check (WU-09)
        if self.strategy_cb_mgr.is_open(intent.strategy_id):
            await self._add_to_dlq(intent, RejectionReason.CIRCUIT_BREAKER, "Per-strategy circuit breaker open")
            return

        # Circuit Breaker Check
        if self.circuit_breaker.is_open():
            logger.warning("Circuit Breaker Open - Rejecting", cmd_id=cmd.cmd_id)
            await self._add_to_dlq(intent, RejectionReason.CIRCUIT_BREAKER, "Circuit breaker open")
            return

        if not self.check_rate_limit():
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
            await self._add_to_dlq(intent, RejectionReason.VALIDATION_ERROR, "Client validation failed")
            return

        if not self.running:
            if cmd.created_ns:
                self._record_queue_latency(cmd)
            await self._dispatch_to_api(cmd)
            return
        await self._enqueue_api(cmd)

    async def _add_to_dlq(
        self,
        intent: OrderIntent,
        reason: RejectionReason,
        error_message: str,
    ) -> None:
        """Add a rejected order to the dead letter queue."""
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
            return max(ref_price * 2, ref_price + scale)
        return max(scale, ref_price // 2)

    async def _dispatch_to_api(self, cmd: OrderCommand) -> None:
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
                self._cmd_tca_map[order_key] = (int(cmd.decision_price), int(cmd.arrival_price))

            if intent.intent_type == IntentType.NEW:
                if self._broker_codec is None:
                    logger.error("No broker codec configured — cannot dispatch order", symbol=intent.symbol)
                    self.metrics.order_reject_total.inc()
                    return
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
                    return

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
                        return

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
                if trade is None:
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    return

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

                # D2: Replace sentinel with real trade
                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade
                    self._pending_order_keys.discard(order_key)

                # Populate lookup using Shioaji trade attributes (broker ID -> order_key).
                await self._register_broker_ids(order_key, trade)
                await self._drain_deferred_terminals(order_key, trade)

                self.rate_limiter.record()
                self.circuit_breaker.record_success()

            elif intent.intent_type == IntentType.FORCE_FLAT:
                if self._broker_codec is None:
                    logger.error("No broker codec configured — cannot dispatch force-flat order", symbol=intent.symbol)
                    self.metrics.order_reject_total.inc()
                    return

                net_qty = self._platform_net_position_for_symbol(intent.symbol)
                if net_qty == 0:
                    logger.info("Force-flat no-op: already flat", symbol=intent.symbol, strategy_id=intent.strategy_id)
                    return

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

                order_params: Dict[str, Any] = {}
                if hasattr(meta, "order_params"):
                    try:
                        order_params = meta.order_params(intent.symbol) or {}
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
                    **order_params,
                )
                if trade is None:
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    return

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
                self.circuit_breaker.record_success()

            elif intent.intent_type == IntentType.CANCEL:
                async with self._live_orders_lock:
                    target_key = self.order_id_resolver.resolve_order_key(
                        intent.strategy_id, intent.target_order_id, self.live_orders
                    )
                    target_trade = self.live_orders.get(target_key)

                if target_trade:
                    logger.info("Canceling Order", target=target_key)
                    result = await self._call_api("cancel_order", self.client.cancel_order, target_trade, intent=intent)
                    if result is None:
                        return
                    self.metrics.order_actions_total.labels(type="cancel").inc()
                    self.rate_limiter.record()
                else:
                    logger.warning("Cancel target not found", target=target_key)

            elif intent.intent_type == IntentType.AMEND:
                async with self._live_orders_lock:
                    target_key = self.order_id_resolver.resolve_order_key(
                        intent.strategy_id, intent.target_order_id, self.live_orders
                    )
                    target_trade = self.live_orders.get(target_key)

                if target_trade:
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
                    if result is None:
                        return
                    self.metrics.order_actions_total.labels(type="amend").inc()
                    self.rate_limiter.record()
                else:
                    logger.warning("Amend target not found", target=target_key)

        except (OSError, TimeoutError, ConnectionError, RuntimeError) as e:
            logger.error("Broker Error", error=str(e))
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            self._emit_trace("order_dispatch_error", intent, {"cmd_id": int(cmd.cmd_id), "error": str(e)})
        else:
            self._emit_trace("order_dispatch_ok", intent, {"cmd_id": int(cmd.cmd_id)})

    async def _enqueue_api(self, cmd: OrderCommand) -> None:
        try:
            self._api_queue.put_nowait(cmd)
            self._emit_trace("order_enqueue_api", cmd.intent, {"cmd_id": int(cmd.cmd_id)})
        except asyncio.QueueFull:
            logger.warning("API queue full - dropping", cmd_id=cmd.cmd_id)
            self._emit_trace("order_reject", cmd.intent, {"reason": "API_QUEUE_FULL", "cmd_id": int(cmd.cmd_id)})

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
            return ("new", intent.strategy_id, intent.symbol)
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
            self._api_pending.pop(amend_key, None)
            self._api_pending[key] = cmd
            return
        if intent.intent_type == IntentType.AMEND:
            cancel_key = ("cancel", intent.strategy_id, intent.target_order_id)
            if cancel_key in self._api_pending:
                return
        self._api_pending[key] = cmd

    async def _api_worker(self) -> None:
        while self.running:
            try:
                item = await self._api_queue.get()
            except asyncio.CancelledError:
                return
            cmd: OrderCommand = (
                self._materialize_typed_command(item) if _is_typed_order_cmd_frame(item) else cast(OrderCommand, item)
            )
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
                    except asyncio.TimeoutError:
                        break

            pending = list(self._api_pending.values())
            self._api_pending.clear()
            for item in pending:
                await self._dispatch_to_api(item)

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
            self.metrics.order_reject_total.inc()
            return None

        base_delay_s = 0.01  # 10ms initial backoff

        try:
            for attempt in range(max_retries + 1):
                try:
                    start_ns = time.perf_counter_ns()
                    result = await asyncio.wait_for(
                        asyncio.to_thread(fn, *args, **kwargs),
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
                    return result

                except Exception as exc:  # noqa: BLE001 — broker SDK retry
                    is_transient = self._is_transient_error(exc)

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
