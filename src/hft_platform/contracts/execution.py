from dataclasses import dataclass, field
from enum import IntEnum, auto
from typing import Optional, List

# Reuse Side/TIF from strategy if possible, but execution might have broader enums
from hft_platform.contracts.strategy import Side

class OrderStatus(IntEnum):
    PENDING_SUBMIT = 0
    SUBMITTED = 1
    PARTIALLY_FILLED = 2
    FILLED = 3
    CANCELLED = 4
    FAILED = 5

@dataclass(slots=True)
class OrderEvent:
    """
    Normalized Order Status Update.
    """
    order_id: str         # Broker ID (ordno/seqno)
    strategy_id: str      # Derived from user_def or map
    symbol: str
    status: OrderStatus
    submitted_qty: int
    filled_qty: int
    remaining_qty: int
    price: int            # Fixed-point
    side: Side
    ingest_ts_ns: int     # Local receive time
    broker_ts_ns: int     # Broker event time

@dataclass(slots=True)
class FillEvent:
    """
    Trade Execution.
    """
    fill_id: str          # Broker deal ID
    account_id: str       # Broker Account ID
    order_id: str
    strategy_id: str
    symbol: str
    side: Side
    qty: int
    price: int            # Fixed-point (x10000)
    fee: int              # Fixed-point (x10000)
    tax: int              # Fixed-point (x10000)
    ingest_ts_ns: int
    match_ts_ns: int

@dataclass(slots=True)
class PositionDelta:
    """
    Incremental update for Risk/UI.
    """
    account_id: str
    strategy_id: str
    symbol: str
    net_qty: int
    avg_price: int        # Fixed-point
    realized_pnl: int     # Fixed-point
    unrealized_pnl: int   # Fixed-point
    delta_source: str     # "FILL", "RECONCILE", "MARK"
