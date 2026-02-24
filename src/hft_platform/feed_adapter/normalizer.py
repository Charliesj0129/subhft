import importlib
import os
import re
import sys
from typing import Any, Dict, Iterable, Optional, cast

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.observability.metrics import MetricsRegistry

# Validated Imports

# Use existing implementation of SymbolMetadata.

logger = get_logger("feed_adapter.normalizer")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_RUST_MIN_LEVELS = int(os.getenv("HFT_RUST_MIN_LEVELS", "0"))
_EVENT_MODE = os.getenv("HFT_EVENT_MODE", "tuple").lower()
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

try:
    try:
        _rust_core = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")

    _RUST_SCALE_BOOK = _rust_core.scale_book
    _RUST_SCALE_BOOK_SEQ = _rust_core.scale_book_seq
    _RUST_SCALE_BOOK_PAIR = _rust_core.scale_book_pair
    _RUST_SCALE_BOOK_PAIR_STATS = getattr(_rust_core, "scale_book_pair_stats", None)
    _RUST_GET_FIELD = _rust_core.get_field
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
    _RUST_GET_FIELD = None
    _RUST_NORMALIZE_TICK = None
    _RUST_NORMALIZE_BIDASK = None
    _RUST_NORMALIZE_BIDASK_NP = None
    _RUST_NORMALIZE_BIDASK_SYNTH = None


