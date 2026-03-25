import asyncio
import os
import time
from typing import Any, Dict, TypeAlias, TypeGuard, cast

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState
from hft_platform.core import timebase
from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.feed_adapter.protocol import BrokerOrderCodec
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.platform_degrade import get_shared_platform_degrade_controller
from hft_platform.order.circuit_breaker import CircuitBreaker, StrategyCircuitBreakerManager
from hft_platform.order.deadletter import DeadLetterQueue, RejectionReason, get_dlq
from hft_platform.order.rate_limiter import PerSymbolRateLimiter, PerSymbolRateResult, RateLimiter
from hft_platform.order.shadow import ShadowOrderSink

logger = get_logger("order_adapter")


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
        "_orphan_detector",
        "__dict__",  # needed for test monkey-patching
    )

    def __init__(
        self,
        config_path: str,
        order_queue: asyncio.Queue[OrderCommand],
        broker_client: Any,
        order_id_map: Dict[str, str] | None = None,
        broker_codec: BrokerOrderCodec | None = None,
    ) -> None:
        self.config_path = config_path
        self.order_queue = order_queue
        self.client = broker_client
        self._broker_codec = broker_codec
        # Map broker order IDs -> order_key ("strategy_id:intent_id")
        # Protected by _order_id_map_lock for concurrent access
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self._order_id_map_lock = asyncio.Lock()
        self._order_id_map_max_size = int(os.getenv("HFT_ORDER_ID_MAP_MAX_SIZE", "10000"))
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._metadata: SymbolMetadata = SymbolMetadata()
        self.price_codec: PriceCodec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

        # State - Protected by _live_orders_lock for concurrent access
        self.live_orders: Dict[str, Any] = {}  # Map "strategy_id:intent_id" -> Trade Object or Status dict
        self._live_orders_lock = asyncio.Lock()

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
        self.position_store = None
        self._orphan_detector: Any = None  # OrphanDetector, set externally

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

    async def run(self) -> None:
        self.running = True
        logger.info("OrderAdapter started")
        self._api_worker_task = asyncio.create_task(self._api_worker())

        # Start optional orphan detector as a background subtask
        if self._orphan_detector is not None:
            await self._orphan_detector.start()
            logger.info("OrphanDetector started")

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
                if timebase.now_ns() > cmd.deadline_ns:
                    logger.warning("Order Timeout (Pre-dispatch)", cmd_id=cmd.cmd_id)
                    self.order_queue.task_done()
                    continue

                await self.execute(cmd)
                self.order_queue.task_done()
        finally:
            # Stop orphan detector if running
            if self._orphan_detector is not None:
                await self._orphan_detector.stop()
                logger.info("OrphanDetector stopped")
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
            if trade is None:
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

            if order_key in self.live_orders:
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]

        # Also clean up rate limit window if needed? No, rate limit is distinct.

    async def _register_broker_ids(self, order_key: str, trade: Any) -> None:
        """Register broker IDs to order_key mapping with lock protection."""
        ids = set()

        if isinstance(trade, dict):
            for key in ("seq_no", "ord_no", "order_id", "id"):
                val = trade.get(key)
                if val:
                    ids.add(val)

            order = trade.get("order")
            if isinstance(order, dict):
                for key in ("seq_no", "ord_no", "order_id", "id"):
                    val = order.get(key)
                    if val:
                        ids.add(val)
        else:
            for attr in ("seq_no", "ord_no", "order_id", "id"):
                val = getattr(trade, attr, None)
                if val:
                    ids.add(val)

            order = getattr(trade, "order", None)
            if order:
                for attr in ("seq_no", "ord_no", "order_id", "id"):
                    val = getattr(order, attr, None)
                    if val:
                        ids.add(val)

        async with self._order_id_map_lock:
            # Evict oldest entries if at limit (simple FIFO eviction)
            if len(self.order_id_map) >= self._order_id_map_max_size:
                # Remove oldest 10% to avoid frequent evictions
                evict_count = max(1, len(self.order_id_map) // 10)
                keys_to_remove = list(self.order_id_map.keys())[:evict_count]
                for k in keys_to_remove:
                    del self.order_id_map[k]
                logger.info("Evicted stale order IDs", count=evict_count, remaining=len(self.order_id_map))

            for oid in ids:
                self.order_id_map[str(oid)] = order_key

    async def execute(self, cmd: OrderCommand) -> None:
        intent = cmd.intent

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
            await self._add_to_dlq(intent, "platform_reduce_only", "Platform is in reduce-only mode")
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
        if intent.intent_type == IntentType.NEW:
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
        pending_qty = 0
        for trade in self.live_orders.values():
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

    async def _dispatch_to_api(self, cmd: OrderCommand) -> None:
        intent = cmd.intent
        self._emit_trace(
            "order_dispatch_start", intent, {"cmd_id": int(cmd.cmd_id), "intent_type": int(intent.intent_type)}
        )
        try:
            order_key = f"{intent.strategy_id}:{intent.intent_id}"

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
                    # If too long, do not pass it, rely on internal map
                    logger.warning("StrategyID too long for custom_field", id=c_field)
                    c_field = ""

                # TIF Mapping via broker codec
                tif_str = self._broker_codec.encode_tif(intent.tif)

                # Broker-specific price type encoding + validation
                price_type = self._broker_codec.encode_price_type(str(order_params.get("price_type", "LMT")))
                if price_type in {"MKT", "MKP"} and tif_str == "ROD":
                    logger.error(
                        "Rejecting invalid order type",
                        reason="MKT/MKP requires IOC/FOK",
                        symbol=intent.symbol,
                        price_type=price_type,
                        tif=tif_str,
                    )
                    self.metrics.order_reject_total.inc()
                    return

                # Live safety: CA must be active when enabled
                if getattr(self.client, "mode", "") != "simulation" and getattr(self.client, "activate_ca", False):
                    if not getattr(self.client, "ca_active", False):
                        logger.error(
                            "Rejecting order: CA not active",
                            symbol=intent.symbol,
                        )
                        self.metrics.order_reject_total.inc()
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

                # Store with lock protection
                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade

                # Populate lookup using Shioaji trade attributes (broker ID -> order_key).
                await self._register_broker_ids(order_key, trade)

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
