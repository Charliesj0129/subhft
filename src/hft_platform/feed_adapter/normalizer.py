import time
import yaml
from typing import Dict, Any, List, Optional, Sequence
from threading import Lock

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.normalizer")


class SymbolMetadata:
    """
    Loads per-symbol configuration from config/symbols.yaml. Supports fields:
    - tick_size (float)
    - decimals (int)
    - price_scale (int, overrides tick_size/decimals)
    - lot_size
    - odd_lot (bool)
    """

    DEFAULT_SCALE = 10_000

    def __init__(self, config_path: str = "config/symbols.yaml"):
        self.config_path = config_path
        self.meta: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self):
        try:
            with open(self.config_path, "r") as f:
                data = yaml.safe_load(f) or {}
                for item in data.get("symbols", []):
                    code = item.get("code")
                    if code:
                        self.meta[code] = item
            logger.info("Loaded symbol metadata", count=len(self.meta))
        except Exception as exc:
            logger.error("Failed to load symbol metadata", error=str(exc))

    def price_scale(self, symbol: str) -> int:
        entry = self.meta.get(symbol, {})
        if "price_scale" in entry:
            return int(entry["price_scale"])
        if "decimals" in entry:
            return 10 ** int(entry["decimals"])
        # tick_size = entry.get("tick_size")
        # if tick_size:
        #     # Deriving scale from tick_size (e.g. 0.01 -> 100) conflicts with system-wide FixedPoint (x10000)
        #     # We disable this derivation to enforce x10000 unless "decimals" or "price_scale" is set.
        #     try:
        #         pass
        #     except (ValueError, ZeroDivisionError):
        #         pass
        return self.DEFAULT_SCALE

    def lot_size(self, symbol: str) -> int:
        return int(self.meta.get(symbol, {}).get("lot_size", 1))

    def is_odd_lot(self, symbol: str) -> bool:
        return bool(self.meta.get(symbol, {}).get("odd_lot", False))


