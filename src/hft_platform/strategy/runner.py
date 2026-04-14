import asyncio
import importlib
import os
import re
import time
from decimal import Decimal
from typing import Any, List

from structlog import get_logger

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, RiskFeedback, Side
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import GapEvent
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.ops.strategy_governor import StrategyHealthGovernor
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.strategy.compat import check_strategy_feature_compat
from hft_platform.strategy.registry import StrategyRegistry

logger = get_logger("strategy_runner")

_KNOWN_TUPLE_TAGS: frozenset[str] = frozenset({"tick", "bidask", "lobstats", "typed_intent_v1"})

_RUST_CIRCUIT_ENABLED = os.getenv("HFT_STRATEGY_CIRCUIT_RUST", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}

_MAX_INTENTS_PER_EVENT: int = int(os.getenv("HFT_MAX_INTENTS_PER_EVENT", "20"))

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except ImportError:
        _rust_core = importlib.import_module("rust_core")
    _RustCircuitBreaker = getattr(_rust_core, "RustCircuitBreaker", None)
except (ImportError, ModuleNotFoundError):
    _RustCircuitBreaker = None


def _get_trace_sampler():
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler

        return get_trace_sampler()
    except ImportError:
        return None


def _obs_policy() -> str:
    value = str(os.getenv("HFT_OBS_POLICY", "")).strip().lower()
    if value in {"minimal", "balanced", "debug"}:
        return value
    return ""


def _typed_intent_symbol(intent: Any) -> str:
    if isinstance(intent, tuple) and len(intent) >= 4 and intent[0] == "typed_intent_v1":
        return str(intent[3])
    return str(getattr(intent, "symbol", ""))


