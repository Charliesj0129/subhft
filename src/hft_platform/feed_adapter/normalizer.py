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
        self._mtime: float | None = None
        self._load()

    def _load(self) -> None:
        import yaml

        self.meta = {}
        self.tags_by_symbol = {}
        self.symbols_by_tag = {}
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
        # Avoid creating empty dict
        entry = self.meta.get(symbol)
        if entry:
            if "price_scale" in entry:
                return int(entry.get("price_scale", self.DEFAULT_SCALE))
            tick_size = entry.get("tick_size")
            if tick_size:
                try:
                    scale = int(round(1 / float(tick_size)))
                    if scale > 0:
                        return scale
                except (TypeError, ValueError, ZeroDivisionError):
                    pass
        return self.DEFAULT_SCALE

    def exchange(self, symbol: str) -> str:
        entry = self.meta.get(symbol) or {}
        return str(entry.get("exchange", ""))

    def product_type(self, symbol: str) -> str:
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
            return raw

        exchange = self.exchange(symbol).upper()
        if exchange in {"TSE", "OTC", "OES"}:
            return "stock"
        if exchange in {"FUT", "FUTURES", "TAIFEX"}:
            return "future"
        if exchange in {"OPT", "OPTIONS"}:
            return "option"
        if exchange in {"IDX", "INDEX"}:
            return "index"
        return ""

    def order_params(self, symbol: str) -> Dict[str, Any]:
        entry = self.meta.get(symbol) or {}
        params: Dict[str, Any] = {}
        for key in ("order_cond", "order_lot", "oc_type", "account"):
            if key in entry and entry[key] is not None:
                params[key] = entry[key]
        return params


class MarketDataNormalizer:
    __slots__ = ("_seq_gen", "metadata", "price_codec", "metrics")

    def __init__(self, config_path: Optional[str] = None, metadata: SymbolMetadata | None = None):
        import itertools

        self._seq_gen = itertools.count(1)
        # self._lock = Lock() # Removed
        self.metadata = metadata or SymbolMetadata(config_path)
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        self.metrics = MetricsRegistry.get()

    def _next_seq(self) -> int:
        return next(self._seq_gen)

    def _get_field(self, payload: Any, keys: list) -> Any:
        """Helper to get value from dict or object using priority keys."""
        for key in keys:
            val = None
            if isinstance(payload, dict):
                val = payload.get(key)
            else:
                val = getattr(payload, key, None)

            if val is not None:
                return val
        return None

    def normalize_tick(self, payload: Any) -> Optional[TickEvent]:
        # Fast path lookup without _coalesce
        # Assuming Shioaji standard keys: 'code', 'close', 'volume', 'ts'
        try:
            symbol = self._get_field(payload, ["code", "Code"])
            if not symbol:
                return None

            # Timestamp
            ts_val = self._get_field(payload, ["ts", "datetime"])
            if hasattr(ts_val, "timestamp"):
                exch_ts = int(ts_val.timestamp() * 1e9)
            else:
                exch_ts = int(ts_val) if ts_val else 0

            # Price
            close_val = self._get_field(payload, ["close", "Close"])
            price = self.price_codec.scale(symbol, close_val) if close_val is not None else 0

            # Volume
            vol_val = self._get_field(payload, ["volume", "Volume"])
            volume = int(vol_val) if vol_val is not None else 0

            meta = MetaData(seq=self._next_seq(), topic="tick", source_ts=exch_ts, local_ts=time.time_ns())

            return TickEvent(
                meta=meta,
                symbol=symbol,
                price=price,
                volume=volume,
                total_volume=int(self._get_field(payload, ["total_volume"]) or 0),
                bid_side_total_vol=0,  # Optimization: skip less used fields unless needed
                ask_side_total_vol=0,
                is_simtrade=bool(self._get_field(payload, ["simtrade"]) or 0),
                is_odd_lot=bool(self._get_field(payload, ["intraday_odd"]) or 0),
            )
        except Exception as e:
            logger.error("Normalize Tick Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="Tick").inc()
            return None

    def normalize_bidask(self, payload: Any) -> Optional[BidAskEvent]:
        try:
            symbol = self._get_field(payload, ["code", "Code"])
            if not symbol:
                return None

            # Timestamp
            ts_val = self._get_field(payload, ["ts", "datetime"])
            if hasattr(ts_val, "timestamp"):
                exch_ts = int(ts_val.timestamp() * 1e9)
            else:
                exch_ts = int(ts_val) if ts_val else 0

            scale = self.price_codec.scale_factor(symbol)

            # Arrays
            # Shioaji sends 'bid_price': [p1, p2...], 'bid_volume': [v1, v2...]
            bp = self._get_field(payload, ["bid_price"]) or []
            bv = self._get_field(payload, ["bid_volume"]) or []
            ap = self._get_field(payload, ["ask_price"]) or []
            av = self._get_field(payload, ["ask_volume"]) or []

            # Convert to numpy
            # We need to scale prices. Using numpy vectorization for scaling is faster.
            # But converting list->numpy is overhead.
            # If lists are small (5 levels), list comp might be faster than np.array(list).

            # Bids with filtering 0s?
            # Shioaji might send 0 for empty levels.

            # Optimization: Use list comprehension for small N (N=5)
            # Returns List[List[int]] directly (faster than numpy conversion)

            # Bids
            # Filter and Scale in one pass
            bids_final = [[int(p * scale), int(v)] for p, v in zip(bp, bv) if p > 0]

            # Asks
            asks_final = [[int(p * scale), int(v)] for p, v in zip(ap, av) if p > 0]

            meta = MetaData(seq=self._next_seq(), topic="bidask", source_ts=exch_ts, local_ts=time.time_ns())

            return BidAskEvent(meta=meta, symbol=symbol, bids=bids_final, asks=asks_final)
        except Exception as e:
            logger.error("Normalize BidAsk Error", error=str(e), payload_type=str(type(payload)))
            if self.metrics:
                self.metrics.normalization_errors_total.labels(type="BidAsk").inc()
            return None

    def normalize_snapshot(self, payload: Dict[str, Any]) -> Optional[BidAskEvent]:
        symbol = self._get_field(payload, ["code", "Code"])
        if not symbol:
            return None

        ts_val = self._get_field(payload, ["ts", "datetime"])
        if hasattr(ts_val, "timestamp"):
            exch_ts = int(ts_val.timestamp() * 1e9)
        else:
            exch_ts = int(ts_val) if ts_val else 0

        scale = self.price_codec.scale_factor(symbol)

        buy_price = self._get_field(payload, ["buy_price"])
        buy_volume = self._get_field(payload, ["buy_volume"])
        sell_price = self._get_field(payload, ["sell_price"])
        sell_volume = self._get_field(payload, ["sell_volume"])

        if buy_price is not None or sell_price is not None:
            bids = []
            asks = []
            if buy_price:
                bids.append([int(float(buy_price) * scale), int(buy_volume or 0)])
            if sell_price:
                asks.append([int(float(sell_price) * scale), int(sell_volume or 0)])

            meta = MetaData(seq=self._next_seq(), topic="snapshot", source_ts=exch_ts, local_ts=time.time_ns())
            return BidAskEvent(meta=meta, symbol=symbol, bids=bids, asks=asks, is_snapshot=True)

        event = self.normalize_bidask(payload)
        if event:
            event.is_snapshot = True
        return event
