import asyncio
import yaml
import time
from typing import Dict, List, Optional, Any
from collections import deque
from structlog import get_logger

from hft_platform.contracts.strategy import OrderCommand, IntentType, Side, TIF
from hft_platform.feed_adapter.normalizer import SymbolMetadata

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("order_adapter")

class OrderAdapter:
    def __init__(self, config_path: str, order_queue: asyncio.Queue, shioaji_client, order_id_map: Dict[str, str] | None = None):
        self.config_path = config_path
        self.order_queue = order_queue
        self.client = shioaji_client 
        self.order_id_map = order_id_map if order_id_map is not None else {}
        self.running = False
        self.metrics = MetricsRegistry.get()
        
        # State
        self.live_orders: Dict[str, Any] = {} # Map "strategy_id:intent_id" -> Trade Object or Status dict
        
        # Rate Limiting State
        self.rate_window = deque()
        self.rate_limit_soft = 180
        self.rate_limit_hard = 250
        self.sliding_window_s = 10
        
        # Circuit Breaker State
        self.failure_count = 0
        self.circuit_threshold = 5
        self.circuit_timeout = 60
        self.circuit_open_until = 0
        
        self.load_config()

    def load_config(self):
        with open(self.config_path, "r") as f:
            cfg = yaml.safe_load(f)
            self.rate_limit_soft = cfg["rate_limits"]["shioaji_soft_cap"]
            self.rate_limit_hard = cfg["rate_limits"]["shioaji_hard_cap"]
            # etc.

    async def run(self):
        self.running = True
        logger.info("OrderAdapter started")
        
        while self.running:
            # Allow exceptions to crash the task (Supervisor will handle)
            cmd: OrderCommand = await self.order_queue.get()
            
            # Check Deadline
            if time.time_ns() > cmd.deadline_ns:
                logger.warning("Order Timeout (Pre-dispatch)", cmd_id=cmd.cmd_id)
                self.order_queue.task_done()
                continue
                
            await self.execute(cmd)
            self.order_queue.task_done()

    def check_rate_limit(self) -> bool:
        """Sliding window check."""
        now = time.time()
        # Remove old entries
        while self.rate_window and now - self.rate_window[0] > self.sliding_window_s:
            self.rate_window.popleft()
            
        if len(self.rate_window) >= self.rate_limit_hard:
            logger.error("Hard Rate Limit Hit", count=len(self.rate_window))
            return False
        
        if len(self.rate_window) >= self.rate_limit_soft:
            logger.warning("Soft Rate Limit Hit", count=len(self.rate_window))
            # Signal throttling to Risk/Strategy here
            
        return True

    def on_terminal_state(self, strategy_id: str, intent_id: str):
        """Called when an order reaches a terminal state (Filled, Cancelled, Rejected)."""
        order_key = f"{strategy_id}:{intent_id}"
        if order_key in self.live_orders:
            logger.info("Removing terminal order", key=order_key)
            del self.live_orders[order_key]
        
        # Also clean up rate limit window if needed? No, rate limit is distinct.


    async def execute(self, cmd: OrderCommand):
         intent = cmd.intent
         
         # Circuit Breaker Check
         if self.circuit_open_until > time.time():
             logger.warning("Circuit Breaker Open - Rejecting", cmd_id=cmd.cmd_id)
             return

         if not self.check_rate_limit():
             # Trigger circuit break? Or just drop?
             # Spec says: Cut off before 250.
             return

         try:
             # Strategy+Intent ID as key
             order_key = f"{intent.strategy_id}:{intent.intent_id}"
             
             if intent.intent_type == IntentType.NEW:
                 logger.info("Placing Order", 
                             symbol=intent.symbol, 
                             price=intent.price, 
                             qty=intent.qty, 
                             side=intent.side)
                 
                 # Dynamic Exchange Lookup
                 exchange = self.client.get_exchange(intent.symbol) or "TSE"
                 
                 # Convert Side IntEnum to String for ShioajiClient
                 action_str = "Buy" if intent.side == Side.BUY else "Sell"
                 
                 # De-scale price (Fixed Point -> Float limit price)
                 # We need usage of SymbolMetadata. Since not passed in init, we load it here or in init.
                 # For efficiency, we should have loaded it in __init__. 
                 # But to minimize diff chunks, we'll lazily instantiate or assume it's available.
                 # Best practice: Instantiate in __init__.
                 # For now:
                 if not hasattr(self, "metadata"):
                      self.metadata = SymbolMetadata()
                      
                 scale = self.metadata.price_scale(intent.symbol)
                 price_float = float(intent.price) / scale
                 
                 # Shioaji custom_field limit is 6 chars
                 c_field = intent.strategy_id
                 if len(c_field) > 6:
                      # If too long, do not pass it, rely on internal map
                      logger.warning("StrategyID too long for custom_field", id=c_field)
                      c_field = ""
                      
                 # TIF Mapping (IntEnum -> Str)
                 tif_map = {TIF.ROD: "ROD", TIF.IOC: "IOC", TIF.FOK: "FOK"}
                 # Default to ROD if unknown
                 tif_str = tif_map.get(intent.tif, "ROD")

                 trade = self.client.place_order(
                     contract_code=intent.symbol,
                     exchange=exchange,
                     action=action_str, 
                     price=price_float,
                     qty=intent.qty,
                     order_type="Limit", # Simplified
                     tif=tif_str,
                     custom_field=c_field
                 )
                 
                 self.metrics.order_actions_total.labels(type="new").inc()
                 self.live_orders[order_key] = trade
                 # Inject timestamp for TTL (hacky if trade is object, assumes it absorbs attrs or we wrap)
                 try:
                     if isinstance(trade, dict):
                         trade["timestamp"] = time.time()
                     else:
                         trade.timestamp = time.time()
                 except: pass # Object might be rigid
                 
                 # Populate lookup using Shioaji trade attributes
                 # If trade object has order.seqno or order.ord_no
                 # For prototype we assume Shioaji Trade object structure or mock return
                 # Assuming trade.order.seqno or similar. 
                 # Safety check:
                 # If mock return
                 if hasattr(trade, "order") and hasattr(trade.order, "seq_no"):
                      self.order_id_map[str(trade.order.seq_no)] = intent.strategy_id
                 elif isinstance(trade, dict) and "seq_no" in trade: # Mock dict
                      self.order_id_map[str(trade["seq_no"])] = intent.strategy_id
                      
                 self.rate_window.append(time.time())
                 self.failure_count = 0 
                 
             elif intent.intent_type == IntentType.CANCEL:
                 target_key = f"{intent.strategy_id}:{intent.target_order_id}"
                 target_trade = self.live_orders.get(target_key)
                 
                 if target_trade:
                     logger.info("Canceling Order", target=target_key)
                     self.client.cancel_order(target_trade)
                     self.metrics.order_actions_total.labels(type="cancel").inc()
                     self.rate_window.append(time.time())
                 else:
                     logger.warning("Cancel target not found", target=target_key)
                 
             elif intent.intent_type == IntentType.AMEND:
                 target_key = f"{intent.strategy_id}:{intent.target_order_id}"
                 target_trade = self.live_orders.get(target_key)
                 
                 if target_trade:
                     # Descale price
                     if not hasattr(self, "metadata"): self.metadata = SymbolMetadata()
                     scale = self.metadata.price_scale(intent.symbol)
                     price_f = float(intent.price) / scale
                     
                     logger.info("Amending Order", target=target_key, new_price=price_f)
                     self.client.update_order(target_trade, price=price_f)
                     self.metrics.order_actions_total.labels(type="amend").inc()
                     self.rate_window.append(time.time())
                 else:
                     logger.warning("Amend target not found", target=target_key)

         except Exception as e:
             logger.error("Broker Error", error=str(e))
             self.metrics.order_reject_total.inc()
             self.failure_count += 1
             if self.failure_count >= self.circuit_threshold:
                 self.circuit_open_until = time.time() + self.circuit_timeout
                 logger.critical("Circuit Breaker Tripped!")
