import asyncio
import os
import time
from typing import Any, Dict

import yaml
from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderCommand, OrderIntent, Side
from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.circuit_breaker import CircuitBreaker
from hft_platform.order.rate_limiter import RateLimiter

logger = get_logger("order_adapter")


class OrderAdapter:
    def __init__(
        self, config_path: str, order_queue: asyncio.Queue, shioaji_client, order_id_map: Dict[str, str] | None = None
    ):
        self.config_path = config_path
        self.order_queue = order_queue
        self.client = shioaji_client
        # Map broker order IDs -> order_key ("strategy_id:intent_id")
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._metadata: SymbolMetadata = SymbolMetadata()
        self.price_codec: PriceCodec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

        # State
        self.live_orders: Dict[str, Any] = {}  # Map "strategy_id:intent_id" -> Trade Object or Status dict

        # Helpers
        self.rate_limiter = RateLimiter(soft_cap=180, hard_cap=250, window_s=10)
        self.circuit_breaker = CircuitBreaker(threshold=5, timeout_s=60)
        self.order_id_resolver = OrderIdResolver(self.order_id_map)
        self._api_timeout_s = float(os.getenv("HFT_API_TIMEOUT_S", "3.0"))
        self._api_guard_timeout_s = float(os.getenv("HFT_API_GUARD_TIMEOUT_S", "0.005"))
        self._api_max_inflight = int(os.getenv("HFT_API_MAX_INFLIGHT", "16"))
        self._api_semaphore = asyncio.Semaphore(self._api_max_inflight)
        self._api_queue_max = int(os.getenv("HFT_API_QUEUE_MAX", "1024"))
        self._api_queue: asyncio.Queue[OrderCommand] = asyncio.Queue(maxsize=self._api_queue_max)
        self._api_coalesce_window_s = float(os.getenv("HFT_API_COALESCE_WINDOW_S", "0.005"))
        self._api_pending: dict[tuple, OrderCommand] = {}
        self._api_worker_task: asyncio.Task | None = None

        self.load_config()

    @property
    def metadata(self) -> SymbolMetadata:
        return self._metadata

    @metadata.setter
    def metadata(self, value: SymbolMetadata):
        self._metadata = value
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))

    def load_config(self):
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

    async def run(self):
        self.running = True
        logger.info("OrderAdapter started")
        self._api_worker_task = asyncio.create_task(self._api_worker())

        while self.running:
            # Allow exceptions to crash the task (Supervisor will handle)
            cmd: OrderCommand = await self.order_queue.get()

            # Check Deadline
            if time.time_ns() > cmd.deadline_ns:
                logger.warning("Order Timeout (Pre-dispatch)", cmd_id=cmd.cmd_id)
                self.order_queue.task_done()
                continue

            await self.execute(cmd)
            self.order_queue.task_done()
        if self._api_worker_task:
            self._api_worker_task.cancel()

    def check_rate_limit(self) -> bool:
        """Sliding window check."""
        return self.rate_limiter.check()

    def on_terminal_state(self, strategy_id: str, order_id: str):
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        order_key = self.order_id_resolver.resolve_order_key(strategy_id, order_id, self.live_orders)

        if order_key in self.live_orders:
            logger.info("Removing terminal order", key=order_key)
            del self.live_orders[order_key]

        # Also clean up rate limit window if needed? No, rate limit is distinct.

    def _register_broker_ids(self, order_key: str, trade: Any):
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

        for oid in ids:
            self.order_id_map[str(oid)] = order_key

    async def execute(self, cmd: OrderCommand):
        # Circuit Breaker Check
        if self.circuit_breaker.is_open():
            logger.warning("Circuit Breaker Open - Rejecting", cmd_id=cmd.cmd_id)
            return

        if not self.check_rate_limit():
            # Trigger circuit break? Or just drop?
            # Spec says: Cut off before 250.
            return
        if not self._validate_client(cmd.intent):
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            return
        if not self.running:
            if cmd.created_ns:
                self._record_queue_latency(cmd)
            await self._dispatch_to_api(cmd)
            return
        await self._enqueue_api(cmd)

    def _validate_client(self, intent: OrderIntent) -> bool:
        if intent.intent_type == IntentType.NEW:
            return hasattr(self.client, "place_order") and hasattr(self.client, "get_exchange")
        if intent.intent_type == IntentType.CANCEL:
            return hasattr(self.client, "cancel_order")
        if intent.intent_type == IntentType.AMEND:
            return hasattr(self.client, "update_order")
        return True

    async def _dispatch_to_api(self, cmd: OrderCommand):
        intent = cmd.intent
        try:
            order_key = f"{intent.strategy_id}:{intent.intent_id}"

            if intent.intent_type == IntentType.NEW:
                logger.info("Placing Order", symbol=intent.symbol, price=intent.price, qty=intent.qty, side=intent.side)

                # Dynamic Exchange Lookup (prefer config metadata)
                meta = self.metadata
                meta_exchange = ""
                if hasattr(meta, "exchange"):
                    try:
                        meta_exchange = meta.exchange(intent.symbol)
                    except Exception:
                        meta_exchange = ""
                exchange = meta_exchange or self.client.get_exchange(intent.symbol) or "TSE"

                product_type = None
                if hasattr(meta, "product_type"):
                    try:
                        product_type = meta.product_type(intent.symbol) or None
                    except Exception:
                        product_type = None

                order_params: Dict[str, Any] = {}
                if hasattr(meta, "order_params"):
                    try:
                        order_params = meta.order_params(intent.symbol) or {}
                    except Exception:
                        order_params = {}

                # Convert Side IntEnum to String for ShioajiClient
                action_str = "Buy" if intent.side == Side.BUY else "Sell"

                # De-scale price (Fixed Point -> Float limit price)
                price_float = self.price_codec.descale(intent.symbol, intent.price)

                # Shioaji custom_field limit is 6 chars
                c_field = intent.strategy_id
                if len(c_field) > 6:
                    # If too long, do not pass it, rely on internal map
                    logger.warning("StrategyID too long for custom_field", id=c_field)
                    c_field = ""

                # TIF Mapping (IntEnum -> Str)
                # Limit -> ROD; IOC/FOK passthrough
                tif_map = {TIF.LIMIT: "ROD", TIF.IOC: "IOC", TIF.FOK: "FOK"}
                tif_str = tif_map.get(intent.tif, "ROD")

                # Shioaji: MKT/MKP must be IOC/FOK (not ROD)
                price_type = str(order_params.get("price_type", "LMT")).upper()
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
                self.live_orders[order_key] = trade
                # Inject timestamp for TTL (hacky if trade is object, assumes it absorbs attrs or we wrap)
                try:
                    if isinstance(trade, dict):
                        trade["timestamp"] = time.time()
                    else:
                        trade.timestamp = time.time()
                except Exception:
                    pass  # Object might be rigid

                # Populate lookup using Shioaji trade attributes (broker ID -> order_key).
                self._register_broker_ids(order_key, trade)

                self.rate_limiter.record()
                self.circuit_breaker.record_success()

            elif intent.intent_type == IntentType.CANCEL:
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

        except Exception as e:
            logger.error("Broker Error", error=str(e))
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()

    async def _enqueue_api(self, cmd: OrderCommand) -> None:
        try:
            self._api_queue.put_nowait(cmd)
        except asyncio.QueueFull:
            logger.warning("API queue full - dropping", cmd_id=cmd.cmd_id)

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
                cmd = await self._api_queue.get()
            except asyncio.CancelledError:
                return
            if cmd.created_ns:
                self._record_queue_latency(cmd)
            self._store_pending(cmd)

            start = time.monotonic()
            while True:
                remaining = self._api_coalesce_window_s - (time.monotonic() - start)
                if remaining <= 0:
                    break
                try:
                    cmd = await asyncio.wait_for(self._api_queue.get(), timeout=remaining)
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
        queue_latency_ns = time.time_ns() - cmd.created_ns
        self.latency.record(
            "order_queue",
            queue_latency_ns,
            trace_id=cmd.intent.trace_id,
            symbol=cmd.intent.symbol,
            strategy_id=cmd.intent.strategy_id,
        )

    async def _call_api(self, op: str, fn, *args, intent: OrderIntent | None = None, **kwargs):
        try:
            await asyncio.wait_for(self._api_semaphore.acquire(), timeout=self._api_guard_timeout_s)
        except asyncio.TimeoutError:
            logger.warning("API guard tripped", op=op)
            self.metrics.order_reject_total.inc()
            return None

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
                    e2e_ns = time.time_ns() - intent.source_ts_ns
                    self.latency.record(
                        "e2e_order",
                        e2e_ns,
                        trace_id=intent.trace_id,
                        symbol=intent.symbol,
                        strategy_id=intent.strategy_id,
                    )
            self.circuit_breaker.record_success()
            return result
        except Exception as exc:
            logger.error("API call failed", op=op, error=str(exc))
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            if intent and self.latency and intent.source_ts_ns:
                e2e_ns = time.time_ns() - intent.source_ts_ns
                self.latency.record(
                    "e2e_order",
                    e2e_ns,
                    trace_id=intent.trace_id,
                    symbol=intent.symbol,
                    strategy_id=intent.strategy_id,
                )
            return None
        finally:
            self._api_semaphore.release()
