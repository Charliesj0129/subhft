import asyncio
import collections
import os
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, Dict, NamedTuple, TypeAlias, TypeGuard, cast

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
from hft_platform.order.shadow_writer import ShadowOrderWriter

logger = get_logger("order_adapter")

_PENDING_SENTINEL = object()
_TERMINAL_BEFORE_REGISTERED = object()
_GUARD_TIMEOUT = object()


class _PhantomEntry(NamedTuple):
    """M4: per-occurrence phantom record. Multiple entries can share the
    same ``(strategy_id, intent_id)`` key when an intent_id is reused in
    the same process — each ``_PhantomEntry`` carries its own
    ``created_ns`` (from ``timebase.now_ns()``) so resolution and
    cleanup operate on the specific occurrence rather than overwriting
    or losing the prior entry.
    """

    monotonic_ts: float
    symbol: str
    created_ns: int
    intent: OrderIntent

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
        "_order_id_map_persist_interval_s",
        "_order_id_map_last_persist_s",
        # H3: per-entry metadata for TTL-based ABA prevention. Sidecar to
        # ``order_id_map`` so the (broker_id -> order_key) lookup API stays
        # unchanged. ``_order_id_meta[broker_id] = (created_ns, state)`` where
        # state is "live" until a terminal callback flips it to "terminal".
        # On persist, terminal entries are filtered out; on load, entries
        # older than ``HFT_ORDER_ID_MAP_TTL_S`` or already terminal are dropped.
        "_order_id_meta",
        "_order_id_map_ttl_ns",
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
        "_api_inflight",
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
        "_cancel_inflight_targets",
        "_cancel_inflight_max",
        "_cancel_inflight_ttl_s",
        "_engine_thread_id",
        # M4: per-occurrence phantom storage. Replaces the parallel
        # ``_phantom_order_keys`` + ``_phantom_intents`` dicts (each keyed
        # by ``"strategy_id:intent_id"`` -> single tuple/intent) with a
        # single dict whose values are append-only lists of
        # ``_PhantomEntry`` so reusing the same intent_id within a process
        # lifetime never overwrites a prior phantom record.
        "_phantom_records",
        # Backwards-compat read/write views — synced by the phantom
        # helpers. Legacy tests that mutate these dicts directly still
        # work because the helpers reconstruct them from
        # ``_phantom_records``. Always reflect the LAST occurrence per
        # key (matching the pre-M4 invariant).
        "_phantom_order_keys",
        "_phantom_intents",
        "_phantom_order_max",
        "_phantom_recovery_ttl_s",
        "_phantom_lock",
        "_audit_writer",
        "_rejection_sink",
        "_pending_fill_index",
        "_pending_fill_registered_at",
        "_pending_fill_ttl_s",
        "_pending_fifo_strict",
        "_pending_fill_lock",
        "_custom_field_counter",
        "_background_tasks",
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
        # P0-E1: protected by a threading.RLock (not asyncio.Lock) because
        # ``_on_exec`` runs on the broker callback thread and calls the
        # resolver pre-hand-off (see ``services/system.py::_on_exec``).
        # Critical sections are small (single dict get / bounded-N eviction),
        # so event-loop latency impact is negligible.
        # P1-8: re-entrant so batch writers (`_register_broker_ids`,
        # `_load_order_id_map`) can hold the lock while delegating individual
        # writes through ``_set_order_id_mapping`` / ``_del_order_id_mapping``
        # without deadlocking. Same-thread re-acquire is O(1).
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self._order_id_map_lock = threading.RLock()
        # Map order_key -> cmd.created_ns for e2e latency tracking (SLO-2)
        # Shared with ExecutionRouter for fill-side lookup
        self._cmd_created_ns_map: Dict[str, int] = cmd_created_ns_map if cmd_created_ns_map is not None else {}
        # TCA price map: order_key -> (decision_price, arrival_price) for fill enrichment
        self._cmd_tca_map: Dict[str, tuple[int, int]] = cmd_tca_map if cmd_tca_map is not None else {}
        self._mid_price_fn: Callable[[str], int] | None = mid_price_fn
        self._storm_guard: Any = None  # Set post-init to close TOCTOU gap (M1)
        self._order_id_map_max_size = int(os.getenv("HFT_ORDER_ID_MAP_MAX_SIZE", "10000"))
        self._order_id_map_persist_path: str = os.getenv("HFT_ORDER_ID_MAP_PERSIST_PATH", ".state/order_id_map.jsonl")
        self._order_id_map_persist_interval_s: float = float(os.getenv("HFT_ORDER_ID_MAP_PERSIST_INTERVAL_S", "1.0"))
        self._order_id_map_last_persist_s: float = 0.0  # noqa: monotonic timestamp
        # H3: per-entry metadata sidecar (broker_id -> (created_ns, state)).
        # ``state`` is "live" or "terminal"; the persist filter drops terminal
        # rows so a future restart cannot resurrect a stale (broker_id ->
        # order_key) binding when the broker re-uses the same id (ABA).
        self._order_id_meta: dict[str, tuple[int, str]] = {}
        # H3: TTL for persisted entries. Default 24h (86400s) — anything older
        # is assumed terminal at the broker even if no terminal callback was
        # observed (callback loss, daemon-thread crash, etc.).
        self._order_id_map_ttl_ns: int = int(
            float(os.getenv("HFT_ORDER_ID_MAP_TTL_S", "86400")) * 1_000_000_000
        )
        self._load_order_id_map()
        self._cmd_map_max_size = int(os.getenv("HFT_CMD_MAP_MAX_SIZE", "10000"))
        self.running = False
        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._metadata: SymbolMetadata = SymbolMetadata()
        self.price_codec: PriceCodec = PriceCodec(SymbolMetadataPriceScaleProvider(self._metadata))
        self._actual_to_config: dict[str, str] = {}  # reverse alias: TMFE6 → TMFR1
        # Option-3 Gate 3: optional ContractFamilyResolver. When ``intent.contract``
        # is set AND the resolver's snapshot has a native_hint for it, the
        # adapter trusts ``intent.contract.display()`` directly as the broker
        # code, bypassing the ``_actual_to_config`` reverse-alias lookup which
        # was the Bug 12 (alias dict empty on restart) root cause.
        self._contract_resolver: Any = None

        # State - Protected by _live_orders_lock for concurrent access
        self.live_orders: Dict[str, Any] = {}  # Map "strategy_id:intent_id" -> Trade Object or Status dict
        self._live_orders_lock = asyncio.Lock()
        self._pending_order_keys: set[str] = set()
        # TTL sweep: evict orphaned live_orders entries (missed terminal callbacks)
        self._live_orders_ttl_s: float = float(os.getenv("HFT_LIVE_ORDERS_TTL_S", "300"))  # noqa: duration
        self._live_orders_max_size: int = int(os.getenv("HFT_LIVE_ORDERS_MAX_SIZE", "10000"))
        self._live_orders_last_sweep_s: float = 0.0  # noqa: monotonic timestamp
        self._live_orders_inserted_at: Dict[str, float] = {}  # order_key -> monotonic timestamp
        # Bounded deque: auto-evicts oldest entries when full (OOM protection)
        self._deferred_terminals: collections.deque[tuple[str, str, float]] = collections.deque(maxlen=256)

        # Bug #29: bounded LRU of recently-terminal order_keys, populated by
        # on_terminal_state. Used by CANCEL path to distinguish race-loser
        # (order filled/cancelled just before CANCEL arrived → success) from
        # genuine unknown order_id (typo / strategy bug → preserve WARNING+DLQ).
        self._recently_terminal_orders: collections.OrderedDict[str, tuple[float, str]] = collections.OrderedDict()
        self._recently_terminal_max: int = int(os.getenv("HFT_RECENT_TERMINAL_MAX", "2048"))
        self._recently_terminal_ttl_s: float = float(os.getenv("HFT_RECENT_TERMINAL_TTL_S", "60"))
        self._cancel_inflight_targets: collections.OrderedDict[str, float] = collections.OrderedDict()
        self._cancel_inflight_max: int = int(os.getenv("HFT_CANCEL_INFLIGHT_MAX", "2048"))
        self._cancel_inflight_ttl_s: float = float(os.getenv("HFT_CANCEL_INFLIGHT_TTL_S", "30"))
        # P1-3: ``_recently_terminal_orders`` and ``_cancel_inflight_targets`` are
        # OrderedDicts mutated by the helpers below. They are designed for
        # engine-loop-only use (no lock). Capture the engine thread id lazily on
        # the first call so a misuse from a broker/recorder thread surfaces as
        # ``RuntimeError`` instead of silently corrupting the LRU state.
        self._engine_thread_id: int | None = None

        # Helpers
        self.rate_limiter = RateLimiter(soft_cap=180, hard_cap=250, window_s=10)
        self.circuit_breaker = CircuitBreaker(threshold=5, timeout_s=60)
        self.order_id_resolver = OrderIdResolver(self.order_id_map, lock=self._order_id_map_lock)
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
        # P1-4 hole fix: snapshot of items lifted from _api_pending into the
        # dispatch loop's local `pending` list. Drained per-iteration as each
        # item is finalized; outer cancel/exception handlers release any
        # remainder so dedup slots never leak.
        self._api_inflight: list[OrderCommand] = []
        self._api_worker_task: asyncio.Task | None = None
        self._supports_typed_command_ingress = True
        self._trace_sampler = _get_trace_sampler()

        # Dead Letter Queue for rejected orders
        self._dlq: DeadLetterQueue = get_dlq()

        # Per-symbol rate limiter, per-strategy circuit breaker, shadow mode
        self.per_symbol_rate_limiter = PerSymbolRateLimiter()
        self.strategy_cb_mgr = StrategyCircuitBreakerManager()
        try:
            shadow_batch_size = max(1, int(os.getenv("HFT_SHADOW_ORDER_BATCH_SIZE", "1")))
        except ValueError:
            shadow_batch_size = 1
        self.shadow_sink = ShadowOrderSink(writer=ShadowOrderWriter(batch_size=shadow_batch_size))
        self.platform_degrade_controller = get_shared_platform_degrade_controller(metrics=self.metrics)
        self.position_store: Any = None

        # Idempotency dedup for non-gateway path (avoid double-dedup when Gateway is active)
        _gateway_on = os.getenv("HFT_GATEWAY_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        self._dedup_store: IdempotencyStore | None = None if _gateway_on else IdempotencyStore(persist_enabled=False)

        # Phantom order tracking: timed-out mutating calls that may have succeeded at broker
        # R2-03: Bounded dict with TTL eviction (Architecture Governance Rule 12)
        # M4 (2026-04-25): per-occurrence list. The same (strategy_id,
        # intent_id) can repeat within one process lifetime (DLQ replay,
        # retry, or just intent_id reuse by the strategy) — each occurrence
        # gets its own ``_PhantomEntry`` so neither side overwrites the
        # other. Resolution and cleanup pop FIFO from the per-key list,
        # keeping each occurrence independently traceable.
        self._phantom_records: dict[str, list[_PhantomEntry]] = {}
        # Backwards-compat views maintained by the helpers below. Legacy
        # tests and metric scrapers can still read these dicts; writes
        # through these dicts are picked up by ``_sync_phantom_views``
        # on the next helper call.
        self._phantom_order_keys: dict[str, tuple[float, str]] = {}
        self._phantom_intents: dict[str, OrderIntent] = {}
        self._phantom_order_max: int = 1000
        self._phantom_recovery_ttl_s: float = float(
            os.getenv("HFT_PHANTOM_RECOVERY_TTL_S", "30")
        )
        # P0-E2 + M4: serialises access to ``_phantom_records``. Multiple
        # coroutine tasks (``_call_api``, ``_handle_dispatch_exception``,
        # ``release_stale_phantom_pendings``, ``resolve_phantom_fill``,
        # ``clear_phantom_candidate``, and the over-capacity eviction branch
        # inside ``_call_api``) can observe interleaved reads/writes; the
        # previous unguarded list-comprehension over the records dict
        # inside the capacity eviction path could raise
        # ``RuntimeError: dictionary changed size during iteration`` when a
        # peer task popped keys. ``threading.Lock`` (not asyncio) because
        # ``resolve_phantom_fill`` is documented as callable from other
        # threads in future (it currently runs on the loop, but the lock
        # discipline needs to remain correct if that changes).
        self._phantom_lock = threading.Lock()

        # Pending fill index: maps "{symbol}:{side}" -> [order_key, ...] for strategy_id
        # resolution in deal callbacks where order_id_map has no seed data (Shioaji futures).
        self._pending_fill_index: dict[str, list[str]] = {}
        self._pending_fill_registered_at: dict[str, float] = {}
        self._pending_fill_ttl_s: float = float(os.getenv("HFT_PENDING_FILL_TTL_S", "7200"))  # noqa: duration
        # H6: opt-in strict mode. When enabled, resolve_strategy_from_deal
        # returns None on ambiguity (multiple pending entries for the same
        # symbol+side) so the caller routes to UNKNOWN / DLQ rather than
        # silently misattributing a fill via FIFO pop.
        self._pending_fifo_strict: bool = os.getenv("HFT_PENDING_FIFO_STRICT", "0").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._pending_fill_lock = threading.Lock()  # protects _pending_fill_index across threads
        self._custom_field_counter: int = 0

        # Prevent GC of fire-and-forget tasks (safety orders during HALT drain)
        self._background_tasks: set[asyncio.Task] = set()

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

    def _begin_dispatch_ticket(self, intent: OrderIntent) -> int | None:
        """H1: open a dispatch ticket on StormGuard for HALT-TOCTOU detection.

        Returns the ticket id when StormGuard is wired, or None when no
        StormGuard is bound (unit-test path) or the dispatch is on the
        HALT-allowed path (CANCEL/FORCE_FLAT, halt-exempt strategies, or
        reduce-only intents — these are validated by ``_api_worker``'s
        pre-check and do not need post-dispatch defensive cancellation).
        ``begin_dispatch`` itself reapplies the validation, so a False
        result here also short-circuits the broker call.
        """
        sg = self._storm_guard
        if sg is None:
            return None
        begin = getattr(sg, "begin_dispatch", None)
        if begin is None:
            return None
        try:
            ok, _reason, ticket_id = begin(intent)
        except Exception:  # noqa: BLE001 — never block dispatch on telemetry
            return None
        if not ok:
            # StormGuard rejected at begin — propagate by returning a
            # sentinel-like None plus letting the caller observe via
            # ``ticket_id is None``. The pre-check in ``_api_worker``
            # should have caught this; we still log and rely on the
            # follow-on broker call to be skipped.
            return None
        return ticket_id

    async def _end_dispatch_ticket(
        self,
        ticket_id: int | None,
        intent: OrderIntent,
        trade: Any,
        cmd_id: int,
    ) -> None:
        """H1: close a dispatch ticket and emit a defensive cancel when
        StormGuard transitioned to HALT during the broker await window.

        Constitution constraint: cancels are always allowed during HALT
        (`.agent/rules/25-architecture-governance.md §6`), so the
        defensive cancel is dispatched directly via ``client.cancel_order``
        (NOT ``place_order``). Idempotent w.r.t. ticket_id ``None``.
        """
        if ticket_id is None:
            return
        sg = self._storm_guard
        if sg is None:
            return
        end = getattr(sg, "end_dispatch", None)
        if end is None:
            return
        try:
            halted_during_dispatch = bool(end(ticket_id))
        except Exception:  # noqa: BLE001 — never crash the order path
            return
        if not halted_during_dispatch:
            return
        # HALT triggered while we were awaiting the broker. The broker may
        # have accepted the order. Emit a defensive cancel and bump the
        # dedicated metric so SREs can see TOCTOU recoveries in dashboards.
        try:
            self.metrics.order_halt_post_dispatch_cancel_total.inc()
        except Exception:  # noqa: BLE001 — metric must never block
            pass
        if trade is None or trade is _GUARD_TIMEOUT:
            # The broker call did not produce a trade object — nothing
            # to cancel locally. The caller has already DLQ'd the intent.
            logger.warning(
                "halt_post_dispatch_no_trade",
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                cmd_id=cmd_id,
            )
            return
        logger.warning(
            "halt_post_dispatch_defensive_cancel",
            intent_id=intent.intent_id,
            strategy_id=intent.strategy_id,
            symbol=intent.symbol,
            cmd_id=cmd_id,
        )
        try:
            await asyncio.wait_for(
                self._run_blocking_call(self.client.cancel_order, trade),
                timeout=self._api_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort defensive cancel
            logger.error(
                "halt_post_dispatch_defensive_cancel_failed",
                intent_id=intent.intent_id,
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                cmd_id=cmd_id,
                error=str(exc),
            )

    def _intent_reduces_position(self, intent: OrderIntent) -> bool:
        """Bug 24: True iff intent strictly reduces |net_position|.

        Delegates to StormGuard's wired predicate so we reuse the single source
        of truth (position_provider injected by RiskEngine at bootstrap).
        Conservative fallback (False) when StormGuard is unwired or raises,
        preserving the legacy HALT blocking behaviour.
        """
        sg = self._storm_guard
        if sg is None:
            return False
        check = getattr(sg, "_intent_reduces_position", None)
        if check is None:
            return False
        try:
            return bool(check(intent))
        except Exception:  # noqa: BLE001 — predicate must never raise
            return False

    def set_alias_map(self, alias_map: dict[str, str]) -> None:
        """Set config→actual alias map and build reverse (actual→config).

        Called from bootstrap post-connect hook so the order adapter can
        translate resolved month codes (e.g. TMFE6) back to config codes
        (e.g. TMFR1) that the broker SDK recognises for contract lookup.
        """
        self._actual_to_config = {actual: config for config, actual in alias_map.items()}

    def set_contract_resolver(self, resolver: Any) -> None:
        """Inject the ContractFamilyResolver so the adapter can trust
        ``intent.contract`` when choosing the broker-side code (Gate 3).

        Bootstrap wires this alongside ``set_alias_map`` so new-style intents
        prefer the structured ref while legacy intents fall back to the
        reverse-alias dict unchanged.
        """
        self._contract_resolver = resolver

    def _resolve_broker_contract_code(self, intent: OrderIntent) -> str:
        """Pick the correct broker-side code for ``intent``.

        Preference order (tracked via ``order_contract_code_resolution_total``):
        1. ``resolver_hit``: ``intent.contract.display()`` when a
           ContractFamilyResolver is wired and recognises the ref via its
           ``native_hints`` snapshot. Gate-3 happy path — independent of
           ``_actual_to_config`` being populated by a post-connect hook.
        2. ``alias_fallback``: ``_actual_to_config[intent.symbol]`` — the
           legacy reverse-alias path, retained for legacy intents and
           brokers that have not yet been populated into the resolver.
        3. ``symbol_raw``: ``intent.symbol`` verbatim (last resort).
        """
        contract = getattr(intent, "contract", None)
        resolver = self._contract_resolver
        if contract is not None and resolver is not None:
            try:
                snapshot = resolver.snapshot
                if snapshot.native_hint(contract) is not None:
                    try:
                        self.metrics.order_contract_code_resolution_total.labels(source="resolver_hit").inc()
                    except Exception:  # noqa: BLE001 — metric must never break order path
                        pass
                    return contract.display()
            except Exception:  # noqa: BLE001 — fall back silently
                pass
        if intent.symbol in self._actual_to_config:
            try:
                self.metrics.order_contract_code_resolution_total.labels(source="alias_fallback").inc()
            except Exception:  # noqa: BLE001
                pass
            return self._actual_to_config[intent.symbol]
        try:
            self.metrics.order_contract_code_resolution_total.labels(source="symbol_raw").inc()
        except Exception:  # noqa: BLE001
            pass
        return intent.symbol

    def _send_dispatch_rejection(
        self,
        intent: OrderIntent,
        reason_code: str,
        phantom_pending: bool = False,
    ) -> None:
        """Non-blocking rejection feedback for dispatch failures.

        Bug 23 (2026-04-17): when ``phantom_pending=True``, the intent was
        registered as a phantom candidate (likely reached the broker despite
        the dispatch-path exception). To prevent the strategy from releasing
        its pending counter and emitting a duplicate order (which then causes
        max_pos breach when both phantoms fill), flag ``was_approved=True``
        so strategy-side ``on_risk_feedback`` skips the pending decrement.
        The phantom fill (if any) will arrive later and update state via
        ``on_fill``. If no fill arrives, pending remains elevated — a safe
        liveness loss preferred over an unsafe max_pos breach.
        """
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
                    was_approved=phantom_pending,
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

        NOTE: pending_fill_index is intentionally preserved — broker may replay
        fills for orders that were in-flight at disconnect time, and those fills
        still need strategy_id resolution via the pending fill index.
        """
        async with self._live_orders_lock:
            count = len(self.live_orders)
            if count > 0:
                # Do NOT call _remove_pending_fill — keep index alive for fill replay
                logger.warning(
                    "invalidating_live_orders_after_reconnect",
                    count=count,
                    reason=reason,
                    order_keys=list(self.live_orders.keys())[:10],
                    pending_fill_index_preserved=len(self._pending_fill_index),
                )
                self.live_orders.clear()
                self._pending_order_keys.clear()
                self._live_orders_inserted_at.clear()
            return count

    async def sweep_stale_live_orders(self) -> int:
        """Evict live_orders entries older than TTL (missed terminal callbacks).

        Returns the number of evicted entries.  Rate-limited to once per 60s.
        Called from the supervisor loop.
        """
        now_mono = time.monotonic()
        if now_mono - self._live_orders_last_sweep_s < 60.0:
            return 0
        self._live_orders_last_sweep_s = now_mono
        cutoff = now_mono - self._live_orders_ttl_s
        evicted = 0
        async with self._live_orders_lock:
            stale_keys = [
                k
                for k, ts in self._live_orders_inserted_at.items()
                if ts < cutoff and k not in self._pending_order_keys
            ]
            for k in stale_keys:
                self.live_orders.pop(k, None)
                self._live_orders_inserted_at.pop(k, None)
                self._remove_pending_fill(k)
                evicted += 1
            # Also prune _live_orders_inserted_at for keys no longer in live_orders
            orphaned = [k for k in self._live_orders_inserted_at if k not in self.live_orders]
            for k in orphaned:
                del self._live_orders_inserted_at[k]
        if evicted > 0:
            logger.warning(
                "live_orders_stale_sweep",
                evicted=evicted,
                remaining=len(self.live_orders),
                ttl_s=self._live_orders_ttl_s,
            )
        # Hard cap: if still over max size, evict oldest
        if len(self.live_orders) > self._live_orders_max_size:
            async with self._live_orders_lock:
                sorted_keys = sorted(
                    self._live_orders_inserted_at,
                    key=lambda k: self._live_orders_inserted_at.get(k, 0),
                )
                overflow = len(self.live_orders) - self._live_orders_max_size
                for k in sorted_keys[:overflow]:
                    self.live_orders.pop(k, None)
                    self._live_orders_inserted_at.pop(k, None)
                    evicted += 1
                if overflow > 0:
                    logger.warning(
                        "live_orders_overflow_evicted",
                        evicted=overflow,
                        max_size=self._live_orders_max_size,
                    )
        # Sweep orphaned pending-fill entries that never entered live_orders.
        # These arise when place_order fails before the order is registered in live_orders.
        now_mono = time.monotonic()
        with self._pending_fill_lock:
            expired_keys = [
                k for k, ts in self._pending_fill_registered_at.items() if (now_mono - ts) > self._pending_fill_ttl_s
            ]
            for k in expired_keys:
                self._pending_fill_registered_at.pop(k, None)
                for pkey, pending in list(self._pending_fill_index.items()):
                    filtered = [item for item in pending if item != k]
                    if len(filtered) != len(pending):
                        pending[:] = filtered
                    if not pending:
                        self._pending_fill_index.pop(pkey, None)
            if expired_keys:
                logger.warning(
                    "pending_fill_orphan_sweep",
                    evicted=len(expired_keys),
                    remaining_registered=len(self._pending_fill_registered_at),
                    remaining_index=len(self._pending_fill_index),
                )
                try:
                    self.metrics.pending_fill_expired_total.inc(len(expired_keys))
                except Exception:
                    pass
        return evicted

    async def _handle_dispatch_exception(self, *, intent: OrderIntent, cmd_id: int) -> None:
        """Bug D' (2026-04-20): single-source exception handler for _api_worker.

        Phantom-candidate intents (NEW, FORCE_FLAT) take a hygienic path:
        log at WARNING (not ERROR), and do NOT inflate ``order_reject_total``
        or trip the global circuit breaker — the order may have reached the
        broker and counting it as a real reject corrupts dashboards, fires
        false Telegram CRITICAL alerts, and can stop a healthy strategy.

        Genuine non-phantom dispatch failures keep the original full-reject
        accounting (CB armed, reject metric inc'd, ERROR-level log).
        """
        is_phantom_candidate = intent.intent_type in (IntentType.NEW, IntentType.FORCE_FLAT)
        # Always release dedup so strategy can retry with the same idempotency key
        self._dedup_release(intent.idempotency_key)

        if is_phantom_candidate:
            logger.warning(
                "_api_worker: dispatch failed for phantom candidate",
                cmd_id=cmd_id,
                symbol=intent.symbol,
                exc_info=True,
            )
        else:
            logger.error(
                "_api_worker: dispatch failed for single order",
                cmd_id=cmd_id,
                symbol=intent.symbol,
                exc_info=True,
            )
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            self._update_cb_metric()
            self.strategy_cb_mgr.record_failure(intent.strategy_id)

        # Bug 23: phantom_pending=True keeps strategy pending elevated so the
        # strategy doesn't emit a duplicate while the broker may still fill.
        # The Bug D recovery janitor will release pending after TTL if no
        # callback arrives.
        self._send_dispatch_rejection(
            intent,
            "dispatch_failed",
            phantom_pending=is_phantom_candidate,
        )

        if is_phantom_candidate:
            # M4: append per-occurrence record under _phantom_lock so concurrent
            # ``resolve_phantom_fill`` / ``clear_phantom_candidate`` cannot
            # race with insert + capacity-eviction sweep.
            with self._phantom_lock:
                phantom_key = self._register_phantom(intent)
                if self._phantom_record_count() > self._phantom_order_max:
                    self._evict_stale_phantom_records(max_age_s=3600.0)
            logger.warning(
                "phantom_order_candidate_dispatch_failed",
                strategy_id=intent.strategy_id,
                symbol=intent.symbol,
                order_key=phantom_key,
                cmd_id=cmd_id,
            )
            self.metrics.phantom_order_candidates_total.inc()

        # Clean up live_orders sentinel to prevent permanent slot occupation
        order_key = f"{intent.strategy_id}:{intent.intent_id}"
        async with self._live_orders_lock:
            if self.live_orders.get(order_key) is _PENDING_SENTINEL:
                del self.live_orders[order_key]
                self._pending_order_keys.discard(order_key)
        # NOTE: do NOT remove pending_fill for phantom candidates — the order
        # may have reached the broker and fills arriving later need the
        # pending_fill_index for strategy_id resolution.

    # ── M4: phantom record helpers ────────────────────────────────────────
    # Multi-occurrence phantom storage. ``_phantom_records[key]`` is an
    # append-only list of ``_PhantomEntry``, so the same intent_id reused
    # within one process lifetime cannot overwrite a prior record.
    # ALL writes go through these helpers and ALL reads under ``_phantom_lock``.

    def _get_phantom_records(self) -> dict[str, list[_PhantomEntry]]:
        """M4: lazy accessor for the canonical phantom store.

        Tests construct OrderAdapter via ``__new__`` and seed only the
        slots they exercise. ``_phantom_records`` may not be present on
        those skeleton instances, so every helper that touches the
        canonical store goes through this accessor which auto-initialises
        on first use.
        """
        records = getattr(self, "_phantom_records", None)
        if records is None:
            records = {}
            self._phantom_records = records
        return records

    def _get_phantom_legacy_keys(self) -> dict[str, tuple[float, str]]:
        """M4: lazy accessor for the backwards-compat ``_phantom_order_keys``
        view. Same auto-init rationale as ``_get_phantom_records``.
        """
        view = getattr(self, "_phantom_order_keys", None)
        if view is None:
            view = {}
            self._phantom_order_keys = view
        return view

    def _get_phantom_legacy_intents(self) -> dict[str, OrderIntent]:
        """M4: lazy accessor for the backwards-compat ``_phantom_intents`` view."""
        view = getattr(self, "_phantom_intents", None)
        if view is None:
            view = {}
            self._phantom_intents = view
        return view

    def _register_phantom(self, intent: OrderIntent) -> str:
        """M4: append a new ``_PhantomEntry`` for ``intent``. Multiple
        registrations with the same key (same ``strategy_id:intent_id``)
        accumulate as separate list entries — none overwrites another.
        Returns the canonical phantom key (``"strategy_id:intent_id"``).
        Caller MUST be inside ``_phantom_lock`` already (insert is paired
        with a capacity-eviction sweep).
        """
        phantom_key = f"{intent.strategy_id}:{intent.intent_id}"
        entry = _PhantomEntry(
            monotonic_ts=time.monotonic(),
            symbol=intent.symbol,
            created_ns=timebase.now_ns(),
            intent=intent,
        )
        records = self._get_phantom_records()
        records.setdefault(phantom_key, []).append(entry)
        # Backwards-compat: keep the legacy views aligned with the LAST
        # occurrence per key. The canonical multi-occurrence store remains
        # ``_phantom_records``; legacy code reading these dicts sees the
        # most recent occurrence (the pre-M4 contract).
        self._get_phantom_legacy_keys()[phantom_key] = (entry.monotonic_ts, entry.symbol)
        self._get_phantom_legacy_intents()[phantom_key] = entry.intent
        return phantom_key

    def _evict_stale_phantom_records(self, max_age_s: float = 3600.0) -> int:
        """M4: drop ``_PhantomEntry`` items older than ``max_age_s``.
        Removes empty per-key lists. Caller MUST be inside ``_phantom_lock``.
        Returns the number of entries removed.
        """
        cutoff = time.monotonic() - max_age_s
        removed = 0
        empty_keys: list[str] = []
        records_dict = self._get_phantom_records()
        legacy_keys = self._get_phantom_legacy_keys()
        legacy_intents = self._get_phantom_legacy_intents()
        for key, records in records_dict.items():
            kept = [r for r in records if r.monotonic_ts > cutoff]
            removed += len(records) - len(kept)
            if kept:
                records_dict[key] = kept
                # Resync legacy views to the surviving last occurrence.
                last = kept[-1]
                legacy_keys[key] = (last.monotonic_ts, last.symbol)
                legacy_intents[key] = last.intent
            else:
                empty_keys.append(key)
        for key in empty_keys:
            records_dict.pop(key, None)
            legacy_keys.pop(key, None)
            legacy_intents.pop(key, None)
        return removed

    def _phantom_record_count(self) -> int:
        """M4: total number of ``_PhantomEntry`` items across all keys.
        Used by capacity-eviction. Caller MUST be inside ``_phantom_lock``.
        """
        return sum(len(records) for records in self._get_phantom_records().values())

    def _phantom_drop_key(self, key: str) -> None:
        """M4: drop ALL records for ``key`` and align legacy views.
        Caller MUST be inside ``_phantom_lock``.
        """
        self._get_phantom_records().pop(key, None)
        self._get_phantom_legacy_keys().pop(key, None)
        self._get_phantom_legacy_intents().pop(key, None)

    def _phantom_resync_legacy(self, key: str) -> None:
        """M4: legacy-view sync for ``key`` after popping one occurrence.
        If records remain, point the legacy dicts at the (new) last
        occurrence; if none remain, drop the key from all dicts.
        Caller MUST be inside ``_phantom_lock``.
        """
        records_dict = self._get_phantom_records()
        legacy_keys = self._get_phantom_legacy_keys()
        legacy_intents = self._get_phantom_legacy_intents()
        records = records_dict.get(key)
        if not records:
            records_dict.pop(key, None)
            legacy_keys.pop(key, None)
            legacy_intents.pop(key, None)
            return
        last = records[-1]
        legacy_keys[key] = (last.monotonic_ts, last.symbol)
        legacy_intents[key] = last.intent

    def _phantom_materialize_legacy(self) -> None:
        """M4: build ``_phantom_records`` entries from any legacy
        ``_phantom_order_keys`` rows that lack a canonical record.

        Some tests still seed the legacy view directly (with or without
        a paired ``_phantom_intents`` write); without this materialiser
        ``resolve_phantom_fill`` and ``release_stale_phantom_pendings``
        would silently miss those entries because they iterate the
        canonical store. Caller MUST be inside ``_phantom_lock``.
        """
        records_dict = self._get_phantom_records()
        legacy_keys = self._get_phantom_legacy_keys()
        legacy_intents = self._get_phantom_legacy_intents()
        for key, view in list(legacy_keys.items()):
            if key in records_dict:
                continue
            try:
                ts, symbol = view if isinstance(view, tuple) else (view, "")
            except (TypeError, ValueError):
                continue
            intent = legacy_intents.get(key)
            if intent is None:
                # Synthesise a minimal intent so feedback / FIFO logic
                # has something to work with. Strategy_id is parsed from
                # the key; intent_id is opportunistic (best-effort).
                strat = key.split(":", 1)[0] if ":" in key else key
                rest = key.split(":", 1)[1] if ":" in key else ""
                try:
                    intent_id = int(rest)
                except ValueError:
                    intent_id = 0
                intent = OrderIntent(
                    intent_id=intent_id,
                    strategy_id=strat,
                    symbol=symbol or "",
                    intent_type=IntentType.NEW,
                    side=Side.BUY,
                    price=0,
                    qty=0,
                )
            records_dict[key] = [
                _PhantomEntry(
                    monotonic_ts=float(ts),
                    symbol=symbol,
                    created_ns=0,
                    intent=intent,
                )
            ]

    async def release_stale_phantom_pendings(self, ttl_s: float | None = None) -> int:
        """Bug D (2026-04-20): release strategy pending counters for phantoms past TTL.

        Phantom-pending feedbacks (was_approved=True) keep strategy
        ``_pending_buy/_pending_sell`` counters elevated indefinitely while
        waiting for a fill or cancel callback. When the broker never sends one
        (Shioaji client-side exception that didn't reach the exchange — confirmed
        via Shioaji enumeration during 2026-04-20 incident), the strategy stays
        frozen. After ``ttl_s`` seconds we assume the phantom is orphaned and
        emit a fresh feedback with ``was_approved=False`` so the strategy
        releases the slot.

        Trade-off: if a phantom does later fill, on_fill will create an
        unexpected position update on the strategy. With 30s default TTL this
        is rare (broker callbacks normally arrive sub-second).

        Returns the number of phantoms released. Called from supervisor loop.
        """
        # M4: materialise legacy-only entries (see ``resolve_phantom_fill``).
        with self._phantom_lock:
            self._phantom_materialize_legacy()
        if not self._get_phantom_records():
            return 0
        ttl = ttl_s if ttl_s is not None else self._phantom_recovery_ttl_s
        now_mono = time.monotonic()
        released = 0
        # P0-E2 + M4: snapshot + per-occurrence evict under _phantom_lock,
        # emit feedback after. ``_send_dispatch_rejection`` calls into
        # _rejection_sink.put_nowait; we intentionally release the lock
        # before that to avoid holding the phantom lock across queue I/O.
        expired: list[tuple[str, OrderIntent]] = []
        with self._phantom_lock:
            records_dict = self._get_phantom_records()
            legacy_keys = self._get_phantom_legacy_keys()
            legacy_intents = self._get_phantom_legacy_intents()
            for pkey, records in list(records_dict.items()):
                kept: list[_PhantomEntry] = []
                for record in records:
                    if (now_mono - record.monotonic_ts) < ttl:
                        kept.append(record)
                        continue
                    expired.append((pkey, record.intent))
                if kept:
                    records_dict[pkey] = kept
                    last = kept[-1]
                    legacy_keys[pkey] = (last.monotonic_ts, last.symbol)
                    legacy_intents[pkey] = last.intent
                else:
                    self._phantom_drop_key(pkey)
        for _pkey, intent in expired:
            self._send_dispatch_rejection(
                intent,
                "phantom_recovery_ttl_expired",
                phantom_pending=False,
            )
            released += 1
            try:
                self.metrics.phantom_recovery_releases_total.inc()
            except Exception:
                pass
        if released:
            logger.warning(
                "phantom_recovery_swept",
                released=released,
                ttl_s=ttl,
                remaining=len(self._get_phantom_records()),
            )
        return released

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
        """Return a frozen copy of phantom order keys for reconciliation.

        P0-E2 + M4: snapshots under _phantom_lock so the frozenset construction
        is not racing with a concurrent insert/pop. Returns one entry per key
        regardless of how many occurrences are tracked under that key (the
        external reconciler does not care about per-occurrence counts).
        """
        with self._phantom_lock:
            return frozenset(self._get_phantom_records().keys())

    def clear_phantom_candidate(self, key: str) -> None:
        """Remove ALL phantom records for ``key`` after reconciliation confirms
        resolution.

        P0-E2 + M4: pop entire per-key list atomically under _phantom_lock so
        reconciliation never sees a half-cleared entry. Idempotent w.r.t.
        unknown keys.
        """
        with self._phantom_lock:
            self._phantom_drop_key(key)

    def resolve_phantom_fill(self, fill_event: Any) -> str | None:
        """Attempt to resolve an orphaned fill against phantom order candidates.

        Phantom orders are dispatch-failed orders that may have actually reached
        the broker. This method checks if any phantom candidate matches the fill
        by symbol and side, returning the strategy_id if found.

        P0-E2 + M4: phantom dict mutations are now serialised under
        ``_phantom_lock`` and operate on per-occurrence ``_PhantomEntry`` lists.
        A single fill resolves a single FIFO occurrence — sibling occurrences
        for the same intent_id (re-submitted within the same process) remain
        intact and resolvable independently.

        Returns strategy_id or None.
        """
        # M4: materialise any legacy-only entries so the canonical-store
        # iteration below can resolve them (test fixtures still seed
        # ``_phantom_order_keys`` directly without writing through the
        # helpers).
        with self._phantom_lock:
            self._phantom_materialize_legacy()
        if not self._get_phantom_records():
            return None
        symbol = getattr(fill_event, "symbol", "")
        side = getattr(fill_event, "side", None)
        if not symbol:
            return None
        # Bug 16: None side on a fill event is a data integrity error. Previously
        # defaulted to BUY which could misattribute phantom fills. Skip resolution
        # and log; upstream reconciliation will surface the orphan via metrics.
        if side is None:
            logger.warning(
                "phantom_fill_resolution_skipped_none_side",
                symbol=symbol,
                fill_event=repr(fill_event),
            )
            return None
        # Check pending_fill_index first — it has symbol+side specificity.
        # Lock order: _pending_fill_lock → _phantom_lock (no reverse path).
        side_name = "SELL" if int(side) == 1 else "BUY"
        pf_key = f"{symbol}:{side_name}"
        with self._pending_fill_lock:
            pending = self._pending_fill_index.get(pf_key)
            if pending:
                # Found a pending fill entry — pop FIFO and resolve
                order_key = pending.pop(0)
                self._pending_fill_registered_at.pop(order_key, None)
                if not pending:
                    del self._pending_fill_index[pf_key]
                strategy_id = order_key.split(":", 1)[0] if ":" in order_key else order_key
                # M4: pop ONE phantom occurrence (FIFO) for ``order_key`` so a
                # sibling occurrence (same intent_id, second submission) stays
                # resolvable for its own fill.
                with self._phantom_lock:
                    records = self._get_phantom_records().get(order_key)
                    if records:
                        records.pop(0)
                        self._phantom_resync_legacy(order_key)
                logger.warning(
                    "phantom_fill_resolved_via_pending_index",
                    symbol=symbol,
                    side=side_name,
                    order_key=order_key,
                    strategy_id=strategy_id,
                )
                return strategy_id
        # Fallback: match phantom key with verified symbol match.
        now_mono = time.monotonic()
        with self._phantom_lock:
            # Snapshot inside the lock; process candidates below (still holding
            # the lock for the matching pop to avoid a concurrent task stealing
            # the same key between our match decision and the pop).
            for pkey, records in list(self._get_phantom_records().items()):
                if not records:
                    continue
                # FIFO: examine the oldest record first. A symbol mismatch
                # means we cannot use ANY occurrence under this key (they
                # all share the same symbol since the same intent_id from
                # the same strategy hits the same symbol), so move on.
                head = records[0]
                # Skip entries older than 2 hours (stale phantoms)
                if (now_mono - head.monotonic_ts) > 7200.0:
                    continue
                # C-2 fix: MUST verify symbol match to prevent cross-strategy misattribution
                if head.symbol and head.symbol != symbol:
                    continue
                strategy_id = pkey.split(":", 1)[0] if ":" in pkey else pkey
                # M4: pop ONE occurrence FIFO; siblings retained.
                records.pop(0)
                self._phantom_resync_legacy(pkey)
                logger.warning(
                    "phantom_fill_resolved_via_phantom_keys",
                    symbol=symbol,
                    side=side_name,
                    phantom_key=pkey,
                    strategy_id=strategy_id,
                )
                return strategy_id
        return None

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
                    try:
                        self.metrics.order_reject_total.inc()
                    except Exception:  # noqa: BLE001
                        pass
                    self._dedup_release(cmd.intent.idempotency_key)
                    await self._add_to_dlq(
                        cmd.intent,
                        RejectionReason.DEADLINE_EXCEEDED,
                        "DEADLINE_EXPIRED",
                    )
                    self._send_dispatch_rejection(cmd.intent, "dispatch_deadline_expired")
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
            try:
                self.shadow_sink.flush()
            except Exception as exc:  # noqa: BLE001
                logger.warning("shadow_sink_flush_failed", error=str(exc))
            logger.info("OrderAdapter stopped")

    def check_rate_limit(self) -> bool:
        """Sliding window check."""
        return self.rate_limiter.check()

    # ── P1-8: order_id_map mutation helpers ────────────────────────────────
    # Every write into ``self.order_id_map`` MUST go through these helpers so
    # the lock discipline cannot drift. ``_order_id_map_lock`` is an RLock so
    # batch writers (`_register_broker_ids`) can hold it while delegating to
    # the helpers without deadlocking. Each call emits a ``debug`` log with
    # the ``source=`` argument for forensic tracing of the mutation site.

    def _get_order_id_meta(self) -> dict[str, tuple[int, str]]:
        """H3: lazy accessor for the metadata sidecar.

        Tests construct OrderAdapter via ``__new__`` and seed only the
        slots they exercise. ``_order_id_meta`` may not be present on
        those skeleton instances, so every helper that touches the
        sidecar goes through this accessor which auto-initialises on
        first use. ``getattr`` guard keeps the production path
        zero-overhead.
        """
        meta = getattr(self, "_order_id_meta", None)
        if meta is None:
            meta = {}
            self._order_id_meta = meta
        return meta

    def _set_order_id_mapping(
        self,
        token: str,
        order_key: str,
        *,
        source: str,
        created_ns: int | None = None,
        state: str = "live",
    ) -> None:
        """Set ``order_id_map[token] = order_key`` under the shared RLock.

        H3: also stamps ``_order_id_meta[token] = (created_ns, state)`` so
        the persistence layer can drop terminal/expired entries on
        restart. ``created_ns`` defaults to ``timebase.now_ns()`` for new
        registrations; the loader supplies the persisted timestamp so a
        round-trip preserves age. ``source`` is a stable identifier for
        the call site, surfaced in the debug log to help correlate broker-ID
        registration with downstream order_key resolution failures.
        """
        ts_ns = int(created_ns) if created_ns is not None else timebase.now_ns()
        meta = self._get_order_id_meta()
        with self._order_id_map_lock:
            self.order_id_map[token] = order_key
            meta[token] = (ts_ns, state)
        logger.debug(
            "order_id_map_set",
            token=token,
            order_key=order_key,
            source=source,
            t_ns=ts_ns,
            state=state,
        )

    def _del_order_id_mapping(self, token: str, *, source: str) -> str | None:
        """Pop ``order_id_map[token]`` under the shared RLock.

        Returns the prior mapped value (or ``None`` if absent). Logs the
        delete with ``source=`` for the same forensic reasons as the setter.
        H3: also drops the metadata sidecar entry.
        """
        meta = self._get_order_id_meta()
        with self._order_id_map_lock:
            prior = self.order_id_map.pop(token, None)
            meta.pop(token, None)
        logger.debug("order_id_map_del", token=token, prior=prior, source=source)
        return prior

    def _mark_order_id_terminal(self, order_key: str) -> int:
        """H3: flag every broker_id mapped to ``order_key`` as terminal.

        Called from ``on_terminal_state`` so the next persist drops these
        rows, preventing a future restart from resurrecting (broker_id ->
        order_key) bindings whose underlying order is already done — which
        is the ABA window the broker exploits when it re-uses a numeric id.
        Returns the number of entries marked. Idempotent.
        """
        meta = self._get_order_id_meta()
        marked = 0
        with self._order_id_map_lock:
            for token, mapped in list(self.order_id_map.items()):
                if mapped != order_key:
                    continue
                prior = meta.get(token)
                if prior is None:
                    meta[token] = (timebase.now_ns(), "terminal")
                else:
                    meta[token] = (prior[0], "terminal")
                marked += 1
        if marked:
            logger.debug(
                "order_id_map_marked_terminal",
                order_key=order_key,
                count=marked,
            )
        return marked

    def _load_order_id_map(self) -> None:
        """Load order_id_map from disk on startup (restart-safe strategy resolution).

        H3: supports two schemas:

        * Old ``{k, v}`` — pre-H3 format. Treated as terminal-stale and
          dropped on first load. Old persisted state is inherently stale
          (no creation timestamp, no terminal-state tracking); resurrecting
          it can revive a (broker_id -> order_key) binding whose underlying
          order is long gone, which is the ABA attack vector the new
          schema closes.
        * New ``{k, v, t_ns, s}`` — H3 format. Filters entries where
          ``s == "terminal"`` or ``now_ns - t_ns > _order_id_map_ttl_ns``.
          Surviving entries are inserted with their persisted ``t_ns`` so
          their age clock continues across the restart.
        """
        path = self._order_id_map_persist_path
        if not os.path.exists(path):
            return
        try:
            import orjson

            loaded = 0
            skipped_terminal = 0
            skipped_ttl = 0
            skipped_legacy = 0
            now_ns = timebase.now_ns()
            ttl_ns = getattr(self, "_order_id_map_ttl_ns", 86400 * 1_000_000_000)
            with open(path, "rb") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        obj = orjson.loads(raw)
                    except Exception:
                        continue
                    if not (isinstance(obj, dict) and "k" in obj and "v" in obj):
                        continue
                    token = str(obj["k"])
                    order_key = str(obj["v"])
                    t_ns_raw = obj.get("t_ns")
                    state_raw = obj.get("s")
                    if t_ns_raw is None or state_raw is None:
                        # Legacy schema — drop. Pre-H3 entries have no age
                        # info, so we cannot honour TTL for them; skipping
                        # is the conservative choice (a missing mapping
                        # falls back to UNKNOWN attribution downstream,
                        # which is far safer than an ABA misattribution).
                        skipped_legacy += 1
                        continue
                    try:
                        t_ns = int(t_ns_raw)
                    except (TypeError, ValueError):
                        skipped_legacy += 1
                        continue
                    state = str(state_raw)
                    if state != "live":
                        skipped_terminal += 1
                        continue
                    if ttl_ns > 0 and (now_ns - t_ns) > ttl_ns:
                        skipped_ttl += 1
                        continue
                    self._set_order_id_mapping(
                        token,
                        order_key,
                        source="persisted_load",
                        created_ns=t_ns,
                        state=state,
                    )
                    loaded += 1
            # Enforce max size
            while len(self.order_id_map) > self._order_id_map_max_size:
                first_key = next(iter(self.order_id_map))
                self._del_order_id_mapping(first_key, source="persisted_evict")
            logger.info(
                "order_id_map_loaded",
                count=loaded,
                path=path,
                skipped_terminal=skipped_terminal,
                skipped_ttl=skipped_ttl,
                skipped_legacy=skipped_legacy,
            )
        except Exception as exc:
            logger.warning("order_id_map_load_failed", error=str(exc), path=path)

    def persist_order_id_map(self) -> None:
        """Persist order_id_map to disk atomically (temp+fsync+rename).

        Called during graceful shutdown. Safe to call from thread pool.

        H3: writes the new schema ``{k, v, t_ns, s}`` and filters out any
        entry whose state has been flipped to ``"terminal"`` (via
        ``_mark_order_id_terminal``). Terminal entries are dropped from
        disk so a subsequent restart cannot resurrect a stale binding —
        even though the broker may still echo the old broker_id when it
        re-uses the numeric id (the ABA attack vector). Snapshotting under
        the lock guarantees the state and order_id_map views agree.
        """
        path = self._order_id_map_persist_path
        # Snapshot under the lock so the order_id_map and metadata views agree.
        meta_dict = self._get_order_id_meta()
        with self._order_id_map_lock:
            snapshot: list[tuple[str, str, int, str]] = []
            now_ns = timebase.now_ns()
            for k, v in self.order_id_map.items():
                meta = meta_dict.get(k)
                if meta is None:
                    # Defensive: unmetered entry (direct mutation, legacy
                    # callsite, or test path) — stamp as live with the
                    # current timestamp so a future load applies TTL only.
                    snapshot.append((k, v, now_ns, "live"))
                    continue
                t_ns, state = meta
                if state == "terminal":
                    continue  # H3: drop terminals from the persisted snapshot
                snapshot.append((k, v, t_ns, state))
        try:
            import orjson

            persist_dir = os.path.dirname(path) or "."
            os.makedirs(persist_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=persist_dir)
            try:
                with os.fdopen(fd, "wb") as f:
                    for k, v, t_ns, state in snapshot:
                        f.write(
                            orjson.dumps({"k": k, "v": v, "t_ns": t_ns, "s": state})
                            + b"\n"
                        )
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, path)
            finally:
                # M2 (2026-04-25): finally-cleanup so orphan tmpfiles don't
                # accumulate when the worker dies between fsync and rename.
                if os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            logger.info("order_id_map_persisted", count=len(snapshot), path=path)
        except Exception as exc:
            logger.warning("order_id_map_persist_failed", error=str(exc), path=path)

    def _maybe_persist_order_id_map(self, *, force: bool = False) -> None:
        """Throttle order-id checkpointing to bound crash-recovery loss.

        Offloads the synchronous fsync to the default thread pool executor
        to avoid blocking the event loop (Constitution Law 3).
        """
        now_s = time.monotonic()
        if not force and self._order_id_map_persist_interval_s > 0:
            if (now_s - self._order_id_map_last_persist_s) < self._order_id_map_persist_interval_s:
                return
        self._order_id_map_last_persist_s = now_s
        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, self.persist_order_id_map)
        except RuntimeError:
            # No running loop (shutdown or non-async context) — persist inline
            self.persist_order_id_map()

    def _next_custom_field_token(self) -> str:
        """Allocate a 6-char broker-safe token without reusing current map keys."""
        attempts = 0
        while attempts <= max(len(self.order_id_map) + 1, 16):
            token = f"{self._custom_field_counter:06X}"[-6:]
            self._custom_field_counter += 1
            if token not in self.order_id_map:
                return token
            attempts += 1
        return f"{time.monotonic_ns() & 0xFFFFFF:06X}"

    @staticmethod
    def _pending_fill_key(symbol: str, side: Side) -> str:
        return f"{symbol}:{side.name}"

    async def _register_pending_fill(self, order_key: str, symbol: str, side: Side, custom_field_token: str) -> None:
        """Register strong and weak early-fill correlation before broker IDs exist.

        Uses threading.Lock (not asyncio.Lock) because resolve_strategy_from_deal()
        is called from the broker callback thread. The critical section is sub-μs
        (dict/list ops only). Both pending_fill_index AND order_id_map are updated
        atomically under the same lock to eliminate the window where a fill could
        arrive between the two registrations.

        P0-E1: acquires ``_order_id_map_lock`` in addition to the pending-fill
        lock so the single write at ``self.order_id_map[custom_field_token]``
        is visible to broker-thread readers under the same lock discipline as
        ``_register_broker_ids``. Lock order is strictly
        ``_pending_fill_lock`` → ``_order_id_map_lock``; no reverse path.
        """
        key = self._pending_fill_key(symbol, side)
        with self._pending_fill_lock:
            pending = self._pending_fill_index.setdefault(key, [])
            if order_key not in pending:
                pending.append(order_key)
            self._pending_fill_registered_at[order_key] = time.monotonic()
            # P1-8: route through the shared helper so every writer site has
            # the same lock + audit-log discipline. Lock order is preserved
            # (``_pending_fill_lock`` already held → helper takes the
            # re-entrant ``_order_id_map_lock``).
            self._set_order_id_mapping(
                custom_field_token, order_key, source="register_pending_fill"
            )
        self._maybe_persist_order_id_map()

    def _remove_pending_fill(self, order_key: str) -> None:
        """Drop stale pending-fill fallback state for an order."""
        with self._pending_fill_lock:
            self._pending_fill_registered_at.pop(order_key, None)
            empty_keys: list[str] = []
            for key, pending in self._pending_fill_index.items():
                filtered = [item for item in pending if item != order_key]
                if len(filtered) != len(pending):
                    pending[:] = filtered
                if not pending:
                    empty_keys.append(key)
            for key in empty_keys:
                self._pending_fill_index.pop(key, None)

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
                _is_exempt = bool(_sid) and self._is_strategy_halt_exempt(str(_sid))
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
                self._background_tasks.add(_task)
                _task.add_done_callback(self._background_tasks.discard)
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

    def _assert_engine_thread(self) -> None:
        """P1-3: enforce engine-loop-only access to terminal LRU trackers.

        ``_recently_terminal_orders`` and ``_cancel_inflight_targets`` are plain
        ``OrderedDict`` instances without locks because every legitimate caller
        (``_record_recent_terminal``, ``_prune_cancel_inflight``,
        ``_mark_cancel_inflight``, ``_clear_cancel_inflight``,
        ``_is_recently_terminal``) runs on the asyncio loop thread. If a future
        edit calls one from the broker callback thread or a worker pool, the
        ``move_to_end`` / ``popitem`` mutations would race silently. This guard
        lazily pins the engine thread id on first use and raises afterwards on
        mismatch so the regression is loud.
        """
        tid = threading.get_ident()
        # ``__slots__`` storage: tests that build adapters via ``__new__``
        # without invoking ``__init__`` may not have the slot populated.
        # ``getattr`` with default keeps the helper robust to both paths.
        engine_tid = getattr(self, "_engine_thread_id", None)
        if engine_tid is None:
            self._engine_thread_id = tid
            return
        if tid != engine_tid:
            raise RuntimeError(
                "OrderAdapter terminal-tracking accessed from non-engine thread "
                f"(expected={engine_tid} got={tid})"
            )

    def _record_recent_terminal(self, order_key: str, reason: str) -> None:
        """Bug #29: remember recently-terminal order_keys for idempotent cancel.
        Bounded LRU + TTL eviction; called next to live_orders deletion."""
        self._assert_engine_thread()
        self._clear_cancel_inflight(order_key)
        now = time.monotonic()
        self._recently_terminal_orders[order_key] = (now, reason)
        self._recently_terminal_orders.move_to_end(order_key)
        cutoff = now - self._recently_terminal_ttl_s
        while self._recently_terminal_orders:
            oldest_key = next(iter(self._recently_terminal_orders))
            ts, _ = self._recently_terminal_orders[oldest_key]
            if ts < cutoff or len(self._recently_terminal_orders) > self._recently_terminal_max:
                self._recently_terminal_orders.popitem(last=False)
            else:
                break

    def _is_recently_terminal(self, order_key: str) -> bool:
        self._assert_engine_thread()
        entry = self._recently_terminal_orders.get(order_key)
        if entry is None:
            return False
        ts, _ = entry
        if time.monotonic() - ts > self._recently_terminal_ttl_s:
            self._recently_terminal_orders.pop(order_key, None)
            return False
        return True

    def _prune_cancel_inflight(self) -> None:
        self._assert_engine_thread()
        if not self._cancel_inflight_targets:
            return
        cutoff = time.monotonic() - self._cancel_inflight_ttl_s
        while self._cancel_inflight_targets:
            oldest_key = next(iter(self._cancel_inflight_targets))
            ts = self._cancel_inflight_targets[oldest_key]
            if ts < cutoff or len(self._cancel_inflight_targets) > self._cancel_inflight_max:
                self._cancel_inflight_targets.popitem(last=False)
            else:
                break

    def _is_cancel_inflight(self, target_key: str) -> bool:
        self._prune_cancel_inflight()
        return target_key in self._cancel_inflight_targets

    def _mark_cancel_inflight(self, target_key: str) -> None:
        self._prune_cancel_inflight()
        self._cancel_inflight_targets[target_key] = time.monotonic()
        self._cancel_inflight_targets.move_to_end(target_key)

    def _clear_cancel_inflight(self, target_key: str) -> None:
        self._assert_engine_thread()
        self._cancel_inflight_targets.pop(target_key, None)

    async def on_terminal_state(self, strategy_id: str, order_id: str) -> None:
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        resolved_order_key = f"{strategy_id}:{order_id}" if order_id is not None else f"{strategy_id}:"
        async with self._live_orders_lock:
            order_key = self.order_id_resolver.resolve_order_key(strategy_id, order_id, self.live_orders)
            resolved_order_key = order_key
            entry = self.live_orders.get(order_key)

            if entry is not None and entry is not _PENDING_SENTINEL:
                # Normal path — order is registered, clean up
                logger.info("Removing terminal order", key=order_key)
                del self.live_orders[order_key]
                self._record_recent_terminal(order_key, reason="terminal")
                # Clean up e2e latency tracking entry (SLO-2)
                self._cmd_created_ns_map.pop(order_key, None)
                # Clean up TCA price tracking entry
                self._cmd_tca_map.pop(order_key, None)
                self._remove_pending_fill(order_key)
                return

            # Check if any order for this strategy is in-flight
            has_pending = any(k.startswith(f"{strategy_id}:") for k in self._pending_order_keys)
            if has_pending:
                if len(self._deferred_terminals) == self._deferred_terminals.maxlen:
                    # Evicted entry's order will linger in live_orders until TTL sweep.
                    # Proactively clean it up to avoid blocking new orders for that strategy.
                    evicted = self._deferred_terminals[0]  # will be popped by append
                    evicted_key = f"{evicted[0]}:{evicted[1]}"
                    self.live_orders.pop(evicted_key, None)
                    self._live_orders_inserted_at.pop(evicted_key, None)
                    self._cmd_created_ns_map.pop(evicted_key, None)
                    self._cmd_tca_map.pop(evicted_key, None)
                    self.metrics.deferred_terminal_overflow_total.inc()
                    logger.error(
                        "deferred_terminal_overflow",
                        strategy_id=strategy_id,
                        broker_order_id=order_id,
                        evicted_key=evicted_key,
                        msg="Oldest deferred terminal evicted — live_orders entry cleaned up",
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
                self._record_recent_terminal(order_key, reason="terminal")
        self._remove_pending_fill(resolved_order_key)

    def register_broker_ids_bulk(self, ids: Iterable[str], order_key: str) -> bool:
        """P0-E1: external-writer helper that acquires ``_order_id_map_lock``.

        Callable from any thread (CPython ``threading.RLock`` is re-entrant
        safe; our critical sections are non-nested except where a batch writer
        delegates per-key writes through ``_set_order_id_mapping``).
        ExecutionRouter's ``_backfill_order_id_map`` calls this instead of
        mutating ``order_id_map`` directly so there is a single writer-side
        lock discipline across Decision and Execution planes.

        Returns True if any new mapping was added.
        """
        changed = False
        # P1-8: hold the RLock across the whole batch so the get/set pair is
        # atomic (no peer writer can slip a different value between the
        # ``.get(key)`` check and the helper's write). The helper re-acquires
        # the same RLock per write; re-entrant acquire is O(1).
        with self._order_id_map_lock:
            for broker_id in ids:
                key = str(broker_id or "")
                if not key:
                    continue
                if self.order_id_map.get(key) == order_key:
                    continue
                self._set_order_id_mapping(key, order_key, source="register_broker_ids_bulk")
                changed = True
        return changed

        # Also clean up rate limit window if needed? No, rate limit is distinct.

    async def _register_broker_ids(self, order_key: str, trade: Any) -> bool:
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

        changed = False
        # P0-E1: synchronous lock, because broker-thread readers (_on_exec)
        # also take this lock. All critical sections here are dict ops only.
        # P1-8: ``_order_id_map_lock`` is now an RLock so the per-key writes
        # below can re-route through ``_set_order_id_mapping`` /
        # ``_del_order_id_mapping`` (which take the lock again) without
        # deadlocking. This ensures every mutation path emits a uniform
        # ``order_id_map_set`` / ``order_id_map_del`` debug log keyed by
        # ``source=``.
        with self._order_id_map_lock:
            # Evict oldest entries if at limit — skip entries whose order_key
            # is still in live_orders to prevent orphaning active fills (M6).
            if len(self.order_id_map) >= self._order_id_map_max_size:
                evict_target = max(1, len(self.order_id_map) // 10)
                evicted = 0
                for k in list(self.order_id_map.keys()):
                    if evicted >= evict_target:
                        break
                    mapped_key = self.order_id_map[k]
                    # Protect entries linked to live orders or pending fills (recently placed)
                    if mapped_key in self.live_orders:
                        continue
                    if mapped_key in self._pending_fill_registered_at:
                        continue
                    self._del_order_id_mapping(k, source="register_broker_ids_evict")
                    evicted += 1
                    changed = True
                logger.info("Evicted stale order IDs", count=evicted, remaining=len(self.order_id_map))

            for oid in ids:
                oid_key = str(oid)
                if self.order_id_map.get(oid_key) == order_key:
                    continue
                self._set_order_id_mapping(oid_key, order_key, source="register_broker_ids")
                changed = True
        if changed:
            self._maybe_persist_order_id_map()
        return bool(ids)

    def resolve_strategy_from_deal(self, symbol: str, action: str) -> str | None:
        """Resolve strategy_id from pending fills index.

        Called from broker thread (_on_exec) for deal callbacks where
        order_id_map has no seed data (Shioaji futures: place_order returns
        empty broker IDs).

        Thread-safe: uses _pending_fill_lock to protect list mutations
        against concurrent access from the main asyncio loop.

        Returns strategy_id string or None if no pending order matches.
        """
        action_text = str(action).lower()
        side = "SELL" if "sell" in action_text or action == -1 else "BUY"
        key = f"{symbol}:{side}"
        with self._pending_fill_lock:
            pending = self._pending_fill_index.get(key)
            now = time.monotonic()
            while pending:
                head = pending[0]
                registered_at = self._pending_fill_registered_at.get(head, now)
                if (now - registered_at) < self._pending_fill_ttl_s:
                    break
                pending.pop(0)
                self._pending_fill_registered_at.pop(head, None)
                try:
                    self.metrics.pending_fill_expired_total.inc()
                except Exception:
                    pass  # metric may not exist during tests
                logger.warning("pending_fill_index_expired", symbol=symbol, action=action, order_key=head)
            if not pending:
                return None
            remaining_before_pop = len(pending)
            # H6: under strict mode, refuse ambiguous FIFO pops so the
            # caller can DLQ / UNKNOWN-route the fill rather than silently
            # misattribute. Permissive mode (default) preserves the legacy
            # behaviour — FIFO head wins, warning emitted.
            if remaining_before_pop > 1 and self._pending_fifo_strict:
                logger.warning(
                    "pending_fill_fifo_ambiguous_blocked",
                    symbol=symbol,
                    action=action,
                    candidates=list(pending),
                    msg="strict mode — fill refused to avoid cross-strategy misattribution",
                )
                try:
                    self.metrics.pending_fill_ambiguous_blocked_total.inc()
                except Exception:  # noqa: BLE001
                    pass
                return None
            # FIFO pop: takes the oldest pending order for this symbol+side.
            order_key = pending.pop(0)
            self._pending_fill_registered_at.pop(order_key, None)
            if remaining_before_pop > 1:
                logger.warning(
                    "pending_fill_fifo_ambiguous",
                    symbol=symbol,
                    action=action,
                    chosen_key=order_key,
                    remaining_candidates=len(pending),
                    msg="FIFO fallback with multiple candidates — fill may be misattributed",
                )
            if not pending:
                del self._pending_fill_index[key]
        # order_key is "STRATEGY_ID:intent_id"
        strategy_id = order_key.split(":", 1)[0] if ":" in order_key else order_key
        logger.info(
            "pending_fill_index_resolved",
            symbol=symbol,
            action=action,
            order_key=order_key,
            strategy_id=strategy_id,
        )
        return strategy_id

    def resolve_strategy_from_deal_candidates(self, symbols: list[str], action: str) -> str | None:
        for symbol in symbols:
            if not symbol:
                continue
            resolved = self.resolve_strategy_from_deal(symbol, action)
            if resolved:
                return resolved
        return None

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
                    self._remove_pending_fill(resolved)
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
        # Bug 24 (2026-04-17): reducing cover orders also bypass (mirrors Bug 21/22
        # reduce-only bypass at validator / Gateway layers — adapter is the last gate
        # and must remain consistent under HALT, else cover orders stranded in DLQ).
        _halt_exempt = (
            intent.intent_type == IntentType.CANCEL
            or intent.intent_type == IntentType.FORCE_FLAT
            or self._is_strategy_halt_exempt(intent.strategy_id)
            or self._intent_reduces_position(intent)
        )
        if _is_halt and not _halt_exempt:
            await self._add_to_dlq(
                intent,
                RejectionReason.STORMGUARD_HALT,
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
                await self._add_to_dlq(intent, RejectionReason.IDEMPOTENCY_DUPLICATE, "Duplicate idempotency_key")
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
                await self._add_to_dlq(intent, RejectionReason.PLATFORM_REDUCE_ONLY, "Platform is in reduce-only mode")
                return
            self._reserve_platform_reduce_only_close(intent)

            # Shadow mode intercept (WU-10)
            if self.shadow_sink.enabled:
                self.shadow_sink.intercept(intent)
                self.per_symbol_rate_limiter.record(intent.symbol)
                self._send_dispatch_rejection(intent, "shadow_intercepted", phantom_pending=False)
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
        """Check if a strategy is halt-exempt via StormGuard's public API.

        Uses ``StormGuard.is_halt_exempt`` exclusively; the prior
        ``getattr(sg, "_halt_exempt_strategies", ...)`` fallback reached past
        the public surface into ``__slots__``-protected state and would
        silently approve every strategy if the slot were renamed.
        Removed 2026-04-27 alongside RC-3.
        """
        sg = self._storm_guard
        if sg is None:
            return False
        return bool(sg.is_halt_exempt(strategy_id))

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
        cancel_target_key = ""
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
                    await self._add_to_dlq(intent, RejectionReason.BROKER_CODEC_MISSING, "no_broker_codec")
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

                # TIF Mapping via broker codec
                tif_str = self._broker_codec.encode_tif(intent.tif)

                # D2: Pre-register sentinel to track in-flight order
                async with self._live_orders_lock:
                    self.live_orders[order_key] = _PENDING_SENTINEL
                    self._pending_order_keys.add(order_key)
                    self._live_orders_inserted_at[order_key] = time.monotonic()

                c_field = self._next_custom_field_token()
                await self._register_pending_fill(order_key, intent.symbol, intent.side, c_field)

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
                    self._remove_pending_fill(order_key)
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
                        self._remove_pending_fill(order_key)
                        return False

                # Resolve the broker-side contract code. Gate 3 prefers
                # ``intent.contract.display()`` when the resolver has a native
                # hint for it (structured path); falls back to the reverse
                # alias dict for legacy intents. See
                # :meth:`_resolve_broker_contract_code` for ordering rationale.
                order_contract_code = self._resolve_broker_contract_code(intent)
                # H1: open a StormGuard ticket so a HALT triggered during the
                # broker await window is observable post-dispatch and we can
                # emit a defensive cancel for an order that may have reached
                # the broker after we were told to stop. ``ticket_id`` is None
                # when StormGuard is unwired (unit tests) — disabling the
                # post-dispatch check is safe because there is no HALT source.
                ticket_id = self._begin_dispatch_ticket(intent)
                trade: Any = None
                try:
                    trade = await self._call_api(
                        "place_order",
                        self.client.place_order,
                        contract_code=order_contract_code,
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
                finally:
                    # F-D1: ticket MUST be closed even when the broker call
                    # raises (OSError/TimeoutError/ConnectionError/RuntimeError
                    # are caught by the outer except). Without this finally,
                    # exception-failed dispatches leak tickets in
                    # _inflight_dispatch_tickets forever. trade is None on
                    # exception → _end_dispatch_ticket skips the defensive
                    # cancel branch (no broker artefact to cancel).
                    await self._end_dispatch_ticket(ticket_id, intent, trade, cmd.cmd_id)
                if trade is None or trade is _GUARD_TIMEOUT:
                    _is_timeout = trade is _GUARD_TIMEOUT
                    _fail_reason = "api_timeout" if _is_timeout else "api_failure"
                    _dlq_reason = RejectionReason.API_TIMEOUT if _is_timeout else RejectionReason.CONNECTION_ERROR
                    async with self._live_orders_lock:
                        self.live_orders.pop(order_key, None)
                        self._pending_order_keys.discard(order_key)
                    if not _is_timeout:
                        self._remove_pending_fill(order_key)
                    self.metrics.order_reject_total.inc()
                    self._dedup_commit(intent.idempotency_key, False, _fail_reason, cmd.cmd_id)
                    await self._add_to_dlq(intent, _dlq_reason, _fail_reason)
                    return False

                self.metrics.order_actions_total.labels(type="new").inc()
                # Bug #35 (2026-04-21): Removed `trade.timestamp = ...` setattr
                # block. Shioaji Trade is a strict Pydantic model that rejects
                # unknown fields with ValueError, generating one warning per
                # order with no benefit — TTL tracking already uses the
                # `_live_orders_inserted_at` sidecar populated at line 1730.

                # Register broker IDs BEFORE removing from pending keys so that
                # fast fill callbacks arriving during this window are still
                # deferred (DECISION-007 race fix).
                has_broker_ids = await self._register_broker_ids(order_key, trade)
                if has_broker_ids:
                    self._remove_pending_fill(order_key)

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
                self._audit_log_order(
                    {
                        "event": "dispatched",
                        "intent_type": "NEW",
                        "order_key": order_key,
                        "symbol": intent.symbol,
                        "side": str(intent.side),
                        "price": intent.price,
                        "qty": intent.qty,
                        "strategy_id": intent.strategy_id,
                        "cmd_id": int(cmd.cmd_id),
                    }
                )

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

                order_key = f"{intent.strategy_id}:{intent.intent_id}"
                async with self._live_orders_lock:
                    self.live_orders[order_key] = _PENDING_SENTINEL
                    self._pending_order_keys.add(order_key)
                    self._live_orders_inserted_at[order_key] = time.monotonic()

                c_field = self._next_custom_field_token()
                await self._register_pending_fill(order_key, intent.symbol, close_side, c_field)

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
                    if trade is not _GUARD_TIMEOUT:
                        self._remove_pending_fill(order_key)
                    return False

                # Bug #35: TTL tracked via `_live_orders_inserted_at` sidecar
                # (line 1910). No need to mutate the Pydantic Trade object.

                async with self._live_orders_lock:
                    self.live_orders[order_key] = trade
                    self._pending_order_keys.discard(order_key)

                has_broker_ids = await self._register_broker_ids(order_key, trade)
                if has_broker_ids:
                    self._remove_pending_fill(order_key)
                await self._drain_deferred_terminals(order_key, trade)
                self.metrics.order_actions_total.labels(type="force_flat").inc()
                self.rate_limiter.record()
                self.per_symbol_rate_limiter.record(intent.symbol)
                self.circuit_breaker.record_success()
                self._update_cb_metric()
                self.strategy_cb_mgr.record_success(intent.strategy_id)
                self._audit_log_order(
                    {
                        "event": "dispatched",
                        "intent_type": "FORCE_FLAT",
                        "order_key": order_key,
                        "symbol": intent.symbol,
                        "side": str(close_side),
                        "qty": close_qty,
                        "strategy_id": intent.strategy_id,
                        "cmd_id": int(cmd.cmd_id),
                    }
                )

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
                    cancel_target_key = target_key
                    if self._is_cancel_inflight(target_key):
                        logger.info(
                            "cancel_already_inflight",
                            target=target_key,
                            strategy_id=intent.strategy_id,
                            cmd_id=int(cmd.cmd_id),
                        )
                        self._audit_log_order(
                            {
                                "event": "cancel_no_op_already_inflight",
                                "intent_type": "CANCEL",
                                "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                                "target_key": target_key,
                                "symbol": intent.symbol,
                                "strategy_id": intent.strategy_id,
                                "cmd_id": int(cmd.cmd_id),
                            }
                        )
                        return True
                    self._mark_cancel_inflight(target_key)
                    logger.info("Canceling Order", target=target_key)
                    result = await self._call_api("cancel_order", self.client.cancel_order, target_trade, intent=intent)
                    if result is None or result is _GUARD_TIMEOUT:
                        self._clear_cancel_inflight(target_key)
                        return False
                    self.metrics.order_actions_total.labels(type="cancel").inc()
                    self.rate_limiter.record()
                    self.per_symbol_rate_limiter.record(intent.symbol)
                    self._audit_log_order(
                        {
                            "event": "dispatched",
                            "intent_type": "CANCEL",
                            "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                            "target_key": target_key,
                            "symbol": intent.symbol,
                            "strategy_id": intent.strategy_id,
                            "cmd_id": int(cmd.cmd_id),
                        }
                    )
                elif target_trade is _PENDING_SENTINEL:
                    logger.warning("Cancel target still pending", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.CANCEL_TARGET_PENDING, "Cancel target still pending")
                elif target_trade is _TERMINAL_BEFORE_REGISTERED:
                    logger.warning("Cancel target terminated before registered", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(
                        intent, RejectionReason.CANCEL_TARGET_TERMINAL, "Cancel target terminated before registered"
                    )
                else:
                    # Bug #29: distinguish race-loser cancels (target was just
                    # filled/cancelled, removed from live_orders before CANCEL
                    # arrived) from true unknown order_ids (typo, strategy bug).
                    if self._is_recently_terminal(target_key):
                        logger.info(
                            "cancel_already_terminal",
                            target=target_key,
                            cancel_outcome="not_found_local",
                            strategy_id=intent.strategy_id,
                            cmd_id=int(cmd.cmd_id),
                        )
                        self.metrics.order_cancel_already_terminal_total.labels(
                            reason="not_found_local"
                        ).inc()
                        self._audit_log_order(
                            {
                                "event": "cancel_no_op_already_terminal",
                                "intent_type": "CANCEL",
                                "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                                "target_key": target_key,
                                "symbol": intent.symbol,
                                "strategy_id": intent.strategy_id,
                                "cmd_id": int(cmd.cmd_id),
                            }
                        )
                        return True
                    logger.warning("Cancel target not found", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.CANCEL_TARGET_NOT_FOUND, "Cancel target not found")
                    return False

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
                    # H1: same TOCTOU window applies to AMEND because update_order
                    # can also take ~395ms on Shioaji P95. The defensive cancel
                    # post-HALT cancels the underlying live order (target_trade),
                    # not the amend itself.
                    amend_ticket_id = self._begin_dispatch_ticket(intent)
                    result: Any = None
                    try:
                        result = await self._call_api(
                            "update_order",
                            self.client.update_order,
                            target_trade,
                            price=price_f,
                            intent=intent,
                        )
                    finally:
                        # F-D1: ticket close paired with begin_dispatch via
                        # finally so broker exceptions don't leak tickets.
                        # target_trade is the AMEND target — defensive cancel
                        # cancels the underlying live order on HALT mid-await.
                        await self._end_dispatch_ticket(
                            amend_ticket_id, intent, target_trade, cmd.cmd_id
                        )
                    if result is None or result is _GUARD_TIMEOUT:
                        return False
                    self.metrics.order_actions_total.labels(type="amend").inc()
                    self.rate_limiter.record()
                    self.per_symbol_rate_limiter.record(intent.symbol)
                    self._audit_log_order(
                        {
                            "event": "dispatched",
                            "intent_type": "AMEND",
                            "order_key": f"{intent.strategy_id}:{intent.intent_id}",
                            "target_key": target_key,
                            "symbol": intent.symbol,
                            "new_price": intent.price,
                            "strategy_id": intent.strategy_id,
                            "cmd_id": int(cmd.cmd_id),
                        }
                    )
                elif target_trade is _PENDING_SENTINEL:
                    logger.warning("Amend target still pending", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.AMEND_TARGET_PENDING, "Amend target still pending")
                elif target_trade is _TERMINAL_BEFORE_REGISTERED:
                    logger.warning("Amend target terminated before registered", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(
                        intent, RejectionReason.AMEND_TARGET_TERMINAL, "Amend target terminated before registered"
                    )
                else:
                    logger.warning("Amend target not found", target=target_key)
                    self.metrics.order_reject_total.inc()
                    await self._add_to_dlq(intent, RejectionReason.AMEND_TARGET_NOT_FOUND, "Amend target not found")

        except (OSError, TimeoutError, ConnectionError, RuntimeError) as e:
            logger.error("Broker Error", error=str(e))
            self.metrics.order_reject_total.inc()
            self.circuit_breaker.record_failure()
            self._update_cb_metric()
            self.strategy_cb_mgr.record_failure(intent.strategy_id)
            self._emit_trace("order_dispatch_error", intent, {"cmd_id": int(cmd.cmd_id), "error": str(e)})
            self._audit_log_order(
                {
                    "event": "dispatch_failed",
                    "intent_type": str(intent.intent_type),
                    "order_key": order_key,
                    "symbol": intent.symbol,
                    "strategy_id": intent.strategy_id,
                    "cmd_id": int(cmd.cmd_id),
                    "error": str(e),
                }
            )
            # Clean up sentinel to prevent permanent slot occupation (D2 rollback)
            if cancel_target_key:
                self._clear_cancel_inflight(cancel_target_key)
            async with self._live_orders_lock:
                if order_key in self.live_orders and self.live_orders.get(order_key) is _PENDING_SENTINEL:
                    del self.live_orders[order_key]
                    self._pending_order_keys.discard(order_key)
            self._remove_pending_fill(order_key)
            return False
        else:
            self._emit_trace("order_dispatch_ok", intent, {"cmd_id": int(cmd.cmd_id)})
        return True

    # M3: priority ordering for the api-queue eviction policy. Higher
    # numbers preempt lower numbers when the queue is full. CANCEL and
    # FORCE_FLAT are safety intents (must reach the broker even under
    # pressure); AMEND mutates an existing live order and is preferred
    # over NEW which can usually be retried by the strategy.
    _API_INTENT_PRIORITY: dict[IntentType, int] = {
        IntentType.CANCEL: 4,
        IntentType.FORCE_FLAT: 3,
        IntentType.AMEND: 2,
        IntentType.NEW: 1,
    }

    @classmethod
    def _api_intent_priority(cls, intent_type: IntentType | None) -> int:
        if intent_type is None:
            return 0
        return cls._API_INTENT_PRIORITY.get(intent_type, 0)

    async def _enqueue_api(self, cmd: OrderCommand) -> bool:
        """Enqueue command to API worker. Returns True on success, False if DLQ'd.

        M3: when the queue is full, the incoming intent can preempt the
        oldest queued intent of strictly lower priority (CANCEL > FORCE_FLAT
        > AMEND > NEW). The evicted command is routed to the DLQ. NEW
        intents never preempt anything — strategies retry them organically.
        """
        try:
            self._api_queue.put_nowait(cmd)
            self._emit_trace("order_enqueue_api", cmd.intent, {"cmd_id": int(cmd.cmd_id)})
            return True
        except asyncio.QueueFull:
            evictor_type = cmd.intent.intent_type
            evicted = self._evict_lower_priority_for_safety_intent(evictor_type)
            if evicted is not None:
                evicted_type = evicted.intent.intent_type
                try:
                    self._api_queue.put_nowait(cmd)
                except asyncio.QueueFull:
                    # Race: queue filled again before we could put; put
                    # the evicted command back and fall through to DLQ.
                    try:
                        self._api_queue.put_nowait(evicted)
                    except asyncio.QueueFull:
                        await self._add_to_dlq(
                            evicted.intent,
                            RejectionReason.RATE_LIMIT,
                            "Lost during priority eviction race",
                        )
                else:
                    self._emit_trace(
                        "order_enqueue_api_preempt",
                        cmd.intent,
                        {
                            "cmd_id": int(cmd.cmd_id),
                            "evicted_cmd_id": int(evicted.cmd_id),
                            "evictor_intent_type": evictor_type.name,
                            "evicted_intent_type": evicted_type.name,
                        },
                    )
                    logger.warning(
                        "api_queue_priority_eviction",
                        stage="enqueue_api",
                        evicted_cmd_id=int(evicted.cmd_id),
                        evictor_cmd_id=int(cmd.cmd_id),
                        evicted_intent_type=evicted_type.name,
                        evictor_intent_type=evictor_type.name,
                        queue_depth=self._api_queue.qsize(),
                    )
                    try:
                        self.metrics.api_queue_priority_eviction_total.labels(
                            evicted_intent_type=evicted_type.name,
                            evictor_intent_type=evictor_type.name,
                        ).inc()
                    except Exception:  # noqa: BLE001 — metric must never block path
                        pass
                    await self._add_to_dlq(
                        evicted.intent,
                        RejectionReason.RATE_LIMIT,
                        f"Preempted by higher-priority {evictor_type.name}",
                    )
                    return True
            logger.warning(
                "API queue full - routing to DLQ",
                cmd_id=cmd.cmd_id,
                strategy_id=cmd.intent.strategy_id,
                symbol=cmd.intent.symbol,
                intent_type=str(cmd.intent.intent_type),
            )
            self.metrics.order_reject_total.inc()
            self._emit_trace("order_reject", cmd.intent, {"reason": "API_QUEUE_FULL", "cmd_id": int(cmd.cmd_id)})
            await self._add_to_dlq(cmd.intent, RejectionReason.RATE_LIMIT, "API queue full")
            return False

    def _evict_lower_priority_for_safety_intent(
        self, evictor_intent_type: IntentType
    ) -> OrderCommand | None:
        """M3: remove and return the oldest queued command whose intent
        priority is strictly less than ``evictor_intent_type``.

        Priority order from highest to lowest: CANCEL > FORCE_FLAT > AMEND
        > NEW. Lowest-priority queued items are preferred targets so a
        single high-priority arrival doesn't churn the entire queue.
        Accesses ``asyncio.Queue._queue`` (collections.deque) directly —
        stable CPython behaviour but guarded with a try/except.
        Returns None when no eligible victim exists or the queue internals
        are unavailable.
        """
        evictor_pri = self._api_intent_priority(evictor_intent_type)
        if evictor_pri <= self._api_intent_priority(IntentType.NEW):
            return None  # NEW (or unknown) cannot evict anything
        try:
            internal = self._api_queue._queue  # type: ignore[attr-defined]
        except AttributeError:
            return None
        if not internal:
            return None
        # Pick the lowest-priority victim, breaking ties by oldest (FIFO).
        # ``enumerate(list(internal))`` snapshots the deque so concurrent
        # mutations (defensive — single asyncio loop owns this queue) do
        # not invalidate iteration.
        target: tuple[int, OrderCommand] | None = None
        target_pri = evictor_pri  # must be strictly lower than evictor
        for idx, item in enumerate(list(internal)):
            intent = getattr(item, "intent", None)
            if intent is None:
                continue
            pri = self._api_intent_priority(intent.intent_type)
            if pri >= evictor_pri:
                continue  # not evictable by this evictor
            if target is None or pri < target_pri:
                target = (idx, item)
                target_pri = pri
                if pri == self._api_intent_priority(IntentType.NEW):
                    # Lowest possible victim already found at this index;
                    # FIFO break: keep the first one found.
                    break
        if target is None:
            return None
        try:
            internal.remove(target[1])
        except ValueError:
            return None
        return target[1]

    # H7 backwards-compat alias retained for any external callers; the
    # new policy supersedes "evict NEW for CANCEL" with a general
    # priority-based eviction. M3 (2026-04-25).
    def _evict_new_for_cancel(self) -> OrderCommand | None:
        return self._evict_lower_priority_for_safety_intent(IntentType.CANCEL)

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
                # P1-4: drain any residual coalesced pending before exit.
                # Rare, but if a previous iteration stored items into
                # ``_api_pending`` and we were cancelled before clearing,
                # those dedup slots would leak otherwise.
                self._release_pending_on_cancel()
                raise
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
                        except asyncio.CancelledError:
                            # P1-4: Python 3.12 wait_for / Queue.get cancel race.
                            # Even if ``wait_for`` successfully retrieved an item
                            # (materialised into ``cmd`` above) we will never
                            # reach the store step here; release anything that
                            # was already staged. Re-raise after cleanup.
                            self._release_pending_on_cancel()
                            raise

                pending = list(self._api_pending.values())
                self._api_pending.clear()
                # Prioritize urgent intents (CANCEL, FORCE_FLAT) ahead of NEWs
                # to ensure risk-reducing orders execute before risk-increasing ones.
                pending.sort(
                    key=lambda c: 0 if c.intent.intent_type in (IntentType.CANCEL, IntentType.FORCE_FLAT) else 1
                )
                # P1-4 hole fix: stage the snapshot in _api_inflight so
                # cancel / unexpected-exception handlers see un-dispatched
                # items and can release their dedup slots. Each branch below
                # removes its item from _api_inflight only on the SUCCESS
                # path; if CancelledError fires mid-await the remove is
                # skipped and the outer handler picks the item up.
                self._api_inflight = list(pending)
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
                        self.metrics.order_halt_skip_total.inc()
                        self.metrics.order_reject_total.inc()
                        self._dedup_release(item.intent.idempotency_key)
                        await self._add_to_dlq(
                            item.intent,
                            RejectionReason.STORMGUARD_HALT,
                            "STORMGUARD_HALT_SKIP",
                            halt_exempt_blocked=self._is_strategy_halt_exempt(item.intent.strategy_id),
                        )
                        self._send_dispatch_rejection(item.intent, "dispatch_halt_skip")
                        try:
                            self._api_inflight.remove(item)
                        except ValueError:
                            pass
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
                            RejectionReason.DEADLINE_EXCEEDED,
                            "DEADLINE_EXPIRED",
                        )
                        self._send_dispatch_rejection(item.intent, "dispatch_deadline_expired")
                        try:
                            self._api_inflight.remove(item)
                        except ValueError:
                            pass
                        continue
                    try:
                        ok = await self._dispatch_to_api(item)
                        if ok:
                            self._dedup_commit(item.intent.idempotency_key, True, "dispatched", item.cmd_id)
                        # Reached here = dispatch finished without cancel.
                        # On a CancelledError flight from _dispatch_to_api,
                        # this remove is skipped and the outer handler at
                        # line ~2750 picks the item up via _api_inflight.
                        try:
                            self._api_inflight.remove(item)
                        except ValueError:
                            pass
                    except Exception:
                        await self._handle_dispatch_exception(intent=item.intent, cmd_id=item.cmd_id)
                        try:
                            self._api_inflight.remove(item)
                        except ValueError:
                            pass
            except asyncio.CancelledError:
                # P1-4: shutdown / task-cancel path. Release any dedup slots
                # for commands in ``_api_pending`` that we have not yet
                # dispatched, so the same idempotency_key can be resubmitted
                # cleanly on the next worker lifetime. Re-raise after cleanup.
                self._release_pending_on_cancel()
                raise
            except Exception:
                logger.error("_api_worker: unexpected exception in dispatch loop", exc_info=True)
                self.metrics.order_reject_total.inc()
                self.circuit_breaker.record_failure()
                self._update_cb_metric()
                if current_cmd is not None:
                    self.strategy_cb_mgr.record_failure(current_cmd.intent.strategy_id)
                # Release dedup slots for orphaned commands before clearing.
                # P1-4 hole fix: also drain _api_inflight (un-dispatched
                # snapshot items), not just _api_pending.
                for orphaned in self._api_pending.values():
                    self._dedup_release(orphaned.intent.idempotency_key)
                self._api_pending.clear()
                for orphaned in self._api_inflight:
                    self._dedup_release(orphaned.intent.idempotency_key)
                self._api_inflight.clear()

    def _release_pending_on_cancel(self) -> None:
        """P1-4: release dedup slots + drop `_api_pending` AND `_api_inflight`
        on cancellation.

        Called from `_api_worker`'s CancelledError handlers so a shutdown
        that catches us in the coalesce window OR mid-dispatch-loop does
        not leak dedup reservations.

        Two collections to drain:
          * ``_api_pending`` — items staged in the coalesce window but not
            yet snapshotted into the dispatch loop.
          * ``_api_inflight`` — items that were snapshotted (and removed
            from ``_api_pending``) but have not yet been finalized
            (dispatched / halted / deadline-expired / exception-handled).

        Without draining ``_api_inflight``, a cancel during
        ``await self._dispatch_to_api(item)`` would leak that item's dedup
        reservation (P1-4 hole found by Codex stop-time review).
        """
        released = 0
        for orphaned in self._api_pending.values():
            try:
                self._dedup_release(orphaned.intent.idempotency_key)
            except Exception:  # noqa: BLE001 — dedup release must never raise
                pass
            released += 1
        self._api_pending.clear()
        for orphaned in self._api_inflight:
            try:
                self._dedup_release(orphaned.intent.idempotency_key)
            except Exception:  # noqa: BLE001 — dedup release must never raise
                pass
            released += 1
        self._api_inflight.clear()
        if released:
            try:
                logger.warning("_api_worker_cancel_released_pending", count=released)
            except Exception:  # noqa: BLE001 — log must never mask CancelledError
                pass

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
                        # Bug #29: only NEW intents can produce phantom orders.
                        # CANCEL/AMEND failures are not "phantom orders" — gating
                        # prevents spurious phantom_recovery_releases on cancels.
                        if intent is not None and intent.intent_type == IntentType.NEW:
                            # M4: append per-occurrence record under _phantom_lock.
                            # Multiple registrations with the same key (intent_id
                            # reused within process lifetime) accumulate as
                            # separate entries; resolution / cleanup pop FIFO.
                            with self._phantom_lock:
                                phantom_key = self._register_phantom(intent)
                                # R2-03: Evict entries older than 1 hour when over capacity.
                                if self._phantom_record_count() > self._phantom_order_max:
                                    self._evict_stale_phantom_records(max_age_s=3600.0)
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
