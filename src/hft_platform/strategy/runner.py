import asyncio
import os
import re
import time
from decimal import Decimal
from typing import Any, List

from structlog import get_logger

from hft_platform.contracts.strategy import OrderIntent
from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.strategy.base import BaseStrategy, StrategyContext
from hft_platform.strategy.registry import StrategyRegistry

logger = get_logger("strategy_runner")


def _obs_policy() -> str:
    value = str(os.getenv("HFT_OBS_POLICY", "")).strip().lower()
    if value in {"minimal", "balanced", "debug"}:
        return value
    return ""


class StrategyRunner:
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
        self._feature_tuple_source = getattr(fe, "get_feature_tuple", None) if fe else None

        self.metrics = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._obs_policy = _obs_policy()
        self._diagnostic_metrics_enabled = self._obs_policy != "minimal"
        self.symbol_metadata = symbol_metadata or SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.symbol_metadata))
        self._intent_seq = 0
        self._positions_cache: dict = {}
        self._positions_dirty = True
        self._current_source_ts_ns = 0
        self._current_trace_id = ""
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
        # Cache for parsed position keys: "pos:strat_id:symbol" → (strat_id, symbol)
        self._position_key_cache: dict[str, tuple[str, str]] = {}

        # Load initial
        for strat in self.registry.instantiate():
            self.register(strat)

        self.running = False

    async def run(self):
        self.running = True
        logger.info("StrategyRunner started")
        try:
            batch_size = int(os.getenv("HFT_BUS_BATCH_SIZE", "0") or "0")
            if batch_size > 1:
                async for batch in self.bus.consume_batch(batch_size):
                    for event in batch:
                        await self.process_event(event)
            else:
                async for event in self.bus.consume():
                    await self.process_event(event)
        except asyncio.CancelledError:
            pass
        finally:
            self._flush_pending_strategy_metrics()

    def register(self, strategy: BaseStrategy):
        self.strategies.append(strategy)
        self._resolve_strategy_symbols(strategy)
        self._strat_executors.append(self._build_executor_entry(strategy))
        sid = strategy.strategy_id
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
            except Exception:
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
            feature_tuple_source=self._feature_tuple_source,
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
                0,
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
        )

    def _scale_price(self, symbol: str, price: int | Decimal) -> int:
        return self.price_codec.scale(symbol, price)

    def _build_positions_by_strategy(self):
        if not self.position_store:
            return {}
        raw = getattr(self.position_store, "positions", None)
        if not isinstance(raw, dict):
            return {}

        # S1: Take a snapshot before iteration to prevent dict-changed-during-iteration
        # from concurrent broker callback threads that may update positions.
        raw = dict(raw)

        positions_by_strategy: dict = {}
        fallback: dict = {}

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

        if fallback:
            positions_by_strategy["*"] = fallback

        return positions_by_strategy

    def invalidate_positions(self):
        self._positions_dirty = True

    async def process_event(self, event: Any):
        source_ts_ns, trace_id = self._extract_event_trace(event)
        self._current_source_ts_ns = source_ts_ns
        self._current_trace_id = trace_id
        # Invalidate on position delta events
        if hasattr(event, "delta_source"):
            self._positions_dirty = True

        if self._positions_dirty:
            self._positions_cache = self._build_positions_by_strategy()
            self._positions_dirty = False
        positions_by_strategy = self._positions_cache

        target_strat_id = getattr(event, "strategy_id", None)
        event_symbol = getattr(event, "symbol", "")

        # Keep executors in sync with strategy list (tests may replace list)
        if not self._executors_match_strategy_list():
            self._rebuild_executors()

        # Use cached executors
        for strategy, ctx, lat_m, int_m, alpha_intent_m, alpha_flat_m, alpha_last_ts_g in self._strat_executors:
            if not strategy.enabled:
                # S4: Check if halted strategy is eligible for cooldown recovery
                sid = strategy.strategy_id
                if self._circuit_states.get(sid) == "halted":
                    halted_at = self._circuit_halted_at_ns.get(sid, 0)
                    if halted_at and timebase.now_ns() - halted_at >= self._circuit_cooldown_ns:
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

            positions = positions_by_strategy.get(strategy.strategy_id) or positions_by_strategy.get("*", {})
            ctx.positions = positions

            start = time.perf_counter_ns()
            try:
                intents = strategy.handle_event(ctx, event)
            except Exception as e:
                logger.error("Strategy Exception", id=strategy.strategy_id, error=str(e))
                intents = []
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
                self._circuit_success_counts[sid] = 0
                failures = self._failure_counts.get(sid, 0) + 1
                self._failure_counts[sid] = failures
                state = self._circuit_states.get(sid, "normal")
                half_threshold = max(1, self._circuit_threshold // 2)
                if state == "normal" and failures >= half_threshold:
                    self._circuit_states[sid] = "degraded"
                    logger.warning("Strategy circuit degraded", id=sid, failures=failures)
                if failures >= self._circuit_threshold and state != "halted":
                    self._circuit_states[sid] = "halted"
                    strategy.enabled = False
                    self._circuit_halted_at_ns[sid] = timebase.now_ns()
                    logger.error(
                        "Strategy circuit breaker halted",
                        id=sid,
                        failures=failures,
                        threshold=self._circuit_threshold,
                    )
            else:
                # S4: Gradual recovery: in degraded state, require N consecutive successes
                sid = strategy.strategy_id
                state = self._circuit_states.get(sid, "normal")
                if state == "degraded":
                    sc = self._circuit_success_counts.get(sid, 0) + 1
                    self._circuit_success_counts[sid] = sc
                    if sc >= self._circuit_recovery_threshold:
                        self._circuit_states[sid] = "normal"
                        self._failure_counts[sid] = 0
                        self._circuit_success_counts[sid] = 0
                        logger.info("Strategy circuit recovered to normal", id=sid)

            duration = time.perf_counter_ns() - start

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
                        alpha_last_ts_g.set(time.monotonic())
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
                for intent in intents:
                    if (
                        self._typed_intent_fastpath
                        and isinstance(intent, tuple)
                        and intent
                        and intent[0] == "typed_intent_v1"
                    ):
                        self._risk_submit_typed(intent)
                    else:
                        self._risk_submit(intent)

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
        elif hasattr(event, "ts"):
            try:
                source_ts_ns = int(getattr(event, "ts") or 0)
            except Exception:
                source_ts_ns = 0
        if not source_ts_ns:
            source_ts_ns = timebase.now_ns()
        return source_ts_ns, trace_id