def _typed_intent_type(intent: Any) -> int | None:
    if isinstance(intent, tuple) and len(intent) >= 5 and intent[0] == "typed_intent_v1":
        try:
            return int(intent[4])
        except (TypeError, ValueError):
            return None
    value = getattr(intent, "intent_type", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _typed_intent_tif(intent: Any) -> int | None:
    """Extract TIF from typed intent tuple (index 8) or OrderIntent attribute."""
    if isinstance(intent, tuple) and len(intent) >= 9 and intent[0] == "typed_intent_v1":
        try:
            return int(intent[8])
        except (TypeError, ValueError):
            return None
    value = getattr(intent, "tif", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _typed_intent_side(intent: Any) -> int | None:
    """Extract Side from typed intent tuple (index 5) or OrderIntent attribute."""
    if isinstance(intent, tuple) and len(intent) >= 6 and intent[0] == "typed_intent_v1":
        try:
            return int(intent[5])
        except (TypeError, ValueError):
            return None
    value = getattr(intent, "side", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _typed_intent_identity(intent: Any) -> tuple[int, str, str, int | None]:
    """Extract (intent_id, strategy_id, symbol, side) from typed tuple or OrderIntent."""
    if isinstance(intent, tuple) and len(intent) >= 4 and intent[0] == "typed_intent_v1":
        _iid = int(intent[1]) if len(intent) > 1 else 0
        _sid = str(intent[2]) if len(intent) > 2 else ""
        _sym = str(intent[3]) if len(intent) > 3 else ""
        _side = int(intent[5]) if len(intent) > 5 else None
        return (_iid, _sid, _sym, _side)
    return (
        getattr(intent, "intent_id", 0),
        getattr(intent, "strategy_id", ""),
        getattr(intent, "symbol", ""),
        getattr(intent, "side", None),
    )


def _get_symbol_net_qty(position_store: Any, symbol: str, strategy_id: str | None = None) -> int:
    """Return net_qty for *symbol*, optionally filtered to *strategy_id*.

    When *strategy_id* is provided, only that strategy's exposure is counted.
    This prevents CLOSE_ONLY from allowing a flat strategy to open positions
    based on another strategy's offsetting exposure.

    O(n) over the position map, but only called during CLOSE_ONLY phase
    (not on every tick). Returns 0 if position_store is None or empty.
    """
    if position_store is None:
        return 0
    positions = getattr(position_store, "positions", None)
    if not positions:
        return 0
    _total = 0
    for _key, _pos in positions.items():
        # Key format: "account:strategy:symbol"
        if not _key.endswith(f":{symbol}"):
            continue
        if strategy_id is not None and f":{strategy_id}:" not in _key:
            continue
        _total += _pos.net_qty
    return _total


class StrategyRunner:
    __slots__ = (
        "bus",
        "risk_queue",
        "lob_engine",
        "feature_engine",
        "position_store",
        "registry",
        "strategies",
        "_strat_executors",
        "_start_cursor",
        "_risk_submit",
        "_risk_submit_typed",
        "_typed_intent_fastpath",
        "_lob_snapshot_source",
        "_lob_l1_source",
        "_feature_value_source",
        "_feature_view_source",
        "_feature_set_source",
        "_feature_profile_source",
        "_feature_tuple_source",
        "_feature_staleness_source",
        "_staleness_counter",
        "_consumer_seq",
        "metrics",
        "latency",
        "_trace_sampler",
        "_obs_policy",
        "_diagnostic_metrics_enabled",
        "symbol_metadata",
        "price_codec",
        "_intent_seq",
        "_positions_cache",
        "_positions_dirty",
        "_current_source_ts_ns",
        "_current_trace_id",
        "_strategy_metrics_sample_every",
        "_strategy_metrics_batch",
        "_strategy_metrics_seq",
        "_strategy_pending_intents",
        "_strategy_pending_alpha_intent",
        "_strategy_pending_alpha_flat",
        "_circuit_threshold",
        "_circuit_recovery_threshold",
        "_circuit_cooldown_ns",
        "_failure_counts",
        "_circuit_states",
        "_circuit_success_counts",
        "_circuit_halted_at_ns",
        "_rust_circuit",
        "_position_key_cache",
        "_strat_index",
        "_feature_compat_fail_fast",
        "track_gate",
        "_strategies_version",
        "_executors_version",
        # Timeout circuit breaker (wall-clock)
        "_timeout_ns",
        "_timeout_strikes_limit",
        "_timeout_recover_ns",
        "_timeout_consecutive",
        "_timeout_broken",
        "_timeout_broken_at_ns",
        "_default_intent_ttl_ns",
        "_rejection_sink",
        "_rejection_queue",
        "_storm_guard",
        "_stale_event_threshold_ns",
        "_stale_event_skip_total",
        "_stale_event_metric",
        "__dict__",  # needed for test monkey-patching
    )

    def __init__(
        self,
        bus,
        risk_queue,  # asyncio.Queue or LocalIntentChannel (CE2-03 backward compat)
        lob_engine=None,
        position_store=None,
        feature_engine=None,
        config_path: str = "config/base/strategies.yaml",
        symbol_metadata: SymbolMetadata | None = None,
    ):
        self.bus = bus
        self.risk_queue = risk_queue
        self.lob_engine = lob_engine
        self.feature_engine = feature_engine or getattr(lob_engine, "feature_engine", None)
        self.position_store = position_store
        cfg_path = os.getenv("HFT_STRATEGY_CONFIG") or config_path
        self.registry = StrategyRegistry(cfg_path)
        self.strategies: List[BaseStrategy] = []
        # Cache of (strategy, ctx, lat_m, int_m, alpha_intent_m, alpha_flat_m, alpha_last_ts_g)
        self._strat_executors: list[tuple[BaseStrategy, StrategyContext, Any, Any, Any, Any, Any]] = []
        self._risk_submit = self._resolve_risk_submit(risk_queue)
        self._risk_submit_typed = getattr(risk_queue, "submit_typed_nowait", None)
        self._typed_intent_fastpath = callable(self._risk_submit_typed) and os.getenv(
            "HFT_TYPED_INTENT_CHANNEL", "1"
        ).lower() not in {"0", "false", "no", "off"}
        self._lob_snapshot_source = getattr(lob_engine, "get_book_snapshot", None) if lob_engine else None
        self._lob_l1_source = getattr(lob_engine, "get_l1_scaled", None) if lob_engine else None
        fe = self.feature_engine
        self._feature_value_source = getattr(fe, "get_feature", None) if fe else None
        self._feature_view_source = getattr(fe, "get_feature_view", None) if fe else None
        self._feature_set_source = getattr(fe, "feature_set_id", None) if fe else None
        self._feature_profile_source = getattr(fe, "active_profile_id", None) if fe else None
        self._feature_tuple_source = getattr(fe, "get_feature_tuple", None) if fe else None
        self._feature_staleness_source = getattr(fe, "last_update_ns", None) if fe else None
        self._consumer_seq: int = -1  # tracks last-processed bus sequence for drain
        self._start_cursor: int | None = None  # set externally to replay events published before runner started
        self.metrics = MetricsRegistry.get()
        self._staleness_counter = getattr(self.metrics, "feature_staleness_detected_total", None)
        self.latency = LatencyRecorder.get()
        self.strategy_governor = StrategyHealthGovernor(metrics=self.metrics)
        self._trace_sampler = _get_trace_sampler()
        self._obs_policy = _obs_policy()
        self._diagnostic_metrics_enabled = self._obs_policy != "minimal"
        self.symbol_metadata = symbol_metadata or SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.symbol_metadata))
        self._intent_seq = 0
        self._positions_cache: dict = {}
        self._positions_dirty = True
        self._current_source_ts_ns = 0
        self._current_trace_id = ""
        _stale_ms = int(os.getenv("HFT_STALE_EVENT_THRESHOLD_MS", "500"))
        self._stale_event_threshold_ns: int = _stale_ms * 1_000_000
        self._stale_event_skip_total: int = 0
        self._stale_event_metric = getattr(self.metrics, "stale_event_skip_total", None)
        try:
            default_sample = "1"
            if self._obs_policy == "balanced":
                default_sample = "2"
            elif self._obs_policy == "minimal":
                default_sample = "8"
            self._strategy_metrics_sample_every = max(
                1, int(os.getenv("HFT_STRATEGY_METRICS_SAMPLE_EVERY", default_sample))
            )
        except ValueError:
            self._strategy_metrics_sample_every = 1
        try:
            default_batch = "1"
            if self._obs_policy == "balanced":
                default_batch = "8"
            elif self._obs_policy == "minimal":
                default_batch = "32"
            self._strategy_metrics_batch = max(1, int(os.getenv("HFT_STRATEGY_METRICS_BATCH", default_batch)))
        except ValueError:
            self._strategy_metrics_batch = 1
        self._strategy_metrics_seq: dict[str, int] = {}
        self._strategy_pending_intents: dict[str, int] = {}
        self._strategy_pending_alpha_intent: dict[str, int] = {}
        self._strategy_pending_alpha_flat: dict[str, int] = {}

        # Circuit breaker: 3-state FSM (normal → degraded → halted) per strategy
        _threshold_env = os.getenv("HFT_STRATEGY_CIRCUIT_THRESHOLD", "10")
        self._circuit_threshold: int = int(_threshold_env) if _threshold_env.isdigit() else 10
        self._circuit_recovery_threshold: int = max(1, self._circuit_threshold // 2)
        _cooldown_s = float(os.getenv("HFT_STRATEGY_CIRCUIT_COOLDOWN_S", "60"))
        self._circuit_cooldown_ns: int = max(1_000_000_000, int(_cooldown_s * 1_000_000_000))
        self._failure_counts: dict[str, int] = {}
        self._circuit_states: dict[str, str] = {}  # "normal" | "degraded" | "halted"
        self._circuit_success_counts: dict[str, int] = {}
        self._circuit_halted_at_ns: dict[str, int] = {}
        # Rust-accelerated circuit breaker (replaces 5 dict lookups with 1 HashMap lookup)
        self._rust_circuit: Any = None
        if _RUST_CIRCUIT_ENABLED and _RustCircuitBreaker is not None:
            try:
                self._rust_circuit = _RustCircuitBreaker(
                    self._circuit_threshold,
                    self._circuit_recovery_threshold,
                    self._circuit_cooldown_ns,
                )
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                logger.warning("rust_circuit_breaker_init_failed", error=str(exc))
        # TrackGate: per-event session phase filtering (set externally by SessionGovernor)
        self.track_gate: Any = None  # TrackGate | None

        # Default intent TTL: intents older than this are rejected by RiskEngine TTL check
        self._default_intent_ttl_ns: int = int(os.getenv("HFT_DEFAULT_INTENT_TTL_MS", "5000")) * 1_000_000

        # Timeout circuit breaker: wall-clock protection per strategy
        _timeout_ms = float(os.getenv("HFT_STRATEGY_TIMEOUT_MS", "50"))
        self._timeout_ns: int = int(_timeout_ms * 1_000_000)
        _strikes = os.getenv("HFT_STRATEGY_TIMEOUT_STRIKES", "3")
        self._timeout_strikes_limit: int = int(_strikes) if _strikes.isdigit() else 3
        _recover_s = float(os.getenv("HFT_STRATEGY_TIMEOUT_RECOVER_S", "60"))
        self._timeout_recover_ns: int = int(_recover_s * 1_000_000_000)
        self._timeout_consecutive: dict[str, int] = {}
        self._timeout_broken: dict[str, bool] = {}
        self._timeout_broken_at_ns: dict[str, int] = {}

        # Per-strategy intent flood cap: limits intents submitted per event
        self._max_intents_per_event: int = int(os.getenv("HFT_MAX_INTENTS_PER_EVENT", "20"))

        # Cache for parsed position keys: "pos:strat_id:symbol" → (strat_id, symbol)
        self._position_key_cache: dict[str, tuple[str, str]] = {}
        # Unit 10: Strategy-by-id index for O(1) targeted dispatch
        self._strat_index: dict[str, list[int]] = {}
        # M-2: Version counters for O(1) executor staleness check (replaces O(n) list scan)
        self._strategies_version: int = 0
        self._executors_version: int = 0
        self._feature_compat_fail_fast = os.getenv("HFT_STRATEGY_FEATURE_COMPAT_FAIL_FAST", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # Rejection sink: receives RiskFeedback when risk_queue is full (set by bootstrap)
        self._rejection_sink: asyncio.Queue | None = None

        # Rejection queue: shared queue for consuming RiskFeedback dispatched to strategies (set by bootstrap)
        self._rejection_queue: asyncio.Queue | None = None

        # StormGuard reference: set by bootstrap to trigger HALT on persistent risk_queue_full
        self._storm_guard: Any = None

        # Gradual queue-full degradation: STORM first, HALT after N consecutive failures
        self._queue_full_consecutive: int = 0
        self._queue_full_halt_threshold: int = int(os.getenv("HFT_QUEUE_FULL_HALT_THRESHOLD", "3"))

        # Load initial
        for strat in self.registry.instantiate():
            self.register(strat)

        self.running = False

    def set_start_cursor(self, cursor: int) -> None:
        """Set the bus cursor to replay from, capturing events published before runner started."""
        self._start_cursor = cursor

    def set_rejection_sink(self, sink: asyncio.Queue) -> None:
        """Set the queue where StrategyRunner writes RiskFeedback on risk_queue overflow."""
        self._rejection_sink = sink

    def set_rejection_queue(self, queue: asyncio.Queue) -> None:
        """Set the shared queue for consuming RiskFeedback dispatched to strategies."""
        self._rejection_queue = queue

    def reset_stale_counter(self) -> None:
        """Reset stale event skip counter (called after reconnect)."""
        prev = self._stale_event_skip_total
        self._stale_event_skip_total = 0
        if prev > 0:
            logger.info("stale_event_counter_reset", previous_total=prev)

    def set_storm_guard(self, storm_guard: Any) -> None:
        """Set StormGuard reference for triggering HALT on persistent queue-full."""
        self._storm_guard = storm_guard

    def set_publish_sink(self, sink: Any) -> None:
        """Set the publish callback for strategy-to-bus publication."""
        self._publish_sink = sink

    async def run(self):
        self.running = True
        start_cursor = self._start_cursor
        # Seed _consumer_seq from start_cursor so increment-based tracking
        # correctly reflects the actual bus position (not the global write cursor).
        if start_cursor is not None:
            self._consumer_seq = start_cursor
        else:
            self._consumer_seq = self.bus.cursor
        logger.info("StrategyRunner started", start_cursor=start_cursor, consumer_seq=self._consumer_seq)
        try:
            batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
            if batch_size > 1:
                async for batch in self.bus.consume_batch(
                    batch_size, start_cursor=start_cursor, consumer_name="strategy_runner"
                ):
                    for event in batch:
                        await self.process_event(event)
                        self._consumer_seq += 1
            else:
                async for event in self.bus.consume(start_cursor=start_cursor, consumer_name="strategy_runner"):
                    await self.process_event(event)
                    self._consumer_seq += 1
        except asyncio.CancelledError:
            pass
        finally:
            self._flush_pending_strategy_metrics()

    async def _run_rejection_consumer(self) -> None:
        """Consume RiskFeedback from rejection queue and dispatch to strategies."""
        if self._rejection_queue is None:
            return
        while True:
            try:
                feedback = await self._rejection_queue.get()
            except asyncio.CancelledError:
                break
            try:
                indices = self._strat_index.get(feedback.strategy_id)
                if indices:
                    for idx in indices:
                        executor = self._strat_executors[idx]
                        executor[0].on_risk_feedback(feedback)
                else:
                    logger.warning("rejection_feedback_unknown_strategy", strategy_id=feedback.strategy_id)
            except Exception:
                logger.exception("rejection_feedback_dispatch_error", strategy_id=getattr(feedback, "strategy_id", "?"))
            finally:
                self._rejection_queue.task_done()

    async def drain_to_cursor(self, target_cursor: int, timeout_s: float) -> tuple[int, int]:
        """Drain bus events up to *target_cursor* within *timeout_s* seconds.

        Reads events from the bus starting at the current consumer position and
        processes them until either the target cursor is reached or the timeout
        expires.  The bus cursor is sampled once by the caller before calling
        this method; no new publishes after that snapshot are processed.

        Returns:
            (drained, skipped) where *drained* is the number of events processed
            and *skipped* is the number left unprocessed (> 0 only on timeout).
        """
        # Start from the last sequence the consumer processed (tracked during run()).
        # If _consumer_seq is -1 (never consumed), nothing to drain.
        local_seq = self._consumer_seq
        if local_seq >= target_cursor:
            return 0, 0

        deadline = asyncio.get_event_loop().time() + timeout_s
        drained = 0
        size = self.bus.size

        while local_seq < target_cursor:
            if asyncio.get_event_loop().time() >= deadline:
                skipped = target_cursor - local_seq
                return drained, skipped

            local_seq += 1
            # Read the event directly from the bus buffer (same logic as consume())
            kind = self.bus._kind_ring[local_seq % size] if self.bus._kind_ring is not None else 0
            if kind == 1 and self.bus._tick_ring is not None:
                event = self.bus._tick_ring.get(local_seq)
            elif kind == 2 and self.bus._bidask_ring is not None:
                event = self.bus._bidask_ring.get(local_seq)
            elif kind == 3 and self.bus._lobstats_ring is not None:
                event = self.bus._lobstats_ring.get(local_seq)
            elif self.bus._use_rust and self.bus._ring is not None:
                event = self.bus._ring.get(local_seq)
            else:
                buf = self.bus.buffer
                event = buf[local_seq % size] if buf is not None else None

            if event is not None:
                try:
                    await self.process_event(event)
                    drained += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "drain_to_cursor: process_event error",
                        seq=local_seq,
                        error=str(exc),
                    )

            # Yield control periodically to avoid starving the event loop
            if drained % 64 == 0:
                await asyncio.sleep(0)

        return drained, 0

    def register(self, strategy: BaseStrategy):
        compat_issues = check_strategy_feature_compat(strategy, self.feature_engine)
        for issue in compat_issues:
            log_fn = logger.error if issue.level == "error" else logger.warning
            log_fn(
                "Strategy/Feature compatibility issue",
                strategy_id=issue.strategy_id,
                code=issue.code,
                message=issue.message,
                level=issue.level,
            )
            try:
                if (
                    issue.level == "error"
                    and self.metrics
                    and hasattr(self.metrics, "feature_profile_compat_failures_total")
                ):
                    self.metrics.feature_profile_compat_failures_total.labels(
                        strategy=str(issue.strategy_id),
                        code=str(issue.code),
                    ).inc()
            except (TypeError, ValueError) as exc:
                logger.debug("compat_metric_emit_failed", error=str(exc))
        if self._feature_compat_fail_fast and any(i.level == "error" for i in compat_issues):
            raise RuntimeError(
                f"Strategy '{strategy.strategy_id}' failed feature compatibility checks: "
                + "; ".join(i.code for i in compat_issues if i.level == "error")
            )
        self.strategies.append(strategy)
        self._strategies_version += 1
        self._resolve_strategy_symbols(strategy)
        self._strat_executors.append(self._build_executor_entry(strategy))
        self._executors_version = self._strategies_version
        idx = len(self._strat_executors) - 1
        sid = strategy.strategy_id
        self._strat_index.setdefault(sid, []).append(idx)
        self._strategy_metrics_seq.setdefault(sid, 0)
        self._strategy_pending_intents.setdefault(sid, 0)
        self._strategy_pending_alpha_intent.setdefault(sid, 0)
        self._strategy_pending_alpha_flat.setdefault(sid, 0)
        logger.info("Registered strategy", id=strategy.strategy_id)

    def _resolve_strategy_symbols(self, strategy: BaseStrategy) -> None:
        resolved = set()
        used_tag = False
        raw_symbols = getattr(strategy, "symbols", None) or []
        if isinstance(raw_symbols, (set, list, tuple)):
            candidates = list(raw_symbols)
        else:
            candidates = [raw_symbols]

        for item in candidates:
            if not item:
                continue
            if isinstance(item, str) and item.lower().startswith("tag:"):
                used_tag = True
                tag_str = item[4:]
                tags = [t for t in re.split(r"[|,]", tag_str) if t]
                resolved.update(self.symbol_metadata.symbols_for_tags(tags))
            else:
                resolved.add(str(item))

        raw_tags = getattr(strategy, "symbol_tags", None) or []
        if raw_tags:
            used_tag = True
            resolved.update(self.symbol_metadata.symbols_for_tags(raw_tags))

        if resolved or used_tag:
            strategy.symbols = set(resolved)

    def _rebuild_executors(self):
        """Rebuild executor cache when strategies are replaced externally."""
        self._strat_executors = [self._build_executor_entry(strategy) for strategy in self.strategies]
        self._strat_index = {}
        for idx, strategy in enumerate(self.strategies):
            self._strat_index.setdefault(strategy.strategy_id, []).append(idx)
        # M-2: Sync executor version to current strategy version after rebuild
        self._strategies_version += 1
        self._executors_version = self._strategies_version

    def _flush_pending_strategy_metrics(self) -> None:
        if not self.metrics:
            self._strategy_pending_intents.clear()
            self._strategy_pending_alpha_intent.clear()
            self._strategy_pending_alpha_flat.clear()
            return
        for strategy, _ctx, _lat_m, int_m, alpha_intent_m, alpha_flat_m, _alpha_last_ts_g in self._strat_executors:
            sid = strategy.strategy_id
            pending_intents = self._strategy_pending_intents.get(sid, 0)
            if pending_intents and int_m:
                int_m.inc(pending_intents)
            self._strategy_pending_intents[sid] = 0

            pending_alpha_intent = self._strategy_pending_alpha_intent.get(sid, 0)
            if pending_alpha_intent and alpha_intent_m:
                alpha_intent_m.inc(pending_alpha_intent)
            self._strategy_pending_alpha_intent[sid] = 0

            pending_alpha_flat = self._strategy_pending_alpha_flat.get(sid, 0)
            if pending_alpha_flat and alpha_flat_m:
                alpha_flat_m.inc(pending_alpha_flat)
            self._strategy_pending_alpha_flat[sid] = 0

    def _resolve_risk_submit(self, risk_queue: Any):
        if hasattr(risk_queue, "submit_nowait"):
            return risk_queue.submit_nowait
        return risk_queue.put_nowait

    def _build_executor_entry(  # noqa: E501
        self, strategy: BaseStrategy
    ) -> tuple[BaseStrategy, StrategyContext, Any, Any, Any, Any, Any]:
        # Cache metrics and a reusable StrategyContext per strategy to reduce per-event allocations.
        lat_m = self.metrics.strategy_latency_ns.labels(strategy=strategy.strategy_id) if self.metrics else None
        int_m = self.metrics.strategy_intents_total.labels(strategy=strategy.strategy_id) if self.metrics else None

        alpha_intent_m = None
        alpha_flat_m = None
        alpha_last_ts_g = None
        if self.metrics:
            alpha_events_total = getattr(self.metrics, "alpha_signal_events_total", None)
            alpha_last_signal_ts = getattr(self.metrics, "alpha_last_signal_ts", None)
            try:
                if alpha_events_total is not None:
                    alpha_intent_m = alpha_events_total.labels(strategy=strategy.strategy_id, outcome="intent")
                    alpha_flat_m = alpha_events_total.labels(strategy=strategy.strategy_id, outcome="flat")
                if alpha_last_signal_ts is not None:
                    alpha_last_ts_g = alpha_last_signal_ts.labels(strategy=strategy.strategy_id)
            except (TypeError, ValueError) as exc:
                logger.debug("alpha_metrics_init_failed", strategy=strategy.strategy_id, error=str(exc))
                alpha_intent_m = None
                alpha_flat_m = None
                alpha_last_ts_g = None

        ctx = StrategyContext(
            positions={},
            strategy_id=strategy.strategy_id,
            intent_factory=self._intent_factory,
            price_scaler=self._scale_price,
            lob_source=self._lob_snapshot_source,
            lob_l1_source=self._lob_l1_source,
            feature_source=self._feature_value_source,
            feature_view_source=self._feature_view_source,
            feature_set_source=self._feature_set_source,
            feature_profile_source=self._feature_profile_source,
            feature_tuple_source=self._feature_tuple_source,
            feature_staleness_source=self._feature_staleness_source,
            staleness_counter=self._staleness_counter,
        )
        return (strategy, ctx, lat_m, int_m, alpha_intent_m, alpha_flat_m, alpha_last_ts_g)

    def _executors_match_strategy_list(self) -> bool:
        if len(self._strat_executors) != len(self.strategies):
            return False
        for idx, strategy in enumerate(self.strategies):
            if self._strat_executors[idx][0] is not strategy:
                return False
        return True

    def _intent_factory(
        self,
        strategy_id,
        symbol,
        side,
        price,
        qty,
        tif,
        intent_type,
        target_order_id=None,
        source_ts_ns: int | None = None,
        trace_id: str | None = None,
        price_type: str = "LMT",
    ) -> Any:
        self._intent_seq += 1
        if source_ts_ns is None:
            source_ts_ns = self._current_source_ts_ns
        if trace_id is None:
            trace_id = self._current_trace_id
        if self._typed_intent_fastpath:
            return (
                "typed_intent_v1",
                int(self._intent_seq),
                str(strategy_id),
                str(symbol),
                int(intent_type),
                int(side),
                int(price),
                int(qty),
                int(tif),
                str(target_order_id or ""),
                int(timebase.now_ns()),
                int(source_ts_ns or 0),
                "",
                str(trace_id or ""),
                "",
                self._default_intent_ttl_ns,  # ttl_ns
                0,  # decision_price — populated by StrategyRunner from LOB mid
            )
        return OrderIntent(
            intent_id=self._intent_seq,
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            target_order_id=target_order_id,
            timestamp_ns=timebase.now_ns(),
            source_ts_ns=int(source_ts_ns or 0),
            trace_id=str(trace_id or ""),
            price_type=price_type,
            ttl_ns=self._default_intent_ttl_ns,
        )

    def _scale_price(self, symbol: str, price: int | Decimal) -> int:
        return self.price_codec.scale(symbol, price)

    def _build_positions_by_strategy(self):
        if not self.position_store:
            return {}
        positions_by_strategy: dict = {}
        fallback: dict = {}
        rust_fast_path = False
        # Unit 9: Use Rust fast-path if available
        rust_tracker = getattr(self.position_store, "_rust_tracker", None)
        if rust_tracker is not None and hasattr(rust_tracker, "get_positions_by_strategy"):
            try:
                positions_by_strategy = rust_tracker.get_positions_by_strategy()
                rust_fast_path = True
            except Exception:
                pass  # Fallback to Python path

        if not rust_fast_path:
            if hasattr(self.position_store, "snapshot_positions"):
                raw = self.position_store.snapshot_positions()
            else:
                raw = getattr(self.position_store, "positions", None)
                if not isinstance(raw, dict):
                    return {}
                raw = dict(raw)

            for key, value in raw.items():
                if hasattr(value, "strategy_id") and hasattr(value, "symbol") and hasattr(value, "net_qty"):
                    positions_by_strategy.setdefault(value.strategy_id, {})[value.symbol] = value.net_qty
                    continue

                # S6: Cache parsed key tuples to avoid split() on every rebuild
                if isinstance(key, str) and ":" in key:
                    parsed = self._position_key_cache.get(key)
                    if parsed is None:
                        parts = key.split(":")
                        if len(parts) >= 3:
                            parsed = (parts[1], parts[2])
                            self._position_key_cache[key] = parsed
                    if parsed is not None:
                        strat_id, symbol = parsed
                        net_qty = value.net_qty if hasattr(value, "net_qty") else value
                        positions_by_strategy.setdefault(strat_id, {})[symbol] = net_qty
                        continue

                fallback[key] = value.net_qty if hasattr(value, "net_qty") else value

        # Merge pending recovery positions (loaded at startup but not yet merged
        # into positions via first fill).  Recovery keys use format
        # "account:strategy:symbol" or "account:symbol".  Route entries with
        # strategy_id to the correct bucket; others go to "*" wildcard.
        recovery = getattr(self.position_store, "_recovery_positions", None)
        if recovery:
            for rkey, rdata in recovery.items():
                net_qty = rdata.get("net_qty", 0) if isinstance(rdata, dict) else 0
                if net_qty == 0:
                    continue
                parts = rkey.split(":")
                if len(parts) >= 3:
                    strat_id, sym = parts[1], parts[2]
                    bucket = positions_by_strategy.setdefault(strat_id, {})
                    bucket[sym] = bucket.get(sym, 0) + net_qty
                else:
                    sym = parts[-1] if len(parts) >= 2 else rkey
                    fallback[sym] = fallback.get(sym, 0) + net_qty

        if fallback:
            positions_by_strategy["*"] = fallback

        return positions_by_strategy

    def invalidate_positions(self):
        self._positions_dirty = True

    async def process_event(self, event: Any):
        # Defensive guard: skip unexpected tuple events (allow known typed ring tags)
        if isinstance(event, tuple) and (not event or event[0] not in _KNOWN_TUPLE_TAGS):
            logger.warning("Unexpected tuple event skipped", length=len(event), head=repr(event[0] if event else None))
            return
        source_ts_ns, trace_id = self._extract_event_trace(event)
        self._current_source_ts_ns = source_ts_ns
        self._current_trace_id = trace_id
        # Staleness guard: skip events older than threshold to prevent
        # strategies from acting on stale market data during event loop lag.
        if source_ts_ns > 0:
            event_age_ns = timebase.now_ns() - source_ts_ns
            if event_age_ns > self._stale_event_threshold_ns:
                self._stale_event_skip_total += 1
                if self._stale_event_metric is not None:
                    self._stale_event_metric.inc()
                if self._stale_event_skip_total % 100 == 1:
                    logger.warning(
                        "stale_event_skipped",
                        age_ms=event_age_ns / 1_000_000,
                        threshold_ms=self._stale_event_threshold_ns / 1_000_000,
                        total_skipped=self._stale_event_skip_total,
                    )
                return
        # Invalidate on position delta events
        if hasattr(event, "delta_source"):
            self._positions_dirty = True

        if self._positions_dirty:
            self._positions_cache = self._build_positions_by_strategy()
            self._positions_dirty = False
        positions_by_strategy = self._positions_cache

        target_strat_id = getattr(event, "strategy_id", None)
        event_symbol = getattr(event, "symbol", "")

        # M-2: O(1) version-counter check replaces O(n) element-wise scan.
        # Length guard handles external monkey-patching (e.g. tests replacing .strategies list).
        if self._executors_version != self._strategies_version or len(self._strat_executors) != len(self.strategies):
            self._rebuild_executors()

        # Unit 10: Use index for O(1) targeted dispatch when target_strat_id is set
        if target_strat_id and target_strat_id in self._strat_index:
            executors_iter = [self._strat_executors[i] for i in self._strat_index[target_strat_id]]
        else:
            executors_iter = self._strat_executors

        # Use cached executors
        _event_had_drops = False
        for strategy, ctx, lat_m, int_m, alpha_intent_m, alpha_flat_m, alpha_last_ts_g in executors_iter:
            if not strategy.enabled:
                # S4: Check if halted strategy is eligible for cooldown recovery
                sid = strategy.strategy_id
                rc = self._rust_circuit
                if rc is not None:
                    should_reenable, _new_state = rc.check_cooldown(sid, timebase.now_ns())
                    if should_reenable:
                        strategy.enabled = True
                        logger.info("Strategy circuit cooldown elapsed — re-enabling in degraded", id=sid)
                    else:
                        continue
                elif self._circuit_states.get(sid) == "halted":
                    halted_at = self._circuit_halted_at_ns.get(sid, 0)
                    if halted_at and time.monotonic_ns() - halted_at >= self._circuit_cooldown_ns:
                        self._circuit_states[sid] = "degraded"
                        self._failure_counts[sid] = self._circuit_threshold // 2
                        self._circuit_success_counts[sid] = 0
                        strategy.enabled = True
                        logger.info("Strategy circuit cooldown elapsed — re-enabling in degraded", id=sid)
                        # Fall through to process this event
                    else:
                        continue
                else:
                    continue

            if target_strat_id and strategy.strategy_id != target_strat_id:
                continue

            _governor = self.strategy_governor
            if _governor is not None and _governor.is_quarantined(strategy.strategy_id):
                self._emit_trace(
                    "strategy_quarantine_skip",
                    trace_id,
                    {
                        "strategy_id": strategy.strategy_id,
                        "event_type": type(event).__name__,
                        "symbol": getattr(event, "symbol", ""),
                    },
                )
                # DECISION-08: Do NOT record circuit breaker failures during
                # quarantine. Quarantine is the containment mechanism; recording
                # failures for skipped events causes the circuit breaker to
                # permanently disable the strategy before quarantine expires.
                continue

            # Timeout circuit breaker: skip strategies that are broken, with auto-recovery
            sid_for_timeout = strategy.strategy_id
            if self._timeout_broken.get(sid_for_timeout, False):
                broken_at = self._timeout_broken_at_ns.get(sid_for_timeout, 0)
                if broken_at and (time.monotonic_ns() - broken_at) >= self._timeout_recover_ns:
                    self._timeout_broken[sid_for_timeout] = False
                    self._timeout_consecutive[sid_for_timeout] = 0
                    if self.metrics:
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid_for_timeout}").set(
                                0
                            )  # normal
                        except Exception:  # noqa: BLE001
                            pass
                    logger.info("Strategy timeout circuit breaker recovered", id=sid_for_timeout)
                else:
                    continue

            positions = positions_by_strategy.get(strategy.strategy_id) or positions_by_strategy.get("*", {})
            ctx.positions = dict(positions)  # Shallow copy to prevent strategy mutation corrupting cache

            start = time.perf_counter_ns()
            if getattr(self, "_trace_sampler", None) is not None:
                self._emit_trace(
                    "strategy_dispatch_start",
                    trace_id,
                    {
                        "strategy_id": strategy.strategy_id,
                        "event_type": type(event).__name__,
                        "symbol": getattr(event, "symbol", ""),
                    },
                )
            try:
                intents = strategy.handle_event(ctx, event)
            except Exception as e:  # noqa: BLE001 — wraps user strategy code
                logger.error("Strategy Exception", id=strategy.strategy_id, error=str(e))
                self._emit_trace(
                    "strategy_exception",
                    trace_id,
                    {
                        "strategy_id": strategy.strategy_id,
                        "event_type": type(event).__name__,
                        "error": str(e),
                    },
                )
                intents = []
                _gov = self.strategy_governor
                if _gov is not None:
                    transition = _gov.quarantine(strategy.strategy_id, reason="strategy_exception")
                    self._emit_trace(
                        "strategy_quarantined",
                        trace_id,
                        {
                            "strategy_id": strategy.strategy_id,
                            "event_type": type(event).__name__,
                            "reason": transition.reason,
                        },
                    )
                if self.metrics:
                    exc_m = getattr(self.metrics, "strategy_exceptions_total", None)
                    if exc_m:
                        exc_m.labels(
                            strategy=strategy.strategy_id,
                            exception_type=type(e).__name__,
                            method="handle_event",
                        ).inc()
                # S4: Circuit breaker 3-state FSM (normal → degraded → halted)
                sid = strategy.strategy_id
                rc = self._rust_circuit
                if rc is not None:
                    new_state, should_disable = rc.record_failure(sid, timebase.now_ns())
                    if should_disable:
                        strategy.enabled = False
                        logger.error(
                            "Strategy circuit breaker halted",
                            id=sid,
                            threshold=self._circuit_threshold,
                        )
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(2)
                        except Exception:  # noqa: BLE001
                            pass
                    elif new_state == 1:  # DEGRADED
                        logger.warning("Strategy circuit degraded", id=sid)
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(1)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    self._circuit_success_counts[sid] = 0
                    failures = self._failure_counts.get(sid, 0) + 1
                    self._failure_counts[sid] = failures
                    state = self._circuit_states.get(sid, "normal")
                    half_threshold = max(1, self._circuit_threshold // 2)
                    if state == "normal" and failures >= half_threshold:
                        self._circuit_states[sid] = "degraded"
                        logger.warning("Strategy circuit degraded", id=sid, failures=failures)
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(1)
                        except Exception:  # noqa: BLE001
                            pass
                    if failures >= self._circuit_threshold and state != "halted":
                        self._circuit_states[sid] = "halted"
                        strategy.enabled = False
                        self._circuit_halted_at_ns[sid] = time.monotonic_ns()
                        logger.error(
                            "Strategy circuit breaker halted",
                            id=sid,
                            failures=failures,
                            threshold=self._circuit_threshold,
                        )
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(2)
                        except Exception:  # noqa: BLE001
                            pass
            else:
                # S4: Gradual recovery: in degraded state, require N consecutive successes
                sid = strategy.strategy_id
                rc = self._rust_circuit
                if rc is not None:
                    _new_state, recovered = rc.record_success(sid)
                    if recovered:
                        logger.info("Strategy circuit recovered to normal", id=sid)
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(0)
                        except Exception:  # noqa: BLE001
                            pass
                else:
                    state = self._circuit_states.get(sid, "normal")
                    if state == "degraded":
                        sc = self._circuit_success_counts.get(sid, 0) + 1
                        self._circuit_success_counts[sid] = sc
                        if sc >= self._circuit_recovery_threshold:
                            self._circuit_states[sid] = "normal"
                            self._failure_counts[sid] = 0
                            self._circuit_success_counts[sid] = 0
                            logger.info("Strategy circuit recovered to normal", id=sid)
                            try:
                                self.metrics.circuit_breaker_state.labels(component=f"runner:{sid}").set(0)
                            except Exception:  # noqa: BLE001
                                pass

            # TrackGate per-intent filtering (session phase enforcement)
            if getattr(self, "track_gate", None) is not None and intents:
                intents = StrategyRunner.filter_intents_by_phase(
                    intents,
                    self.track_gate,
                    self.position_store,
                    strategy_id=strategy.strategy_id,
                )

            duration = time.perf_counter_ns() - start

            # Timeout circuit breaker: check wall-clock duration
            # GapEvent handlers do recovery work (reset state, re-request snapshots)
            # and should not count toward timeout strikes.
            _timeout_sid = strategy.strategy_id
            _is_gap = isinstance(event, GapEvent)
            if not _is_gap and duration > self._timeout_ns:
                consec = self._timeout_consecutive.get(_timeout_sid, 0) + 1
                self._timeout_consecutive[_timeout_sid] = consec
                if self.metrics:
                    _timeout_m = getattr(self.metrics, "strategy_timeout_total", None)
                    if _timeout_m is not None:
                        _timeout_m.labels(strategy_name=_timeout_sid).inc()
                logger.warning(
                    "Strategy handle_event exceeded timeout",
                    id=_timeout_sid,
                    duration_ns=duration,
                    timeout_ns=self._timeout_ns,
                    consecutive=consec,
                )
                if consec >= self._timeout_strikes_limit:
                    self._timeout_broken[_timeout_sid] = True
                    self._timeout_broken_at_ns[_timeout_sid] = time.monotonic_ns()
                    if self.metrics:
                        _cb_m = getattr(self.metrics, "strategy_circuit_break_total", None)
                        if _cb_m is not None:
                            _cb_m.labels(strategy_name=_timeout_sid).inc()
                        try:
                            self.metrics.circuit_breaker_state.labels(component=f"runner:{_timeout_sid}").set(
                                2
                            )  # halted
                        except Exception:  # noqa: BLE001
                            pass
                    logger.warning(
                        "Strategy timeout circuit breaker activated",
                        id=_timeout_sid,
                        strikes=consec,
                        recover_s=self._timeout_recover_ns / 1_000_000_000,
                    )
            elif not _is_gap:
                self._timeout_consecutive[_timeout_sid] = 0

            if getattr(self, "_trace_sampler", None) is not None:
                self._emit_trace(
                    "strategy_dispatch_done",
                    trace_id,
                    {
                        "strategy_id": strategy.strategy_id,
                        "event_type": type(event).__name__,
                        "duration_ns": int(duration),
                        "intent_count": len(intents or []),
                        "symbol": getattr(event, "symbol", ""),
                    },
                )

            # Direct metric use
            sid = strategy.strategy_id
            seq = self._strategy_metrics_seq.get(sid, 0) + 1
            self._strategy_metrics_seq[sid] = seq
            if lat_m and (seq % self._strategy_metrics_sample_every == 0):
                lat_m.observe(duration)
            if intents and int_m:
                if self._strategy_metrics_batch <= 1:
                    int_m.inc(len(intents))
                else:
                    self._strategy_pending_intents[sid] = self._strategy_pending_intents.get(sid, 0) + len(intents)
            # Alpha liveness: track signal outcome and last active timestamp
            if self.metrics and self._diagnostic_metrics_enabled:
                if intents:
                    if alpha_intent_m:
                        if self._strategy_metrics_batch <= 1:
                            alpha_intent_m.inc()
                        else:
                            self._strategy_pending_alpha_intent[sid] = (
                                self._strategy_pending_alpha_intent.get(sid, 0) + 1
                            )
                    if alpha_last_ts_g:
                        alpha_last_ts_g.set(timebase.now_s())
                else:
                    if alpha_flat_m:
                        if self._strategy_metrics_batch <= 1:
                            alpha_flat_m.inc()
                        else:
                            self._strategy_pending_alpha_flat[sid] = self._strategy_pending_alpha_flat.get(sid, 0) + 1
            if self._strategy_metrics_batch > 1 and (seq % self._strategy_metrics_batch == 0):
                pending_intents = self._strategy_pending_intents.get(sid, 0)
                if pending_intents and int_m:
                    int_m.inc(pending_intents)
                    self._strategy_pending_intents[sid] = 0
                pending_alpha_intent = self._strategy_pending_alpha_intent.get(sid, 0)
                if pending_alpha_intent and alpha_intent_m:
                    alpha_intent_m.inc(pending_alpha_intent)
                    self._strategy_pending_alpha_intent[sid] = 0
                pending_alpha_flat = self._strategy_pending_alpha_flat.get(sid, 0)
                if pending_alpha_flat and alpha_flat_m:
                    alpha_flat_m.inc(pending_alpha_flat)
                    self._strategy_pending_alpha_flat[sid] = 0
            if self.latency and self._diagnostic_metrics_enabled:
                self.latency.record(
                    "strategy",
                    duration,
                    trace_id=trace_id,
                    symbol=event_symbol,
                    strategy_id=strategy.strategy_id,
                )

            if intents:
                if len(intents) > self._max_intents_per_event:
                    logger.warning(
                        "strategy_intent_flood",
                        strategy_id=strategy.strategy_id,
                        intent_count=len(intents),
                        cap=self._max_intents_per_event,
                    )
                    intents = intents[: self._max_intents_per_event]
                _d7_submitted = 0
                _d7_dropped = 0
                for intent in intents:
                    # Populate decision prices from LOB L1 data
                    if self._lob_l1_source is not None:
                        _event_symbol = (
                            getattr(intent, "symbol", None)
                            if isinstance(intent, OrderIntent)
                            else (intent[3] if isinstance(intent, tuple) and len(intent) > 3 else None)
                        )
                        if _event_symbol:
                            _l1 = self._lob_l1_source(_event_symbol)
                            if _l1 is not None:
                                _mid = _l1[3] // 2  # mid_price_x2 // 2
                                if _mid > 0:
                                    if isinstance(intent, OrderIntent):
                                        intent.decision_mid = _mid  # deprecated: use decision_price
                                        intent.decision_price = _mid
                                    elif (
                                        isinstance(intent, tuple)
                                        and len(intent) >= 17
                                        and intent[0] == "typed_intent_v1"
                                    ):
                                        # Typed intent tuple: position 16 is decision_price
                                        intent = (*intent[:16], _mid)

                    self._emit_trace(
                        "strategy_intent_submit",
                        trace_id,
                        {
                            "strategy_id": strategy.strategy_id,
                            "intent_type": int(getattr(intent, "intent_type", -1))
                            if not (isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1")
                            else -2,
                            "typed": bool(isinstance(intent, tuple) and intent and intent[0] == "typed_intent_v1"),
                        },
                    )
                    try:
                        if (
                            self._typed_intent_fastpath
                            and isinstance(intent, tuple)
                            and intent
                            and intent[0] == "typed_intent_v1"
                        ):
                            self._risk_submit_typed(intent)
                        else:
                            self._risk_submit(intent)
                        _d7_submitted += 1
                    except asyncio.QueueFull:
                        _d7_dropped += 1
                        self.metrics.intent_queue_full_total.inc()
                        _fb_iid, _fb_sid, _fb_sym, _fb_side = _typed_intent_identity(intent)
                        logger.error(
                            "intent_submit_queue_full",
                            strategy_id=_fb_sid or "?",
                            symbol=_fb_sym,
                            submitted=_d7_submitted,
                            dropped=_d7_dropped,
                            batch_size=len(intents),
                        )
                        if self._rejection_sink is not None:
                            try:
                                self._rejection_sink.put_nowait(
                                    RiskFeedback(
                                        intent_id=_fb_iid,
                                        strategy_id=_fb_sid,
                                        symbol=_fb_sym,
                                        reason_code="risk_queue_full",
                                        timestamp_ns=timebase.now_ns(),
                                        side=Side(_fb_side) if _fb_side is not None else None,
                                    )
                                )
                            except asyncio.QueueFull:
                                self.metrics.rejection_sink_overflow_total.inc()
                                # DEC2-006: Direct callback fallback when sink is full.
                                # Without this, the strategy permanently leaks pending slots.
                                try:
                                    _inline_fb = RiskFeedback(
                                        intent_id=_fb_iid,
                                        strategy_id=_fb_sid,
                                        symbol=_fb_sym,
                                        reason_code="risk_queue_full_sink_overflow",
                                        timestamp_ns=timebase.now_ns(),
                                        side=Side(_fb_side) if _fb_side is not None else None,
                                    )
                                    if hasattr(strategy, "on_risk_feedback"):
                                        strategy.on_risk_feedback(_inline_fb)
                                except Exception as _fb_exc:
                                    logger.error(
                                        "inline_risk_feedback_failed",
                                        strategy_id=_fb_sid,
                                        error=str(_fb_exc),
                                    )
                if _d7_dropped > 0:
                    # QueueFull is an infrastructure backpressure issue, not a
                    # strategy fault. Do NOT advance the circuit breaker here —
                    # that would penalise healthy strategies for a full risk
                    # queue. The intent_queue_full_total metric (incremented
                    # per drop above) already provides observability.
                    logger.warning(
                        "intent_submit_queue_full_batch",
                        strategy_id=strategy.strategy_id,
                        submitted=_d7_submitted,
                        dropped=_d7_dropped,
                    )
                    _event_had_drops = True

        # Gradual degradation: track per-event (not per-strategy) to avoid
        # non-dropping strategies resetting the counter mid-event.
        if _event_had_drops:
            self._queue_full_consecutive += 1
            if self._storm_guard is not None:
                if self._queue_full_consecutive >= self._queue_full_halt_threshold:
                    self._storm_guard.trigger_halt("risk_queue_full_persistent")
                else:
                    self._storm_guard.trigger_storm("risk_queue_full")
        else:
            self._queue_full_consecutive = 0

    @staticmethod
    def filter_intents_by_phase(
        intents: list, track_gate: Any, position_store: Any = None, strategy_id: str | None = None
    ) -> list:
        """Filter intents based on session phase from TrackGate.

        During CLOSE_ONLY: allow CANCEL, FORCE_FLAT, and position-reducing IOC orders.
        During FORCE_FLAT: allow only CANCEL and FORCE_FLAT (position flattener handles exits).

        IOC NEW orders in CLOSE_ONLY are only permitted if they reduce existing exposure:
        - BUY is allowed only if net_qty < 0 (closing a short)
        - SELL is allowed only if net_qty > 0 (closing a long)
        If position_store is None, IOC NEW orders are conservatively blocked.
        """
        from hft_platform.ops.session_governor import SessionPhase  # noqa: PLC0415

        _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
        _BUY = int(Side.BUY)
        _SELL = int(Side.SELL)
        _filtered: list = []
        for _intent in intents:
            _intent_symbol = _typed_intent_symbol(_intent)
            _intent_type = _typed_intent_type(_intent)
            _phase = track_gate.get_phase(_intent_symbol)
            if _phase == SessionPhase.OPEN:
                _filtered.append(_intent)
            elif _phase == SessionPhase.CLOSE_ONLY:
                if _intent_type in _CLOSE_ONLY_TYPES:
                    _filtered.append(_intent)
                elif _intent_type == int(IntentType.NEW) and _typed_intent_tif(_intent) == int(TIF.IOC):
                    # Position-aware check: only allow IOC if it reduces exposure
                    _side = _typed_intent_side(_intent)
                    _net_qty = _get_symbol_net_qty(position_store, _intent_symbol, strategy_id)
                    if (_side == _BUY and _net_qty < 0) or (_side == _SELL and _net_qty > 0):
                        _filtered.append(_intent)
                    else:
                        logger.debug(
                            "close_only_ioc_blocked_no_exposure",
                            symbol=_intent_symbol,
                            side=_side,
                            net_qty=_net_qty,
                        )
            elif _phase == SessionPhase.FORCE_FLAT:
                if _intent_type in _CLOSE_ONLY_TYPES:
                    _filtered.append(_intent)
        return _filtered

    def _emit_trace(self, stage: str, trace_id: str, payload: dict[str, Any]) -> None:
        sampler = getattr(self, "_trace_sampler", None)
        if sampler is None:
            return
        try:
            sampler.emit(stage=stage, trace_id=str(trace_id or ""), payload=payload)
        except (TypeError, ValueError) as exc:
            logger.debug("trace_emit_failed", error=str(exc))
            return

    # Tuple timestamp index by tag for _extract_event_trace
    _TUPLE_TS_INDEX: dict[str, int] = {"tick": 7, "bidask": 4, "lobstats": 2}

    def _extract_event_trace(self, event: Any) -> tuple[int, str]:
        source_ts_ns = 0
        trace_id = ""
        meta = getattr(event, "meta", None)
        if meta is not None:
            source_ts_ns = int(getattr(meta, "local_ts", 0) or getattr(meta, "source_ts", 0) or 0)
            seq = getattr(meta, "seq", None)
            topic = getattr(meta, "topic", "event")
            if seq is not None:
                trace_id = f"{topic}:{seq}"
        elif isinstance(event, tuple) and len(event) > 2 and isinstance(event[0], str):
            ts_idx = self._TUPLE_TS_INDEX.get(event[0])
            if ts_idx is not None and len(event) > ts_idx:
                try:
                    source_ts_ns = int(event[ts_idx] or 0)
                except (TypeError, ValueError):
                    source_ts_ns = 0
        elif hasattr(event, "ts"):
            try:
                source_ts_ns = int(getattr(event, "ts") or 0)
            except (TypeError, ValueError):
                source_ts_ns = 0
        if not source_ts_ns:
            source_ts_ns = timebase.now_ns()
        return source_ts_ns, trace_id