class MarketDataNormalizer:
    def __init__(self, config_path: str = "config/symbols.yaml"):
        self._seq = 0
        self._lock = Lock()
        self.metadata = SymbolMetadata(config_path)
        self.metrics = MetricsRegistry.get()

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    @staticmethod
    def capture_local_time_ns() -> int:
        return time.time_ns()

    @staticmethod
    def _coalesce(payload: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        return None

    def _scale_price(self, symbol: str, value: Any) -> int:
        if value is None:
            return 0
        try:
            scale = self.metadata.price_scale(symbol)
            return int(float(value) * scale)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _to_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _normalize_levels(self, symbol: str, prices: Sequence[Any], volumes: Sequence[Any]) -> List[Dict[str, int]]:
        levels = []
        for price, vol in zip(prices, volumes):
            scaled_price = self._scale_price(symbol, price)
            volume_int = self._to_int(vol)
            if scaled_price == 0 and volume_int == 0:
                continue
            levels.append({"price": scaled_price, "volume": volume_int})
        return levels

    def normalize_tick(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Tick payload per sinotrade_tutor_md/market_data/streaming/stocks.md.
        """
        try:
            symbol = self._coalesce(payload, "code", "Code")
            if not symbol:
                return None

            local_ts = self.capture_local_time_ns()
            exch_ts = self._to_int(self._coalesce(payload, "ts", "datetime", "DateTime", "Timestamp"))

            close = self._coalesce(payload, "close", "Close")
            volume = self._coalesce(payload, "volume", "Volume")

            event = {
                "type": "Tick",
                "seq": self._next_seq(),
                "symbol": symbol,
                "exch_ts": exch_ts,
                "local_ts": local_ts,
                "price": self._scale_price(symbol, close),
                "volume": self._to_int(volume),
                "total_volume": self._to_int(self._coalesce(payload, "total_volume", "TotalVolume")),
                "amount": self._to_int(self._coalesce(payload, "amount", "Amount")),
                "total_amount": self._to_int(self._coalesce(payload, "total_amount", "AmountSum", "TotalAmount")),
                "tick_type": self._to_int(self._coalesce(payload, "tick_type", "TickType")),
                "chg_type": self._to_int(self._coalesce(payload, "chg_type", "ChgType")),
                "price_chg": self._scale_price(symbol, self._coalesce(payload, "price_chg", "PriceChg")),
                "pct_chg": float(self._coalesce(payload, "pct_chg", "PctChg") or 0),
                "bid_side_total_vol": self._to_int(self._coalesce(payload, "bid_side_total_vol")),
                "ask_side_total_vol": self._to_int(self._coalesce(payload, "ask_side_total_vol")),
                "bid_side_total_cnt": self._to_int(self._coalesce(payload, "bid_side_total_cnt")),
                "ask_side_total_cnt": self._to_int(self._coalesce(payload, "ask_side_total_cnt")),
                "simtrade": self._to_int(self._coalesce(payload, "simtrade", "Simtrade")),
                "intraday_odd": self._to_int(self._coalesce(payload, "intraday_odd", "IntradayOdd")),
                "suspend": self._to_int(self._coalesce(payload, "suspend", "Suspend")),
            }
            return event
        except Exception as exc:
            logger.error("Tick normalization failed", error=str(exc), payload=str(payload)[:200])
            self.metrics.normalization_errors_total.labels(type="Tick").inc()
            return None

    def normalize_bidask(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize BidAsk payload per sinotrade_tutor_md/market_data/streaming/stocks.md.
        """
        try:
            symbol = self._coalesce(payload, "code", "Code")
            if not symbol:
                return None

            local_ts = self.capture_local_time_ns()
            exch_ts = self._to_int(self._coalesce(payload, "ts", "datetime"))

            bid_prices = self._coalesce(payload, "bid_price", "BidPrice") or []
            bid_volumes = self._coalesce(payload, "bid_volume", "BidVolume") or []
            ask_prices = self._coalesce(payload, "ask_price", "AskPrice") or []
            ask_volumes = self._coalesce(payload, "ask_volume", "AskVolume") or []

            bids = self._normalize_levels(symbol, bid_prices, bid_volumes)
            asks = self._normalize_levels(symbol, ask_prices, ask_volumes)

            event = {
                "type": "BidAsk",
                "seq": self._next_seq(),
                "symbol": symbol,
                "exch_ts": exch_ts,
                "local_ts": local_ts,
                "bids": bids,
                "asks": asks,
                "diff_bid_vol": list(self._coalesce(payload, "diff_bid_vol", "DiffBidVol") or []),
                "diff_ask_vol": list(self._coalesce(payload, "diff_ask_vol", "DiffAskVol") or []),
                "suspend": self._to_int(self._coalesce(payload, "suspend", "Suspend")),
                "simtrade": self._to_int(self._coalesce(payload, "simtrade", "Simtrade")),
                "intraday_odd": self._to_int(self._coalesce(payload, "intraday_odd", "IntradayOdd")),
            }
            return event
        except Exception as exc:
            logger.error("BidAsk normalization failed", error=str(exc), payload=str(payload)[:200])
            self.metrics.normalization_errors_total.labels(type="BidAsk").inc()
            return None

    def normalize_snapshot(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Normalize Snapshot payload per sinotrade_tutor_md/market_data/snapshot.md.
        """
        try:
            symbol = self._coalesce(payload, "code", "Code")
            if not symbol:
                return None

            local_ts = self.capture_local_time_ns()
            exch_ts = self._to_int(self._coalesce(payload, "ts", "Timestamp"))

            bids: List[Dict[str, int]] = []
            asks: List[Dict[str, int]] = []

            buy_price = self._coalesce(payload, "buy_price", "BuyPrice")
            buy_volume = self._coalesce(payload, "buy_volume", "BuyVolume")
            sell_price = self._coalesce(payload, "sell_price", "SellPrice")
            sell_volume = self._coalesce(payload, "sell_volume", "SellVolume")

            if buy_price is not None and buy_volume is not None:
                bids.append({"price": self._scale_price(symbol, buy_price), "volume": self._to_int(buy_volume)})
            if sell_price is not None and sell_volume is not None:
                asks.append({"price": self._scale_price(symbol, sell_price), "volume": self._to_int(sell_volume)})

            if not bids and "bids" in payload:
                bids = self._normalize_levels(symbol, [b.get("price") for b in payload["bids"]], [b.get("volume") for b in payload["bids"]])
            if not asks and "asks" in payload:
                asks = self._normalize_levels(symbol, [a.get("price") for a in payload["asks"]], [a.get("volume") for a in payload["asks"]])

            event = {
                "type": "Snapshot",
                "seq": self._next_seq(),
                "symbol": symbol,
                "exch_ts": exch_ts,
                "local_ts": local_ts,
                "open": self._scale_price(symbol, self._coalesce(payload, "open", "Open")),
                "high": self._scale_price(symbol, self._coalesce(payload, "high", "High")),
                "low": self._scale_price(symbol, self._coalesce(payload, "low", "Low")),
                "close": self._scale_price(symbol, self._coalesce(payload, "close", "Close")),
                "bids": bids,
                "asks": asks,
                "volume": self._to_int(self._coalesce(payload, "volume", "Volume")),
                "total_volume": self._to_int(self._coalesce(payload, "total_volume", "TotalVolume")),
            }
            return event
        except Exception as exc:
            logger.error("Snapshot normalization failed", error=str(exc), payload=str(payload)[:200])
            self.metrics.normalization_errors_total.labels(type="Snapshot").inc()
            return None
    def normalize_deal(self, payload: Any) -> Any:
        # Import internally to avoid circular dep if any
        from hft_platform.contracts.execution import FillEvent, Side
        
        try:
            # Helper to get attr or key
            def get(key, alt_keys=[]):
                if isinstance(payload, dict):
                     val = payload.get(key)
                     if val is None:
                         for k in alt_keys:
                             val = payload.get(k)
                             if val is not None: break
                     return val
                else:
                     val = getattr(payload, key, None)
                     if val is None:
                         for k in alt_keys:
                             val = getattr(payload, k, None)
                             if val is not None: break
                     return val

            symbol = get("code", ["Code", "symbol"])
            if not symbol:
                return None
                
            price = get("price", ["Price"])
            qty = get("qty", ["quantity", "Quantity", "volume"])
            action = get("action", ["Action"]) # "Buy"/"Sell" or Enum
            
            # Side conversion
            side = Side.BUY # Default
            if action:
                a_str = str(action).lower()
                if "sell" in a_str or action == -1:
                    side = Side.SELL
            
            # Scale price
            scaled_price = self._scale_price(symbol, price)
            
            return FillEvent(
                fill_id=str(self._next_seq()),
                account_id="sim",
                symbol=symbol,
                side=side,
                price=scaled_price,
                qty=int(qty or 0),
                fee=0,
                tax=0,
                ingest_ts_ns=self.capture_local_time_ns()
            )
        except Exception as exc:
            logger.error("Deal normalization failed", error=str(exc))
            return None
