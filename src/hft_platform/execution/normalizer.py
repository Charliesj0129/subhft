from dataclasses import dataclass
from typing import Dict, Any, Optional
import time
from structlog import get_logger

from hft_platform.contracts.execution import OrderEvent, FillEvent, OrderStatus, Side

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("exec_normalizer")

@dataclass(slots=True)
class RawExecEvent:
    """
    Lightweight capture of Shioaji callback args.
    """
    topic: str # "order" or "deal"
    data: Dict[str, Any]
    ingest_ts_ns: int

class ExecutionNormalizer:
    def __init__(self, raw_queue=None, order_id_map: Optional[Dict[str, str]] = None):
        self.raw_queue = raw_queue 
        self.metrics = MetricsRegistry.get()
        self.order_id_map = order_id_map if order_id_map is not None else {}
        
    def normalize_order(self, raw: RawExecEvent) -> Optional[OrderEvent]:
        self.metrics.execution_events_total.labels(type="order").inc()
        d = raw.data
        # Mapping logic (Mock implementation based on typical fields)
        # Shioaji fields: ord_no, seq_no, id (order_id), action, price, qty, status
        
        try:
            status = self._map_status(d.get("status", {}).get("status"))
            
            oid = str(d.get("ord_no", "") or d.get("seq_no", ""))
            return OrderEvent(
                order_id=oid,
                strategy_id=self.order_id_map.get(oid, "UNKNOWN"),
                symbol=d.get("contract", {}).get("code", "UNKNOWN"),
                status=status,
                submitted_qty=d.get("order", {}).get("quantity", 0),
                filled_qty=0, # Need to track cumulatives? Shioaji provides snapshot usually
                remaining_qty=0, 
                price=int(d.get("order", {}).get("price", 0) * 10000), # Fixed point
                side=Side.BUY if d.get("order", {}).get("action") == "Buy" else Side.SELL, 
                ingest_ts_ns=raw.ingest_ts_ns,
                broker_ts_ns=int(time.time_ns()) # Placeholder if not in payload
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
                 if "sell" in s or action == -1: # Shioaji might use Int or String
                     side = Side.SELL

            # Explicit symbol extraction to avoid one-liner precedence bugs
            sym = str(get("code") or "")
            if not sym:
                 c = get("contract")
                 if c:
                    if isinstance(c, dict): sym = c.get("code", "")
                    else: sym = getattr(c, "code", "")

            return FillEvent(
                fill_id=str(get("seq_no") or ""),
                account_id=str(get("account_id") or "sim-account-01"),
                order_id=str(get("ord_no") or ""),
                strategy_id=str(get("custom_field")) if get("custom_field") else self.order_id_map.get(str(get("ord_no") or ""), self.order_id_map.get(str(get("seq_no") or ""), "UNKNOWN")),
                symbol=sym,
                side=side,
                qty=qty,
                price=int(price_raw * 10000), # Fixed point assumption
                fee=0,
                tax=0,
                ingest_ts_ns=raw.ingest_ts_ns,
                match_ts_ns=int(get("ts") or time.time_ns())
            )
        except Exception:
            logger.error("Fill normalization failed")
            return None

    def _map_status(self, s: str) -> OrderStatus:
        s = str(s).upper()
        if "PENDING" in s: return OrderStatus.PENDING_SUBMIT
        if "SUBMITTED" in s or "PRESUBMITTED" in s: return OrderStatus.SUBMITTED
        if "FILLED" in s: return OrderStatus.FILLED
        if "CANCELLED" in s or "CANCELED" in s: return OrderStatus.CANCELLED
        if "FAILED" in s: return OrderStatus.FAILED
        return OrderStatus.SUBMITTED
