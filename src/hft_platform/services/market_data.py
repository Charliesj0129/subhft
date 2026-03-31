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
from typing import Any, cast
from zoneinfo import ZoneInfo

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.engine.event_bus import RingBufferBus
from hft_platform.events import BidAskEvent, FeatureUpdateEvent, LOBStatsEvent, TickEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feed_adapter.lob_engine import LOBEngine
from hft_platform.feed_adapter.normalizer import MarketDataNormalizer, SymbolMetadata

# TODO: replace with BrokerClientProtocol once WU-1 merges
from hft_platform.feed_adapter.shioaji.signatures import detect_crash_signature
from hft_platform.ipc.shm_snapshot import ShmSnapshotWriter, _symbol_hash
from hft_platform.observability.latency import LatencyRecorder
from hft_platform.observability.metrics import MetricsRegistry

from ._md_ingestion import (
    _FEATURE_QUALITY_FLAG_LABELS,
    _MD_CODE_FIELDS,
    _MD_NESTED_FIELDS,
    _MD_PRICE_FIELDS,
    _MD_TIME_FIELDS,
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


def _get_trace_sampler():
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler

        return get_trace_sampler()
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        return None


def _looks_like_md(obj: object) -> bool:
    if obj is None:
        return False
    if isinstance(obj, dict):
        keys = obj.keys()
        if "code" in keys or "symbol" in keys:
            return True
        if (
            "bid_price" in keys
            or "ask_price" in keys
            or "close" in keys
            or "price" in keys
            or "bid_volume" in keys
            or "ask_volume" in keys
            or "buy_price" in keys
            or "sell_price" in keys
        ):
            return True
        return "ts" in keys or "datetime" in keys
    has_code = getattr(obj, "code", None) is not None or getattr(obj, "symbol", None) is not None
    has_price = (
        hasattr(obj, "bid_price")
        or hasattr(obj, "ask_price")
        or hasattr(obj, "close")
        or hasattr(obj, "price")
        or hasattr(obj, "bid_volume")
        or hasattr(obj, "ask_volume")
    )
    has_time = hasattr(obj, "ts") or hasattr(obj, "datetime")
    return bool(has_price or (has_code and (has_price or has_time)))


def _unwrap_md(obj: object) -> object:
    if obj is None:
        return obj
    if isinstance(obj, dict):
        tick = obj.get("tick")
        if _looks_like_md(tick):
            return tick
        bidask = obj.get("bidask")
        if _looks_like_md(bidask):
            return bidask
        return obj
    tick = getattr(obj, "tick", None)
    if _looks_like_md(tick):
        return tick
    bidask = getattr(obj, "bidask", None)
    if _looks_like_md(bidask):
        return bidask
    return obj


def _summarize_md(obj: object) -> dict[str, Any]:
    if obj is None:
        return {}
    nested: dict[str, str]
    if isinstance(obj, dict):
        keys = list(obj.keys())
        present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if k in obj]
        nested = {k: type(obj.get(k)).__name__ for k in _MD_NESTED_FIELDS if k in obj}
        return {"keys": keys[:20], "present": present, "nested": nested}
    present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if hasattr(obj, k)]
    nested = {}
    for k in _MD_NESTED_FIELDS:
        if hasattr(obj, k):
            nested[k] = type(getattr(obj, k)).__name__
    return {"attrs": present, "nested": nested}


def _try_fast_extract_callback_payload(*args: Any, **kwargs: Any) -> tuple[object | None, object | None]:
    exchange = kwargs.get("exchange")

    for key in ("quote", "tick", "bidask", "data", "msg"):
        if key not in kwargs:
            continue
        candidate = _unwrap_md(kwargs[key])
        if _looks_like_md(candidate):
            return exchange, candidate

    argc = len(args)
    if argc == 2:
        a0, a1 = args
        # Common Shioaji shape: (exchange/topic, msg)
        msg = _unwrap_md(a1)
        if _looks_like_md(msg):
            if exchange is None and (isinstance(a0, str) or hasattr(a0, "name")):
                exchange = a0
            return exchange, msg
        # Alternate order fallback
        msg = _unwrap_md(a0)
        if _looks_like_md(msg):
            if exchange is None and (isinstance(a1, str) or hasattr(a1, "name")):
                exchange = a1
            return exchange, msg
    elif argc == 1:
        msg = _unwrap_md(args[0])
        if _looks_like_md(msg):
            return exchange, msg
    elif argc >= 3:
        # Another common shape: (topic, quote, event) — pick the last MD-like payload quickly.
        for candidate in (args[-1], args[-2], args[0]):
            msg = _unwrap_md(candidate)
            if _looks_like_md(msg):
                if exchange is None and argc > 0 and (isinstance(args[0], str) or hasattr(args[0], "name")):
                    exchange = args[0]
                return exchange, msg

    return exchange, None


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        return max(1, int(default))


