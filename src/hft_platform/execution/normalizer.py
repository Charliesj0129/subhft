import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.core.order_ids import OrderIdResolver
from hft_platform.core.pricing import PriceCodec, SymbolMetadataPriceScaleProvider
from hft_platform.feed_adapter.normalizer import SymbolMetadata
from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("exec_normalizer")


@dataclass(slots=True)
class RawExecEvent:
    """
    Lightweight capture of Shioaji callback args.
    """

    topic: str  # "order" or "deal"
    data: Dict[str, Any]
    ingest_ts_ns: int


class ExecutionNormalizer:
    def __init__(
        self,
        raw_queue=None,
        order_id_map: Optional[Dict[str, str]] = None,
        strategy_id_resolvers: Optional[list[Callable[[RawExecEvent], Optional[str]]]] = None,
    ):
        self.raw_queue = raw_queue
        self.metrics = MetricsRegistry.get()
        self.metadata = SymbolMetadata()
        self.price_codec = PriceCodec(SymbolMetadataPriceScaleProvider(self.metadata))
        # Maps broker order IDs -> order_key ("strategy_id:intent_id") or strategy_id.
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self.order_id_resolver = OrderIdResolver(self.order_id_map)
        self.strategy_id_resolvers = strategy_id_resolvers or [
            self._resolve_from_custom_field,
            self._resolve_from_order_id_map,
        ]

    def _resolve_from_custom_field(self, raw: RawExecEvent) -> Optional[str]:
        d = raw.data
        if not isinstance(d, dict):
            return None
        custom_field = d.get("order", {}).get("custom_field") or d.get("custom_field")
        if custom_field:
            return str(custom_field)
        return None

    def _resolve_from_order_id_map(self, raw: RawExecEvent) -> Optional[str]:
        d = raw.data
        if not isinstance(d, dict):
            return None
        ord_no = str(d.get("ord_no", "") or "")
        seq_no = str(d.get("seq_no", "") or "")
        other_id = str(d.get("order_id") or d.get("id") or "")
        resolved = self.order_id_resolver.resolve_strategy_id_from_candidates([ord_no, seq_no, other_id])
        return resolved if resolved != "UNKNOWN" else None

    def _resolve_strategy_id(self, raw: RawExecEvent) -> str:
        for resolver in self.strategy_id_resolvers:
            try:
                candidate = resolver(raw)
            except Exception:
                candidate = None
            if candidate:
                return candidate
        return "UNKNOWN"

    def normalize_order(self, raw: RawExecEvent) -> Optional[OrderEvent]:
        self.metrics.execution_events_total.labels(type="order").inc()
        d = raw.data
        # Mapping logic (Mock implementation based on typical fields)
        # Shioaji fields: ord_no, seq_no, id (order_id), action, price, qty, status

        try:
            status = self._map_status(d.get("status", {}).get("status"))
            ord_no = str(d.get("ord_no", "") or "")
            seq_no = str(d.get("seq_no", "") or "")
            oid = ord_no or seq_no
            strategy_id = self._resolve_strategy_id(raw)

            symbol = d.get("contract", {}).get("code", "UNKNOWN")
            price_val = d.get("order", {}).get("price") or 0
            price = self.price_codec.scale(symbol, price_val)

            return OrderEvent(
                order_id=oid,
                strategy_id=strategy_id,
                symbol=symbol,
                status=status,
                submitted_qty=d.get("order", {}).get("quantity", 0),
                filled_qty=0,  # Need to track cumulatives? Shioaji provides snapshot usually
                remaining_qty=0,
                price=price,
                side=Side.BUY if d.get("order", {}).get("action") == "Buy" else Side.SELL,
                ingest_ts_ns=raw.ingest_ts_ns,
                broker_ts_ns=int(time.time_ns()),  # Placeholder if not in payload
            )
        except Exception as e:
            logger.error("Order normalization failed", error=str(e), data=d)
            return None

    def normalize_fill(self, raw: RawExecEvent) -> Optional[FillEvent]:
        self.metrics.execution_events_total.labels(type="fill").inc()
        d = raw.data

        def get(key, default=None):
            if isinstance(d, dict):
                return d.get(key, default)
            return getattr(d, key, default)

        try:
            qty = int(get("quantity") or get("qty") or get("volume") or 0)
            price_raw = float(get("price") or 0)

            # Map action/side
            action = get("action")
            side = Side.BUY
            if action:
                s = str(action).lower()
                if "sell" in s or action == -1:  # Shioaji might use Int or String
                    side = Side.SELL

            # Explicit symbol extraction to avoid one-liner precedence bugs
            sym = str(get("code") or "")
            if not sym:
                c = get("contract")
                if c:
                    if isinstance(c, dict):
                        sym = c.get("code", "")
                    else:
                        sym = getattr(c, "code", "")

            strategy_id = self._resolve_strategy_id(raw)
            scale_price = self.price_codec.scale(sym, price_raw)

            return FillEvent(
                fill_id=str(get("seq_no") or ""),
                account_id=str(get("account_id") or "sim-account-01"),
                order_id=str(get("ord_no") or ""),
                strategy_id=strategy_id,
                symbol=sym,
                side=side,
                qty=qty,
                price=scale_price,
                fee=0,
                tax=0,
                ingest_ts_ns=raw.ingest_ts_ns,
                match_ts_ns=int(get("ts") or time.time_ns()),
            )
        except Exception:
            logger.error("Fill normalization failed")
            return None

    def _map_status(self, s: str) -> OrderStatus:
        s = str(s).upper()
        if "PENDING" in s:
            return OrderStatus.PENDING_SUBMIT
        if "SUBMITTED" in s or "PRESUBMITTED" in s:
            return OrderStatus.SUBMITTED
        if "FILLED" in s:
            return OrderStatus.FILLED
        if "CANCELLED" in s or "CANCELED" in s:
            return OrderStatus.CANCELLED
        if "FAILED" in s:
            return OrderStatus.FAILED
        return OrderStatus.SUBMITTED