class SymbolMetadata:
    """
    Loads per-symbol configuration.
    """

    DEFAULT_SCALE = 10_000

    def __init__(self, config_path: Optional[str] = None):
        # simplified for this context, assuming existing logic was ok, just need it here
        if config_path is None:
            config_path = os.getenv("SYMBOLS_CONFIG")
            if not config_path:
                if os.path.exists("config/symbols.yaml"):
                    config_path = "config/symbols.yaml"
                else:
                    config_path = "config/base/symbols.yaml"

        self.config_path = config_path
        self.meta: Dict[str, Dict[str, Any]] = {}
        self.tags_by_symbol: Dict[str, set[str]] = {}
        self.symbols_by_tag: Dict[str, set[str]] = {}
        self._price_scale_cache: Dict[str, int] = {}
        self._exchange_cache: Dict[str, str] = {}
        self._product_type_cache: Dict[str, str] = {}
        self._mtime: float | None = None
        self._load()

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
            with open(self.config_path, "r") as f:
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
        except Exception:
            pass

    def reload(self) -> None:
        self._load()

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

    def order_params(self, symbol: str) -> Dict[str, Any]:
        entry = self.meta.get(symbol) or {}
        params: Dict[str, Any] = {}
        for key in ("order_cond", "order_lot", "oc_type", "account"):
            if key in entry and entry[key] is not None:
                params[key] = entry[key]
        return params


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
        "_last_local_ts_ns",
        "_last_skew_log_ns",
        "_fixed5_scratch_enabled",
        "_fixed5_bid_prices_np",
        "_fixed5_bid_vols_np",
        "_fixed5_ask_prices_np",
        "_fixed5_ask_vols_np",
    )

    def __init__(self, config_path: Optional[str] = None, metadata: SymbolMetadata | None = None):
        import itertools

        self._seq_gen = itertools.count(1)
        # self._lock = Lock() # Removed
        self.metadata = metadata or SymbolMetadata(config_path)
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self.metrics = MetricsRegistry.get()
        self._last_symbol: str | None = None
        self._last_scale: int = SymbolMetadata.DEFAULT_SCALE
        self._last_local_ts_ns = {"tick": 0, "bidask": 0, "snapshot": 0}
        self._last_skew_log_ns = 0
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
            except Exception:
                self._fixed5_scratch_enabled = False

    def _next_seq(self) -> int:
        return next(self._seq_gen)

    def _get_field(self, payload: Any, keys: list) -> Any:
        """Helper to get value from dict or object using priority keys."""
        if _RUST_ENABLED and _RUST_GET_FIELD is not None:
            try:
                value = _RUST_GET_FIELD(payload, keys)
                if value is not None:
                    return value
            except Exception:
                pass

        if isinstance(payload, dict):
            get = payload.get
            for key in keys:
                val = get(key)
                if val is not None:
                    return val
            return None

        for key in keys:
            val = getattr(payload, key, None)
            if val is not None:
                return val
        return None

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

    def normalize_tick(self, payload: Any) -> Optional[TickEvent | tuple]:
        # Fast path lookup without _coalesce
        # Assuming Shioaji standard keys: 'code', 'close', 'volume', 'ts'
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") or payload.get("datetime")
                close_val = payload.get("close") or payload.get("Close")
                vol_val = payload.get("volume") or payload.get("Volume")
                total_volume = int(payload.get("total_volume") or 0)
                is_simtrade = bool(payload.get("simtrade") or 0)
                is_odd_lot = bool(payload.get("intraday_odd") or 0)
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                ts_val = getattr(payload, "ts", None) or getattr(payload, "datetime", None)
                close_val = getattr(payload, "close", None) or getattr(payload, "Close", None)
                vol_val = getattr(payload, "volume", None) or getattr(payload, "Volume", None)
                total_volume = int(getattr(payload, "total_volume", None) or 0)
                is_simtrade = bool(getattr(payload, "simtrade", None) or 0)
                is_odd_lot = bool(getattr(payload, "intraday_odd", None) or 0)

            if not symbol:
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
                            if exch_ts_py:
                                exch_ts = exch_ts_py
                            if _RETURN_TUPLE:
                                return rust_tuple
                            local_ts = timebase.now_ns()
                            if exch_ts:
                                exch_ts = _clamp_future_ts(exch_ts, local_ts, "tick", _sym)
                                if local_ts < exch_ts:
                                    local_ts = exch_ts
                                else:
                                    delta = local_ts - exch_ts
                                    if _TS_MAX_LAG_NS and delta > _TS_MAX_LAG_NS:
                                        if _TS_SKEW_LOG_COOLDOWN_NS and (
                                            local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS
                                        ):
                                            logger.warning(
                                                "Feed time skew",
                                                topic="tick",
                                                symbol=_sym,
                                                delta_ns=delta,
                                                max_ns=_TS_MAX_LAG_NS,
                                            )
                                            self._last_skew_log_ns = local_ts
                                        if self.metrics:
                                            self.metrics.feed_time_skew_ns.labels(topic="tick").set(delta)
                                        local_ts = exch_ts + _TS_MAX_LAG_NS
                            if self.metrics:
                                if exch_ts:
                                    lag_ns = local_ts - exch_ts
                                    if lag_ns >= 0:
                                        self.metrics.feed_latency_ns.observe(lag_ns)
                                last = self._last_local_ts_ns.get("tick", 0)
                                if last:
                                    delta = local_ts - last
                                    if delta >= 0:
                                        self.metrics.feed_interarrival_ns.observe(delta)
                                self._last_local_ts_ns["tick"] = local_ts
                            meta = MetaData(
                                seq=self._next_seq(),
                                topic="tick",
                                source_ts=exch_ts,
                                local_ts=local_ts,
                            )
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
                            )
                    except Exception:
                        pass
                price = int(float(close_val) * scale)
            else:
                price = 0

            volume = int(vol_val) if vol_val is not None else 0

            if _RETURN_TUPLE:
                return (
                    "tick",
                    symbol,
                    price,
                    volume,
                    total_volume,
                    is_simtrade,
                    is_odd_lot,
                    exch_ts,
                )

            local_ts = timebase.now_ns()
            if exch_ts:
                exch_ts = _clamp_future_ts(exch_ts, local_ts, "tick", symbol)
                if local_ts < exch_ts:
                    local_ts = exch_ts
                else:
                    delta = local_ts - exch_ts
                    if _TS_MAX_LAG_NS and delta > _TS_MAX_LAG_NS:
                        if _TS_SKEW_LOG_COOLDOWN_NS and (local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS):
                            logger.warning(
                                "Feed time skew",
                                topic="tick",
                                symbol=symbol,
                                delta_ns=delta,
                                max_ns=_TS_MAX_LAG_NS,
                            )
                            self._last_skew_log_ns = local_ts
                        if self.metrics:
                            self.metrics.feed_time_skew_ns.labels(topic="tick").set(delta)
                        local_ts = exch_ts + _TS_MAX_LAG_NS
            if self.metrics:
                if exch_ts:
                    lag_ns = local_ts - exch_ts
                    if lag_ns >= 0:
                        self.metrics.feed_latency_ns.observe(lag_ns)
                last = self._last_local_ts_ns.get("tick", 0)
                if last:
                    delta = local_ts - last
                    if delta >= 0:
                        self.metrics.feed_interarrival_ns.observe(delta)
                self._last_local_ts_ns["tick"] = local_ts
            meta = MetaData(seq=self._next_seq(), topic="tick", source_ts=exch_ts, local_ts=local_ts)

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
            )
        except Exception as e:
            logger.error("Normalize Tick Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="Tick").inc()
            return None

    def normalize_bidask(self, payload: Any) -> Optional[BidAskEvent | tuple]:
        try:
            if isinstance(payload, dict):
                symbol = payload.get("code") or payload.get("Code")
                ts_val = payload.get("ts") or payload.get("datetime")
                bp = payload.get("bid_price") or []
                bv = payload.get("bid_volume") or []
                ap = payload.get("ask_price") or []
                av = payload.get("ask_volume") or []
            else:
                symbol = getattr(payload, "code", None) or getattr(payload, "Code", None)
                ts_val = getattr(payload, "ts", None) or getattr(payload, "datetime", None)
                bp = getattr(payload, "bid_price", None) or []
                bv = getattr(payload, "bid_volume", None) or []
                ap = getattr(payload, "ask_price", None) or []
                av = getattr(payload, "ask_volume", None) or []
            if not symbol:
                return None

            exch_ts = _extract_ts_ns(ts_val)

            scale = self._get_scale(symbol)

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
                        stats = (
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception:
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
                    bids_final, asks_final, stats = _RUST_SCALE_BOOK_PAIR_STATS(bp, bv, ap, av, scale)
                except Exception:
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
                        stats = (
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception:
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
                        stats = (
                            int(best_bid),
                            int(best_ask),
                            int(bid_depth),
                            int(ask_depth),
                            float(mid_price),
                            float(spread),
                            float(imbalance),
                        )
                except Exception:
                    bids_final = None
                    asks_final = None
                    stats = None

            # If a Rust normalize path already returned bids/asks+stats, do not recompute
            # the same work again via scale_book_pair_stats.
            if stats is None and use_rust and _RUST_SCALE_BOOK_PAIR_STATS and _RUST_STATS_TUPLE:
                try:
                    bids_final, asks_final, stats = _RUST_SCALE_BOOK_PAIR_STATS(bp, bv, ap, av, scale)
                except Exception:
                    bids_final = None
                    asks_final = None
                    stats = None
            if bids_final is None and use_rust and _RUST_SCALE_BOOK_PAIR:
                try:
                    bids_final, asks_final = _RUST_SCALE_BOOK_PAIR(bp, bv, ap, av, scale)
                except Exception:
                    bids_final = None
                    asks_final = None

            if bids_final is None:
                if use_rust and _RUST_SCALE_BOOK_SEQ:
                    try:
                        bids_final = _RUST_SCALE_BOOK_SEQ(bp, bv, scale)
                    except Exception:
                        bids_final = None
                if bids_final is None:
                    bids_final = [
                        [int(float(price) * scale), int(volume)] for price, volume in zip(bp, bv) if price and volume
                    ]

            if asks_final is None:
                if use_rust and _RUST_SCALE_BOOK_SEQ:
                    try:
                        asks_final = _RUST_SCALE_BOOK_SEQ(ap, av, scale)
                    except Exception:
                        asks_final = None
                if asks_final is None:
                    asks_final = [
                        [int(float(price) * scale), int(volume)] for price, volume in zip(ap, av) if price and volume
                    ]

            if not synthesized:
                bids_final, asks_final, synthesized = self._maybe_synthesize_side(symbol, bids_final, asks_final, scale)

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
            if exch_ts:
                exch_ts = _clamp_future_ts(exch_ts, local_ts, "bidask", symbol)
                if local_ts < exch_ts:
                    local_ts = exch_ts
                else:
                    delta = local_ts - exch_ts
                    if _TS_MAX_LAG_NS and delta > _TS_MAX_LAG_NS:
                        if _TS_SKEW_LOG_COOLDOWN_NS and (local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS):
                            logger.warning(
                                "Feed time skew",
                                topic="bidask",
                                symbol=symbol,
                                delta_ns=delta,
                                max_ns=_TS_MAX_LAG_NS,
                            )
                            self._last_skew_log_ns = local_ts
                        if self.metrics:
                            self.metrics.feed_time_skew_ns.labels(topic="bidask").set(delta)
                        local_ts = exch_ts + _TS_MAX_LAG_NS
            if self.metrics:
                if exch_ts:
                    lag_ns = local_ts - exch_ts
                    if lag_ns >= 0:
                        self.metrics.feed_latency_ns.observe(lag_ns)
                last = self._last_local_ts_ns.get("bidask", 0)
                if last:
                    delta = local_ts - last
                    if delta >= 0:
                        self.metrics.feed_interarrival_ns.observe(delta)
                self._last_local_ts_ns["bidask"] = local_ts
            meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=local_ts)
            event_stats = stats if stats is not None and not synthesized else None
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids_final, asks=asks_final, stats=event_stats)
        except Exception as e:
            logger.error("Normalize BidAsk Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="BidAsk").inc()
            return None

    def normalize_snapshot(self, payload: Dict[str, Any]) -> Optional[BidAskEvent | tuple]:
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
            return None

        exch_ts = _extract_ts_ns(ts_val)

        scale = self._get_scale(symbol)

        if buy_price is not None or sell_price is not None:
            bids = []
            asks = []
            if buy_price:
                bids.append([int(float(buy_price) * scale), int(buy_volume or 0)])
            if sell_price:
                asks.append([int(float(sell_price) * scale), int(sell_volume or 0)])
            bids, asks, _ = self._maybe_synthesize_side(symbol, bids, asks, scale)

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids, asks, exch_ts, True)

            local_ts = timebase.now_ns()
            if exch_ts:
                exch_ts = _clamp_future_ts(exch_ts, local_ts, "snapshot", symbol)
                if local_ts < exch_ts:
                    local_ts = exch_ts
                else:
                    delta = local_ts - exch_ts
                    if _TS_MAX_LAG_NS and delta > _TS_MAX_LAG_NS:
                        if _TS_SKEW_LOG_COOLDOWN_NS and (local_ts - self._last_skew_log_ns > _TS_SKEW_LOG_COOLDOWN_NS):
                            logger.warning(
                                "Feed time skew",
                                topic="snapshot",
                                symbol=symbol,
                                delta_ns=delta,
                                max_ns=_TS_MAX_LAG_NS,
                            )
                            self._last_skew_log_ns = local_ts
                        if self.metrics:
                            self.metrics.feed_time_skew_ns.labels(topic="snapshot").set(delta)
                        local_ts = exch_ts + _TS_MAX_LAG_NS
            if self.metrics:
                if exch_ts:
                    lag_ns = local_ts - exch_ts
                    if lag_ns >= 0:
                        self.metrics.feed_latency_ns.observe(lag_ns)
                last = self._last_local_ts_ns.get("snapshot", 0)
                if last:
                    delta = local_ts - last
                    if delta >= 0:
                        self.metrics.feed_interarrival_ns.observe(delta)
                self._last_local_ts_ns["snapshot"] = local_ts
            meta = MetaData(seq=self._next_seq(), topic="snapshot", source_ts=exch_ts, local_ts=local_ts)
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids, asks=asks, is_snapshot=True)

        event = self.normalize_bidask(payload)
        if isinstance(event, tuple):
            if len(event) > 6:
                return (event[0], event[1], event[2], event[3], event[4], True, *event[6:])
            return (event[0], event[1], event[2], event[3], event[4], True)
        if event:
            event.is_snapshot = True
        return event
