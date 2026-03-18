"""MarketDataService — orchestrates ingestion, normalization, LOB, feature, and recording.

Delegates to private helper modules:
- ``_md_ingestion``     — payload parsing, constants, ``FeedState``
- ``_md_observability``  — metrics, tracing, feature-shadow parity
- ``_md_reconnect``      — reconnection, rollover, watchdog, trading-hours
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from typing import Any
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import BidAskEvent, FeatureUpdateEvent, TickEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata

# TODO: replace with BrokerClientProtocol once WU-1 merges
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry

from ._md_ingestion import (
    FeedState,
    env_int,
    get_trace_sampler,
    looks_like_md,
    obs_policy,
    summarize_md,
    try_fast_extract_callback_payload,
    unwrap_md,
)
from ._md_observability import MarketDataObservabilityMixin
from ._md_reconnect import MarketDataReconnectMixin

logger = get_logger("service.market_data")

# Re-export so ``from hft_platform.services.market_data import FeedState`` keeps working.
__all__ = ["FeedState", "MarketDataService"]


class MarketDataService(MarketDataObservabilityMixin, MarketDataReconnectMixin):
    def __init__(
        self,
        bus: RingBufferBus,
        raw_queue: asyncio.Queue,
        client: Any,  # BrokerClient (ShioajiClient | FubonClientFacade)
        publish_full_events: bool = True,
        symbol_metadata: SymbolMetadata | None = None,
        recorder_queue: asyncio.Queue | None = None,
        feature_engine: FeatureEngine | None = None,
    ):
        self.bus = bus
        self.raw_queue = raw_queue
        self.client = client
        self.publish_full_events = publish_full_events
        self.recorder_queue = recorder_queue

        self.lob = LOBEngine()
        feature_enabled = os.getenv("HFT_FEATURE_ENGINE_ENABLED", "0").lower() in {"1", "true", "yes", "on"}
        self.feature_engine = feature_engine or (FeatureEngine() if feature_enabled else None)
        try:
            setattr(self.lob, "feature_engine", self.feature_engine)
        except Exception:
            pass
        self._feature_shadow_engine: FeatureEngine | None = None
        self.symbol_metadata = symbol_metadata or SymbolMetadata()
        self.normalizer = MarketDataNormalizer(metadata=self.symbol_metadata)

        self.state = FeedState.INIT
        self.running = False
        self.last_event_ts = timebase.now_s()
        self.last_event_mono = time.monotonic()
        self.heartbeat_threshold_s = 5.0
        self.resubscribe_gap_s = float(os.getenv("HFT_MD_RESUBSCRIBE_GAP_S", "15"))
        self.resubscribe_cooldown_s = float(os.getenv("HFT_MD_RESUBSCRIBE_COOLDOWN_S", "15"))
        self.reconnect_gap_s = float(os.getenv("HFT_MD_RECONNECT_GAP_S", "60"))
        self.force_reconnect_gap_s = float(os.getenv("HFT_MD_FORCE_RECONNECT_GAP_S", "300"))
        self.reconnect_cooldown_s = float(os.getenv("HFT_MD_RECONNECT_COOLDOWN_S", "60"))
        self.reconnect_timeout_s = float(os.getenv("HFT_MD_RECONNECT_TIMEOUT_S", "30"))
        self._heartbeat_gap_metric_cooldown_s = float(os.getenv("HFT_MD_GAP_METRIC_COOLDOWN_S", "30"))
        self._last_heartbeat_gap_metric_ts = 0.0
        self.reconnect_days = {
            d.strip().lower() for d in os.getenv("HFT_RECONNECT_DAYS", "").split(",") if d.strip()
        }
        self.reconnect_hours = os.getenv("HFT_RECONNECT_HOURS", "")
        self.reconnect_hours_2 = os.getenv("HFT_RECONNECT_HOURS_2", "")
        self.reconnect_tz = os.getenv("HFT_RECONNECT_TZ") or timebase.TZ_NAME or "Asia/Taipei"
        try:
            self._reconnect_tzinfo: dt.tzinfo = ZoneInfo(self.reconnect_tz)
        except Exception:
            logger.warning("Invalid reconnect tz, defaulting to UTC", tz=self.reconnect_tz)
            self._reconnect_tzinfo = dt.timezone.utc
        self._last_reconnect_ts = 0.0
        self._last_resubscribe_ts = 0.0
        self._resubscribe_attempts = 0
        self._last_rollover_reconnect_date: dt.date | None = None
        self._last_rollover_seen_date: dt.date | None = None
        self._pending_reconnect_reason: str | None = None
        self._pending_reconnect_gap: float = 0.0
        self._pending_reconnect_since: float | None = None
        self.metrics: dict[str, Any] = {"count": 0, "start_ts": timebase.now_s()}
        self.metrics_registry = MetricsRegistry.get()
        self.latency = LatencyRecorder.get()
        self._trace_sampler = get_trace_sampler()
        self._feed_last_event_metric_child = None
        self._feed_reconnect_gap_metric_child = None
        self._md_callback_parse_metric_children: dict[str, Any] = {}
        self._feature_update_metric_children: dict[tuple[str, str], Any] = {}
        self._feature_quality_flag_metric_children: dict[str, Any] = {}
        self._feature_latency_metric_child = None
        self._feature_shadow_checks_metric_children: dict[tuple[str, str], Any] = {}
        self._feature_shadow_mismatch_metric_children: dict[tuple[str, str], Any] = {}
        self._feature_set_id_cached = (
            str(self.feature_engine.feature_set_id())
            if self.feature_engine and hasattr(self.feature_engine, "feature_set_id")
            else "unknown"
        )
        self.log_raw = os.getenv("HFT_MD_LOG_RAW", "0") == "1"
        self.log_raw_every = int(os.getenv("HFT_MD_LOG_EVERY", "1000"))
        self._raw_log_counter = 0
        self.log_normalized = os.getenv("HFT_MD_LOG_NORMALIZED", "0") == "1"
        self.log_normalized_every = int(os.getenv("HFT_MD_LOG_NORMALIZED_EVERY", "1000"))
        self._normalized_log_counter = 0
        self._raw_first_seen = False
        self._raw_first_parsed = False
        self._first_tick_event = False
        self._first_bidask_event = False
        self._record_direct = self.recorder_queue is not None and os.getenv(
            "HFT_MD_RECORD_DIRECT", "1"
        ).lower() not in {"0", "false", "no", "off"}
        drop_default = os.getenv("HFT_RECORDER_DROP_ON_FULL", "1")
        self._record_drop_on_full = os.getenv("HFT_MD_RECORD_DROP_ON_FULL", drop_default).lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # EC-3: Recorder queue overflow graceful degradation
        self._record_degrade_threshold = int(os.getenv("HFT_RECORD_DEGRADE_THRESHOLD", "500"))
        self._record_degraded = False
        self._record_degraded_since: float = 0.0
        self._record_degraded_drops = 0
        self._record_degrade_check_s = 10.0
        self._record_degrade_last_check: float = 0.0

        # Per-symbol feed gap monitoring
        self._symbol_last_tick: dict[str, float] = {}
        self._symbol_gap_threshold_s = float(os.getenv("HFT_SYMBOL_GAP_THRESHOLD_S", "6.0"))
        self._watchdog_interval_s = float(os.getenv("HFT_WATCHDOG_INTERVAL_S", "1.0"))
        self._symbol_gap_min_stale_count = max(1, int(os.getenv("HFT_SYMBOL_GAP_MIN_STALE_COUNT", "5")))
        self._symbol_gap_min_active_symbols = max(1, int(os.getenv("HFT_SYMBOL_GAP_MIN_ACTIVE_SYMBOLS", "24")))
        self._symbol_gap_active_lookback_s = max(
            0.0, float(os.getenv("HFT_SYMBOL_GAP_ACTIVE_LOOKBACK_S", "90.0"))
        )
        self._symbol_gap_stale_ratio_threshold = min(
            1.0,
            max(0.0, float(os.getenv("HFT_SYMBOL_GAP_STALE_RATIO_THRESHOLD", "0.85"))),
        )
        self._symbol_gap_severe_gap_s = max(0.0, float(os.getenv("HFT_SYMBOL_GAP_SEVERE_GAP_S", "30.0")))
        self._symbol_gap_consecutive_cycles = max(1, int(os.getenv("HFT_SYMBOL_GAP_CONSECUTIVE_CYCLES", "5")))
        self._symbol_gap_consecutive_hits = 0
        self._symbol_gap_resubscribe_cooldown_s = float(
            os.getenv("HFT_SYMBOL_GAP_RESUBSCRIBE_COOLDOWN_S", "120")
        )
        self._last_symbol_gap_resubscribe_ts = 0.0
        self._symbol_gap_metric_cooldown_s = float(os.getenv("HFT_SYMBOL_GAP_METRIC_COOLDOWN_S", "30"))
        self._last_symbol_gap_metric_ts = 0.0
        self._symbol_gap_skip_off_hours = os.getenv("HFT_SYMBOL_GAP_SKIP_OFF_HOURS", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._symbol_gap_off_hours_log_interval_s = float(
            os.getenv("HFT_SYMBOL_GAP_OFF_HOURS_LOG_INTERVAL_S", "300")
        )
        self._last_symbol_gap_off_hours_log_ts = 0.0
        self._symbol_tick_inline = os.getenv("HFT_SYMBOL_TICK_INLINE", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # P0-1: raw_queue backpressure tracking
        raw_queue_maxsize = getattr(self.raw_queue, "maxsize", 0) or 0
        self._raw_queue_size = (
            raw_queue_maxsize if raw_queue_maxsize > 0 else int(os.getenv("HFT_RAW_QUEUE_SIZE", "10000"))
        )
        self._raw_queue_high_watermark = float(os.getenv("HFT_RAW_QUEUE_HIGH_WATERMARK", "0.8"))
        self._dropped_count = 0
        self._high_watermark_warned = False

        # Market open grace period (C4)
        self._market_open_grace_s = float(os.getenv("HFT_MARKET_OPEN_GRACE_S", "60"))
        self._market_open_grace_gap_threshold_s = float(os.getenv("HFT_MARKET_OPEN_GRACE_GAP_S", "30"))

        # Observability sampling
        policy = obs_policy()
        md_metrics_default = 16 if policy == "minimal" else (4 if policy == "balanced" else 1)
        md_latency_default = 16 if policy == "minimal" else (4 if policy == "balanced" else 1)
        cb_parse_metrics_default = 64 if policy != "debug" else 1
        feature_metrics_default = 16 if policy == "minimal" else (4 if policy == "balanced" else 1)
        feature_latency_default = 16 if policy == "minimal" else (4 if policy == "balanced" else 1)
        self._md_metrics_sample_every = env_int("HFT_MD_METRICS_SAMPLE_EVERY", md_metrics_default)
        self._md_latency_sample_every = env_int("HFT_MD_LATENCY_SAMPLE_EVERY", md_latency_default)
        self._md_callback_parse_metrics_every = env_int(
            "HFT_MD_CALLBACK_PARSE_METRICS_EVERY", cb_parse_metrics_default
        )
        self._feature_metrics_sample_every = env_int(
            "HFT_FEATURE_METRICS_SAMPLE_EVERY", feature_metrics_default
        )
        self._feature_latency_sample_every = env_int(
            "HFT_FEATURE_LATENCY_SAMPLE_EVERY", feature_latency_default
        )
        self._feature_shadow_sample_every = env_int(
            "HFT_FEATURE_SHADOW_SAMPLE_EVERY", 64 if policy != "debug" else 1
        )
        self._feature_shadow_warn_every = env_int("HFT_FEATURE_SHADOW_WARN_EVERY", 100)
        self._feature_shadow_abs_tolerance = float(os.getenv("HFT_FEATURE_SHADOW_ABS_TOL", "0"))
        self._md_metrics_counter = 0
        self._md_latency_counter = 0
        self._md_callback_parse_counter = 0
        self._feature_metrics_counter = 0
        self._feature_latency_counter = 0
        self._feature_shadow_counter = 0
        self._feature_shadow_mismatch_counter = 0

        self._init_feature_shadow_engine()

    # -- main loop -----------------------------------------------------------

    async def run(self) -> None:
        self.running = True
        self.loop = asyncio.get_running_loop()
        logger.info("MarketDataService started")
        self.lob.start_metrics_worker(self.loop)

        monitor_task = asyncio.create_task(self._monitor_loop())
        watchdog_task = asyncio.create_task(self._watchdog_loop())

        await self._connect_sequence()

        try:
            while self.running:
                msg = await self.raw_queue.get()
                if isinstance(msg, tuple) and len(msg) == 2:
                    _exchange, raw = msg
                else:
                    raw = msg
                self.last_event_ts = timebase.now_s()
                self.last_event_mono = time.monotonic()
                self._md_metrics_counter += 1
                if self._md_metrics_counter % self._md_metrics_sample_every == 0:
                    if self._feed_last_event_metric_child is None:
                        self._feed_last_event_metric_child = self.metrics_registry.feed_last_event_ts.labels(
                            source="market_data"
                        )
                    self._feed_last_event_metric_child.set(self.last_event_ts)
                self.metrics["count"] += 1

                # P0-1: Track queue depth and high watermark
                qsize = self.raw_queue.qsize()
                if self.metrics_registry and self._md_metrics_counter % self._md_metrics_sample_every == 0:
                    self.metrics_registry.raw_queue_depth.set(qsize)
                if self._raw_queue_size > 0:
                    utilization = qsize / self._raw_queue_size
                    if utilization >= self._raw_queue_high_watermark and not self._high_watermark_warned:
                        self._high_watermark_warned = True
                        logger.warning(
                            "raw_queue high watermark",
                            utilization=round(utilization, 2),
                            qsize=qsize,
                            limit=self._raw_queue_size,
                        )
                    elif utilization < self._raw_queue_high_watermark * 0.8:
                        self._high_watermark_warned = False

                if self.log_raw:
                    self._raw_log_counter += 1
                    if self._raw_log_counter % self.log_raw_every == 0:
                        raw_type = type(raw).__name__
                        if isinstance(raw, dict):
                            sample: Any = {
                                k: raw.get(k)
                                for k in ("code", "close", "bid_price", "ask_price", "ts")
                                if k in raw
                            }
                        else:
                            sample = getattr(raw, "code", None) or raw_type
                        logger.info("MD Raw Recv", type=raw_type, sample=str(sample)[:200])

                self._process_raw(raw)
                self.raw_queue.task_done()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("MD Error", error=str(e))
        finally:
            monitor_task.cancel()
            watchdog_task.cancel()

    def _process_raw(self, raw: Any) -> None:
        """Normalize, update LOB/features, publish, and record a single raw message."""
        event = None
        try:
            norm_start_ns = time.perf_counter_ns()
            if isinstance(raw, dict):
                is_bid = "bid_price" in raw or "bid_volume" in raw or "ask_price" in raw
                is_tick = "close" in raw or "price" in raw
            else:
                is_bid = hasattr(raw, "bid_price") or hasattr(raw, "bid_volume") or hasattr(raw, "ask_price")
                is_tick = hasattr(raw, "close") or hasattr(raw, "price")

            if is_bid:
                event = self.normalizer.normalize_bidask(raw)
            elif is_tick:
                event = self.normalizer.normalize_tick(raw)
            norm_duration = time.perf_counter_ns() - norm_start_ns
        except Exception as ne:
            logger.error("Normalization check failed", error=str(ne), raw_type=str(type(raw)))
            self._emit_trace("md_normalize_error", "", {"raw_type": str(type(raw)), "error": str(ne)})
            norm_duration = 0

        if not event:
            return

        self._log_first_event(event)
        self._update_symbol_tick_inline(event)

        trace_id = self._build_trace_id(event)
        self._md_latency_counter += 1
        if norm_duration and self.latency and self._md_latency_counter % self._md_latency_sample_every == 0:
            self.latency.record(
                "normalize",
                norm_duration,
                trace_id=trace_id,
                symbol=getattr(event, "symbol", ""),
            )
        if self.log_normalized:
            self._normalized_log_counter += 1
            if self._normalized_log_counter % self.log_normalized_every == 0:
                logger.info("MD Normalized", type=str(type(event)), symbol=event.symbol)

        lob_start_ns = time.perf_counter_ns()
        stats = self.lob.process_event(event)
        feature_update = self._maybe_update_features(event, stats)
        lob_duration = time.perf_counter_ns() - lob_start_ns
        if self.latency and self._md_latency_counter % self._md_latency_sample_every == 0:
            self.latency.record(
                "lob_process",
                lob_duration,
                trace_id=trace_id,
                symbol=getattr(event, "symbol", ""),
            )
        self._emit_trace(
            "md_event",
            trace_id,
            {
                "symbol": getattr(event, "symbol", ""),
                "event_type": type(event).__name__,
                "norm_ns": int(norm_duration or 0),
                "lob_ns": int(lob_duration or 0),
                "has_stats": bool(stats is not None),
                "has_feature_update": bool(feature_update is not None),
            },
        )
        if feature_update is not None:
            self._emit_trace(
                "feature_update",
                trace_id,
                {
                    "symbol": getattr(feature_update, "symbol", getattr(event, "symbol", "")),
                    "feature_set_id": getattr(
                        feature_update, "feature_set_id", self._feature_set_id_cached
                    ),
                    "quality_flags": int(getattr(feature_update, "quality_flags", 0) or 0),
                    "changed_mask": int(getattr(feature_update, "changed_mask", 0) or 0),
                },
            )
        if self._record_direct and isinstance(event, (TickEvent, BidAskEvent)):
            self._record_direct_event(event)

        self._publish_events(event, stats, feature_update)

    # -- helpers for _process_raw -------------------------------------------

    def _log_first_event(self, event: TickEvent | BidAskEvent) -> None:
        if isinstance(event, TickEvent) and not self._first_tick_event:
            self._first_tick_event = True
            logger.info("First Tick event", symbol=event.symbol, price=event.price, volume=event.volume)
        elif isinstance(event, BidAskEvent) and not self._first_bidask_event:
            self._first_bidask_event = True
            bids_len = len(event.bids) if event.bids is not None else 0
            asks_len = len(event.asks) if event.asks is not None else 0
            logger.info(
                "First BidAsk event",
                symbol=event.symbol,
                snapshot=event.is_snapshot,
                bids_len=bids_len,
                asks_len=asks_len,
            )

    def _update_symbol_tick_inline(self, event: TickEvent | BidAskEvent) -> None:
        symbol = getattr(event, "symbol", None)
        if not symbol:
            return
        if self._symbol_tick_inline:
            self._symbol_last_tick[symbol] = time.monotonic()
        else:
            asyncio.create_task(self._update_symbol_tick(symbol))

    @staticmethod
    def _build_trace_id(event: TickEvent | BidAskEvent) -> str:
        meta = getattr(event, "meta", None)
        if meta is not None:
            seq = getattr(meta, "seq", None)
            topic = getattr(meta, "topic", "event")
            if seq is not None:
                return f"{topic}:{seq}"
        return ""

    def _publish_events(
        self,
        event: TickEvent | BidAskEvent,
        stats: object | None,
        feature_update: FeatureUpdateEvent | None,
    ) -> None:
        if self.publish_full_events:
            if stats or feature_update:
                payload: list[Any] = [event]
                if stats:
                    payload.append(stats)
                if feature_update:
                    payload.append(feature_update)
                self._publish_many_nowait(payload)
            else:
                self._publish_nowait(event)
        elif stats or feature_update:
            payload = []
            if stats:
                payload.append(stats)
            if feature_update:
                payload.append(feature_update)
            self._publish_many_nowait(payload)

    # -- connect sequence ----------------------------------------------------

    async def _connect_sequence(self) -> None:
        try:
            self._set_state(FeedState.CONNECTING)
            await self._call_client(self.client.login)
            await self._call_client(self.client.validate_symbols)

            self._set_state(FeedState.SNAPSHOTTING)
            try:
                snapshots = await self._call_client(self.client.fetch_snapshots)
            except Exception as exc:
                logger.warning("Snapshot fetch failed; continuing", error=str(exc))
                snapshots = []

            for snap in snapshots:
                try:
                    normalize_fn = getattr(self.normalizer, "normalize_snapshot", None)
                    if normalize_fn is None:
                        logger.warning("Normalizer does not implement normalize_snapshot; skipping snapshot")
                        break
                    event = normalize_fn(snap)
                    if not event:
                        continue
                    stats = self.lob.process_event(event)
                    feature_update = self._maybe_update_features(event, stats)
                    self._publish_events(event, stats, feature_update)
                except Exception as exc:
                    logger.warning("Snapshot normalize failed; skipping", error=str(exc))

            await self._call_client(self.client.subscribe_basket, self._on_shioaji_event)
            self._set_state(FeedState.CONNECTED)

        except Exception as e:
            logger.error("Connect failed", error=str(e))
            self._set_state(FeedState.DISCONNECTED)

    # -- broker callback -----------------------------------------------------

    def _on_shioaji_event(self, *args: Any, **kwargs: Any) -> None:
        """Unified callback for Shioaji events."""
        try:
            if not self._raw_first_seen:
                self._raw_first_seen = True
                logger.info(
                    "First quote callback",
                    args_types=[type(a).__name__ for a in args],
                    kwargs_keys=list(kwargs.keys()),
                )
            if self.log_raw:
                logger.debug("Callback hit", args_len=len(args))

            exchange, msg = try_fast_extract_callback_payload(*args, **kwargs)
            parse_result = "fast" if msg is not None else "fallback"

            if msg is None:
                if exchange is None and "exchange" in kwargs:
                    exchange = kwargs["exchange"]
                for arg in args:
                    candidate = unwrap_md(arg)
                    is_md = looks_like_md(candidate)
                    if exchange is None and not is_md and (hasattr(arg, "name") or isinstance(arg, str)):
                        exchange = arg
                    if is_md:
                        msg = candidate
                if msg is None:
                    if len(args) >= 2:
                        msg = unwrap_md(args[-1])
                    elif len(args) == 1:
                        msg = unwrap_md(args[0])
                if msg is not None:
                    msg = unwrap_md(msg)
                parse_result = "fallback" if msg is not None else "miss"

            if self.metrics_registry:
                self._md_callback_parse_counter += 1
                if self._md_callback_parse_counter % self._md_callback_parse_metrics_every == 0:
                    try:
                        if hasattr(self.metrics_registry, "market_data_callback_parse_total"):
                            child = self._md_callback_parse_metric_children.get(parse_result)
                            if child is None:
                                child = self.metrics_registry.market_data_callback_parse_total.labels(
                                    result=parse_result
                                )
                                self._md_callback_parse_metric_children[parse_result] = child
                            child.inc()
                    except Exception:
                        pass

            if not self.log_raw and msg is not None and not self._raw_first_parsed:
                self._raw_first_parsed = True
                logger.info(
                    "Quote callback parsed",
                    msg_type=type(msg).__name__,
                    msg_code=getattr(msg, "code", None),
                    exchange=str(exchange) if exchange is not None else None,
                    msg_fields=summarize_md(msg),
                )

            if hasattr(self, "loop"):
                if msg is not None:
                    self.loop.call_soon_threadsafe(self._enqueue_raw, exchange, msg)
                else:
                    if self.log_raw:
                        logger.warning(
                            "Could not parse msg from callback args",
                            args_types=[type(a).__name__ for a in args],
                        )
            else:
                logger.error("Callback loop missing")

        except Exception as e:
            self._record_shioaji_crash_signature(str(e), context="md_callback")
            logger.error(f"Error in Shioaji callback: {e}")

    # -- monitor loop -------------------------------------------------------

    async def _monitor_loop(self) -> None:
        while self.running:
            await asyncio.sleep(5.0)

            now = timebase.now_s()
            elapsed = now - self.metrics["start_ts"]
            count = self.metrics["count"]
            eps = count / elapsed if elapsed > 0 else 0

            self.metrics["count"] = 0
            self.metrics["start_ts"] = now

            raw_q = self.raw_queue.qsize()
            logger.info("Metrics", eps=round(eps, 2), raw_queue=raw_q, state=self.state)

            gap = time.monotonic() - self.last_event_mono
            await self._run_monitor_reconnect_checks(gap)

            if self.symbol_metadata.reload_if_changed():
                logger.info("Symbols config reloaded", count=len(self.symbol_metadata.meta))
                try:
                    await asyncio.to_thread(self.client.reload_symbols)
                except Exception as exc:
                    logger.error("Symbol reload failed", error=str(exc))

    # -- symbol tick (legacy async path) ------------------------------------

    async def _update_symbol_tick(self, symbol: str) -> None:
        self._symbol_last_tick[symbol] = time.monotonic()

    # -- bus publish helpers -------------------------------------------------

    async def _publish(self, event: Any) -> None:
        publish_fn = getattr(self.bus, "publish", None)
        if not publish_fn:
            return
        result = publish_fn(event)
        if asyncio.iscoroutine(result):
            await result

    def _publish_nowait(self, event: Any) -> None:
        publish_nowait = getattr(self.bus, "publish_nowait", None)
        if publish_nowait:
            publish_nowait(event)
            return
        asyncio.create_task(self._publish(event))

    def _publish_many_nowait(self, events: list[Any]) -> None:
        publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
        if publish_many_nowait:
            publish_many_nowait(events)
            return
        for event in events:
            self._publish_nowait(event)

    # -- recorder direct event -----------------------------------------------

    def _record_direct_event(self, event: TickEvent | BidAskEvent) -> None:
        if self.recorder_queue is None:
            return

        # EC-3: Skip recording entirely in degraded mode
        if self._record_degraded:
            now = time.monotonic()
            if now - self._record_degrade_last_check >= self._record_degrade_check_s:
                self._record_degrade_last_check = now
                qsize = self.recorder_queue.qsize()
                maxsize = getattr(self.recorder_queue, "maxsize", 0) or 0
                if maxsize > 0 and qsize < maxsize * 0.5:
                    logger.info(
                        "Recorder queue recovered, exiting degraded mode",
                        drops_during_degraded=self._record_degraded_drops,
                        degraded_duration_s=round(now - self._record_degraded_since, 1),
                    )
                    self._record_degraded = False
                    self._record_degraded_drops = 0
                else:
                    self._record_degraded_drops += 1
                    return
            else:
                self._record_degraded_drops += 1
                return

        try:
            from hft_platform.recorder.mapper import map_event_to_record

            mapped = map_event_to_record(event, self.symbol_metadata, self.normalizer.price_codec)
        except Exception as exc:
            logger.warning("Direct record mapping failed", error=str(exc), event_type=type(event).__name__)
            return
        if not mapped:
            return
        topic, payload = mapped
        if self._record_drop_on_full:
            try:
                self.recorder_queue.put_nowait({"topic": topic, "data": payload})
            except asyncio.QueueFull:
                self._dropped_count += 1
                if self._dropped_count >= self._record_degrade_threshold and not self._record_degraded:
                    self._record_degraded = True
                    self._record_degraded_since = time.monotonic()
                    self._record_degrade_last_check = self._record_degraded_since
                    self._record_degraded_drops = 0
                    logger.warning(
                        "Recorder queue overflow: entering degraded mode",
                        consecutive_drops=self._dropped_count,
                        threshold=self._record_degrade_threshold,
                    )
        else:
            asyncio.create_task(self.recorder_queue.put({"topic": topic, "data": payload}))

    def _enqueue_raw(self, exchange: Any, msg: Any) -> None:
        """Enqueue raw quote messages with backpressure handling."""
        try:
            self.raw_queue.put_nowait((exchange, msg))
        except asyncio.QueueFull:
            self._dropped_count += 1
            if self.metrics_registry:
                self.metrics_registry.raw_queue_dropped_total.inc()
            if self._dropped_count % 100 == 1:
                logger.warning(
                    "raw_queue full, dropping tick",
                    dropped=self._dropped_count,
                    queue_size=self._raw_queue_size,
                )

    def _set_state(self, new_state: FeedState) -> None:
        if self.state != new_state:
            logger.info("State change", old=self.state, new=new_state)
            self.state = new_state

    async def _call_client(self, func: Any, *args: Any) -> Any:
        if os.getenv("HFT_MD_SYNC_CONNECT") == "1":
            return func(*args)
        if hasattr(func, "assert_called") or getattr(func, "_mock_name", None):
            return func(*args)
        return await asyncio.to_thread(func, *args)

    # -- public API ----------------------------------------------------------

    def get_max_feed_gap_s(self) -> float:
        """Return the maximum feed gap across all symbols in seconds."""
        try:
            tick_values = list(self._symbol_last_tick.values())
        except RuntimeError:
            return 0.0

        if not tick_values:
            return float(os.getenv("HFT_FEED_GAP_NO_DATA_S", "0.0"))

        now = time.monotonic()
        max_gap = 0.0
        for last_ts in tick_values:
            gap = now - last_ts
            if gap > max_gap:
                max_gap = gap
        return max_gap

    def get_feed_gaps_by_symbol(self) -> dict[str, float]:
        """Return feed gap for each symbol in seconds."""
        try:
            tick_snapshot = dict(self._symbol_last_tick)
        except RuntimeError:
            return {}
        if not tick_snapshot:
            return {}
        now = time.monotonic()
        return {symbol: now - last_ts for symbol, last_ts in tick_snapshot.items()}
