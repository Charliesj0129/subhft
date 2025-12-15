import asyncio
import time
from typing import Dict, List, Optional
from structlog import get_logger

from hft_platform.contracts.strategy import OrderCommand, IntentType
from hft_platform.contracts.execution import OrderEvent, FillEvent, OrderStatus, Side

logger = get_logger("sim_broker")

class SimulatedBroker:
    def __init__(self, bus, lob_engine, latency_profile=None, slippage_profile=None):
        self.bus = bus
        self.lob_engine = lob_engine
        self.latency_profile = latency_profile
        self.slippage_profile = slippage_profile
        self.active_orders: Dict[str, OrderCommand] = {} # order_id -> command
        self.filled_orders: List[FillEvent] = []
        self.running = False

    async def run(self):
        self.running = True
        logger.info("SimulatedBroker started")
        # In event-driven backtest, broker is reactive but also needs to match continuously.
        # Ideally, it subscribes to MarketData to trigger matching checks.
        
        # NOTE: In this design, SimulatedBroker effectively replaces OrderAdapter AND 
        # needs to listen to MarketData events to match passive orders.
        # But `StrategyRunner` drives the bus.
        # If we use `hftbacktest` lib, it handles this loop.
        # For simplicity in this bespoke engine:
        # We hook into process_order (called by Runner or Adapter replacement) 
        # AND we need a way to check fills on every tick.
        
        # Simplified Logic:
        # 1. Orders are accepted instantly (plus latency).
        # 2. Aggressive orders match immediately against LOB snapshot.
        # 3. Passive orders rest until LOB crosses price.
        
        # Since this is an async component, it could consume from order_queue AND bus (for market data).
        # But aggregating two streams in one loop is tricky without a merge.
        # For Phase 7 prototype: match immediately on entry (Aggressive only) + minimal passive support.
        while self.running:
             await asyncio.sleep(1)

    async def submit_order(self, cmd: OrderCommand):
         """Called by BacktestRunner or hooked Adapter replacement."""
         # 1. Simulate Network Latency (advance internal clock or just log)
         # 2. Match
         symbol = cmd.intent.symbol
         book = self.lob_engine.get_book(symbol)
         
         # Assuming LOB has methods top_bids/asks
         # Very basic matching:
         # If Buy, check Best Ask.
         # If Price >= Best Ask, Fill.
         
         filled = False
         fill_price = 0
         fill_qty = 0
         
         if cmd.intent.side == Side.BUY:
             asks = book.top_asks(1)
             if asks:
                 best_ask = asks[0]
                 if cmd.intent.price >= best_ask.price:
                     # Fill!
                     filled = True
                     # Apply slippage?
                     fill_price = best_ask.price
                     fill_qty = min(cmd.intent.qty, best_ask.quantity) # Partial fill model?
                     # Full fill for simplicity
                     fill_qty = cmd.intent.qty 
         else:
             bids = book.top_bids(1)
             if bids:
                 best_bid = bids[0]
                 if cmd.intent.price <= best_bid.price:
                     filled = True
                     fill_price = best_bid.price
                     fill_qty = cmd.intent.qty

         if filled:
             # Emit Fill
             fill = FillEvent(
                 fill_id=f"fill-{cmd.cmd_id}",
                 account_id="backtest-acc",
                 order_id=str(cmd.intent.intent_id), # Map correctly
                 strategy_id=cmd.intent.strategy_id,
                 symbol=symbol,
                 side=cmd.intent.side,
                 qty=fill_qty,
                 price=int(fill_price * 10000), # norm expected
                 fee=0,
                 tax=0,
                 ingest_ts_ns=time.time_ns(),
                 match_ts_ns=time.time_ns()
             )
             await self.bus.publish(fill)
             logger.info("SimOrder Filled", id=cmd.cmd_id, price=fill_price)
         else:
             # Rest in active orders (Queue)
             # Logic for passive matching needs to be triggered by Market Data updates.
             logger.info("SimOrder Placed (Passive)", id=cmd.cmd_id)
             self.active_orders[cmd.cmd_id] = cmd

    def on_market_data(self, event):
        # Trigger matching for passive orders
        pass
