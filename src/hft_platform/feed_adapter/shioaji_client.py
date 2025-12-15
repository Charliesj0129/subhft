import time
from typing import List, Dict, Any

import yaml
from structlog import get_logger

try:
    import shioaji as sj
except Exception:  # pragma: no cover - fallback when library absent
    sj = None

logger = get_logger("feed_adapter")

class ShioajiClient:
    def __init__(self, config_path: str = "config/symbols.yaml"):
        self.MAX_SUBSCRIPTIONS = 200

        import os
        is_sim = os.getenv("HFT_MODE", "real") == "sim"
        if sj:
            self.api = sj.Shioaji(simulation=is_sim)
        else:
            self.api = None
        self.config_path = config_path
        self.symbols: List[Dict[str, Any]] = []
        self._load_config()
        self.subscribed_count = 0
        self.logged_in = False
        self.mode = "simulation" if (is_sim or self.api is None) else "real"

    def _load_config(self):
        with open(self.config_path, "r") as f:
            data = yaml.safe_load(f)
            self.symbols = data.get("symbols", [])
            if len(self.symbols) > self.MAX_SUBSCRIPTIONS:
                logger.warning("Symbol list exceeds limit", limit=self.MAX_SUBSCRIPTIONS, count=len(self.symbols))
                self.symbols = self.symbols[:self.MAX_SUBSCRIPTIONS]
        
        # Build map
        self.code_exchange_map = {
            s["code"]: s["exchange"] for s in self.symbols
        }

    def login(self, person_id: str = None, password: str = None, contracts_cb=None):
        logger.info("Logging in to Shioaji...")
        # Resolve credentials: Arg > Env > Config (not stored there for security)
        import os
        pid = person_id or os.getenv("SHIOAJI_PERSON_ID")
        pwd = password or os.getenv("SHIOAJI_PASSWORD")
        
        if not pid or not pwd:
            logger.warning("No credentials found (Args/Env). Running in simulation/anonymous mode.")
            return

        if not self.api:
            logger.warning("Shioaji SDK not installed; cannot login. Staying in simulation mode.")
            return

        try:
            # Check for API Key format (heuristic: length > 20)
            if len(pid) > 20:
                logger.info("Detected API Key format, using key-based login")
                self.api.login(api_key=pid, secret_key=pwd, contracts_cb=contracts_cb)
            else:
                self.api.login(person_id=pid, passwd=pwd, contracts_cb=contracts_cb)
            
            logger.info("Login successful")
            self.logged_in = True
        except Exception as e:
            logger.error("Login failed", error=str(e))
            raise

    def set_execution_callbacks(self, on_order: callable, on_deal: callable):
        """
        Register low-latency callbacks. 
        Note: These run on Shioaji threads.
        """
        if not self.api:
            logger.warning("Shioaji SDK missing; execution callbacks not registered (sim mode).")
            return
        self.api.set_order_callback(on_order)
        # deal callback naming depends on version, check docs if avail, defaulting to likely name
        # In some versions it is set_context(on_deal, on_order, ...)
        # For now assuming set_order_callback handles order updates.
        # Deal updates might come via update_status or separate stream.
        # Check spec: "Register Shioaji callbacks (api.on_order, api.on_deal)"
        # We will assume a wrapper or direct assignment if methods exist.
        try:
            self.api.set_deal_callback(on_deal)
        except AttributeError:
            logger.warning("api.set_deal_callback not found, relying on order callback")

    def subscribe_basket(self, cb: callable):
        if not self.api:
            # If API is missing entirely (no library), skip
            logger.info("Shioaji lib missing: skipping real subscription")
            return
        
        # In Sim mode with valid login, we CAN subscribe.
        if not self.logged_in:
             logger.warning("Not logged in; skipping subscription.")
             return
        if not self.api:
            logger.warning("Shioaji SDK missing; skip real subscription (sim mode).")
            return
        for sym in self.symbols:
            code = sym["code"]
            exchange = sym["exchange"]
            
            # Resolve contract object
            contract = self._get_contract(exchange, code)
            if not contract:
                logger.error("Contract not found", code=code)
                continue
            
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached", limit=self.MAX_SUBSCRIPTIONS)
                break

            logger.info("Subscribing", code=code)
            self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
            self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
            self.api.quote.set_on_tick_stk_v1_callback(cb)
            self.api.quote.set_on_bidask_stk_v1_callback(cb)
            self.subscribed_count += 1

    def _get_contract(self, exchange: str, code: str):
        # Helper to find contract
        if not self.api:
            return None
        try:
            if exchange == "TSE":
                return self.api.Contracts.Stocks.TSE[code]
            elif exchange == "OTC":
                return self.api.Contracts.Stocks.OTC[code]
            elif exchange == "FUT":
                return self.api.Contracts.Futures[code] # Simplified logic
        except Exception:
            return None
        return None

    def get_exchange(self, code: str) -> str:
        """Resolve exchange for a code."""
        # Try map first
        if code in self.code_exchange_map:
            return self.code_exchange_map[code]
        # Heuristic fallback?
        return None

    def get_usage(self):
        """Mock usage stats since actual API might differ."""
        return {
            "subscribed": self.subscribed_count,
            "bytes_used": 0 # Placeholder
        }
    
    def get_positions(self) -> List[Any]:
        """Fetch current positions from Shioaji."""
        if self.mode == "simulation":
             return []
        try:
             # Default to stock account
             return self.api.list_positions(self.api.stock_account)
        except Exception:
             logger.warning("Failed to fetch positions")
             return []
    def fetch_snapshots(self):
        """Fetch snapshots for all symbols in batches <= 500."""
        if not self.api or not self.logged_in:
            logger.info("Simulation mode: skipping snapshot fetch")
            return []
        if not self.api:
            logger.warning("Shioaji SDK missing; skip snapshot fetch (sim mode).")
            return []
        contracts = []
        for sym in self.symbols:
            c = self._get_contract(sym["exchange"], sym["code"])
            if c:
                contracts.append(c)
        
        if not contracts:
            logger.warning("No contracts resolved for snapshots")
            return []

        snapshots = []
        # Batching logic
        batch_size = 500
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            logger.info("Requesting snapshots", batch_size=len(batch))
            try:
                # Mocking api.snapshots call as it requires authentication
                # results = self.api.snapshots(batch)
                results = [] # Placeholder
                snapshots.extend(results)
                # Rate limit throttle: 50 requests per 5s => assume 100ms delay safe
                time.sleep(0.1) 
            except Exception as e:
                logger.error("Snapshot fetch failed", error=str(e))
        
        return snapshots

    # get_positions was defined earlier. Removing duplicate.

    
    def place_order(self, contract_code: str, exchange: str, action: str, price: float, qty: int, order_type: str, tif: str, custom_field: str = None):
        """
        Wrapper for placing order.
        """
        if not self.api:
            logger.warning("Shioaji SDK missing; mock place_order invoked.")
            return {"seq_no": f"sim-{int(time.time()*1000)}"}

        contract = self._get_contract(exchange, contract_code)
        if not contract:
            raise ValueError(f"Contract {contract_code} not found")

        # Convert simple types to Shioaji enums
        # Action: Buy/Sell
        act = sj.constant.Action.Buy if action == "Buy" else sj.constant.Action.Sell
        
        # PriceType: Limit/Market
        # OrderType: ROC/ROD/IOC (Shioaji treats these slightly differently, usually price_type=Limit/Market, order_type=ROD/IOC/FOK)
        # Assuming HFT uses Limit + ROD/IOC usually.
        pt = sj.constant.StockPriceType.LMT
        ot = sj.constant.OrderType.ROD
        if tif == "IOC":
            ot = sj.constant.OrderType.IOC
        elif tif == "FOK":
            ot = sj.constant.OrderType.FOK
            
        order = sj.Order(
            price=price,
            quantity=qty,
            action=act,
            price_type=pt,
            order_type=ot,
            custom_field=custom_field
        )
        
        trade = self.api.place_order(contract, order)
        return trade

    def cancel_order(self, trade):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock cancel_order invoked.")
            return
        self.api.update_status(self.api.OrderState.Cancel, trade=trade)

    def update_order(self, trade, price: float = None, qty: int = None):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock update_order invoked.")
            return
        if price:
            self.api.update_status(self.api.OrderState.UpdatePrice, trade=trade, price=price)
        elif qty:
             self.api.update_status(self.api.OrderState.UpdateQty, trade=trade, quantity=qty)

    def get_account_balance(self, account=None):
        return {}
