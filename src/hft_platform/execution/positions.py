from typing import Dict, List, Optional
from dataclasses import dataclass, field
import time
from structlog import get_logger

from hft_platform.contracts.execution import FillEvent, PositionDelta, Side

logger = get_logger("positions")

@dataclass
class Position:
    account_id: str
    strategy_id: str
    symbol: str
    
    net_qty: int = 0
    avg_price: float = 0.0 # Using float for internal calc, but might want fixed-point
    
    realized_pnl: float = 0.0
    fees: float = 0.0
    
    last_update_ts: int = 0
    
    def update(self, fill: FillEvent, scale: float = 1.0):
        # Weighted Average Price logic
        # If open position increases: update avg_price
        # If position closes/reduces: realize PnL
        
        fill_qty = fill.qty
        fill_price = float(fill.price) / scale 
        
        is_buy = (fill.side == Side.BUY)
        signed_fill_qty = fill_qty if is_buy else -fill_qty
        
        # Check if closing
        # Closing if signs are different
        current_sign = 1 if self.net_qty > 0 else -1 if self.net_qty < 0 else 0
        fill_sign = 1 if is_buy else -1
        
        closing = False
        if current_sign != 0 and fill_sign != current_sign:
            closing = True
            
        if closing:
            # We are closing some amount
            # qty to close is min(abs(net), abs(signed_fill))
            close_qty = min(abs(self.net_qty), abs(signed_fill_qty))
            
            # PnL = (Exit Price - Entry Price) * Qty * Sign
            # If LONG, Sell at X. PnL = (X - Avg) * Qty
            # If SHORT, Buy at X. PnL = (Avg - X) * Qty = (Exit - Entry) * Qty * (-1)? No.
            # Realized PnL generic: (SellPrice - BuyPrice) * Qty
            
            pnl = 0.0
            if is_buy: # Covering a SHORT
                # Entry was avg_price, Exit is fill_price. We are BUYING to close.
                # PnL = (Entry - Exit) * Qty
                pnl = (self.avg_price - fill_price) * close_qty
            else: # Selling a LONG
                # Entry was avg_price, Exit is fill_price.
                # PnL = (Exit - Entry) * Qty
                pnl = (fill_price - self.avg_price) * close_qty
                
            self.realized_pnl += pnl
            
            # Update Net Qty
            self.net_qty += signed_fill_qty
            
            # If we flipped position side (e.g. Long 10, Sell 20 -> Short 10)
            # The remaining 10 starts new avg price
            if (current_sign > 0 and self.net_qty < 0) or (current_sign < 0 and self.net_qty > 0):
                self.avg_price = fill_price
                
        else:
            # Increasing position or flat -> open
            # New Avg = (OldNet*OldAvg + FillQty*FillPrice) / NewNet
            
            if self.net_qty == 0:
                self.avg_price = fill_price
                self.net_qty += signed_fill_qty
            else:
                 total_val = (self.net_qty * self.avg_price) + (signed_fill_qty * fill_price)
                 self.net_qty += signed_fill_qty
                 self.avg_price = total_val / self.net_qty

        self.last_update_ts = fill.match_ts_ns

from hft_platform.observability.metrics import MetricsRegistry

from hft_platform.feed_adapter.normalizer import SymbolMetadata

class PositionStore:
    def __init__(self):
        # map: f"{account}:{strategy}:{symbol}" -> Position
        self.positions: Dict[str, Position] = {}
        self.metrics = MetricsRegistry.get()
        self.metadata = SymbolMetadata()
        
    def on_fill(self, fill: FillEvent) -> PositionDelta:
        key = self._key(fill.account_id, fill.strategy_id, fill.symbol)
        if key not in self.positions:
            self.positions[key] = Position(fill.account_id, fill.strategy_id, fill.symbol)
            
        pos = self.positions[key]
        scale = self.metadata.price_scale(fill.symbol)
        pos.update(fill, scale=scale)
        logger.info("Fill processed", key=key, net_qty=pos.net_qty, pnl=pos.realized_pnl)
        
        # Emit delta / Update PnL Gauge
        if self.metrics:
            self.metrics.position_pnl_realized.labels(strategy=pos.strategy_id, symbol=pos.symbol).set(pos.realized_pnl)
        
        # Emit delta (Re-scale avg_price to Fixed Point for consistency)
        return PositionDelta(
            account_id=pos.account_id,
            strategy_id=pos.strategy_id,
            symbol=pos.symbol,
            net_qty=pos.net_qty,
            avg_price=int(pos.avg_price * scale),
            realized_pnl=int(pos.realized_pnl), # PnL usually in currency units (dollars), not scaled points
            unrealized_pnl=0, 
            delta_source="FILL"
        )

    def _key(self, acc, strat, sym):
        return f"{acc}:{strat}:{sym}"
