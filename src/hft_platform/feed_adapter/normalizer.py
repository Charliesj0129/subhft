import importlib
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import replace
from typing import Any, cast

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import BidAskEvent, BookStats, FusedBookStats, MetaData, TickEvent
from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.trade_classifier import TradeClassifier

# Validated Imports

# Use existing implementation of SymbolMetadata.

logger = get_logger("feed_adapter.normalizer")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_RUST_MIN_LEVELS = int(os.getenv("HFT_RUST_MIN_LEVELS", "0"))
_EVENT_MODE = os.getenv("HFT_EVENT_MODE", "event").lower()
if "pytest" in sys.modules:
    _EVENT_MODE = "event"
_RETURN_TUPLE = _EVENT_MODE in {"tuple", "raw"}
_RUST_STATS_TUPLE = os.getenv("HFT_RUST_STATS_TUPLE", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_RUST_FORCE = os.getenv("HFT_RUST_FORCE", "1").lower() not in {"0", "false", "no", "off"}
_SYNTHETIC_SIDE = os.getenv("HFT_MD_SYNTHETIC_SIDE", "0").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_SYNTHETIC_TICKS = max(1, int(os.getenv("HFT_MD_SYNTHETIC_TICKS", "1")))
# Experimental scratch-array path for fixed 5-level books. Disabled by default
# because Python-level element copies can be slower on some hosts.
_SHIOAJI_FIXED5_SCRATCH = os.getenv("HFT_MD_FIXED5_SCRATCH", "0").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
try:
    _TS_MAX_LAG_NS = int(float(os.getenv("HFT_TS_MAX_LAG_S", "5")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_MAX_LAG_S, using 0", error=str(exc))
    _TS_MAX_LAG_NS = 0
try:
    _TS_MAX_FUTURE_NS = int(float(os.getenv("HFT_TS_MAX_FUTURE_S", "5")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_MAX_FUTURE_S, using 0", error=str(exc))
    _TS_MAX_FUTURE_NS = 0
try:
    _TS_SKEW_LOG_COOLDOWN_NS = int(float(os.getenv("HFT_TS_SKEW_LOG_COOLDOWN_S", "60")) * 1e9)
except Exception as exc:
    logger.warning("Failed to parse HFT_TS_SKEW_LOG_COOLDOWN_S, using 0", error=str(exc))
    _TS_SKEW_LOG_COOLDOWN_NS = 0

_FUSED_ENABLED = os.environ.get("HFT_FUSED_NORMALIZER", "0") == "1"

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except ImportError:
        _rust_core = importlib.import_module("rust_core")

    _RUST_SCALE_BOOK = _rust_core.scale_book
    _RUST_SCALE_BOOK_SEQ = _rust_core.scale_book_seq
    _RUST_SCALE_BOOK_PAIR = _rust_core.scale_book_pair
    _RUST_SCALE_BOOK_PAIR_STATS = getattr(_rust_core, "scale_book_pair_stats", None)
    _RUST_NORMALIZE_TICK = getattr(_rust_core, "normalize_tick_tuple", None)
    _RUST_NORMALIZE_BIDASK = getattr(_rust_core, "normalize_bidask_tuple", None)
    _RUST_NORMALIZE_BIDASK_NP = getattr(_rust_core, "normalize_bidask_tuple_np", None)
    _RUST_NORMALIZE_BIDASK_SYNTH = getattr(_rust_core, "normalize_bidask_tuple_with_synth", None)
except Exception as exc:
    # CRITICAL: Rust acceleration disabled - performance will be 10-100x slower
    logger.warning(
        "Rust acceleration DISABLED - falling back to pure Python (10-100x slower)",
        error=str(exc),
        hint="Run 'maturin develop --manifest-path rust_core/Cargo.toml' to build",
    )
    _RUST_SCALE_BOOK = None
    _RUST_SCALE_BOOK_SEQ = None
    _RUST_SCALE_BOOK_PAIR = None
    _RUST_SCALE_BOOK_PAIR_STATS = None
    _RUST_NORMALIZE_TICK = None
    _RUST_NORMALIZE_BIDASK = None
    _RUST_NORMALIZE_BIDASK_NP = None
    _RUST_NORMALIZE_BIDASK_SYNTH = None

# Fused normalizer+LOB pipeline (single Rust call replaces normalize → book update → stats)
_HAS_FUSED = False
_RustNormalizerLobFused: type | None = None
if _FUSED_ENABLED and _RUST_SCALE_BOOK is not None:  # _rust_core loaded successfully
    try:
        _RustNormalizerLobFused = getattr(_rust_core, "RustNormalizerLobFused", None)
        _HAS_FUSED = _RustNormalizerLobFused is not None
    except Exception as exc:
        logger.debug("fused_normalizer_init_failed", error=str(exc))
        _HAS_FUSED = False


class SymbolMetadata:
    """
    Loads per-symbol configuration.
    """

    DEFAULT_SCALE = 10_000

    def __init__(self, config_path: str | None = None):
        # simplified for this context, assuming existing logic was ok, just need it here
        if config_path is None:
            config_path = os.getenv("SYMBOLS_CONFIG")
            if not config_path:
                # Resolve config path relative to the project root (not cwd)
                # so tests that change cwd don't break metadata loading.
                _pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                _project_root = os.path.dirname(os.path.dirname(_pkg_dir))
                _abs_symbols = os.path.join(_project_root, "config", "symbols.yaml")
                _abs_base = os.path.join(_project_root, "config", "base", "symbols.yaml")
                if os.path.exists(_abs_symbols):
                    config_path = _abs_symbols
                elif os.path.exists(_abs_base):
                    config_path = _abs_base
                elif os.path.exists("config/symbols.yaml"):
                    config_path = "config/symbols.yaml"
                else:
                    config_path = "config/base/symbols.yaml"

        self.config_path = config_path
        self.meta: dict[str, dict[str, Any]] = {}
        self.tags_by_symbol: dict[str, set[str]] = {}
        self.symbols_by_tag: dict[str, set[str]] = {}
        self._price_scale_cache: dict[str, int] = {}
        self._exchange_cache: dict[str, str] = {}
        self._product_type_cache: dict[str, str] = {}
        self._mtime: float | None = None
        from hft_platform.core.instrument_registry import InstrumentRegistry

        self.registry = InstrumentRegistry()
        self._load()
        self._populate_registry()

    def _load(self) -> None:
        import yaml

        self.meta = {}
        self.tags_by_symbol = {}
        self.symbols_by_tag = {}
        self._price_scale_cache = {}
        self._exchange_cache = {}
        self._product_type_cache = {}
        try:
            self._mtime = os.path.getmtime(self.config_path)
        except OSError:
            self._mtime = None
        try:
            with open(self.config_path) as f:
                data = yaml.safe_load(f) or {}
                for item in data.get("symbols", []):
                    code = item.get("code")
                    if not code:
                        continue
                    self.meta[code] = item
                    tags_raw = item.get("tags", [])
                    if isinstance(tags_raw, str):
                        tags = [t.strip() for t in re.split(r"[|,]", tags_raw) if t.strip()]
                    elif isinstance(tags_raw, (list, tuple, set)):
                        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
                    else:
                        tags = []
                    normalized = {t.lower() for t in tags}
                    if normalized:
                        self.tags_by_symbol[code] = normalized
                        for tag in normalized:
                            self.symbols_by_tag.setdefault(tag, set()).add(code)
        except Exception as exc:
            logger.warning("symbol_metadata_load_failed", error=str(exc), config_path=self.config_path)

    def reload(self) -> None:
        self._load()
        self._populate_registry()

    def reload_if_changed(self) -> bool:
        try:
            mtime = os.path.getmtime(self.config_path)
        except OSError:
            return False
        if self._mtime is None or mtime > self._mtime:
            self._load()
            return True
        return False

    def symbols_for_tags(self, tags: Iterable[str]) -> set[str]:
        resolved = set()
        for tag in tags:
            key = str(tag).strip().lower()
            if not key:
                continue
            resolved.update(self.symbols_by_tag.get(key, set()))
        return resolved

    def price_scale(self, symbol: str) -> int:
        cached = self._price_scale_cache.get(symbol)
        if cached is not None:
            return cached
        # Avoid creating empty dict
        entry = self.meta.get(symbol)
        if entry:
            if "price_scale" in entry:
                scale = int(entry.get("price_scale", self.DEFAULT_SCALE))
                self._price_scale_cache[symbol] = scale
                return scale
            tick_size = entry.get("tick_size")
            if tick_size:
                try:
                    scale = int(round(1 / float(tick_size)))
                    if scale > 0:
                        self._price_scale_cache[symbol] = scale
                        return scale
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        self._price_scale_cache[symbol] = self.DEFAULT_SCALE
        return self.DEFAULT_SCALE

    def contract_multiplier(self, symbol: str) -> int:
        """Return contract point-value multiplier for PnL calculation.

        Stocks return 1 (1 share × price diff = PnL in NTD).
        Futures return point_value from config (e.g. TMF=10, MXF=50, TXF=200).
        """
        entry = self.meta.get(symbol)
        if entry:
            pv = entry.get("point_value")
            if pv is not None:
                return int(pv)
        return 1

    def exchange(self, symbol: str) -> str:
        cached = self._exchange_cache.get(symbol)
        if cached is not None:
            return cached
        entry = self.meta.get(symbol) or {}
        value = str(entry.get("exchange", ""))
        self._exchange_cache[symbol] = value
        return value

    def product_type(self, symbol: str) -> str:
        cached = self._product_type_cache.get(symbol)
        if cached is not None:
            return cached
        entry = self.meta.get(symbol) or {}
        raw = (
            entry.get("product_type")
            or entry.get("security_type")
            or entry.get("type")
            or entry.get("asset_type")
            or ""
        )
        raw = str(raw).strip().lower()
        if raw:
            self._product_type_cache[symbol] = raw
            return raw

        exchange = self.exchange(symbol).upper()
        if exchange in {"TSE", "OTC", "OES"}:
            self._product_type_cache[symbol] = "stock"
            return "stock"
        if exchange in {"FUT", "FUTURES", "TAIFEX"}:
            self._product_type_cache[symbol] = "future"
            return "future"
        if exchange in {"OPT", "OPTIONS"}:
            self._product_type_cache[symbol] = "option"
            return "option"
        if exchange in {"IDX", "INDEX"}:
            self._product_type_cache[symbol] = "index"
            return "index"
        self._product_type_cache[symbol] = ""
        return ""

    def order_params(self, symbol: str) -> dict[str, Any]:
        entry = self.meta.get(symbol) or {}
        params: dict[str, Any] = {}
        for key in ("order_cond", "order_lot", "oc_type", "account"):
            if key in entry and entry[key] is not None:
                params[key] = entry[key]
        return params

    def _safe_tick_size_scaled(self, entry: dict[str, Any], code: str) -> int:
        """Compute tick_size_scaled with fallback for invalid tick_size values."""
        raw = entry.get("tick_size", 1.0)
        try:
            val = float(raw)
            if val <= 0:
                val = 1.0
        except (TypeError, ValueError):
            val = 1.0
        return int(round(val * self.price_scale(code)))

    def _populate_registry(self) -> None:
        """Build InstrumentProfile entries from symbols.yaml metadata."""
        from hft_platform.core.instrument_registry import (
            FeeStructure,
            InstrumentProfile,
            InstrumentType,
            OptionRight,
            TradingHours,
        )

        profiles = []
        for code, entry in self.meta.items():
            ptype_str = self.product_type(code)
            itype = {
                "future": InstrumentType.FUTURE,
                "option": InstrumentType.OPTION,
                "stock": InstrumentType.EQUITY,
                "equity": InstrumentType.EQUITY,
                "index": InstrumentType.INDEX,
            }.get(ptype_str, InstrumentType.EQUITY)

            fee = FeeStructure(
                tax_rate_bps=int(entry.get("tax_rate_bps", 20)),
                commission_per_lot=int(entry.get("commission_per_lot", 130000)),
            )
            hours = TradingHours(
                day_open=str(entry.get("day_open", "08:45")),
                day_close=str(entry.get("day_close", "13:45")),
                night_open=entry.get("night_open"),
                night_close=entry.get("night_close"),
            )

            strike_scaled = None
            option_right = None
            expiry = None
            if itype == InstrumentType.OPTION:
                raw_strike = entry.get("strike") or entry.get("strike_price")
                if raw_strike is not None:
                    strike_scaled = int(round(float(raw_strike) * self.price_scale(code)))
                raw_right = str(entry.get("right") or entry.get("option_right", ""))
                if raw_right.upper() in ("C", "CALL"):
                    option_right = OptionRight.CALL
                elif raw_right.upper() in ("P", "PUT"):
                    option_right = OptionRight.PUT
                raw_expiry = entry.get("expiry")
                if raw_expiry is not None:
                    from datetime import date as _d

                    if isinstance(raw_expiry, _d):
                        expiry = raw_expiry
                    else:
                        try:
                            expiry = _d.fromisoformat(str(raw_expiry))
                        except ValueError:
                            pass

            profile = InstrumentProfile(
                symbol=code,
                instrument_type=itype,
                underlying=str(entry.get("underlying", "")),
                exchange=self.exchange(code),
                multiplier=self.contract_multiplier(code),
                tick_size_scaled=self._safe_tick_size_scaled(entry, code),
                price_scale=self.price_scale(code),
                fee_structure=fee,
                trading_hours=hours,
                lot_size=int(entry.get("lot_size", 1)),
                strike_scaled=strike_scaled,
                option_right=option_right,
                expiry=expiry,
            )
            profiles.append(profile)

        self.registry.reload_static(profiles)


def _extract_ts_ns(ts_val: Any) -> int:
    return timebase.coerce_ns(ts_val)


def _clamp_future_ts(exch_ts: int, now_ns: int, topic: str, symbol: str) -> int:
    if not exch_ts or not _TS_MAX_FUTURE_NS:
        return exch_ts
    delta_ns = exch_ts - now_ns
    if delta_ns > _TS_MAX_FUTURE_NS:
        logger.warning(
            "Exchange timestamp in future",
            topic=topic,
            symbol=symbol,
            delta_ns=delta_ns,
            max_future_ns=_TS_MAX_FUTURE_NS,
        )
        return now_ns
    return exch_ts


class MarketDataNormalizer:
    __slots__ = (
        "_seq_gen",
        "metadata",
        "price_codec",
        "metrics",
        "_last_symbol",
        "_last_scale",
        "_last_local_ts_tick",
        "_last_local_ts_bidask",
        "_last_local_ts_snapshot",
        "_last_skew_log_ns",
        "_fixed5_scratch_enabled",
        "_fixed5_bid_prices_np",
        "_fixed5_bid_vols_np",
        "_fixed5_ask_prices_np",
        "_fixed5_ask_vols_np",
        "_fused",
        "_trade_classifier",
        "_latency_metrics_counter",
        "_latency_metrics_sample_every",
        "_rust_fallback_tick",
        "_rust_fallback_bidask",
        "_skip_tick_missing_symbol",
        "_skip_tick_negative_price",
        "_skip_bidask_missing_symbol",
        "_skip_snapshot_missing_symbol",
    )

    def __init__(self, config_path: str | None = None, metadata: SymbolMetadata | None = None):
        import itertools

        self._seq_gen = itertools.count(1)
        # self._lock = Lock() # Removed
        self.metadata = metadata or SymbolMetadata(config_path)
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self.metrics = MetricsRegistry.get()
        self._rust_fallback_tick = self.metrics.rust_fallback_total.labels(type="tick") if self.metrics else None
        self._rust_fallback_bidask = self.metrics.rust_fallback_total.labels(type="bidask") if self.metrics else None
        _skip = self.metrics.normalization_skip_total if self.metrics else None
        self._skip_tick_missing_symbol = _skip.labels(type="tick", reason="missing_symbol") if _skip else None
        self._skip_tick_negative_price = _skip.labels(type="tick", reason="negative_price") if _skip else None
        self._skip_bidask_missing_symbol = _skip.labels(type="bidask", reason="missing_symbol") if _skip else None
        self._skip_snapshot_missing_symbol = _skip.labels(type="snapshot", reason="missing_symbol") if _skip else None
        self._last_symbol: str | None = None
        self._last_scale: int = SymbolMetadata.DEFAULT_SCALE
        self._last_local_ts_tick: int = 0
        self._last_local_ts_bidask: int = 0
        self._last_local_ts_snapshot: int = 0
        self._latency_metrics_counter: int = 0
        self._latency_metrics_sample_every: int = max(1, int(os.getenv("HFT_NORMALIZER_METRICS_SAMPLE_EVERY", "4")))
        self._last_skew_log_ns = 0
        self._trade_classifier = TradeClassifier()
        self._fused: Any = None
        if _HAS_FUSED and _RustNormalizerLobFused is not None:
            try:
                self._fused = _RustNormalizerLobFused()
            except Exception as exc:
                logger.debug("fused_normalizer_instance_failed", error=str(exc))
                self._fused = None
        self._fixed5_scratch_enabled = False
        self._fixed5_bid_prices_np = None
        self._fixed5_bid_vols_np = None
        self._fixed5_ask_prices_np = None
        self._fixed5_ask_vols_np = None
        if _SHIOAJI_FIXED5_SCRATCH and _RUST_NORMALIZE_BIDASK_NP is not None:
            try:
                import numpy as np

                self._fixed5_bid_prices_np = np.empty(5, dtype=np.float64)
                self._fixed5_bid_vols_np = np.empty(5, dtype=np.int64)
                self._fixed5_ask_prices_np = np.empty(5, dtype=np.float64)
                self._fixed5_ask_vols_np = np.empty(5, dtype=np.int64)
                self._fixed5_scratch_enabled = True
            except Exception as exc:
                logger.debug("fixed5_scratch_init_failed", error=str(exc))
                self._fixed5_scratch_enabled = False

    def _next_seq(self) -> int:
        return next(self._seq_gen)

    def _validate_and_sync_timestamp(self, exch_ts: int, local_ts: int, topic: str, symbol: str) -> tuple[int, int]:
        """Clamp future exchange timestamps and sync/cap local_ts against exch_ts.

        Returns ``(exch_ts, local_ts)`` after all adjustments.
        """
        if exch_ts:
            exch_ts = _clamp_future_ts(exch_ts, local_ts, topic, symbol)
            if local_ts < exch_ts:
                local_ts = exch_ts
            else:
                delta = local_ts - exch_ts
                if _TS_MAX_LAG_NS and delta > _TS_MAX_LAG_NS:
                    if _TS_SKEW_LOG_COOLDOWN_NS and (local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS):
                        logger.warning(
                            "Feed time skew",
                            topic=topic,
                            symbol=symbol,
                            delta_ns=delta,
                            max_ns=_TS_MAX_LAG_NS,
                        )
                        self._last_skew_log_ns = local_ts
                    if self.metrics:
                        self.metrics.feed_time_skew_ns.labels(topic=topic).set(delta)
                    local_ts = exch_ts + _TS_MAX_LAG_NS
        return exch_ts, local_ts

    def _record_latency_metrics(self, exch_ts: int, local_ts: int, last_ts_attr: str) -> None:
        """Record feed_latency_ns and feed_interarrival_ns metrics and update the
        named ``_last_local_ts_*`` attribute.

        Metrics are sampled every ``_latency_metrics_sample_every`` calls to reduce
        per-tick Prometheus overhead.  The ``last_ts_attr`` timestamp is always
        updated so that interarrival deltas remain accurate on sampled events.
        """
        if self.metrics:
            self._latency_metrics_counter += 1
            sample = self._latency_metrics_counter % self._latency_metrics_sample_every == 0
            if exch_ts and sample:
                lag_ns = local_ts - exch_ts
                if lag_ns >= 0:
                    self.metrics.feed_latency_ns.observe(lag_ns)
            last = getattr(self, last_ts_attr)
            if last and sample:
                delta = local_ts - last
                if delta >= 0:
                    self.metrics.feed_interarrival_ns.observe(delta)
            setattr(self, last_ts_attr, local_ts)

    def _get_scale(self, symbol: str) -> int:
        if symbol == self._last_symbol:
            return self._last_scale
        scale = int(self.metadata.price_scale(symbol))
        if scale <= 0:
            scale = 1
        self._last_symbol = symbol
        self._last_scale = scale
        return scale

    def _maybe_synthesize_side(
        self,
        symbol: str,
        bids: list | Any,
        asks: list | Any,
        scale: int,
    ) -> tuple[list | Any, list | Any, bool]:
        if not _SYNTHETIC_SIDE:
            return bids, asks, False

        def _has_levels(levels: list | Any) -> bool:
            if levels is None:
                return False
            if hasattr(levels, "size"):
                return getattr(levels, "size", 0) > 0
            return bool(levels)

        has_bids = _has_levels(bids)
        has_asks = _has_levels(asks)
        if has_bids and has_asks:
            return bids, asks, False

        tick_size = None
        entry = self.metadata.meta.get(symbol) if self.metadata else None
        raw_tick = entry.get("tick_size") if entry else None
        if raw_tick is not None:
            try:
                tick_size = float(raw_tick)
            except (TypeError, ValueError):
                tick_size = None
        if not tick_size and scale > 0:
            tick_size = 1.0 / float(scale)
        if not tick_size:
            tick_size = 1.0
        tick_int = max(1, int(round(tick_size * scale)))

        def _best_price(levels: list | Any) -> int:
            if hasattr(levels, "size"):
                levels_any = cast(Any, levels)
                return int(levels_any[0, 0]) if getattr(levels_any, "size", 0) > 0 else 0
            return int(levels[0][0]) if levels else 0

        synthesized = False
        if not has_bids and has_asks:
            best_ask = _best_price(asks)
            bid_price = max(best_ask - (tick_int * _SYNTHETIC_TICKS), 1)
            bids = [[bid_price, 1]]
            synthesized = True
        elif not has_asks and has_bids:
            best_bid = _best_price(bids)
            ask_price = max(best_bid + (tick_int * _SYNTHETIC_TICKS), 1)
            asks = [[ask_price, 1]]
            synthesized = True

        return bids, asks, synthesized

    def normalize_tick(self, payload: Any) -> TickEvent | tuple | None:
        # Fast path lookup without _coalesce
        # Assuming Shioaji standard keys: 'code', 'close', 'volume', 'ts'
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") if payload.get("ts") is not None else payload.get("datetime")
                close_val = payload.get("close") if payload.get("close") is not None else payload.get("Close")
                vol_val = payload.get("volume") if payload.get("volume") is not None else payload.get("Volume")
                total_volume = int(payload.get("total_volume") or 0)
                is_simtrade = bool(payload.get("simtrade") or 0)
                is_odd_lot = bool(payload.get("intraday_odd") or 0)
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                _ts = getattr(payload, "ts", None)
                ts_val = _ts if _ts is not None else getattr(payload, "datetime", None)
                _close = getattr(payload, "close", None)
                close_val = _close if _close is not None else getattr(payload, "Close", None)
                _vol = getattr(payload, "volume", None)
                vol_val = _vol if _vol is not None else getattr(payload, "Volume", None)
                total_volume = int(getattr(payload, "total_volume", None) or 0)
                is_simtrade = bool(getattr(payload, "simtrade", None) or 0)
                is_odd_lot = bool(getattr(payload, "intraday_odd", None) or 0)

            if not symbol:
                if self._skip_tick_missing_symbol:
                    self._skip_tick_missing_symbol.inc()
                return None

            exch_ts = _extract_ts_ns(ts_val)
            exch_ts_py = exch_ts

            if close_val is not None:
                scale = self._get_scale(symbol)
                if _RUST_ENABLED and _RUST_NORMALIZE_TICK is not None:
                    try:
                        rust_tuple = _RUST_NORMALIZE_TICK(payload, symbol, scale)
                        if rust_tuple is not None:
                            (
                                _,
                                _sym,
                                price,
                                volume,
                                total_volume,
                                is_simtrade,
                                is_odd_lot,
                                exch_ts,
                            ) = rust_tuple
                            # Rust extract::<f64> fails on str values; fall through to Python
                            if price == 0 and close_val:
                                raise ValueError("rust returned zero price for non-zero close")
                            if price <= 0:
                                if self._skip_tick_negative_price:
                                    self._skip_tick_negative_price.inc()
                                return None
                            if exch_ts_py:
                                exch_ts = exch_ts_py
                            if _RETURN_TUPLE:
                                _td, _tc = self._trade_classifier.classify(_sym, int(price))
                                return rust_tuple + (_td, _tc)
                            local_ts = timebase.now_ns()
                            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "tick", _sym)
                            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_tick")
                            meta = MetaData(
                                seq=self._next_seq(),
                                topic="tick",
                                source_ts=exch_ts,
                                local_ts=local_ts,
                            )
                            _td, _tc = self._trade_classifier.classify(_sym, int(price))
                            return TickEvent(
                                meta=meta,
                                symbol=_sym,
                                price=int(price),
                                volume=int(volume),
                                total_volume=int(total_volume),
                                bid_side_total_vol=0,
                                ask_side_total_vol=0,
                                is_simtrade=bool(is_simtrade),
                                is_odd_lot=bool(is_odd_lot),
                                trade_direction=_td,
                                trade_confidence=_tc,
                            )
                    except Exception as exc:
                        logger.debug("rust_tick_fallback", error=str(exc))
                        if self._rust_fallback_tick:
                            self._rust_fallback_tick.inc()
                price = int(round(float(close_val) * scale))
                if price <= 0:
                    if self._skip_tick_negative_price:
                        self._skip_tick_negative_price.inc()
                    return None
            else:
                price = 0

            if price <= 0:
                if self._skip_tick_negative_price:
                    self._skip_tick_negative_price.inc()
                return None

            volume = int(vol_val) if vol_val is not None else 0

            if _RETURN_TUPLE:
                _td, _tc = self._trade_classifier.classify(symbol, price)
                return (
                    "tick",
                    symbol,
                    price,
                    volume,
                    total_volume,
                    is_simtrade,
                    is_odd_lot,
                    exch_ts,
                    _td,
                    _tc,
                )

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "tick", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_tick")
            meta = MetaData(seq=self._next_seq(), topic="tick", source_ts=exch_ts, local_ts=local_ts)

            _td, _tc = self._trade_classifier.classify(symbol, price)
            return TickEvent(
                meta=meta,
                symbol=symbol,
                price=price,
                volume=volume,
                total_volume=total_volume,
                bid_side_total_vol=0,  # Optimization: skip less used fields unless needed
                ask_side_total_vol=0,
                is_simtrade=is_simtrade,
                is_odd_lot=is_odd_lot,
                trade_direction=_td,
                trade_confidence=_tc,
            )
        except Exception as e:
            logger.error("Normalize Tick Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="Tick").inc()
            return None

    def normalize_bidask(self, payload: Any) -> BidAskEvent | tuple | None:
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") if payload.get("ts") is not None else payload.get("datetime")
                bp = payload.get("bid_price") or []
                bv = payload.get("bid_volume") or []
                ap = payload.get("ask_price") or []
                av = payload.get("ask_volume") or []
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                _ba_ts = getattr(payload, "ts", None)
                ts_val = _ba_ts if _ba_ts is not None else getattr(payload, "datetime", None)
                bp = getattr(payload, "bid_price", None) or []
                bv = getattr(payload, "bid_volume", None) or []
                ap = getattr(payload, "ask_price", None) or []
                av = getattr(payload, "ask_volume", None) or []
            if not symbol:
                if self._skip_bidask_missing_symbol:
                    self._skip_bidask_missing_symbol.inc()
                return None

            exch_ts = _extract_ts_ns(ts_val)

            scale = self._get_scale(symbol)

            # Fused path: single Rust call does scale + book update + stats
            fused = self._fused
            if fused is not None:
                try:
                    # tick_size_scaled = 1 tick unit in scaled price space (reserved for future use)
                    tick_size_scaled = 1
                    fused_result = fused.process_bidask(
                        symbol,
                        list(bp) if not isinstance(bp, list) else bp,
                        list(bv) if not isinstance(bv, list) else bv,
                        list(ap) if not isinstance(ap, list) else ap,
                        list(av) if not isinstance(av, list) else av,
                        scale,
                        tick_size_scaled,
                    )
                    if fused_result is not None:
                        (
                            bids_np,
                            asks_np,
                            best_bid,
                            best_ask,
                            bid_depth,
                            ask_depth,
                            mid_x2,
                            spread_scaled,
                            imbalance_ppm,
                            version,
                            top_imbalance,
                        ) = fused_result
                        bb = int(best_bid)
                        ba = int(best_ask)
                        bd = int(bid_depth)
                        ad = int(ask_depth)
                        mx2 = int(mid_x2)
                        ss = int(spread_scaled)
                        timb = float(top_imbalance)
                        # Standard stats (backward-compat: mid_price as float, spread as float)
                        compat_stats = BookStats(bb, ba, bd, ad, mx2 / 2.0, float(ss), timb)
                        # Fused stats: integer mid_x2 + spread_scaled for LOBEngine bypass
                        fused_stats = FusedBookStats(bb, ba, bd, ad, mx2, ss, timb)

                        self._trade_classifier.update_quotes(symbol, bb, ba)

                        if _RETURN_TUPLE:
                            return (
                                "bidask",
                                symbol,
                                bids_np,
                                asks_np,
                                exch_ts,
                                False,
                                bb,
                                ba,
                                bd,
                                ad,
                                mx2 / 2.0,
                                float(ss),
                                timb,
                            )

                        local_ts = timebase.now_ns()
                        exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "bidask", symbol)
                        self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_bidask")
                        meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=local_ts)
                        return BidAskEvent(
                            meta=meta,
                            symbol=symbol,
                            bids=bids_np,
                            asks=asks_np,
                            stats=compat_stats,
                            fused_stats=fused_stats,
                        )
                except Exception as exc:
                    # Fall through to standard path
                    logger.debug("rust_bidask_fallback", stage="fused_path", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()

            # Convert to numpy
            # We need to scale prices. Using numpy vectorization for scaling is faster.
            # But converting list->numpy is overhead.
            # If lists are small (5 levels), list comp might be faster than np.array(list).

            # Bids with filtering 0s?
            # Shioaji might send 0 for empty levels.

            # Optimization: Use list comprehension for small N (N=5)
            # Returns List[List[int]] directly (faster than numpy conversion)

            # Bids / Asks
            # Rust path uses zero-copy NumPy views and returns int64 ndarray (N,2).
            bids_final = None
            asks_final = None
            rust_available = bool(_RUST_SCALE_BOOK_PAIR or _RUST_SCALE_BOOK_SEQ or _RUST_NORMALIZE_BIDASK)
            use_rust = _RUST_ENABLED and rust_available and (_RUST_FORCE or _RUST_MIN_LEVELS <= 0)
            if not use_rust and _RUST_ENABLED and _RUST_MIN_LEVELS > 0:
                use_rust = rust_available and (
                    len(bp) >= _RUST_MIN_LEVELS
                    and len(bv) >= _RUST_MIN_LEVELS
                    and len(ap) >= _RUST_MIN_LEVELS
                    and len(av) >= _RUST_MIN_LEVELS
                )

            stats = None
            synthesized = False
            if use_rust and _SYNTHETIC_SIDE and _RUST_NORMALIZE_BIDASK_SYNTH is not None:
                try:
                    import numpy as np

                    bid_prices_np = np.asarray(bp, dtype=np.float64)
                    bid_vols_np = np.asarray(bv, dtype=np.int64)
                    ask_prices_np = np.asarray(ap, dtype=np.float64)
                    ask_vols_np = np.asarray(av, dtype=np.int64)

                    tick_size = None
                    entry = self.metadata.meta.get(symbol) if self.metadata else None
                    raw_tick = entry.get("tick_size") if entry else None
                    if raw_tick is not None:
                        try:
                            tick_size = float(raw_tick)
                        except (TypeError, ValueError):
                            tick_size = None
                    if not tick_size and scale > 0:
                        tick_size = 1.0 / float(scale)
                    if not tick_size:
                        tick_size = 1.0
                    tick_int = max(1, int(round(tick_size * scale)))

                    rust_tuple = _RUST_NORMALIZE_BIDASK_SYNTH(
                        symbol,
                        exch_ts,
                        bid_prices_np,
                        bid_vols_np,
                        ask_prices_np,
                        ask_vols_np,
                        scale,
                        tick_int,
                        _SYNTHETIC_TICKS,
                    )
                    if rust_tuple is not None:
                        (
                            _,
                            _sym,
                            bids_final,
                            asks_final,
                            exch_ts,
                            _is_snapshot,
                            best_bid,
                            best_ask,
                            bid_depth,
                            ask_depth,
                            mid_price,
                            spread,
                            imbalance,
                            synthesized,
                        ) = rust_tuple
                        stats = BookStats(
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="synth_bidask", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None
                    stats = None
                    synthesized = False

            # Hot path for standard Shioaji bidask streams: avoid Python np.asarray() churn
            # when Rust can directly scale Python sequences and compute stats.
            if (
                stats is None
                and use_rust
                and not _SYNTHETIC_SIDE
                and _RUST_SCALE_BOOK_PAIR_STATS is not None
                and _RUST_STATS_TUPLE
            ):
                try:
                    bids_final, asks_final, _raw_stats = _RUST_SCALE_BOOK_PAIR_STATS(bp, bv, ap, av, scale)
                    stats = BookStats(*_raw_stats) if _raw_stats is not None else None
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="scale_book_pair_stats", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None
                    stats = None

            if stats is None and use_rust and _RUST_NORMALIZE_BIDASK_NP is not None:
                try:
                    use_fixed5 = (
                        self._fixed5_scratch_enabled
                        and isinstance(bp, (list, tuple))
                        and isinstance(bv, (list, tuple))
                        and isinstance(ap, (list, tuple))
                        and isinstance(av, (list, tuple))
                        and len(bp) == 5
                        and len(bv) == 5
                        and len(ap) == 5
                        and len(av) == 5
                    )
                    if use_fixed5:
                        bid_prices_np = self._fixed5_bid_prices_np  # type: ignore[assignment]
                        bid_vols_np = self._fixed5_bid_vols_np  # type: ignore[assignment]
                        ask_prices_np = self._fixed5_ask_prices_np  # type: ignore[assignment]
                        ask_vols_np = self._fixed5_ask_vols_np  # type: ignore[assignment]
                        if bid_prices_np is None or bid_vols_np is None or ask_prices_np is None or ask_vols_np is None:
                            use_fixed5 = False
                    if use_fixed5:
                        # Shioaji stock/futures bidask is typically fixed 5 levels.
                        for i in range(5):
                            bid_prices_np[i] = float(bp[i])
                            bid_vols_np[i] = int(bv[i])
                            ask_prices_np[i] = float(ap[i])
                            ask_vols_np[i] = int(av[i])
                    else:
                        import numpy as np

                        bid_prices_np = np.asarray(bp, dtype=np.float64)
                        bid_vols_np = np.asarray(bv, dtype=np.int64)
                        ask_prices_np = np.asarray(ap, dtype=np.float64)
                        ask_vols_np = np.asarray(av, dtype=np.int64)

                    rust_tuple = _RUST_NORMALIZE_BIDASK_NP(
                        symbol,
                        exch_ts,
                        bid_prices_np,
                        bid_vols_np,
                        ask_prices_np,
                        ask_vols_np,
                        scale,
                    )
                    if rust_tuple is not None:
                        (
                            _,
                            _sym,
                            bids_final,
                            asks_final,
                            exch_ts,
                            _is_snapshot,
                            best_bid,
                            best_ask,
                            bid_depth,
                            ask_depth,
                            mid_price,
                            spread,
                            imbalance,
                        ) = rust_tuple
                        stats = BookStats(
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="normalize_bidask_np", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None
                    stats = None

            if stats is None and use_rust and _RUST_NORMALIZE_BIDASK is not None:
                try:
                    rust_tuple = _RUST_NORMALIZE_BIDASK(payload, symbol, scale)
                    if rust_tuple is not None:
                        (
                            _,
                            _sym,
                            bids_final,
                            asks_final,
                            exch_ts,
                            _is_snapshot,
                            best_bid,
                            best_ask,
                            bid_depth,
                            ask_depth,
                            mid_price,
                            spread,
                            imbalance,
                        ) = rust_tuple
                        exch_ts_py = _extract_ts_ns(ts_val)
                        if exch_ts_py:
                            exch_ts = exch_ts_py
                        stats = BookStats(
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="normalize_bidask", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None
                    stats = None

            # If a Rust normalize path already returned bids/asks+stats, do not recompute
            # the same work again via scale_book_pair_stats.
            if stats is None and use_rust and _RUST_SCALE_BOOK_PAIR_STATS and _RUST_STATS_TUPLE:
                try:
                    bids_final, asks_final, _raw_stats2 = _RUST_SCALE_BOOK_PAIR_STATS(bp, bv, ap, av, scale)
                    stats = BookStats(*_raw_stats2) if _raw_stats2 is not None else None
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="scale_book_pair_stats_retry", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None
                    stats = None
            if bids_final is None and use_rust and _RUST_SCALE_BOOK_PAIR:
                try:
                    bids_final, asks_final = _RUST_SCALE_BOOK_PAIR(bp, bv, ap, av, scale)
                except Exception as exc:
                    logger.debug("rust_bidask_fallback", stage="scale_book_pair", error=str(exc))
                    if self._rust_fallback_bidask:
                        self._rust_fallback_bidask.inc()
                    bids_final = None
                    asks_final = None

            if bids_final is None:
                if use_rust and _RUST_SCALE_BOOK_SEQ:
                    try:
                        bids_final = _RUST_SCALE_BOOK_SEQ(bp, bv, scale)
                    except Exception as exc:
                        logger.debug("rust_bidask_fallback", stage="scale_book_seq_bid", error=str(exc))
                        if self._rust_fallback_bidask:
                            self._rust_fallback_bidask.inc()
                        bids_final = None
                if bids_final is None:
                    bids_final = [
                        [int(round(float(price) * scale)), int(volume)]
                        for price, volume in zip(bp, bv)
                    ]

            if asks_final is None:
                if use_rust and _RUST_SCALE_BOOK_SEQ:
                    try:
                        asks_final = _RUST_SCALE_BOOK_SEQ(ap, av, scale)
                    except Exception as exc:
                        logger.debug("rust_bidask_fallback", stage="scale_book_seq_ask", error=str(exc))
                        if self._rust_fallback_bidask:
                            self._rust_fallback_bidask.inc()
                        asks_final = None
                if asks_final is None:
                    asks_final = [
                        [int(round(float(price) * scale)), int(volume)]
                        for price, volume in zip(ap, av)
                    ]

            if not synthesized:
                bids_final, asks_final, synthesized = self._maybe_synthesize_side(symbol, bids_final, asks_final, scale)

            # Update trade classifier with latest best bid/ask
            if stats is not None:
                self._trade_classifier.update_quotes(symbol, stats[0], stats[1])
            elif bids_final and asks_final:
                _bb = int(bids_final[0][0]) if hasattr(bids_final, "__getitem__") and len(bids_final) > 0 else 0
                _ba = int(asks_final[0][0]) if hasattr(asks_final, "__getitem__") and len(asks_final) > 0 else 0
                if _bb > 0 and _ba > 0:
                    self._trade_classifier.update_quotes(symbol, _bb, _ba)

            if _RETURN_TUPLE:
                if stats is not None and not synthesized:
                    return (
                        "bidask",
                        symbol,
                        bids_final,
                        asks_final,
                        exch_ts,
                        False,
                        stats[0],
                        stats[1],
                        stats[2],
                        stats[3],
                        stats[4],
                        stats[5],
                        stats[6],
                    )
                return ("bidask", symbol, bids_final, asks_final, exch_ts, False)

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "bidask", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_bidask")
            meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=local_ts)
            event_stats = stats if stats is not None and not synthesized else None
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids_final, asks=asks_final, stats=event_stats)
        except Exception as e:
            logger.error("Normalize BidAsk Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="BidAsk").inc()
            return None

    def normalize_snapshot(self, payload: dict[str, Any]) -> BidAskEvent | tuple | None:
        if isinstance(payload, dict):
            symbol = payload.get("code") or payload.get("Code")
            ts_val = payload.get("ts") or payload.get("datetime")
            buy_price = payload.get("buy_price")
            buy_volume = payload.get("buy_volume")
            sell_price = payload.get("sell_price")
            sell_volume = payload.get("sell_volume")
        else:
            symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
            ts_val = getattr(payload, "ts", None) or getattr(payload, "datetime", None)
            buy_price = getattr(payload, "buy_price", None)
            buy_volume = getattr(payload, "buy_volume", None)
            sell_price = getattr(payload, "sell_price", None)
            sell_volume = getattr(payload, "sell_volume", None)

        if not symbol:
            if self._skip_snapshot_missing_symbol:
                self._skip_snapshot_missing_symbol.inc()
            return None

        exch_ts = _extract_ts_ns(ts_val)

        scale = self._get_scale(symbol)

        if buy_price is not None or sell_price is not None:
            bids = []
            asks = []
            if buy_price:
                bids.append([int(round(float(buy_price) * scale)), int(buy_volume or 0)])
            if sell_price:
                asks.append([int(round(float(sell_price) * scale)), int(sell_volume or 0)])
            bids, asks, _ = self._maybe_synthesize_side(symbol, bids, asks, scale)

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids, asks, exch_ts, True)

            local_ts = timebase.now_ns()
            exch_ts, local_ts = self._validate_and_sync_timestamp(exch_ts, local_ts, "snapshot", symbol)
            self._record_latency_metrics(exch_ts, local_ts, "_last_local_ts_snapshot")
            meta = MetaData(seq=self._next_seq(), topic="snapshot", source_ts=exch_ts, local_ts=local_ts)
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids, asks=asks, is_snapshot=True)

        event = self.normalize_bidask(payload)
        if isinstance(event, tuple):
            if len(event) > 6:
                return (event[0], event[1], event[2], event[3], event[4], True, *event[6:])
            return (event[0], event[1], event[2], event[3], event[4], True)
        if event:
            event = replace(event, is_snapshot=True)
        return event
