"""Per-fill slippage tracking: captures decision-time mid-price vs fill price."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from structlog import get_logger

from hft_platform.contracts.strategy import Side

logger = get_logger("execution.slippage_tracker")


@dataclass(slots=True)
class SlippageRecord:
    order_id: str
    symbol: str
    side: Side
    decision_mid: int
    fill_price: int
    slippage_ticks: int
    slippage_ntd: int
    latency_ns: int
    ts: int


class SlippageTracker:
    __slots__ = ("_records",)

    def __init__(self) -> None:
        self._records: list[SlippageRecord] = []

    @staticmethod
    def compute_slippage(
        *, order_id: str, symbol: str, side: Side,
        decision_mid: int, fill_price: int,
        order_ts_ns: int, fill_ts_ns: int,
        tick_size_scaled: int, point_value: int,
    ) -> Optional[SlippageRecord]:
        if decision_mid == 0:
            return None
        side_sign = 1 if side == Side.BUY else -1
        raw_diff = (fill_price - decision_mid) * side_sign
        slippage_ticks = raw_diff // tick_size_scaled
        slippage_ntd = slippage_ticks * point_value
        return SlippageRecord(
            order_id=order_id, symbol=symbol, side=side,
            decision_mid=decision_mid, fill_price=fill_price,
            slippage_ticks=slippage_ticks, slippage_ntd=slippage_ntd,
            latency_ns=fill_ts_ns - order_ts_ns, ts=fill_ts_ns,
        )

    def to_row_dict(self, record: SlippageRecord) -> dict:
        return {
            "order_id": record.order_id, "symbol": record.symbol,
            "side": int(record.side), "decision_mid": record.decision_mid,
            "fill_price": record.fill_price, "slippage_ticks": record.slippage_ticks,
            "slippage_ntd": record.slippage_ntd, "latency_ns": record.latency_ns,
            "ts": record.ts,
        }
