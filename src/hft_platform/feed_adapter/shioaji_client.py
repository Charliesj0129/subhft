import os
import threading
import time
from typing import Any, Callable, Dict, List

import yaml
from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry
from hft_platform.order.rate_limiter import RateLimiter

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
    def __init__(self, config_path: str | None = None, shioaji_config: dict[str, Any] | None = None):
        self.MAX_SUBSCRIPTIONS = 200
        self.contracts_timeout = int(os.getenv("SHIOAJI_CONTRACTS_TIMEOUT", "10000"))
        self.fetch_contract = os.getenv("SHIOAJI_FETCH_CONTRACT", "1") != "0"
        self.subscribe_trade = os.getenv("SHIOAJI_SUBSCRIBE_TRADE", "1") != "0"
        self.allow_symbol_fallback = os.getenv("HFT_ALLOW_SYMBOL_FALLBACK") == "1"
        self.allow_synthetic_contracts = os.getenv("HFT_ALLOW_SYNTHETIC_CONTRACTS") == "1"
        self.index_exchange = os.getenv("HFT_INDEX_EXCHANGE", "TSE").upper()
        self.resubscribe_cooldown = float(os.getenv("HFT_RESUBSCRIBE_COOLDOWN", "1.5"))
        self.shioaji_config = shioaji_config or {}

        def _as_bool(value: Any) -> bool:
            if value is None:
                return False
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        if "activate_ca" in self.shioaji_config:
            self.activate_ca = _as_bool(self.shioaji_config.get("activate_ca"))
        else:
            self.activate_ca = os.getenv("SHIOAJI_ACTIVATE_CA", "0") == "1" or os.getenv("HFT_ACTIVATE_CA", "0") == "1"
        self.ca_path = self.shioaji_config.get("ca_path") or os.getenv("SHIOAJI_CA_PATH") or os.getenv("CA_CERT_PATH")
        ca_password = (
            self.shioaji_config.get("ca_password") or os.getenv("SHIOAJI_CA_PASSWORD") or os.getenv("CA_PASSWORD")
        )
        if not ca_password:
            env_key = self.shioaji_config.get("ca_password_env")
            if env_key:
                ca_password = os.getenv(str(env_key))
        self.ca_password = ca_password

        if config_path is None:
            config_path = os.getenv("SYMBOLS_CONFIG")
            if not config_path:
                if os.path.exists("config/symbols.yaml"):
                    config_path = "config/symbols.yaml"
                else:
                    config_path = "config/base/symbols.yaml"

        sim_override = self.shioaji_config.get("simulation") if "simulation" in self.shioaji_config else None
        if sim_override is None:
            is_sim = os.getenv("HFT_MODE", "real") == "sim"
        else:
            is_sim = _as_bool(sim_override)
        if sj:
            self.api = sj.Shioaji(simulation=is_sim)
        else:
            self.api = None
        self.config_path = config_path
        self.symbols: List[Dict[str, Any]] = []
        self._load_config()
        self.subscribed_count = 0
        self.subscribed_codes: set[str] = set()
        self.tick_callback = None
        self._callbacks_registered = False
        self.logged_in = False
        self.mode = "simulation" if (is_sim or self.api is None) else "real"
        if self.mode == "simulation":
            self.activate_ca = False
        self.ca_active = False
        self._reconnect_lock = threading.Lock()
        self._last_reconnect_ts = 0.0
        self._reconnect_backoff_s = float(os.getenv("HFT_RECONNECT_BACKOFF_S", "30"))
        self._reconnect_backoff_max_s = float(os.getenv("HFT_RECONNECT_BACKOFF_MAX_S", "600"))
        self.metrics = MetricsRegistry.get()
        self._api_cache: dict[str, tuple[float, Any]] = {}
        self._api_cache_lock = threading.Lock()
        self._positions_cache_ttl_s = float(os.getenv("HFT_POSITIONS_CACHE_TTL_S", "1.5"))
        self._usage_cache_ttl_s = float(os.getenv("HFT_USAGE_CACHE_TTL_S", "5"))
        self._account_cache_ttl_s = float(os.getenv("HFT_ACCOUNT_CACHE_TTL_S", "5"))
        self._margin_cache_ttl_s = float(os.getenv("HFT_MARGIN_CACHE_TTL_S", "5"))
        self._profit_cache_ttl_s = float(os.getenv("HFT_PROFIT_CACHE_TTL_S", "10"))
        self._positions_detail_cache_ttl_s = float(os.getenv("HFT_POSITION_DETAIL_CACHE_TTL_S", "10"))
        self._api_last_latency_ms: dict[str, float] = {}
        self._api_rate_limiter = RateLimiter(
            soft_cap=int(os.getenv("HFT_SHIOAJI_API_SOFT_CAP", "20")),
            hard_cap=int(os.getenv("HFT_SHIOAJI_API_HARD_CAP", "25")),
            window_s=int(os.getenv("HFT_SHIOAJI_API_WINDOW_S", "5")),
        )

        # Register self globally
        if self not in CLIENT_REGISTRY:
            CLIENT_REGISTRY.append(self)
            logger.info("Registered ShioajiClient in Global Registry")

    def _record_api_latency(self, op: str, start_ns: int, ok: bool = True) -> None:
        if not self.metrics:
            return
        latency_ms = (time.perf_counter_ns() - start_ns) / 1e6
        result = "ok" if ok else "error"
        self.metrics.shioaji_api_latency_ms.labels(op=op, result=result).observe(latency_ms)
        last = self._api_last_latency_ms.get(op)
        if last is not None:
            jitter = abs(latency_ms - last)
            self.metrics.shioaji_api_jitter_ms.labels(op=op).set(jitter)
            if hasattr(self.metrics, "shioaji_api_jitter_ms_hist"):
                self.metrics.shioaji_api_jitter_ms_hist.labels(op=op).observe(jitter)
        self._api_last_latency_ms[op] = latency_ms
        if not ok:
            self.metrics.shioaji_api_errors_total.labels(op=op).inc()

    def _cache_get(self, key: str) -> Any | None:
        now = time.time()
        with self._api_cache_lock:
            entry = self._api_cache.get(key)
            if not entry:
                return None
            expires_at, value = entry
            if now >= expires_at:
                self._api_cache.pop(key, None)
                return None
            return value

    def _cache_set(self, key: str, ttl_s: float, value: Any) -> None:
        expires_at = time.time() + max(0.0, ttl_s)
        with self._api_cache_lock:
            self._api_cache[key] = (expires_at, value)

    def _rate_limit_api(self, op: str) -> bool:
        if not self._api_rate_limiter.check():
            logger.warning("API rate limit hit", op=op)
            return False
        self._api_rate_limiter.record()
        return True

    def _process_tick(self, exchange, msg):
        """Internal method called by global dispatcher"""
        try:
            if self.tick_callback:
                self.tick_callback(exchange, msg)
        except Exception as e:
            logger.error("Error processing tick", error=str(e))

    def _load_config(self):
        with open(self.config_path, "r") as f:
            data = yaml.safe_load(f) or {}
            self.symbols = data.get("symbols", [])
            if len(self.symbols) > self.MAX_SUBSCRIPTIONS:
                if os.getenv("HFT_ALLOW_TRUNCATE_SUBSCRIPTIONS") == "1":
                    logger.warning(
                        "Symbol list exceeds limit",
                        limit=self.MAX_SUBSCRIPTIONS,
                        count=len(self.symbols),
                    )
                    self.symbols = self.symbols[: self.MAX_SUBSCRIPTIONS]
                else:
                    raise ValueError(f"Symbol list exceeds limit ({len(self.symbols)} > {self.MAX_SUBSCRIPTIONS}).")

        # Build map
        self.code_exchange_map = {s["code"]: s["exchange"] for s in self.symbols if s.get("code") and s.get("exchange")}

    def login(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        person_id: str | None = None,
        ca_passwd: str | None = None,
        contracts_cb=None,
    ):
        logger.info("Logging in to Shioaji...")
        self.ca_active = False
        # Resolve credentials: Arg > Env
        key = api_key or os.getenv("SHIOAJI_API_KEY")
        secret = secret_key or os.getenv("SHIOAJI_SECRET_KEY")
        pid = person_id or os.getenv("SHIOAJI_PERSON_ID")
        ca_pwd = ca_passwd or os.getenv("SHIOAJI_CA_PASSWORD") or os.getenv("CA_PASSWORD")

        if key and secret:
            logger.info("Using API Key/Secret for login")
            start_ns = time.perf_counter_ns()
            try:
                self.api.login(
                    api_key=key,
                    secret_key=secret,
                    contracts_timeout=self.contracts_timeout,
                    contracts_cb=contracts_cb,
                    fetch_contract=self.fetch_contract,
                    subscribe_trade=self.subscribe_trade,
                )
                self._record_api_latency("login", start_ns, ok=True)
            except Exception:
                self._record_api_latency("login", start_ns, ok=False)
                raise
            logger.info("Login successful (API Key)")
            if not self.fetch_contract:
                self._ensure_contracts()
            if self.activate_ca:
                if not pid:
                    logger.warning("CA activation requested but missing SHIOAJI_PERSON_ID")
                if not self.ca_path or not ca_pwd:
                    logger.warning("CA activation requested but missing CA_CERT_PATH/CA_PASSWORD")
                else:
                    try:
                        start_ns = time.perf_counter_ns()
                        self.api.activate_ca(ca_path=self.ca_path, ca_passwd=ca_pwd)
                        self._record_api_latency("activate_ca", start_ns, ok=True)
                        self.ca_active = True
                        logger.info("CA activated")
                    except Exception as exc:
                        self._record_api_latency("activate_ca", start_ns, ok=False)
                        logger.error("CA activation failed", error=str(exc))
            self.logged_in = True
            return

        if not self.api:
            logger.warning("Shioaji SDK not installed; cannot login. Staying in simulation mode.")
            return

        logger.warning("No API key/secret found (Args/Env). Running in simulation/anonymous mode.")
        return

    def set_execution_callbacks(self, on_order: Callable[..., Any], on_deal: Callable[..., Any]):
        """
        Register low-latency callbacks.
        Note: These run on Shioaji threads.
        """
        if not self.api:
            logger.warning("Shioaji SDK missing; execution callbacks not registered (sim mode).")
            return
        order_state = getattr(sj.constant, "OrderState", None) if sj else None
        deal_states = set()
        if order_state:
            for name in ("StockDeal", "FuturesDeal"):
                state = getattr(order_state, name, None)
                if state is not None:
                    deal_states.add(state)

        def _order_cb(stat, msg):
            try:
                if stat in deal_states:
                    on_deal(msg)
                else:
                    on_order(stat, msg)
            except Exception as exc:
                logger.error("Execution callback failed", error=str(exc))

        self._order_callback = _order_cb
        self.api.set_order_callback(self._order_callback)

    def _ensure_contracts(self) -> None:
        if not self.api or not hasattr(self.api, "fetch_contracts"):
            return
        try:
            start_ns = time.perf_counter_ns()
            self.api.fetch_contracts(contract_download=True)
            self._record_api_latency("fetch_contracts", start_ns, ok=True)
        except Exception as exc:
            self._record_api_latency("fetch_contracts", start_ns, ok=False)
            logger.warning("Contract fetch failed", error=str(exc))

    def _maybe_activate_ca(self) -> None:
        if not self.api or not self.activate_ca:
            return
        if self.mode == "simulation":
            return
        if not self.ca_path or not self.ca_password:
            logger.warning("CA activation requested but missing ca_path/ca_password")
            return
        try:
            self.api.activate_ca(ca_path=self.ca_path, ca_passwd=self.ca_password)
            self.ca_active = True
            logger.info("CA activated")
        except Exception as exc:
            logger.error("CA activation failed", error=str(exc))

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
        self._register_callbacks(cb)

        for sym in self.symbols:
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached", limit=self.MAX_SUBSCRIPTIONS)
                break
            if self._subscribe_symbol(sym, cb):
                code = sym.get("code")
                if code:
                    self.subscribed_codes.add(code)
                self.subscribed_count = len(self.subscribed_codes)

    def _register_callbacks(self, cb: Callable[..., Any]) -> None:
        if not self.api or self._callbacks_registered:
            return
        try:
            self.api.quote.set_on_tick_stk_v1_callback(cb)
            self.api.quote.set_on_bidask_stk_v1_callback(cb)
        except Exception as e:
            logger.error(f"Failed stock v1 callback registration: {e}")

        try:
            self.api.quote.set_event_callback(self._on_quote_event)
        except Exception as exc:
            logger.warning("Failed quote event callback registration", error=str(exc))

        try:
            self.api.quote.set_on_tick_fop_v1_callback(dispatch_tick_cb)
            self.api.quote.set_on_bidask_fop_v1_callback(dispatch_tick_cb)
        except Exception as e:
            logger.error(f"Failed FOP v1 callback registration: {e}")

        self._callbacks_registered = True

    def _on_quote_event(self, resp_code: int, event_code: int, info: str, event: str) -> None:
        if event_code in (1, 2, 3, 4, 12, 13):
            logger.info("Quote event", resp_code=resp_code, event_code=event_code, info=info, event_name=event)
        if event_code in (4, 13):
            self._resubscribe_all()

    def reconnect(self, reason: str = "") -> bool:
        if not self.api:
            return False
        now = time.time()
        cooldown = float(os.getenv("HFT_RECONNECT_COOLDOWN", "30"))
        if now - self._last_reconnect_ts < max(cooldown, self._reconnect_backoff_s):
            return False
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            self._last_reconnect_ts = now
            logger.warning("Reconnecting Shioaji", reason=reason)
            try:
                self.api.logout()
            except Exception:
                pass
            self.logged_in = False
            self._callbacks_registered = False
            self.subscribed_codes = set()
            self.subscribed_count = 0

            self.login()
            if self.logged_in and self.tick_callback:
                self.subscribe_basket(self.tick_callback)
            if self.logged_in:
                self.metrics.feed_reconnect_total.labels(result="ok").inc()
                self._reconnect_backoff_s = float(os.getenv("HFT_RECONNECT_BACKOFF_S", "30"))
            else:
                self.metrics.feed_reconnect_total.labels(result="fail").inc()
                self._reconnect_backoff_s = min(self._reconnect_backoff_s * 2.0, self._reconnect_backoff_max_s)
            return self.logged_in
        finally:
            self._reconnect_lock.release()

    def _resubscribe_all(self) -> None:
        if not self.api or not self.logged_in or not self.tick_callback:
            return
        now = time.time()
        last = getattr(self, "_last_resubscribe_ts", 0.0)
        cooldown = getattr(self, "resubscribe_cooldown", 1.5)
        if now - last < cooldown:
            return
        self._last_resubscribe_ts = now
        self.subscribed_codes = set()
        self.subscribed_count = 0
        for sym in self.symbols:
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached during resubscribe", limit=self.MAX_SUBSCRIPTIONS)
                break
            if self._subscribe_symbol(sym, self.tick_callback):
                code = sym.get("code")
                if code:
                    self.subscribed_codes.add(code)
                self.subscribed_count = len(self.subscribed_codes)

    def resubscribe(self) -> bool:
        if not self.api or not self.logged_in or not self.tick_callback:
            self.metrics.feed_resubscribe_total.labels(result="skip").inc()
            return False
        try:
            self._resubscribe_all()
            self.metrics.feed_resubscribe_total.labels(result="ok").inc()
            return True
        except Exception as exc:
            logger.error("Resubscribe failed", error=str(exc))
            self.metrics.feed_resubscribe_total.labels(result="error").inc()
            return False

    def _subscribe_symbol(self, sym: Dict[str, Any], cb: Callable[..., Any]) -> bool:
        code = sym.get("code")
        exchange = sym.get("exchange")
        product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
        if not code or not exchange:
            logger.error("Invalid symbol entry", symbol=sym)
            return False

        contract = self._get_contract(
            exchange,
            code,
            product_type=product_type,
            allow_synthetic=self.allow_synthetic_contracts,
        )
        if not contract:
            logger.error("Contract not found", code=code)
            return False

        try:
            start_ns = time.perf_counter_ns()
            v = sj.constant.QuoteVersion.v1
            self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
            self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
            self._record_api_latency("subscribe", start_ns, ok=True)
            return True
        except Exception as e:
            self._record_api_latency("subscribe", start_ns, ok=False)
            logger.error(f"Subscription failed for {code}: {e}")
            return False

    def _unsubscribe_symbol(self, sym: Dict[str, Any]) -> None:
        if not self.api or not sj:
            return
        code = sym.get("code")
        exchange = sym.get("exchange")
        product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
        if not code or not exchange:
            return
        contract = self._get_contract(exchange, code, product_type=product_type, allow_synthetic=False)
        if not contract:
            return
        try:
            start_ns = time.perf_counter_ns()
            v = sj.constant.QuoteVersion.v1
            self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
            self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
            self._record_api_latency("unsubscribe", start_ns, ok=True)
        except Exception as e:
            self._record_api_latency("unsubscribe", start_ns, ok=False)
            logger.warning(f"Unsubscribe failed for {code}: {e}")

    def reload_symbols(self) -> None:
        old_map = {s.get("code"): s for s in self.symbols if s.get("code")}
        self._load_config()
        self.code_exchange_map = {s["code"]: s["exchange"] for s in self.symbols if s.get("code") and s.get("exchange")}

        new_map = {s.get("code"): s for s in self.symbols if s.get("code")}
        removed = set(old_map) - set(new_map)
        added = set(new_map) - set(old_map)

        if not self.api or not self.logged_in or not self.tick_callback:
            self.subscribed_codes = set(new_map)
            self.subscribed_count = len(self.subscribed_codes)
            return

        for code in removed:
            self._unsubscribe_symbol(old_map[code])
            self.subscribed_codes.discard(code)

        for code in added:
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                raise ValueError("Subscription limit reached during reload")
            sym = new_map[code]
            if self._subscribe_symbol(sym, self.tick_callback):
                self.subscribed_codes.add(code)

        self.subscribed_count = len(self.subscribed_codes)

    def validate_symbols(self) -> list[str]:
        if not self.api or not self.logged_in:
            return []
        invalid = []
        for sym in self.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            if not self._get_contract(exchange, code, product_type=product_type, allow_synthetic=False):
                invalid.append(code)
        if invalid:
            logger.warning("Unsubscribable symbols detected", count=len(invalid), symbols=invalid[:10])
        return invalid

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

    def _get_contract(
        self,
        exchange: str,
        code: str,
        product_type: str | None = None,
        allow_synthetic: bool = False,
    ):
        if not self.api:
            return None

        exch = str(exchange or "").upper()
        prod = str(product_type or "").strip().lower()

        if prod in {"index", "idx"} or exch in {"IDX", "INDEX"}:
            idx_exch = exch if exch in {"TSE", "OTC"} else self.index_exchange
            idx_group = getattr(self.api.Contracts.Indexs, idx_exch, None)
            return self._lookup_contract(
                idx_group, code, allow_symbol_fallback=self.allow_symbol_fallback, label="index"
            )

        if prod in {"stock", "stk"} or exch in {"TSE", "OTC", "OES"}:
            if exch == "TSE":
                return self._lookup_contract(
                    self.api.Contracts.Stocks.TSE,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OTC":
                return self._lookup_contract(
                    self.api.Contracts.Stocks.OTC,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OES":
                return self._lookup_contract(
                    self.api.Contracts.Stocks.OES,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
            for group in (self.api.Contracts.Stocks.TSE, self.api.Contracts.Stocks.OTC, self.api.Contracts.Stocks.OES):
                contract = self._lookup_contract(
                    group,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
                if contract:
                    return contract

        if prod in {"future", "futures"} or exch in {"FUT", "FUTURES", "TAIFEX"}:
            contract = self._lookup_contract(
                self.api.Contracts.Futures,
                code,
                allow_symbol_fallback=self.allow_symbol_fallback,
                label="future",
            )
            if contract:
                return contract

        if prod in {"option", "options"} or exch in {"OPT", "OPTIONS"}:
            contract = self._lookup_contract(
                self.api.Contracts.Options,
                code,
                allow_symbol_fallback=self.allow_symbol_fallback,
                label="option",
            )
            if contract:
                return contract

        if allow_synthetic and sj:
            return self._build_synthetic_contract(exch, code)

        return None

    def _lookup_contract(self, container: Any, code: str, allow_symbol_fallback: bool, label: str) -> Any | None:
        if not container:
            return None

        try:
            return container[code]
        except Exception:
            pass

        def iter_contracts(value: Any):
            iterable = value.values() if isinstance(value, dict) else value
            for item in iterable:
                yield item
                try:
                    if hasattr(item, "__iter__") and not hasattr(item, "code"):
                        for sub in item:
                            yield sub
                except Exception:
                    continue

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "code", None) == code:
                    return contract
        except Exception:
            return None

        if not allow_symbol_fallback:
            return None

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "symbol", None) == code:
                    logger.warning("Symbol fallback used for contract", code=code, type=label)
                    return contract
        except Exception:
            return None
        return None

    def _build_synthetic_contract(self, exchange: str, code: str) -> Any | None:
        try:
            exch_obj = (
                sj.constant.Exchange.TAIFEX if exchange in {"FUT", "FUTURES", "TAIFEX"} else sj.constant.Exchange.TSE
            )
            sec_type = (
                sj.constant.SecurityType.Future
                if exchange in {"FUT", "FUTURES", "TAIFEX"}
                else sj.constant.SecurityType.Stock
            )
            cat = code[:3] if len(code) >= 3 else code

            contract = sj.contracts.Contract(
                code=code,
                symbol=code,
                name=code,
                category=cat,
                exchange=exch_obj,
                security_type=sec_type,
            )
            logger.info("Constructed synthetic contract", code=code, exchange=exchange)
            return contract
        except Exception as exc:
            logger.error("Failed to construct synthetic contract", error=str(exc))
            return None

    def get_exchange(self, code: str) -> str | None:
        """Resolve exchange for a code."""
        # Try map first
        if code in self.code_exchange_map:
            return self.code_exchange_map[code]
        # Heuristic fallback?
        return None

    def get_usage(self):
        """Usage stats from Shioaji if available."""
        cached = self._cache_get("usage")
        if cached is not None:
            return cached
        if self.api and self.logged_in and hasattr(self.api, "usage"):
            try:
                if not self._rate_limit_api("usage"):
                    return cached or {"subscribed": self.subscribed_count, "bytes_used": 0}
                start_ns = time.perf_counter_ns()
                usage = self.api.usage()
                self._record_api_latency("usage", start_ns, ok=True)
                self._cache_set("usage", self._usage_cache_ttl_s, usage)
                return usage
            except Exception as exc:
                self._record_api_latency("usage", start_ns, ok=False)
                logger.warning("Failed to fetch usage", error=str(exc))
        return {"subscribed": self.subscribed_count, "bytes_used": 0}

    def get_positions(self) -> List[Any]:
        """Fetch current positions from Shioaji."""
        if self.mode == "simulation":
            return []
        cached = self._cache_get("positions")
        if cached is not None:
            return cached
        try:
            if not self._rate_limit_api("positions"):
                return cached or []
            positions: list[Any] = []
            start_ns = time.perf_counter_ns()
            if hasattr(self.api, "stock_account") and self.api.stock_account is not None:
                positions.extend(self.api.list_positions(self.api.stock_account))
            if hasattr(self.api, "futopt_account") and self.api.futopt_account is not None:
                positions.extend(self.api.list_positions(self.api.futopt_account))
            self._record_api_latency("positions", start_ns, ok=True)
            self._cache_set("positions", self._positions_cache_ttl_s, positions)
            return positions
        except Exception:
            self._record_api_latency("positions", start_ns, ok=False)
            logger.warning("Failed to fetch positions")
            return cached or []

    def fetch_snapshots(self):
        """Fetch snapshots for all symbols in batches <= 500."""
        if not self.api or not self.logged_in:
            logger.info("Simulation mode: skipping snapshot fetch")
            return []

        contracts = []
        for sym in self.symbols:
            code = sym.get("code")
            exchange = sym.get("exchange")
            product_type = sym.get("product_type") or sym.get("security_type") or sym.get("type")
            if not code or not exchange:
                continue
            contract = self._get_contract(exchange, code, product_type=product_type, allow_synthetic=False)
            if contract:
                contracts.append(contract)

        if not contracts:
            logger.warning("No contracts resolved for snapshots")
            return []

        snapshots = []
        batch_size = 500
        for i in range(0, len(contracts), batch_size):
            batch = contracts[i : i + batch_size]
            logger.info("Requesting snapshots", batch_size=len(batch))
            try:
                start_ns = time.perf_counter_ns()
                results = self.api.snapshots(batch)
                self._record_api_latency("snapshots", start_ns, ok=True)
                snapshots.extend(results or [])
                time.sleep(0.11)
            except Exception as e:
                self._record_api_latency("snapshots", start_ns, ok=False)
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
        product_type: str | None = None,
        order_cond: str | None = None,
        order_lot: str | None = None,
        oc_type: str | None = None,
        account: Any | None = None,
        price_type: str | None = None,
    ):
        """
        Wrapper for placing order.
        """
        if not self.api:
            logger.warning("Shioaji SDK missing; mock place_order invoked.")
            return {"seq_no": f"sim-{int(time.time() * 1000)}"}

        contract = self._get_contract(exchange, contract_code, product_type=product_type, allow_synthetic=False)
        if not contract:
            raise ValueError(f"Contract {contract_code} not found")

        # Convert simple types to Shioaji enums
        # Action: Buy/Sell
        act = sj.constant.Action.Buy if action == "Buy" else sj.constant.Action.Sell

        if product_type:
            return self._place_order_typed(
                contract=contract,
                action=act,
                price=price,
                qty=qty,
                exchange=exchange,
                product_type=product_type,
                tif=tif,
                order_type=order_type,
                price_type=price_type,
                order_cond=order_cond,
                order_lot=order_lot,
                oc_type=oc_type,
                account=account,
                custom_field=custom_field,
            )

        # Legacy fallback for tests/backward compatibility.
        pt = sj.constant.StockPriceType.LMT
        ot = sj.constant.OrderType.ROD
        if tif == "IOC":
            ot = sj.constant.OrderType.IOC
        elif tif == "FOK":
            ot = sj.constant.OrderType.FOK

        order = sj.Order(price=price, quantity=qty, action=act, price_type=pt, order_type=ot, custom_field=custom_field)
        start_ns = time.perf_counter_ns()
        try:
            result = self.api.place_order(contract, order)
            self._record_api_latency("place_order", start_ns, ok=True)
            return result
        except Exception:
            self._record_api_latency("place_order", start_ns, ok=False)
            raise

    def _place_order_typed(
        self,
        *,
        contract: Any,
        action: Any,
        price: float,
        qty: int,
        exchange: str,
        product_type: str,
        tif: str,
        order_type: str,
        price_type: str | None,
        order_cond: str | None,
        order_lot: str | None,
        oc_type: str | None,
        account: Any | None,
        custom_field: str | None,
    ):
        prod = str(product_type or "").strip().lower()
        if not prod:
            prod = "stock" if str(exchange).upper() in {"TSE", "OTC", "OES"} else "future"

        resolved_account = self._resolve_account(prod, account)
        order = None

        fallback_cls = getattr(sj, "Order", None)

        if prod in {"stock", "stk"}:
            pt = self._map_stock_price_type(price_type)
            ot = self._map_stock_order_type(tif or order_type)
            cond = self._map_stock_order_cond(order_cond)
            lot = self._map_stock_order_lot(order_lot)
            order_cls = getattr(getattr(sj, "order", None), "StockOrder", None) or fallback_cls
            if resolved_account is None and order_cls is not fallback_cls:
                order_cls = fallback_cls
            if order_cls is fallback_cls:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    custom_field=custom_field,
                )
            else:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    order_cond=cond,
                    order_lot=lot,
                    account=resolved_account,
                    custom_field=custom_field,
                )
        else:
            pt = self._map_futures_price_type(price_type)
            ot = self._map_futures_order_type(tif or order_type)
            oc = self._map_futures_oc_type(oc_type)
            order_cls = getattr(getattr(sj, "order", None), "FuturesOrder", None) or fallback_cls
            if resolved_account is None and order_cls is not fallback_cls:
                order_cls = fallback_cls
            if order_cls is fallback_cls:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    custom_field=custom_field,
                )
            else:
                order = order_cls(
                    price=price,
                    quantity=qty,
                    action=action,
                    price_type=pt,
                    order_type=ot,
                    octype=oc,
                    account=resolved_account,
                    custom_field=custom_field,
                )

        start_ns = time.perf_counter_ns()
        try:
            result = self.api.place_order(contract, order)
            self._record_api_latency("place_order", start_ns, ok=True)
            return result
        except Exception:
            self._record_api_latency("place_order", start_ns, ok=False)
            raise

    def _resolve_account(self, product_type: str, account: Any | None) -> Any | None:
        if account is not None:
            if isinstance(account, str):
                if account == "stock" and hasattr(self.api, "stock_account"):
                    return self.api.stock_account
                if account in {"futopt", "future", "option"} and hasattr(self.api, "futopt_account"):
                    return self.api.futopt_account
            return account
        if not self.api:
            return None
        if product_type in {"stock", "stk"} and hasattr(self.api, "stock_account"):
            return self.api.stock_account
        if product_type in {"future", "futures", "option", "options"} and hasattr(self.api, "futopt_account"):
            return self.api.futopt_account
        return None

    def _map_stock_price_type(self, price_type: str | None) -> Any:
        if not sj:
            return None
        key = str(price_type or "LMT").upper()
        return getattr(sj.constant.StockPriceType, key, sj.constant.StockPriceType.LMT)

    def _map_stock_order_type(self, order_type: str | None) -> Any:
        if not sj:
            return None
        key = str(order_type or "ROD").upper()
        return getattr(sj.constant.OrderType, key, sj.constant.OrderType.ROD)

    def _map_stock_order_cond(self, order_cond: str | None) -> Any:
        if not sj:
            return None
        if not order_cond:
            return sj.constant.StockOrderCond.Cash
        key = str(order_cond).strip().lower().replace("_", "").replace("-", "")
        mapping = {
            "cash": "Cash",
            "margin": "MarginTrading",
            "margintrading": "MarginTrading",
            "short": "ShortSelling",
            "shortselling": "ShortSelling",
        }
        name = mapping.get(key, "Cash")
        return getattr(sj.constant.StockOrderCond, name, sj.constant.StockOrderCond.Cash)

    def _map_stock_order_lot(self, order_lot: str | None) -> Any:
        if not sj:
            return None
        if not order_lot:
            return sj.constant.StockOrderLot.Common
        key = str(order_lot).strip().lower().replace("_", "").replace("-", "")
        mapping = {
            "common": "Common",
            "fixing": "Fixing",
            "odd": "Odd",
            "intradayodd": "IntradayOdd",
        }
        name = mapping.get(key, "Common")
        return getattr(sj.constant.StockOrderLot, name, sj.constant.StockOrderLot.Common)

    def _map_futures_price_type(self, price_type: str | None) -> Any:
        if not sj:
            return None
        key = str(price_type or "LMT").upper()
        return getattr(sj.constant.FuturesPriceType, key, sj.constant.FuturesPriceType.LMT)

    def _map_futures_order_type(self, order_type: str | None) -> Any:
        if not sj:
            return None
        key = str(order_type or "ROD").upper()
        fut_type = getattr(sj.constant, "FuturesOrderType", None)
        if fut_type:
            return getattr(fut_type, key, fut_type.ROD)
        return getattr(sj.constant.OrderType, key, sj.constant.OrderType.ROD)

    def _map_futures_oc_type(self, oc_type: str | None) -> Any:
        if not sj:
            return None
        if not oc_type:
            return sj.constant.FuturesOCType.Auto
        key = str(oc_type).strip().lower().replace("_", "").replace("-", "")
        mapping = {"auto": "Auto", "new": "New", "close": "Close"}
        name = mapping.get(key, "Auto")
        return getattr(sj.constant.FuturesOCType, name, sj.constant.FuturesOCType.Auto)

    def cancel_order(self, trade):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock cancel_order invoked.")
            return
        if not hasattr(self.api, "cancel_order"):
            raise RuntimeError("Shioaji API missing cancel_order")
        try:
            start_ns = time.perf_counter_ns()
            result = self.api.cancel_order(trade)
            self._record_api_latency("cancel_order", start_ns, ok=True)
            return result
        except Exception as exc:
            self._record_api_latency("cancel_order", start_ns, ok=False)
            logger.error("cancel_order failed", error=str(exc))
            raise

    def update_order(self, trade, price: float | None = None, qty: int | None = None):
        if not self.api:
            logger.warning("Shioaji SDK missing; mock update_order invoked.")
            return
        if price is not None:
            if hasattr(self.api, "update_order"):
                try:
                    start_ns = time.perf_counter_ns()
                    result = self.api.update_order(trade=trade, price=price)
                    self._record_api_latency("update_order", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._record_api_latency("update_order", start_ns, ok=False)
                    logger.error("update_order(price) failed", error=str(exc))
                    raise
            if hasattr(self.api, "update_price"):
                try:
                    start_ns = time.perf_counter_ns()
                    result = self.api.update_price(trade=trade, price=price)
                    self._record_api_latency("update_price", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._record_api_latency("update_price", start_ns, ok=False)
                    logger.error("update_price failed", error=str(exc))
                    raise
            raise RuntimeError("Shioaji API missing update_order/update_price")
        if qty is not None:
            if hasattr(self.api, "update_order"):
                try:
                    start_ns = time.perf_counter_ns()
                    result = self.api.update_order(trade=trade, qty=qty)
                    self._record_api_latency("update_order", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._record_api_latency("update_order", start_ns, ok=False)
                    logger.error("update_order(qty) failed", error=str(exc))
                    raise
            if hasattr(self.api, "update_qty"):
                try:
                    start_ns = time.perf_counter_ns()
                    result = self.api.update_qty(trade=trade, quantity=qty)
                    self._record_api_latency("update_qty", start_ns, ok=True)
                    return result
                except Exception as exc:
                    self._record_api_latency("update_qty", start_ns, ok=False)
                    logger.error("update_qty failed", error=str(exc))
                    raise
            raise RuntimeError("Shioaji API missing update_order/update_qty")

    def get_account_balance(self, account=None):
        if self.mode == "simulation":
            return {}
        cached = self._cache_get("account_balance")
        if cached is not None:
            return cached
        try:
            if not self._rate_limit_api("account_balance"):
                return cached or {}
            start_ns = time.perf_counter_ns()
            result = None
            if account is not None:
                result = self.api.account_balance(account)
            else:
                result = self.api.account_balance()
            self._record_api_latency("account_balance", start_ns, ok=True)
            self._cache_set("account_balance", self._account_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._record_api_latency("account_balance", start_ns, ok=False)
            logger.warning("Failed to fetch account balance", error=str(exc))
            return cached or {}

    def get_margin(self, account=None):
        if self.mode == "simulation":
            return {}
        cached = self._cache_get("margin")
        if cached is not None:
            return cached
        try:
            if not self._rate_limit_api("margin"):
                return cached or {}
            start_ns = time.perf_counter_ns()
            acct = account
            if acct is None and hasattr(self.api, "futopt_account"):
                acct = self.api.futopt_account
            result = self.api.margin(acct)
            self._record_api_latency("margin", start_ns, ok=True)
            self._cache_set("margin", self._margin_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._record_api_latency("margin", start_ns, ok=False)
            logger.warning("Failed to fetch margin", error=str(exc))
            return cached or {}

    def list_position_detail(self, account=None):
        if self.mode == "simulation":
            return []
        cached = self._cache_get("position_detail")
        if cached is not None:
            return cached
        try:
            if not self._rate_limit_api("position_detail"):
                return cached or []
            start_ns = time.perf_counter_ns()
            acct = account
            if acct is None and hasattr(self.api, "stock_account"):
                acct = self.api.stock_account
            result = self.api.list_position_detail(acct) if acct is not None else self.api.list_position_detail()
            self._record_api_latency("position_detail", start_ns, ok=True)
            self._cache_set("position_detail", self._positions_detail_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._record_api_latency("position_detail", start_ns, ok=False)
            logger.warning("Failed to fetch position detail", error=str(exc))
            return cached or []

    def list_profit_loss(self, account=None, begin_date: str | None = None, end_date: str | None = None):
        if self.mode == "simulation":
            return []
        cache_key = f"profit_loss:{begin_date}:{end_date}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached
        try:
            if not self._rate_limit_api("profit_loss"):
                return cached or []
            start_ns = time.perf_counter_ns()
            acct = account
            if acct is None and hasattr(self.api, "stock_account"):
                acct = self.api.stock_account
            if acct is not None:
                result = self.api.list_profit_loss(acct, begin_date=begin_date, end_date=end_date)
            else:
                result = self.api.list_profit_loss(begin_date=begin_date, end_date=end_date)
            self._record_api_latency("profit_loss", start_ns, ok=True)
            self._cache_set(cache_key, self._profit_cache_ttl_s, result)
            return result
        except Exception as exc:
            self._record_api_latency("profit_loss", start_ns, ok=False)
            logger.warning("Failed to fetch profit/loss", error=str(exc))
            return cached or []