def _obs_policy() -> str:
    policy = os.getenv("HFT_OBS_POLICY", "balanced").strip().lower()
    if policy not in {"minimal", "balanced", "debug"}:
        return "balanced"
    return policy


# FeedState imported from ._md_ingestion


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
        feature_enabled = os.getenv("HFT_FEATURE_ENGINE_ENABLED", "1").lower() in {"1", "true", "yes", "on"}
        self.feature_engine: FeatureEngine | None = None
        if feature_engine is not None:
            self.feature_engine = feature_engine
        elif feature_enabled:
            try:
                self.feature_engine = FeatureEngine()
            except Exception:
                logger.error("feature_engine_init_failed", exc_info=True)
                self.feature_engine = None
        else:
            self.feature_engine = None
        try:
            setattr(self.lob, "feature_engine", self.feature_engine)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass
        self._feature_shadow_engine: FeatureEngine | None = None
        self._shm_publisher: ShmSnapshotWriter | None = None
        self._shm_symbol_index: dict[str, int] = {}
        self._shm_symbol_hashes: dict[str, int] = {}
        self._redis_publisher: Any | None = None
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
        self.reconnect_days = {d.strip().lower() for d in os.getenv("HFT_RECONNECT_DAYS", "").split(",") if d.strip()}
        self.reconnect_hours = os.getenv("HFT_RECONNECT_HOURS", "")
        self.reconnect_hours_2 = os.getenv("HFT_RECONNECT_HOURS_2", "")
        self.reconnect_tz = os.getenv("HFT_RECONNECT_TZ") or timebase.TZ_NAME or "Asia/Taipei"
        try:
            self._reconnect_tzinfo: dt.tzinfo = ZoneInfo(self.reconnect_tz)
        except Exception:
            logger.warning("Invalid reconnect tz, defaulting to UTC", tz=self.reconnect_tz)
            self._reconnect_tzinfo = dt.UTC
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
        self._symbol_gap_active_lookback_s = max(0.0, float(os.getenv("HFT_SYMBOL_GAP_ACTIVE_LOOKBACK_S", "90.0")))
        self._symbol_gap_stale_ratio_threshold = min(
            1.0,
            max(0.0, float(os.getenv("HFT_SYMBOL_GAP_STALE_RATIO_THRESHOLD", "0.85"))),
        )
        self._symbol_gap_severe_gap_s = max(0.0, float(os.getenv("HFT_SYMBOL_GAP_SEVERE_GAP_S", "30.0")))
        self._symbol_gap_consecutive_cycles = max(1, int(os.getenv("HFT_SYMBOL_GAP_CONSECUTIVE_CYCLES", "5")))
        self._symbol_gap_consecutive_hits = 0
        self._symbol_gap_resubscribe_cooldown_s = float(os.getenv("HFT_SYMBOL_GAP_RESUBSCRIBE_COOLDOWN_S", "120"))
        self._last_symbol_gap_resubscribe_ts = 0.0
        self._symbol_gap_metric_cooldown_s = float(os.getenv("HFT_SYMBOL_GAP_METRIC_COOLDOWN_S", "30"))
        self._last_symbol_gap_metric_ts = 0.0
        self._symbol_gap_skip_off_hours = os.getenv("HFT_SYMBOL_GAP_SKIP_OFF_HOURS", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        self._symbol_gap_off_hours_log_interval_s = float(os.getenv("HFT_SYMBOL_GAP_OFF_HOURS_LOG_INTERVAL_S", "300"))
        self._last_symbol_gap_off_hours_log_ts = 0.0
        self._symbol_tick_inline = os.getenv("HFT_SYMBOL_TICK_INLINE", "1").lower() not in {
            "0",
            "false",
            "no",
            "off",
        }

        # Per-message error counter for post-normalization processing
        self._process_raw_error_count = 0

        # P0-1: raw_queue backpressure tracking
        raw_queue_maxsize = getattr(self.raw_queue, "maxsize", 0) or 0
        self._raw_queue_size = (
            raw_queue_maxsize if raw_queue_maxsize > 0 else int(os.getenv("HFT_RAW_QUEUE_SIZE", "10000"))
        )
        self._raw_queue_high_watermark = float(os.getenv("HFT_RAW_QUEUE_HIGH_WATERMARK", "0.8"))
        self._raw_dropped_count = 0
        self._recorder_dropped_count = 0
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
        self._md_callback_parse_metrics_every = env_int("HFT_MD_CALLBACK_PARSE_METRICS_EVERY", cb_parse_metrics_default)
        self._feature_metrics_sample_every = env_int("HFT_FEATURE_METRICS_SAMPLE_EVERY", feature_metrics_default)
        self._feature_latency_sample_every = env_int("HFT_FEATURE_LATENCY_SAMPLE_EVERY", feature_latency_default)
        self._feature_shadow_sample_every = env_int("HFT_FEATURE_SHADOW_SAMPLE_EVERY", 64 if policy != "debug" else 1)
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
        self._init_shm_publisher()
        self._init_redis_publisher()

    def _init_shm_publisher(self) -> None:
        """Initialise optional SHM publisher for monitor snapshots."""
        try:
            shm_name = os.getenv("HFT_MONITOR_SHM_NAME", "hft_monitor_snapshot")
            max_symbols = int(os.getenv("HFT_MONITOR_SHM_MAX_SYMBOLS", "64"))
            self._shm_publisher = ShmSnapshotWriter(shm_name, max_symbols=max_symbols)
            logger.info("shm_publisher_enabled", shm_name=shm_name, max_symbols=max_symbols)
            # Pre-build symbol->index mapping from client subscriptions
            symbols = getattr(self.client, "subscribed_symbols", None)
            if symbols is None:
                symbols = getattr(self.client, "symbols", None)
            if symbols:
                for idx, sym in enumerate(symbols):
                    sym_str = str(sym)
                    if idx < max_symbols:
                        self._shm_symbol_index[sym_str] = idx
                        self._shm_symbol_hashes[sym_str] = _symbol_hash(sym_str)
        except Exception as exc:
            logger.warning("shm_publisher_init_failed", error=str(exc))
            self._shm_publisher = None

    def _init_redis_publisher(self) -> None:
        """Initialise optional Redis publisher for remote monitor access."""
        if os.getenv("HFT_MONITOR_LIVE_ENABLED", "0") != "1":
            return
        try:
            from hft_platform.monitor._redis_publish import MonitorLivePublisher

            host = os.getenv("HFT_MONITOR_REDIS_HOST", "redis")
            port = int(os.getenv("HFT_MONITOR_REDIS_PORT", "6379"))
            password = (
                os.getenv("HFT_MONITOR_REDIS_PASSWORD")
                or os.getenv("HFT_REDIS_PASSWORD")
                or os.getenv("REDIS_PASSWORD")
                or ""
            )
            self._redis_publisher = MonitorLivePublisher(host=host, port=port, password=password)
            self._redis_publisher.start()
            logger.info("redis_publisher_enabled", host=host, port=port)
        except Exception as exc:
            logger.warning("redis_publisher_init_failed", error=str(exc))
            self._redis_publisher = None

    def _publish_to_redis(self, event: object, stats: object) -> None:
        """Publish market data snapshot to Redis for remote monitor. Fire-and-forget."""
        pub = self._redis_publisher
        if pub is None:
            return
        try:
            symbol = getattr(event, "symbol", "")
            meta = getattr(event, "meta", None)
            ingest_ts = getattr(meta, "local_ts", 0) if meta else 0
            if not ingest_ts:
                ingest_ts = getattr(event, "local_ts", 0) or int(time.time_ns())
            payload: dict = {"symbol": symbol, "ingest_ts": ingest_ts}
            # BidAsk data
            bids = getattr(event, "bids", None)
            asks = getattr(event, "asks", None)
            if bids is not None and len(bids) > 0:
                payload["bids_price"] = [int(b[0]) for b in bids[:5]]
                payload["bids_vol"] = [int(b[1]) for b in bids[:5]]
            if asks is not None and len(asks) > 0:
                payload["asks_price"] = [int(a[0]) for a in asks[:5]]
                payload["asks_vol"] = [int(a[1]) for a in asks[:5]]
            # Tick data
            price = getattr(event, "price", None)
            if price is not None:
                payload["price_scaled"] = int(price)
                payload["volume"] = int(getattr(event, "volume", 0) or 0)
            pub.publish_market_data(payload)
        except Exception:
            pass  # fire-and-forget — never block hot path

    # -- main loop -----------------------------------------------------------

    def _publish_to_shm(self, symbol: str, stats: object, feature_tuple: tuple | None) -> None:
        """Publish LOB stats + features to SHM snapshot table (~50ns)."""
        publisher = self._shm_publisher
        if publisher is None:
            return
        idx = self._shm_symbol_index.get(symbol)
        if idx is None:
            # Lazily assign next available slot
            from hft_platform.ipc.shm_snapshot import _symbol_hash

            next_idx = len(self._shm_symbol_index)
            if next_idx >= publisher.max_symbols:
                return
            self._shm_symbol_index[symbol] = next_idx
            self._shm_symbol_hashes[symbol] = _symbol_hash(symbol)
            idx = next_idx

        sym_hash = self._shm_symbol_hashes[symbol]
        ts_ns = int(getattr(stats, "local_ts", 0) or 0) or time.time_ns()

        # Extract 9 LOB fields from stats
        lob_fields = [
            int(getattr(stats, "best_bid", 0) or 0),
            int(getattr(stats, "best_ask", 0) or 0),
            int(getattr(stats, "mid_price_x2", 0) or 0),
            int(getattr(stats, "spread_scaled", 0) or 0),
            int(getattr(stats, "bid_depth", 0) or 0),
            int(getattr(stats, "ask_depth", 0) or 0),
            int(getattr(stats, "l1_bid_qty", 0) or 0),
            int(getattr(stats, "l1_ask_qty", 0) or 0),
            int(getattr(stats, "microprice_x2", 0) or 0),
        ]

        # Extract 16 features (pad with 0 if unavailable)
        if feature_tuple is not None and len(feature_tuple) >= 16:
            features = [int(v) for v in feature_tuple[:16]]
        else:
            features = [0] * 16

        try:
            publisher.publish(idx, ts_ns, sym_hash, lob_fields, features)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass  # fire-and-forget — never block hot path

    def _init_feature_shadow_engine(self) -> None:
        if self.feature_engine is None:
            return
        enabled = os.getenv("HFT_FEATURE_SHADOW_PARITY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return
        try:
            primary_backend = (
                self.feature_engine.kernel_backend() if hasattr(self.feature_engine, "kernel_backend") else "python"
            )
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            primary_backend = "python"
        requested = os.getenv("HFT_FEATURE_SHADOW_BACKEND", "").strip().lower()
        shadow_backend = requested or ("rust" if primary_backend == "python" else "python")
        try:
            shadow = FeatureEngine(
                feature_set_id=(
                    self.feature_engine.feature_set_id() if hasattr(self.feature_engine, "feature_set_id") else None
                ),
                emit_events=True,
                kernel_backend=shadow_backend,
            )
            # If backend fallback happened and becomes identical to primary due missing Rust,
            # still allow compare if explicitly requested.
            if (
                requested == ""
                and hasattr(shadow, "kernel_backend")
                and shadow.kernel_backend() == primary_backend == "python"
            ):
                # Auto mode could not create meaningful alternate backend.
                return
            self._feature_shadow_engine = shadow
        except Exception as exc:
            logger.warning("feature_shadow_engine_init_failed", reason=str(exc))
            self._feature_shadow_engine = None

    async def run(self):
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
                                k: raw.get(k) for k in ("code", "close", "bid_price", "ask_price", "ts") if k in raw
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
        event: TickEvent | BidAskEvent | None = None
        try:
            norm_start_ns = time.perf_counter_ns()
            if isinstance(raw, dict):
                is_bid = "bid_price" in raw or "bid_volume" in raw or "ask_price" in raw
                is_tick = "close" in raw or "price" in raw
            else:
                is_bid = hasattr(raw, "bid_price") or hasattr(raw, "bid_volume") or hasattr(raw, "ask_price")
                is_tick = hasattr(raw, "close") or hasattr(raw, "price")

            normalized: TickEvent | BidAskEvent | tuple[Any, ...] | None = None
            if is_bid:
                normalized = self.normalizer.normalize_bidask(raw)
            elif is_tick:
                normalized = self.normalizer.normalize_tick(raw)
            if isinstance(normalized, (TickEvent, BidAskEvent)):
                event = normalized
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

        try:
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
            if getattr(self, "_trace_sampler", None) is not None:
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
                            "feature_set_id": getattr(feature_update, "feature_set_id", self._feature_set_id_cached),
                            "quality_flags": int(getattr(feature_update, "quality_flags", 0) or 0),
                            "changed_mask": int(getattr(feature_update, "changed_mask", 0) or 0),
                        },
                    )
            if self._record_direct and isinstance(event, (TickEvent, BidAskEvent)):
                self._record_direct_event(event)

            self._publish_events(event, stats, feature_update)
            self._publish_to_redis(event, stats)
        except Exception as exc:
            self._process_raw_error_count += 1
            logger.error(
                "process_raw_post_norm_error",
                symbol=event.symbol,
                error=str(exc),
                event_type=type(event).__name__,
            )
            return

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
            if stats and feature_update:
                self._publish_many_nowait((event, stats, feature_update))
            elif stats:
                self._publish_many_nowait((event, stats))
            elif feature_update:
                self._publish_many_nowait((event, feature_update))
            else:
                self._publish_nowait(event)
        elif stats and feature_update:
            self._publish_many_nowait((stats, feature_update))
        elif stats:
            self._publish_many_nowait((stats,))
        elif feature_update:
            self._publish_many_nowait((feature_update,))

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
                    except Exception as exc:
                        logger.debug("operation_fallback", error=str(exc))
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
            logger.error("shioaji_callback_error", error=str(e))

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

    def _publish_many_nowait(self, events: list[Any] | tuple[Any, ...]) -> None:
        publish_many_nowait = getattr(self.bus, "publish_many_nowait", None)
        if publish_many_nowait:
            publish_many_nowait(events)
            return
        for event in events:
            self._publish_nowait(event)

    def _emit_trace(self, stage: str, trace_id: str, payload: dict[str, Any]) -> None:
        sampler = getattr(self, "_trace_sampler", None)
        if sampler is None:
            return
        try:
            sampler.emit(stage=stage, trace_id=str(trace_id or ""), payload=payload)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return

    def _record_shioaji_crash_signature(self, text: str | None, *, context: str) -> None:
        if not self.metrics_registry or not hasattr(self.metrics_registry, "shioaji_crash_signature_total"):
            return
        signature = detect_crash_signature(text)
        if not signature:
            return
        try:
            self.metrics_registry.shioaji_crash_signature_total.labels(signature=signature, context=context).inc()
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return

    def _maybe_update_features(
        self,
        event: TickEvent | BidAskEvent,
        stats: object | None,
    ) -> FeatureUpdateEvent | None:
        if self.feature_engine is None or stats is None:
            return None
        # Forward classified tick data to FeatureEngine for toxicity tracking
        if isinstance(event, TickEvent) and event.trade_direction != 0:
            self.feature_engine.on_tick(
                event.symbol,
                event.price,
                event.volume,
                event.trade_direction,
                event.trade_confidence,
            )
        if not hasattr(stats, "best_bid") or not hasattr(stats, "best_ask"):
            return None
        meta = getattr(event, "meta", None)
        local_ts_ns = int(getattr(meta, "local_ts", 0) or 0) if meta is not None else 0
        start_ns = time.perf_counter_ns()
        try:
            process_lob_update = getattr(self.feature_engine, "process_lob_update", None)
            if callable(process_lob_update):
                feature_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
            else:
                feature_update = self.feature_engine.process_lob_stats(
                    cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns
                )
            self._maybe_run_feature_shadow_parity(event, stats, local_ts_ns, feature_update)
            self._feature_latency_counter += 1
            self._feature_metrics_counter += 1
            if self.metrics_registry:
                if self._feature_latency_counter % self._feature_latency_sample_every == 0:
                    try:
                        if self._feature_latency_metric_child is None and hasattr(
                            self.metrics_registry, "feature_plane_latency_ns"
                        ):
                            self._feature_latency_metric_child = self.metrics_registry.feature_plane_latency_ns
                        if self._feature_latency_metric_child is not None:
                            self._feature_latency_metric_child.observe(time.perf_counter_ns() - start_ns)
                    except Exception as exc:
                        logger.debug("operation_fallback", error=str(exc))
                        pass
                if self._feature_metrics_counter % self._feature_metrics_sample_every == 0:
                    try:
                        if feature_update is not None:
                            feature_set_id = str(getattr(feature_update, "feature_set_id", self._feature_set_id_cached))
                            self._feature_set_id_cached = feature_set_id
                            result = "emitted"
                            qflags = int(getattr(feature_update, "quality_flags", 0) or 0)
                        else:
                            feature_set_id = self._feature_set_id_cached
                            result = "updated"
                            state_view = None
                            qflags = 0
                            try:
                                if hasattr(self.feature_engine, "get_feature_view"):
                                    state_view = self.feature_engine.get_feature_view(getattr(event, "symbol", ""))
                            except Exception as exc:
                                logger.debug("operation_fallback", error=str(exc))
                                state_view = None
                            if isinstance(state_view, dict):
                                qflags = int(state_view.get("quality_flags", 0) or 0)
                        if hasattr(self.metrics_registry, "feature_plane_updates_total"):
                            key = (result, feature_set_id)
                            child = self._feature_update_metric_children.get(key)
                            if child is None:
                                child = self.metrics_registry.feature_plane_updates_total.labels(
                                    result=result,
                                    feature_set=feature_set_id,
                                )
                                self._feature_update_metric_children[key] = child
                            child.inc()
                        if qflags and hasattr(self.metrics_registry, "feature_quality_flags_total"):
                            for bit, label in _FEATURE_QUALITY_FLAG_LABELS:
                                if qflags & bit:
                                    qchild = self._feature_quality_flag_metric_children.get(label)
                                    if qchild is None:
                                        qchild = self.metrics_registry.feature_quality_flags_total.labels(flag=label)
                                        self._feature_quality_flag_metric_children[label] = qchild
                                    qchild.inc()
                    except Exception as exc:
                        logger.debug("operation_fallback", error=str(exc))
                        pass
            return feature_update
        except Exception as exc:
            self._emit_trace(
                "feature_update_error",
                "",
                {"symbol": getattr(event, "symbol", ""), "reason": str(exc)},
            )
            self._feature_metrics_counter += 1
            if self.metrics_registry and self._feature_metrics_counter % self._feature_metrics_sample_every == 0:
                try:
                    if hasattr(self.metrics_registry, "feature_plane_updates_total"):
                        key = ("error", self._feature_set_id_cached)
                        child = self._feature_update_metric_children.get(key)
                        if child is None:
                            child = self.metrics_registry.feature_plane_updates_total.labels(
                                result="error",
                                feature_set=self._feature_set_id_cached,
                            )
                            self._feature_update_metric_children[key] = child
                        child.inc()
                except Exception as metric_exc:
                    logger.debug("operation_fallback", error=str(metric_exc))
                    pass
            logger.warning("feature_engine_update_failed", reason=str(exc))
            return None

    def _maybe_run_feature_shadow_parity(
        self,
        event: TickEvent | BidAskEvent,
        stats: object,
        local_ts_ns: int,
        primary_update: FeatureUpdateEvent | None,
    ) -> None:
        shadow = self._feature_shadow_engine
        if shadow is None:
            return
        self._feature_shadow_counter += 1
        compare_now = self._feature_shadow_counter % self._feature_shadow_sample_every == 0
        try:
            process_lob_update = getattr(shadow, "process_lob_update", None)
            if callable(process_lob_update):
                shadow_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
            else:
                shadow_update = shadow.process_lob_stats(cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns)
        except Exception as exc:
            logger.warning("feature_shadow_engine_update_failed", reason=str(exc))
            self._emit_feature_shadow_check_metric("skipped")
            return

        if not compare_now:
            return
        self._emit_feature_shadow_check_metric("checked")
        primary_feature_set = str(getattr(primary_update, "feature_set_id", self._feature_set_id_cached))
        primary_values = None
        primary_ids = None
        if primary_update is not None:
            primary_values = tuple(primary_update.values)
            primary_ids = tuple(primary_update.feature_ids)
        else:
            try:
                view = (
                    self.feature_engine.get_feature_view(getattr(event, "symbol", "")) if self.feature_engine else None
                )
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                view = None
            if isinstance(view, dict):
                primary_values = tuple(view.get("values", ()))
                primary_ids = tuple(view.get("feature_ids", ()))
                primary_feature_set = str(view.get("feature_set_id", primary_feature_set))

        shadow_values = None
        shadow_ids = None
        if shadow_update is not None:
            shadow_values = tuple(shadow_update.values)
            shadow_ids = tuple(shadow_update.feature_ids)
        else:
            try:
                sview = shadow.get_feature_view(getattr(event, "symbol", ""))
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                sview = None
            if isinstance(sview, dict):
                shadow_values = tuple(sview.get("values", ()))
                shadow_ids = tuple(sview.get("feature_ids", ()))

        if primary_values is None or shadow_values is None or primary_ids is None or shadow_ids is None:
            return
        if primary_ids != shadow_ids or len(primary_values) != len(shadow_values):
            for fid in primary_ids:
                self._emit_feature_shadow_mismatch_metric(primary_feature_set, str(fid))
            return
        mismatched: list[str] = []
        tol = float(self._feature_shadow_abs_tolerance)
        for fid, pv, sv in zip(primary_ids, primary_values, shadow_values):
            if isinstance(pv, float) or isinstance(sv, float):
                if abs(float(pv) - float(sv)) > tol:
                    mismatched.append(str(fid))
            else:
                if int(pv) != int(sv):
                    mismatched.append(str(fid))
        if mismatched:
            self._feature_shadow_mismatch_counter += 1
            for fid in mismatched:
                self._emit_feature_shadow_mismatch_metric(primary_feature_set, fid)
            self._emit_trace(
                "feature_shadow_mismatch",
                "",
                {
                    "symbol": getattr(event, "symbol", ""),
                    "feature_set_id": primary_feature_set,
                    "mismatch_count": len(mismatched),
                    "mismatched_features": mismatched[:16],
                },
            )
            if self._feature_shadow_mismatch_counter % self._feature_shadow_warn_every == 1:
                logger.warning(
                    "feature_shadow_parity_mismatch",
                    symbol=getattr(event, "symbol", ""),
                    feature_set=primary_feature_set,
                    mismatch_count=len(mismatched),
                    mismatched_features=mismatched[:8],
                )

    def _emit_feature_shadow_check_metric(self, result: str) -> None:
        if not self.metrics_registry or not hasattr(self.metrics_registry, "feature_shadow_parity_checks_total"):
            return
        feature_set_id = self._feature_set_id_cached
        key = (feature_set_id, str(result))
        try:
            child = self._feature_shadow_checks_metric_children.get(key)
            if child is None:
                child = self.metrics_registry.feature_shadow_parity_checks_total.labels(
                    feature_set=feature_set_id,
                    result=str(result),
                )
                self._feature_shadow_checks_metric_children[key] = child
            child.inc()
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass

    def _emit_feature_shadow_mismatch_metric(self, feature_set_id: str, feature_id: str) -> None:
        if not self.metrics_registry or not hasattr(self.metrics_registry, "feature_shadow_parity_mismatch_total"):
            return
        key = (str(feature_set_id), str(feature_id))
        try:
            child = self._feature_shadow_mismatch_metric_children.get(key)
            if child is None:
                child = self.metrics_registry.feature_shadow_parity_mismatch_total.labels(
                    feature_set=str(feature_set_id),
                    feature_id=str(feature_id),
                )
                self._feature_shadow_mismatch_metric_children[key] = child
            child.inc()
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            pass

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
                    self._recorder_dropped_count = 0
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
                self._recorder_dropped_count += 1
                if self._recorder_dropped_count >= self._record_degrade_threshold and not self._record_degraded:
                    self._record_degraded = True
                    self._record_degraded_since = time.monotonic()
                    self._record_degrade_last_check = self._record_degraded_since
                    self._record_degraded_drops = 0
                    logger.warning(
                        "Recorder queue overflow: entering degraded mode",
                        consecutive_drops=self._recorder_dropped_count,
                        threshold=self._record_degrade_threshold,
                    )
        else:
            asyncio.create_task(self.recorder_queue.put({"topic": topic, "data": payload}))

    def _enqueue_raw(self, exchange: Any, msg: Any) -> None:
        """Enqueue raw quote messages with backpressure handling."""
        try:
            self.raw_queue.put_nowait((exchange, msg))
        except asyncio.QueueFull:
            self._raw_dropped_count += 1
            if self.metrics_registry:
                self.metrics_registry.raw_queue_dropped_total.inc()
            if self._raw_dropped_count % 100 == 1:
                logger.warning(
                    "raw_queue full, dropping tick",
                    dropped=self._raw_dropped_count,
                    queue_size=self._raw_queue_size,
                )

    def _set_state(self, new_state: FeedState) -> None:
        if self.state != new_state:
            logger.info("State change", old=self.state, new=new_state)
            self.state = new_state

    async def _attempt_resubscribe(self, gap: float, reason: str = "heartbeat_gap") -> None:
        if not self._within_reconnect_window():
            return
        now = timebase.now_s()
        if now - self._last_resubscribe_ts < self.resubscribe_cooldown_s:
            return
        if self.metrics_registry:
            if reason == "heartbeat_gap":
                if self._feed_reconnect_gap_metric_child is None:
                    self._feed_reconnect_gap_metric_child = self.metrics_registry.feed_reconnect_total.labels(
                        result="gap"
                    )
                gap_metric_child = self._feed_reconnect_gap_metric_child
                if gap_metric_child is not None:
                    gap_metric_child.inc()
            elif reason == "symbol_gap":
                self.metrics_registry.feed_reconnect_total.labels(result="symbol_gap").inc()
        self._last_resubscribe_ts = now
        ok = await asyncio.to_thread(self.client.resubscribe)
        if ok:
            self._resubscribe_attempts = 0
        else:
            self._resubscribe_attempts += 1
        logger.info("Resubscribe attempt", gap=gap, reason=reason, ok=ok, attempts=self._resubscribe_attempts)

    async def _request_reconnect(self, gap: float, reason: str | None = None) -> None:
        if self._within_reconnect_window():
            await self._trigger_reconnect(gap, reason=reason)
            return
        self._mark_pending_reconnect(gap, reason=reason)

    async def _trigger_reconnect(self, gap: float, reason: str | None = None) -> bool:
        now = timebase.now_s()
        if now - self._last_reconnect_ts < self.reconnect_cooldown_s:
            return False
        if not self._within_reconnect_window():
            return False
        self._last_reconnect_ts = now
        reason_label = reason or "heartbeat_gap"
        logger.warning("Triggering reconnect", gap=gap, reason=reason_label)
        self._set_state(FeedState.RECOVERING)
        force_login = reason_label == "session_rollover"
        try:
            ok = await asyncio.wait_for(
                asyncio.to_thread(self.client.reconnect, f"{reason_label} {gap:.1f}s", force_login),
                timeout=max(0.1, float(self.reconnect_timeout_s)),
            )
        except TimeoutError:
            logger.error("Reconnect timed out", reason=reason_label, timeout_s=self.reconnect_timeout_s)
            if self.metrics_registry and hasattr(self.metrics_registry, "feed_reconnect_timeout_total"):
                self.metrics_registry.feed_reconnect_timeout_total.labels(reason=reason_label).inc()
            self._set_state(FeedState.DISCONNECTED)
            return False
        except Exception as exc:
            logger.error("Reconnect raised exception", reason=reason_label, error=str(exc))
            if self.metrics_registry and hasattr(self.metrics_registry, "feed_reconnect_exception_total"):
                self.metrics_registry.feed_reconnect_exception_total.labels(
                    reason=reason_label,
                    exception_type=type(exc).__name__,
                ).inc()
            self._set_state(FeedState.DISCONNECTED)
            return False
        if ok:
            self._set_state(FeedState.CONNECTED)
            self.last_event_ts = timebase.now_s()
            self.last_event_mono = time.monotonic()
            self._resubscribe_attempts = 0
        else:
            self._set_state(FeedState.DISCONNECTED)
        return ok

    def _should_rollover_reconnect(self) -> bool:
        now_dt = dt.datetime.now(tz=self._reconnect_tzinfo)
        last_event_dt = dt.datetime.fromtimestamp(self.last_event_ts, tz=self._reconnect_tzinfo)
        if last_event_dt.date() == now_dt.date():
            return False
        if self._last_rollover_seen_date == now_dt.date():
            return False
        self._last_rollover_seen_date = now_dt.date()
        return True

    def _within_reconnect_window(self) -> bool:
        if not self.reconnect_days and not self.reconnect_hours and not self.reconnect_hours_2:
            return True
        now = dt.datetime.now(tz=self._reconnect_tzinfo)
        if os.getenv("HFT_RECONNECT_USE_CALENDAR", "1").lower() not in {"0", "false", "no", "off"}:
            try:
                from hft_platform.core.market_calendar import get_calendar

                calendar = get_calendar()
                if calendar.available and calendar.days_until_trading(now.date()) > 1:
                    return False
            except ImportError:
                pass
        weekday = now.strftime("%a").lower()
        if self.reconnect_days and weekday not in self.reconnect_days:
            return False

        windows = [w for w in (self.reconnect_hours, self.reconnect_hours_2) if w]
        if not windows:
            return True
        for window in windows:
            try:
                start_str, end_str = window.split("-", 1)
                start = dt.time.fromisoformat(start_str)
                end = dt.time.fromisoformat(end_str)
                now_t = now.timetz().replace(tzinfo=None)
                if start <= end:
                    if start <= now_t <= end:
                        return True
                else:
                    if now_t >= start or now_t <= end:
                        return True
            except Exception as exc:
                logger.debug("operation_fallback", error=str(exc))
                continue
        return False

    def _mark_pending_reconnect(self, gap: float, reason: str | None = None) -> None:
        reason_label = reason or "heartbeat_gap"
        if self._pending_reconnect_reason != reason_label:
            logger.warning("Reconnect pending (outside window)", gap=gap, reason=reason_label)
        self._pending_reconnect_reason = reason_label
        self._pending_reconnect_gap = gap
        if self._pending_reconnect_since is None:
            self._pending_reconnect_since = timebase.now_s()

    async def _call_client(self, func, *args):
        if os.getenv("HFT_MD_SYNC_CONNECT") == "1":
            return func(*args)
        if hasattr(func, "assert_called") or getattr(func, "_mock_name", None):
            return func(*args)
        return await asyncio.to_thread(func, *args)

    # -- public API ----------------------------------------------------------

    # Option symbol prefixes excluded from feed-gap calculation.
    # These instruments are often illiquid (especially far-OTM contracts
    # during night sessions) and can go minutes without a tick, which
    # would falsely trigger StormGuard STORM if included.
    _FEED_GAP_EXCLUDE_PREFIXES: tuple[str, ...] = ("TXO", "MXO", "TEO", "TFO")

    def get_max_feed_gap_s(self) -> float:
        """Return the maximum feed gap across *core* (non-option) symbols.

        Option symbols (prefixed with TXO, MXO, etc.) are excluded because
        far-OTM contracts may not trade for minutes during night sessions,
        producing large gaps that would falsely trigger StormGuard STORM.

        The raw per-symbol gaps are available via :meth:`get_feed_gaps_by_symbol`.
        """
        try:
            snapshot = dict(self._symbol_last_tick)
        except RuntimeError:
            return 0.0

        if not snapshot:
            return float(os.getenv("HFT_FEED_GAP_NO_DATA_S", "0.0"))

        now = time.monotonic()
        max_gap = 0.0
        core_count = 0
        for symbol, last_ts in snapshot.items():
            if any(symbol.startswith(p) for p in self._FEED_GAP_EXCLUDE_PREFIXES):
                continue
            core_count += 1
            gap = now - last_ts
            if gap > max_gap:
                max_gap = gap

        if core_count == 0:
            # All symbols are options — fall back to global max
            return max(now - ts for ts in snapshot.values())
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

    def _is_trading_hours(self) -> bool:
        # Use the broadest product-type session window so the symbol-gap watchdog
        # stays active during futures/options hours (08:45–13:45 + night session).
        # Configurable via HFT_WATCHDOG_PRODUCT_TYPE (default: "future").
        product_type = os.getenv("HFT_WATCHDOG_PRODUCT_TYPE", "future")
        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
            now_dt = dt.datetime.now(calendar._tz)
            return calendar.is_trading_hours(now_dt, product_type=product_type)
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            now_dt = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
            if now_dt.weekday() >= 5:
                return False
            minute = now_dt.hour * 60 + now_dt.minute
            # Fallback: futures day session 08:45–13:45
            return (8 * 60 + 45) <= minute <= (13 * 60 + 45)

    def _is_market_open_grace_period(self) -> bool:
        """Check if within grace period after market open (C4).

        Returns:
            True if within grace period
        """
        if self._market_open_grace_s <= 0:
            return False

        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
        except ImportError:
            return False

        try:
            now = dt.datetime.now(calendar._tz)

            if not calendar.is_trading_day(now.date()):
                return False

            open_time = calendar.get_session_open(now.date())
            if open_time is None:
                return False

            # Check if we're within grace period after open
            elapsed = (now - open_time).total_seconds()
            in_grace = 0 <= elapsed <= self._market_open_grace_s

            # Update gauge
            if self.metrics_registry and hasattr(self.metrics_registry, "market_open_grace_active"):
                self.metrics_registry.market_open_grace_active.set(1 if in_grace else 0)

            return in_grace
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            return False
