import os
import re
import time
from typing import Any, Dict, Iterable, Optional

from structlog import get_logger

from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.events import BidAskEvent, MetaData, TickEvent
from hft_platform.observability.metrics import MetricsRegistry

# Validated Imports

# Use existing implementation of SymbolMetadata.

logger = get_logger("feed_adapter.normalizer")

_RUST_ENABLED = os.getenv("HFT_RUST_ACCEL", "1").lower() not in {"0", "false", "no", "off"}
_RUST_MIN_LEVELS = int(os.getenv("HFT_RUST_MIN_LEVELS", "0"))
_EVENT_MODE = os.getenv("HFT_EVENT_MODE", "tuple").lower()
_RETURN_TUPLE = _EVENT_MODE in {"tuple", "raw"}

try:
    try:
        from hft_platform import rust_core as _rust_core  # type: ignore[attr-defined]
    except Exception:
        import rust_core as _rust_core

    _RUST_SCALE_BOOK = _rust_core.scale_book
    _RUST_SCALE_BOOK_SEQ = _rust_core.scale_book_seq
    _RUST_SCALE_BOOK_PAIR = _rust_core.scale_book_pair
    _RUST_GET_FIELD = _rust_core.get_field
except Exception:
    _RUST_SCALE_BOOK = None
    _RUST_SCALE_BOOK_SEQ = None
    _RUST_SCALE_BOOK_PAIR = None
    _RUST_GET_FIELD = None


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


class MarketDataNormalizer:
    __slots__ = ("_seq_gen", "metadata", "price_codec", "metrics", "_last_symbol", "_last_scale")

    def __init__(self, config_path: Optional[str] = None, metadata: SymbolMetadata | None = None):
        import itertools

        self._seq_gen = itertools.count(1)
        # self._lock = Lock() # Removed
        self.metadata = metadata or SymbolMetadata(config_path)
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self.metrics = MetricsRegistry.get()
        self._last_symbol: str | None = None
        self._last_scale: int = SymbolMetadata.DEFAULT_SCALE

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

            if ts_val is not None and hasattr(ts_val, "timestamp"):
                exch_ts = int(getattr(ts_val, "timestamp")() * 1e9)
            else:
                exch_ts = int(ts_val) if ts_val else 0

            if close_val is not None:
                scale = self._get_scale(symbol)
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

            meta = MetaData(seq=self._next_seq(), topic="tick", source_ts=exch_ts, local_ts=time.time_ns())

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

            if ts_val is not None and hasattr(ts_val, "timestamp"):
                exch_ts = int(getattr(ts_val, "timestamp")() * 1e9)
            else:
                exch_ts = int(ts_val) if ts_val else 0

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
            use_rust = _RUST_ENABLED and _RUST_MIN_LEVELS <= 0
            if not use_rust and _RUST_ENABLED and _RUST_MIN_LEVELS > 0:
                use_rust = (
                    len(bp) >= _RUST_MIN_LEVELS
                    and len(bv) >= _RUST_MIN_LEVELS
                    and len(ap) >= _RUST_MIN_LEVELS
                    and len(av) >= _RUST_MIN_LEVELS
                )

            if use_rust and _RUST_SCALE_BOOK_PAIR:
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
                        bids_final = []
                else:
                    bids_final = []

            if asks_final is None:
                if use_rust and _RUST_SCALE_BOOK_SEQ:
                    try:
                        asks_final = _RUST_SCALE_BOOK_SEQ(ap, av, scale)
                    except Exception:
                        asks_final = []
                else:
                    asks_final = []

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids_final, asks_final, exch_ts, False)

            meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=time.time_ns())
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids_final, asks=asks_final)
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

        if ts_val is not None and hasattr(ts_val, "timestamp"):
            exch_ts = int(getattr(ts_val, "timestamp")() * 1e9)
        else:
            exch_ts = int(ts_val) if ts_val else 0

        scale = self._get_scale(symbol)

        if buy_price is not None or sell_price is not None:
            bids = []
            asks = []
            if buy_price:
                bids.append([int(float(buy_price) * scale), int(buy_volume or 0)])
            if sell_price:
                asks.append([int(float(sell_price) * scale), int(sell_volume or 0)])

            if _RETURN_TUPLE:
                return ("bidask", symbol, bids, asks, exch_ts, True)

            meta = MetaData(seq=self._next_seq(), topic="snapshot", source_ts=exch_ts, local_ts=time.time_ns())
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids, asks=asks, is_snapshot=True)

        event = self.normalize_bidask(payload)
        if isinstance(event, tuple):
            return (event[0], event[1], event[2], event[3], event[4], True)
        if event:
            event.is_snapshot = True
        return event
