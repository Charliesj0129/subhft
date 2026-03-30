from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.core import timebase
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
        raw_queue: Any = None,
        order_id_map: Optional[Dict[str, str]] = None,
        strategy_id_resolvers: Optional[list[Callable[[RawExecEvent], Optional[str]]]] = None,
        fee_calculator: Any = None,
    ) -> None:
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
        self._fee_calculator = fee_calculator

    def _unwrap_data(self, raw: RawExecEvent) -> tuple[Any, Any | None]:
        d = raw.data
        if isinstance(d, dict) and "payload" in d:
            return d.get("payload"), d.get("state") or d.get("status")
        return d, None

    def _resolve_from_custom_field(self, raw: RawExecEvent) -> Optional[str]:
        d, _ = self._unwrap_data(raw)
        if not isinstance(d, dict):
            return None
        custom_field = d.get("order", {}).get("custom_field") or d.get("custom_field")
        if custom_field:
            return str(custom_field)
        return None

    def _resolve_from_order_id_map(self, raw: RawExecEvent) -> Optional[str]:
        d, _ = self._unwrap_data(raw)
        if not isinstance(d, dict):
            return None
        order = d.get("order", {}) if isinstance(d.get("order"), dict) else {}
        ord_no = str(order.get("ordno") or order.get("ord_no") or d.get("ordno") or d.get("ord_no") or "")
        seq_no = str(order.get("seqno") or order.get("seq_no") or d.get("seqno") or d.get("seq_no") or "")
        other_id = str(order.get("id") or d.get("order_id") or d.get("id") or "")
        resolved = self.order_id_resolver.resolve_strategy_id_from_candidates([ord_no, seq_no, other_id])
        return resolved if resolved != "UNKNOWN" else None

    def _resolve_strategy_id(self, raw: RawExecEvent) -> str:
        for resolver in self.strategy_id_resolvers:
            try:
                candidate = resolver(raw)
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                logger.debug("operation_fallback", error=str(exc))
                candidate = None
            if candidate:
                return candidate
        return "UNKNOWN"

    def _normalize_ts_ns(self, value: Any) -> int:
        if value is None:
            return timebase.now_ns()
        try:
            # Use float arithmetic to avoid Decimal allocation on exec path.
            # Detect magnitude then multiply, rounding at the end.
            fv = float(value)
        except (TypeError, ValueError, OverflowError):
            return timebase.now_ns()
        if fv <= 0.0:
            return timebase.now_ns()
        if fv > 1e17:
            return int(fv)  # already nanoseconds
        if fv > 1e14:
            return int(fv * 1_000)  # microseconds -> ns
        if fv > 1e11:
            return int(fv * 1_000_000)  # milliseconds -> ns
        return int(fv * 1_000_000_000)  # seconds -> ns

    def normalize_order(self, raw: RawExecEvent) -> Optional[OrderEvent]:
        self.metrics.execution_events_total.labels(type="order").inc()
        d, state = self._unwrap_data(raw)
        # Mapping logic (Mock implementation based on typical fields)
        # Shioaji fields: ord_no, seq_no, id (order_id), action, price, qty, status

        try:
            if not isinstance(d, dict):
                return None

            status_payload = d.get("status")
            status_text = None
            exchange_ts = None
            if isinstance(status_payload, dict):
                status_text = status_payload.get("status")
                exchange_ts = status_payload.get("exchange_ts") or status_payload.get("ts")
            elif status_payload is not None:
                status_text = str(status_payload)

            op = d.get("operation", {}) if isinstance(d.get("operation"), dict) else {}
            op_type = op.get("op_type") or d.get("op_type")
            op_code = op.get("op_code") or d.get("op_code")

            status = self._map_status(status_text, op_type=op_type, op_code=op_code, state=state)

            if "order" in d and not isinstance(d.get("order"), dict):
                return None
            order = d.get("order", {}) if isinstance(d.get("order"), dict) else {}
            ord_no = str(order.get("ordno") or order.get("ord_no") or d.get("ordno") or d.get("ord_no") or "")
            seq_no = str(order.get("seqno") or order.get("seq_no") or d.get("seqno") or d.get("seq_no") or "")
            oid = ord_no or seq_no or str(order.get("id") or d.get("id") or "")
            strategy_id = self._resolve_strategy_id(raw)

            contract = d.get("contract", {}) if isinstance(d.get("contract"), dict) else {}
            symbol = (
                contract.get("full_code") or contract.get("code")
                or d.get("full_code") or d.get("code") or "UNKNOWN"
            )
            price_val = order.get("price") or 0
            price = self.price_codec.scale(symbol, price_val)

            return OrderEvent(
                order_id=oid,
                strategy_id=strategy_id,
                symbol=symbol,
                status=status,
                submitted_qty=order.get("quantity", 0),
                filled_qty=0,  # Need to track cumulatives? Shioaji provides snapshot usually
                remaining_qty=0,
                price=price,
                side=Side.BUY if order.get("action") == "Buy" else Side.SELL,
                ingest_ts_ns=raw.ingest_ts_ns,
                broker_ts_ns=self._normalize_ts_ns(exchange_ts),
            )
        except (KeyError, TypeError, ValueError) as e:
            logger.error("Order normalization failed", error=str(e), data=d)
            return None

    def normalize_fill(self, raw: RawExecEvent) -> Optional[FillEvent]:
        self.metrics.execution_events_total.labels(type="fill").inc()
        d, _ = self._unwrap_data(raw)

        def get(key: str, default: Any = None) -> Any:
            if isinstance(d, dict):
                return d.get(key, default)
            return getattr(d, key, default)

        try:
            qty = int(get("quantity") or get("qty") or get("volume") or 0)
            price_value = get("price") or 0

            # Map action/side
            action = get("action")
            side = Side.BUY
            if action:
                s = str(action).lower()
                if "sell" in s or action == -1:  # Shioaji might use Int or String
                    side = Side.SELL

            # Prefer full_code (e.g. TMFD6) over base code (TMF) for futures/options
            sym = str(get("full_code") or get("code") or "")
            if not sym:
                c = get("contract")
                if c:
                    if isinstance(c, dict):
                        sym = c.get("full_code", "") or c.get("code", "")
                    else:
                        sym = getattr(c, "full_code", "") or getattr(c, "code", "")

            strategy_id = self._resolve_strategy_id(raw)
            # scale() handles float/int/Decimal inputs with precision
            scale_price = self.price_codec.scale(sym, price_value)

            # Compute fees if calculator is available
            fee = 0
            tax = 0
            if self._fee_calculator is not None:
                side_str = "BUY" if side == Side.BUY else "SELL"
                breakdown = self._fee_calculator.compute(sym, side_str, qty, scale_price)
                fee = breakdown.total
                tax = breakdown.tax

            return FillEvent(
                fill_id=str(get("seqno") or get("seq_no") or ""),
                account_id=str(get("account_id") or "sim-account-01"),
                order_id=str(get("ordno") or get("ord_no") or ""),
                strategy_id=strategy_id,
                symbol=sym,
                side=side,
                qty=qty,
                price=scale_price,
                fee=fee,
                tax=tax,
                ingest_ts_ns=raw.ingest_ts_ns,
                match_ts_ns=self._normalize_ts_ns(get("ts")),
            )
        except Exception as _exc:  # noqa: BLE001
            logger.error("Fill normalization failed")
            return None

    def _map_status(
        self, s: str | None, op_type: str | None = None, op_code: str | None = None, state: Any = None
    ) -> OrderStatus:
        text = str(s).upper() if s is not None else ""
        if "PENDING" in text:
            return OrderStatus.PENDING_SUBMIT
        if "SUBMITTED" in text or "PRESUBMITTED" in text:
            return OrderStatus.SUBMITTED
        if "FILLED" in text:
            return OrderStatus.FILLED
        if "CANCELLED" in text or "CANCELED" in text:
            return OrderStatus.CANCELLED
        if "FAILED" in text:
            return OrderStatus.FAILED
        if op_type:
            op_upper = str(op_type).upper()
            if op_code and str(op_code) != "00":
                return OrderStatus.FAILED
            if "CANCEL" in op_upper:
                return OrderStatus.CANCELLED
            if "UPDATE" in op_upper or "NEW" in op_upper:
                return OrderStatus.SUBMITTED
        return OrderStatus.SUBMITTED
