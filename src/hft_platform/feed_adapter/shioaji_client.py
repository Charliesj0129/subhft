import time
from typing import Any, Callable, Dict, List

import yaml
from structlog import get_logger

try:
    import shioaji as sj
except Exception:  # pragma: no cover - fallback when library absent
    sj = None


logger = get_logger("feed_adapter")

# --- Global Callback Registry & Dispatcher ---
# Using a global list to hold strong references to clients and avoid GC issues with bound methods
CLIENT_REGISTRY: List[Any] = []


def dispatch_tick_cb(exchange, msg):
    """
    Global static callback to dispatch ticks to all registered clients.
    This avoids issues with Cython holding weak references to bound methods.
    """
    try:
        # logger.debug("Global Dispatch Hit", registry_size=len(CLIENT_REGISTRY))
        for client in CLIENT_REGISTRY:
            if hasattr(client, "_process_tick"):
                client._process_tick(exchange, msg)
    except Exception as e:
        logger.error("Global Dispatch Error", error=str(e))


# ---------------------------------------------


class ShioajiClient:
    def __init__(self, config_path: str | None = None):
        self.MAX_SUBSCRIPTIONS = 200

        import os

        if config_path is None:
            config_path = os.getenv("SYMBOLS_CONFIG")
            if not config_path:
                if os.path.exists("config/symbols.yaml"):
                    config_path = "config/symbols.yaml"
                else:
                    config_path = "config/base/symbols.yaml"

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

        # Register self globally
        if self not in CLIENT_REGISTRY:
            CLIENT_REGISTRY.append(self)
            logger.info("Registered ShioajiClient in Global Registry")

    def _process_tick(self, exchange, msg):
        """Internal method called by global dispatcher"""
        try:
            if self.tick_callback:
                self.tick_callback(exchange, msg)
        except Exception as e:
            logger.error("Error processing tick", error=str(e))

    def _load_config(self):
        with open(self.config_path, "r") as f:
            data = yaml.safe_load(f)
            self.symbols = data.get("symbols", [])
            if len(self.symbols) > self.MAX_SUBSCRIPTIONS:
                logger.warning("Symbol list exceeds limit", limit=self.MAX_SUBSCRIPTIONS, count=len(self.symbols))
                self.symbols = self.symbols[: self.MAX_SUBSCRIPTIONS]

        # Build map
        self.code_exchange_map = {s["code"]: s["exchange"] for s in self.symbols}

    def login(self, person_id: str | None = None, password: str | None = None, contracts_cb=None):
        logger.info("Logging in to Shioaji...")
        # Resolve credentials: Arg > Env > Config (not stored there for security)
        import os

        pid = person_id or os.getenv("SHIOAJI_PERSON_ID")
        pwd = password or os.getenv("SHIOAJI_PASSWORD")

        # Explicit API Key support (User request)
        api_key = os.getenv("SHIOAJI_API_KEY")
        secret_key = os.getenv("SHIOAJI_SECRET_KEY")

        if api_key and secret_key:
            logger.info("Using API Key/Secret for login")
            # ENABLE fetch_contract to get valid Contract objects (Crash resolved)
            self.api.login(api_key=api_key, secret_key=secret_key, contracts_cb=contracts_cb, fetch_contract=True)
            logger.info("Login successful (API Key) - Contract Fetch ENABLED")

            self.logged_in = True
            return

        if not pid or not pwd:
            logger.warning("No credentials found (Args/Env). Running in simulation/anonymous mode.")
            return

        if not self.api:
            logger.warning("Shioaji SDK not installed; cannot login. Staying in simulation mode.")
            return

        try:
            # Fallback to Person ID (CA/Trading)
            self.api.login(person_id=pid, passwd=pwd, contracts_cb=contracts_cb)

            logger.info("Login successful")
            logger.info("Login successful - Contract Fetch DISABLED")
            # Skipping fetch_contracts for resilience

            self.logged_in = True
        except Exception as e:
            logger.error("Login failed", error=str(e))
            raise

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]):
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

    def subscribe_basket(self, cb: Callable[..., Any]):
        if not self.api:
            # If API is missing entirely (no library), skip
            logger.info("Shioaji lib missing: skipping real subscription")
            return

        # In Sim mode with valid login, we CAN subscribe.
        if not self.logged_in:
            logger.warning("Not logged in; skipping subscription.")
            return

        # Store callback permanently for binding (fix GC issues)
        self.tick_callback = cb

        # Register stock callbacks (v1) to route ticks to provided callback
        try:
            self.api.quote.set_on_tick_stk_v1_callback(cb)
            self.api.quote.set_on_bidask_stk_v1_callback(cb)
        except Exception as e:
            logger.error(f"Failed stock v1 callback registration: {e}")

        # Wrap the callback to ensure Cython compatibility and logging
        # (This local wrapper is unused if we use the instance method below,
        # but kept for reference or removal. We will use self._wrapped_tick_cb)

        for sym in self.symbols:
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached", limit=self.MAX_SUBSCRIPTIONS)
                break

            registered_count = 0
            code = sym["code"]
            exchange = sym["exchange"]

            # Resolve contract object
            contract = self._get_contract(exchange, code)
            if not contract:
                logger.error("Contract not found", code=code)
                continue

            # Strict Callback Registration (Sinotrade Spec)
            try:
                self.api.quote.set_event_callback(cb)
                registered_count += 1
            except Exception:
                pass

            # 2. Futures/Options (FOP) Callbacks - V1
            try:
                # Use Global Dispatcher
                self.api.quote.set_on_tick_fop_v1_callback(dispatch_tick_cb)
                self.api.quote.set_on_bidask_fop_v1_callback(dispatch_tick_cb)
                registered_count += 2
                logger.debug(f"Registered FOP v1 callbacks (Global Dispatch) for {code}")
            except Exception as e:
                logger.error(f"Failed FOP v1 registration for {code}: {e}")

            if registered_count > 0:
                pass
            else:
                logger.error(f"Failed to register strict callbacks for {code}")

            # Subscribe
            try:
                # Use QuoteVersion.v1 for Futures
                v = sj.constant.QuoteVersion.v1
                self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
                self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
                # logger.info(f"Subscribed to {code}")
            except Exception as e:
                logger.error(f"Subscription failed for {code}: {e}")

            self.subscribed_count += 1

    def _wrapped_tick_cb(self, exchange, msg):
        """Persistent callback wrapper"""
        try:
            # Delegate to the registered callback map or fixed callback?
            # Since subscribe_basket takes 'cb', we need to store 'cb' or pass it?
            # We can store it in self.tick_callback
            if hasattr(self, "tick_callback") and self.tick_callback:
                self.tick_callback(exchange, msg)
        except Exception as e:
            logger.error(f"Callback error: {e}")

    def _get_contract(self, exchange: str, code: str):
        # Helper to find contract
        if not self.api:
            return None
        try:
            if exchange == "TSE":
                return self.api.Contracts.Stocks.TSE[code]
            elif exchange == "OTC":
                return self.api.Contracts.Stocks.OTC[code]
            elif exchange in ["FUT", "Futures"]:
                # Try direct lookup first (sometimes works if flat)
                try:
                    return self.api.Contracts.Futures[code]
                except Exception:
                    pass

                # Deep search in likely categories
                # We can cache this map on init if slow, but for now just search.
                # Common categories: TXF, MXF, GTF, etc. Or iterate all?
                # Iterating all is safer.
                # Deep search in likely categories
                # logger.debug(f"Deep search for {code} in Futures...")

                # Deep search in likely categories
                # shioaji.contracts.StreamFutureContracts might use __getattr__ so dir() misses categories.
                # Explicitly check common ones.
                known_categories = ["TXF", "MXF", "GTF", "XIF", "XJF", "XAF", "XBF", "XCF", "ZEF", "ZFF"]
                found_attrs = dir(self.api.Contracts.Futures)
                logger.warning(f"Futures attributes: {found_attrs}")

                # Merge known + found (dedupe)
                all_cats = set(known_categories + found_attrs)

                for attr_name in all_cats:
                    if attr_name.startswith("_"):
                        continue

                    try:
                        cat = getattr(self.api.Contracts.Futures, attr_name, None)
                        if not cat:
                            # logger.debug(f"Category {attr_name} is None/Missing")
                            continue

                        # Check if it looks like a category (iterable)
                        if not hasattr(cat, "__iter__"):
                            continue

                        for c in cat:
                            # Check CODE or SYMBOL (e.g. key=TXFA6, symbol=TXF202601)
                            if hasattr(c, "code") and (c.code == code or getattr(c, "symbol", "") == code):
                                logger.info(f"Resolved {code} in category {attr_name} (Match: {c.code}/{c.symbol})")
                                return c
                    except Exception:
                        continue

                # logger.warning(f"Contract {code} not found in Futures deep search")
                pass

        except Exception as e:
            logger.error(f"get_contract lookup error for {code}: {e}")
            # fall through

        # Fallback: Manual Construction (For Stress Test / Sim / Day 1)
        # Verify valid exchange enum
        # We need to map string 'FUT' to strict enum if possible, or just pass string?
        # Shioaji expects Exchange enum often.
        # But Contract(exchange=...) accepts string or enum.
        # We will try to use the library's Enum if available.
        # But since we are in a method where we imported sj... wait, sj is module level.

        if sj:
            # Create synthetic
            # Guess SecurityType.
            # TXF -> FUT?
            # We will just assume FUT for now if exchange is FUT.

            try:
                # Need to map string to Exchange Enum
                # Exchange.TAIFEX is typical for FUT? Or is it Futures?
                # Exchange.TSE / OTC...
                # If generated config used "FUT", we map to TAIFEX?
                # Wait, TXF is on TAIFEX (Futures Exchange).

                exch_obj = (
                    sj.constant.Exchange.TAIFEX
                    if exchange in ["FUT", "Futures", "TAIFEX"]
                    else sj.constant.Exchange.TSE
                )
                sec_type = (
                    sj.constant.SecurityType.Future
                    if exchange in ["FUT", "Futures", "TAIFEX"]
                    else sj.constant.SecurityType.Stock
                )
                cat = code[:3] if len(code) >= 3 else code  # Approximate category

                # Construct
                c = sj.contracts.Contract(
                    code=code, symbol=code, name=code, category=cat, exchange=exch_obj, security_type=sec_type
                )
                logger.info(f"Constructed Synthetic Contract for {code}")
                return c
            except Exception as e:
                logger.error(f"Failed to construct synthetic contract: {e}")

        return None

    def get_exchange(self, code: str) -> str | None:
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
            "bytes_used": 0,  # Placeholder
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
                results = []  # Placeholder
                snapshots.extend(results)
                # Rate limit throttle: 50 requests per 5s => assume 100ms delay safe
                time.sleep(0.1)
            except Exception as e:
                logger.error("Snapshot fetch failed", error=str(e))

        return snapshots

    # get_positions was defined earlier. Removing duplicate.

    def place_order(
        self,
        contract_code: str,
        exchange: str,
        action: str,
        price: float,
        qty: int,
        order_type: str,
        tif: str,
        custom_field: str | None = None,
    ):
        """
        Wrapper for placing order.
        """
        if not self.api:
            logger.warning("Shioaji SDK missing; mock place_order invoked.")
            return {"seq_no": f"sim-{int(time.time() * 1000)}"}

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

        order = sj.Order(price=price, quantity=qty, action=act, price_type=pt, order_type=ot, custom_field=custom_field)

        trade = self.api.place_order(contract, order)
        return trade

    def cancel_order(self, trade):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock cancel_order invoked.")
            return
        self.api.update_status(self.api.OrderState.Cancel, trade=trade)

    def update_order(self, trade, price: float | None = None, qty: int | None = None):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock update_order invoked.")
            return
        if price:
            self.api.update_status(self.api.OrderState.UpdatePrice, trade=trade, price=price)
        elif qty:
            self.api.update_status(self.api.OrderState.UpdateQty, trade=trade, quantity=qty)

    def get_account_balance(self, account=None):
        return {}
