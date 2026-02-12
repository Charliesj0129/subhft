import datetime as dt
import os
import threading
import time
from collections import deque
from typing import Any, Callable, Dict, List

import yaml
from structlog import get_logger

from hft_platform.core import timebase
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


def dispatch_tick_cb(*args, **kwargs):
    """
    Global static callback to dispatch ticks/bidask to all registered clients.
    Passes through raw args to avoid signature drift across Shioaji callbacks.
    """
    try:
        if not args and not kwargs:
            return
        for client in CLIENT_REGISTRY:
            if hasattr(client, "_process_tick"):
                client._process_tick(*args, **kwargs)
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
        self.tick_callback: Callable[..., Any] | None = None
        self._callbacks_registered = False
        self._pending_quote_resubscribe = False
        self._pending_quote_ts = 0.0
        self._pending_quote_relogining = False
        self._pending_quote_relogin_thread: threading.Thread | None = None
        self._last_quote_event_ts = 0.0
        self._callbacks_retrying = False
        self._callbacks_retry_thread: threading.Thread | None = None
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
        self._api_cache_max_size = int(os.getenv("HFT_API_CACHE_MAX_SIZE", "1000"))
        self._positions_cache_ttl_s = float(os.getenv("HFT_POSITIONS_CACHE_TTL_S", "1.5"))
        self._usage_cache_ttl_s = float(os.getenv("HFT_USAGE_CACHE_TTL_S", "5"))
        self._account_cache_ttl_s = float(os.getenv("HFT_ACCOUNT_CACHE_TTL_S", "5"))
        self._margin_cache_ttl_s = float(os.getenv("HFT_MARGIN_CACHE_TTL_S", "5"))
        self._profit_cache_ttl_s = float(os.getenv("HFT_PROFIT_CACHE_TTL_S", "10"))
        self._positions_detail_cache_ttl_s = float(os.getenv("HFT_POSITION_DETAIL_CACHE_TTL_S", "10"))
        self._api_last_latency_ms: dict[str, float] = {}
        self._quote_force_relogin_s = float(os.getenv("HFT_QUOTE_FORCE_RELOGIN_S", "15"))
        self._quote_flap_window_s = float(os.getenv("HFT_QUOTE_FLAP_WINDOW_S", "60"))
        self._quote_flap_threshold = int(os.getenv("HFT_QUOTE_FLAP_THRESHOLD", "5"))
        self._quote_flap_cooldown_s = float(os.getenv("HFT_QUOTE_FLAP_COOLDOWN_S", "300"))
        self._quote_flap_events: deque[float] = deque()
        self._last_quote_flap_relogin_ts = 0.0
        self._quote_version_mode = os.getenv("HFT_QUOTE_VERSION", "auto").strip().lower()
        if self._quote_version_mode not in {"v0", "v1", "auto"}:
            self._quote_version_mode = "auto"
        self._quote_version_strict = os.getenv("HFT_QUOTE_VERSION_STRICT", "0").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        self._quote_version = "v1" if self._quote_version_mode in {"v1", "auto"} else "v0"
        self._last_quote_data_ts = 0.0
        self._first_quote_seen = False
        self._quote_watchdog_thread: threading.Thread | None = None
        self._quote_watchdog_running = False
        self._quote_watchdog_interval_s = float(os.getenv("HFT_QUOTE_WATCHDOG_S", "5"))
        self._quote_no_data_s = float(os.getenv("HFT_QUOTE_NO_DATA_S", "30"))
        self._event_callback_registered = False
        self._event_callback_retrying = False
        self._event_callback_retry_thread: threading.Thread | None = None
        self._event_callback_retry_s = float(os.getenv("HFT_QUOTE_EVENT_RETRY_S", "5"))
        self._pending_quote_reason: str | None = None
        self._api_rate_limiter = RateLimiter(
            soft_cap=int(os.getenv("HFT_SHIOAJI_API_SOFT_CAP", "20")),
            hard_cap=int(os.getenv("HFT_SHIOAJI_API_HARD_CAP", "25")),
            window_s=int(os.getenv("HFT_SHIOAJI_API_WINDOW_S", "5")),
        )

        # Session refresh configuration (C3)
        self._session_refresh_interval_s = float(os.getenv("HFT_SESSION_REFRESH_S", "86400"))  # 24 hours
        self._last_session_refresh_ts = 0.0
        self._session_refresh_thread: threading.Thread | None = None
        self._session_refresh_running = False
        self._session_refresh_check_interval_s = 3600.0  # Check every hour

        # Holiday-aware session refresh (O4)
        self._session_refresh_holiday_aware = os.getenv("HFT_SESSION_REFRESH_HOLIDAY_AWARE", "1").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

        # Post-refresh health check (O5)
        self._session_refresh_verify_timeout_s = float(os.getenv("HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S", "10.0"))

        # Market open grace period (C4)
        self._market_open_grace_s = float(os.getenv("HFT_MARKET_OPEN_GRACE_S", "60"))  # 60 seconds
        self._market_open_grace_active = False

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
        now = timebase.now_s()
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
        expires_at = timebase.now_s() + max(0.0, ttl_s)
        with self._api_cache_lock:
            # Evict expired entries if cache is at limit
            if len(self._api_cache) >= self._api_cache_max_size:
                now = timebase.now_s()
                expired_keys = [k for k, (exp, _) in self._api_cache.items() if now >= exp]
                for k in expired_keys:
                    del self._api_cache[k]
                # If still at limit, remove oldest entry
                if len(self._api_cache) >= self._api_cache_max_size:
                    oldest_key = min(self._api_cache.keys(), key=lambda k: self._api_cache[k][0])
                    del self._api_cache[oldest_key]
            self._api_cache[key] = (expires_at, value)

    def _rate_limit_api(self, op: str) -> bool:
        if not self._api_rate_limiter.check():
            logger.warning("API rate limit hit", op=op)
            return False
        self._api_rate_limiter.record()
        return True

    def _process_tick(self, *args, **kwargs):
        """Internal method called by global dispatcher."""
        try:
            self._last_quote_data_ts = timebase.now_s()
            if not self._first_quote_seen:
                self._first_quote_seen = True
                logger.info(
                    "First quote data received",
                    quote_version=self._quote_version,
                    quote_version_mode=self._quote_version_mode,
                )
            if self._pending_quote_resubscribe:
                self._clear_quote_pending()
            if self.tick_callback:
                self.tick_callback(*args, **kwargs)
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
            self._last_session_refresh_ts = timebase.now_s()
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
        self._ensure_callbacks(cb)
        if self._last_quote_data_ts <= 0:
            self._last_quote_data_ts = timebase.now_s()

        logger.info(
            "Subscribing quote basket",
            count=len(self.symbols),
            mode=self.mode,
            quote_version=self._quote_version,
            quote_version_mode=self._quote_version_mode,
        )
        for sym in self.symbols:
            if self.subscribed_count >= self.MAX_SUBSCRIPTIONS:
                logger.error("Subscription limit reached", limit=self.MAX_SUBSCRIPTIONS)
                break
            if self._subscribe_symbol(sym, cb):
                code = sym.get("code")
                if code:
                    self.subscribed_codes.add(code)
                self.subscribed_count = len(self.subscribed_codes)
        logger.info("Quote subscription completed", subscribed=self.subscribed_count)
        self._start_quote_watchdog()
        self._start_session_refresh_thread()

    def _ensure_callbacks(self, cb: Callable[..., Any]) -> None:
        if not self.api:
            return
        if self._callbacks_registered and self._event_callback_registered:
            return
        self._register_callbacks(cb)
        if not self._callbacks_registered:
            self._start_callback_retry(cb)
        if not self._event_callback_registered:
            self._start_event_callback_retry()

    def _register_callbacks(self, cb: Callable[..., Any]) -> bool:
        if not self.api:
            return False
        if self._callbacks_registered and self._event_callback_registered:
            return True
        ok_quote = True
        ok_event = True
        try:
            # Use global dispatcher to avoid weak-ref issues with bound methods.
            ok_quote = self._register_quote_callbacks()
        except Exception as e:
            logger.error("Quote callback registration failed", error=str(e))
            ok_quote = False

        ok_event = self._register_event_callback()

        self._callbacks_registered = ok_quote
        self._event_callback_registered = ok_event

        if ok_quote:
            logger.info(
                "Quote callbacks registered",
                quote_version=self._quote_version,
                quote_version_mode=self._quote_version_mode,
            )
        else:
            logger.warning(
                "Quote callbacks not registered",
                quote_version=self._quote_version,
                quote_version_mode=self._quote_version_mode,
            )

        if ok_event:
            logger.info("Quote event callback registered")
        else:
            logger.warning("Quote event callback not registered")

        return ok_quote and ok_event

    def _register_quote_callbacks(self) -> bool:
        """Register quote callbacks based on active quote version."""
        if not self.api:
            return False
        logger.info(
            "Registering quote callbacks",
            quote_version=self._quote_version,
            quote_version_mode=self._quote_version_mode,
        )
        ok = True
        version = self._quote_version

        def _set_v1() -> bool:
            nonlocal ok
            try:
                self.api.quote.set_on_tick_stk_v1_callback(dispatch_tick_cb)
                self.api.quote.set_on_bidask_stk_v1_callback(dispatch_tick_cb)
                self.api.quote.set_on_tick_fop_v1_callback(dispatch_tick_cb)
                self.api.quote.set_on_bidask_fop_v1_callback(dispatch_tick_cb)
                return True
            except Exception as exc:
                logger.warning("Quote v1 callback registration failed", error=str(exc))
                ok = False
                return False

        def _set_v0() -> bool:
            nonlocal ok
            if not hasattr(self.api.quote, "set_on_tick_stk_callback"):
                logger.warning("Quote v0 callbacks not available on this Shioaji version")
                ok = False
                return False
            try:
                self.api.quote.set_on_tick_stk_callback(dispatch_tick_cb)
                self.api.quote.set_on_bidask_stk_callback(dispatch_tick_cb)
                if hasattr(self.api.quote, "set_on_tick_fop_callback"):
                    self.api.quote.set_on_tick_fop_callback(dispatch_tick_cb)
                if hasattr(self.api.quote, "set_on_bidask_fop_callback"):
                    self.api.quote.set_on_bidask_fop_callback(dispatch_tick_cb)
                return True
            except Exception as exc:
                logger.warning("Quote v0 callback registration failed", error=str(exc))
                ok = False
                return False

        if version == "v1":
            if not _set_v1():
                allow_fallback = self._quote_version_mode == "auto" or (
                    self._quote_version_mode == "v1" and not self._quote_version_strict
                )
                if allow_fallback:
                    logger.warning("Falling back to quote v0 callbacks")
                    self._quote_version = "v0"
                    ok = _set_v0()
                    if ok and self.metrics:
                        self.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                else:
                    ok = False
        else:
            ok = _set_v0()

        return ok

    def _register_event_callback(self) -> bool:
        if not self.api:
            return False
        try:
            self.api.quote.set_event_callback(self._on_quote_event)
            return True
        except Exception as exc:
            logger.warning("Failed quote event callback registration", error=str(exc))
            return False

    def _get_quote_version(self):
        if not sj or not hasattr(sj.constant, "QuoteVersion"):
            return None
        return sj.constant.QuoteVersion.v0 if self._quote_version == "v0" else sj.constant.QuoteVersion.v1

    def _start_session_refresh_thread(self) -> None:
        """Start background thread for preventive session refresh (C3).

        Refreshes session before long holidays to prevent expiration.
        When holiday-aware mode is enabled (O4), only refreshes:
        - When approaching long holidays (days_until_trading > 1)
        - Regular interval when on trading day or day before

        This reduces unnecessary refreshes during normal trading weeks.
        """
        if self._session_refresh_running:
            return
        if self._session_refresh_interval_s <= 0:
            return

        self._session_refresh_running = True
        logger.info(
            "Starting session refresh thread",
            interval_s=self._session_refresh_interval_s,
            check_interval_s=self._session_refresh_check_interval_s,
            holiday_aware=self._session_refresh_holiday_aware,
        )

        def _refresh_loop() -> None:
            try:
                from hft_platform.core.market_calendar import get_calendar

                calendar = get_calendar()
            except ImportError:
                logger.warning("Market calendar not available for session refresh")
                self._session_refresh_running = False
                return

            while self.api and self.logged_in and self._session_refresh_running:
                try:
                    time.sleep(self._session_refresh_check_interval_s)
                    if not self._session_refresh_running:
                        break

                    now = timebase.now_s()
                    now_dt = dt.datetime.now(calendar._tz)

                    # Skip refresh during active trading hours
                    if calendar.is_trading_hours(now_dt):
                        continue

                    days_until = calendar.days_until_trading(now_dt.date())
                    elapsed = now - self._last_session_refresh_ts

                    if self._session_refresh_holiday_aware:
                        # Holiday-aware mode (O4):
                        # - Refresh if approaching long holiday (days_until > 1)
                        # - Regular refresh only on trading day or day before
                        holiday_refresh = days_until > 1 and elapsed > 0  # Approaching holiday
                        regular_refresh = days_until <= 1 and elapsed >= self._session_refresh_interval_s

                        if not (holiday_refresh or regular_refresh):
                            continue

                        reason = "holiday" if holiday_refresh else "regular"
                    else:
                        # Original mode: refresh based on interval only
                        if days_until > 1:
                            continue
                        if elapsed < self._session_refresh_interval_s:
                            continue
                        reason = "interval"

                    logger.info(
                        "Preventive session refresh",
                        reason=reason,
                        days_until_trading=days_until,
                        elapsed_s=round(elapsed, 0),
                    )
                    self._do_session_refresh()
                except Exception as exc:
                    logger.warning("Session refresh check failed", error=str(exc))

            self._session_refresh_running = False

        self._session_refresh_thread = threading.Thread(
            target=_refresh_loop,
            name="shioaji-session-refresh",
            daemon=True,
        )
        self._session_refresh_thread.start()

    def _do_session_refresh(self) -> bool:
        """Perform session refresh via logout/login cycle.

        Includes post-refresh health check (O5) to verify quotes are flowing.

        Returns:
            True if refresh succeeded
        """
        if not self.api:
            return False

        try:
            logger.info("Session refresh: logging out")
            start_ns = time.perf_counter_ns()
            try:
                self.api.logout()
            except Exception as exc:
                logger.warning("Session refresh logout failed", error=str(exc))

            self.logged_in = False
            self._callbacks_registered = False

            logger.info("Session refresh: logging in")
            self.login()

            if self.logged_in:
                self._last_session_refresh_ts = timebase.now_s()
                self._record_api_latency("session_refresh", start_ns, ok=True)
                logger.info("Session refresh login successful")

                if self.tick_callback:
                    self._ensure_callbacks(self.tick_callback)
                    self._resubscribe_all()
                    self._start_quote_watchdog()

                    # Post-refresh health check (O5)
                    if self._verify_quotes_flowing():
                        logger.info("Session refresh completed, quotes flowing")
                        if self.metrics:
                            self.metrics.session_refresh_total.labels(result="ok").inc()
                        return True
                    else:
                        logger.warning("Session refresh completed but quotes not flowing")
                        if self.metrics:
                            self.metrics.session_refresh_total.labels(result="partial").inc()
                        # Still return True since login succeeded
                        return True
                else:
                    # No tick callback means no subscriptions to verify
                    if self.metrics:
                        self.metrics.session_refresh_total.labels(result="ok").inc()
                    logger.info("Session refresh completed (no subscriptions)")
                    return True
            else:
                self._record_api_latency("session_refresh", start_ns, ok=False)
                if self.metrics:
                    self.metrics.session_refresh_total.labels(result="error").inc()
                logger.error("Session refresh failed: login unsuccessful")
                return False
        except Exception as exc:
            logger.error("Session refresh failed", error=str(exc))
            if self.metrics:
                self.metrics.session_refresh_total.labels(result="error").inc()
            return False

    def _verify_quotes_flowing(self, timeout_s: float | None = None) -> bool:
        """Verify quotes are flowing after refresh (O5).

        Waits for new quote data to arrive within timeout period.

        Args:
            timeout_s: Timeout in seconds (default: HFT_SESSION_REFRESH_VERIFY_TIMEOUT_S)

        Returns:
            True if new quote data received within timeout
        """
        if not self.logged_in or not self.subscribed_count:
            # No subscriptions to verify
            return True

        if timeout_s is None:
            timeout_s = self._session_refresh_verify_timeout_s

        start_ts = self._last_quote_data_ts
        deadline = timebase.now_s() + timeout_s

        logger.debug(
            "Verifying quotes flowing",
            timeout_s=timeout_s,
            subscribed_count=self.subscribed_count,
        )

        while timebase.now_s() < deadline:
            if self._last_quote_data_ts > start_ts:
                logger.debug(
                    "Quotes flowing verified",
                    elapsed_s=round(timebase.now_s() - (deadline - timeout_s), 2),
                )
                return True
            time.sleep(0.5)

        logger.warning(
            "Quote verification timeout",
            timeout_s=timeout_s,
            subscribed_count=self.subscribed_count,
        )
        return False

    def _is_market_open_grace_period(self) -> bool:
        """Check if within grace period after market open (C4).

        Returns:
            True if within grace period
        """
        if self._market_open_grace_s <= 0:
            return False

        try:
            from hft_platform.core.market_calendar import get_calendar

            calendar = get_calendar()
        except ImportError:
            return False

        now = dt.datetime.now(calendar._tz)

        if not calendar.is_trading_day(now.date()):
            return False

        open_time = calendar.get_session_open(now.date())
        if open_time is None:
            return False

        # Check if we're within grace period after open
        elapsed = (now - open_time).total_seconds()
        in_grace = 0 <= elapsed <= self._market_open_grace_s

        # Update gauge
        if self.metrics and in_grace != self._market_open_grace_active:
            self.metrics.market_open_grace_active.set(1 if in_grace else 0)
        self._market_open_grace_active = in_grace

        return in_grace

    def _start_quote_watchdog(self) -> None:
        if self._quote_watchdog_running:
            return
        self._quote_watchdog_running = True
        logger.info(
            "Starting quote watchdog",
            interval_s=self._quote_watchdog_interval_s,
            no_data_s=self._quote_no_data_s,
        )

        def _watch() -> None:
            try:
                while self.api and self.logged_in:
                    time.sleep(self._quote_watchdog_interval_s)
                    last = self._last_quote_data_ts
                    if last <= 0:
                        continue
                    gap = timebase.now_s() - last
                    # Use relaxed threshold during market open grace period (C4)
                    threshold = self._quote_no_data_s
                    if self._is_market_open_grace_period():
                        threshold = max(threshold, self._market_open_grace_s)
                    if gap < threshold:
                        continue
                    self._mark_quote_pending("no_data")
                    downgrade_allowed = self._quote_version_mode == "auto" or (
                        self._quote_version_mode == "v1" and not self._quote_version_strict
                    )
                    if downgrade_allowed and self._quote_version == "v1":
                        logger.warning(
                            "No quote data; switching quote version",
                            gap_s=round(gap, 3),
                            to_version="v0",
                        )
                        self._quote_version = "v0"
                        if self.metrics:
                            self.metrics.quote_version_switch_total.labels(direction="downgrade").inc()
                    else:
                        logger.warning("No quote data; re-registering callbacks", gap_s=round(gap, 3))
                    if self.tick_callback:
                        self._callbacks_registered = False
                        self._ensure_callbacks(self.tick_callback)
                        self._resubscribe_all()
                    self._last_quote_data_ts = timebase.now_s()
            finally:
                self._quote_watchdog_running = False

        self._quote_watchdog_thread = threading.Thread(
            target=_watch,
            name="shioaji-quote-watchdog",
            daemon=True,
        )
        self._quote_watchdog_thread.start()

    def _start_callback_retry(self, cb: Callable[..., Any]) -> None:
        if self._callbacks_retrying:
            return
        self._callbacks_retrying = True
        logger.warning("Starting quote callback retry loop")

        def _retry_loop() -> None:
            interval = float(os.getenv("HFT_QUOTE_CB_RETRY_S", "5"))
            while self.api and not self._callbacks_registered:
                ok = self._register_callbacks(cb)
                if ok:
                    logger.info("Quote callbacks registered after retry")
                    break
                logger.warning("Quote callback registration retrying", interval_s=interval)
                time.sleep(interval)
            self._callbacks_retrying = False

        self._callbacks_retry_thread = threading.Thread(
            target=_retry_loop,
            name="shioaji-callback-retry",
            daemon=True,
        )
        self._callbacks_retry_thread.start()

    def _start_event_callback_retry(self) -> None:
        if self._event_callback_retrying:
            return
        self._event_callback_retrying = True
        logger.warning("Starting quote event callback retry loop")

        def _retry_loop() -> None:
            interval = self._event_callback_retry_s
            while self.api and not self._event_callback_registered:
                ok = self._register_event_callback()
                if ok:
                    self._event_callback_registered = True
                    logger.info("Quote event callback registered after retry")
                    break
                logger.warning("Quote event callback registration retrying", interval_s=interval)
                time.sleep(interval)
            self._event_callback_retrying = False

        self._event_callback_retry_thread = threading.Thread(
            target=_retry_loop,
            name="shioaji-event-callback-retry",
            daemon=True,
        )
        self._event_callback_retry_thread.start()

    def _schedule_force_relogin(self) -> None:
        delay = self._quote_force_relogin_s
        if delay <= 0:
            return
        if self._pending_quote_relogining:
            return
        self._pending_quote_relogining = True

        def _relogin_after() -> None:
            try:
                time.sleep(delay)
                if self._pending_quote_resubscribe:
                    logger.warning(
                        "Quote pending too long; forcing reconnect",
                        delay_s=delay,
                    )
                    self.reconnect(reason="quote_pending", force=True)
            finally:
                self._pending_quote_relogining = False

        self._pending_quote_relogin_thread = threading.Thread(
            target=_relogin_after,
            name="shioaji-quote-relogin",
            daemon=True,
        )
        self._pending_quote_relogin_thread.start()

    def _start_forced_relogin(self, reason: str) -> None:
        if self._pending_quote_relogining:
            return
        self._pending_quote_relogining = True

        def _do_relogin() -> None:
            try:
                self.reconnect(reason=reason, force=True)
            finally:
                self._pending_quote_relogining = False

        threading.Thread(
            target=_do_relogin,
            name="shioaji-force-relogin",
            daemon=True,
        ).start()

    def _note_quote_flap(self, now: float) -> None:
        if self._quote_flap_window_s <= 0 or self._quote_flap_threshold <= 0:
            return
        self._quote_flap_events.append(now)
        while self._quote_flap_events and now - self._quote_flap_events[0] > self._quote_flap_window_s:
            self._quote_flap_events.popleft()
        if len(self._quote_flap_events) < self._quote_flap_threshold:
            return
        if now - self._last_quote_flap_relogin_ts < self._quote_flap_cooldown_s:
            return
        self._last_quote_flap_relogin_ts = now
        logger.warning(
            "Quote session flapping; forcing relogin",
            count=len(self._quote_flap_events),
            window_s=self._quote_flap_window_s,
        )
        self._start_forced_relogin("quote_flap")

    def _mark_quote_pending(self, reason: str) -> None:
        now = timebase.now_s()
        if not self._pending_quote_resubscribe or self._pending_quote_reason != reason:
            logger.warning("Quote pending", reason=reason)
        self._pending_quote_resubscribe = True
        self._pending_quote_reason = reason
        self._pending_quote_ts = now
        self._schedule_force_relogin()

    def _clear_quote_pending(self) -> None:
        self._pending_quote_resubscribe = False
        self._pending_quote_reason = None
        self._pending_quote_ts = 0.0
        logger.info("Quote data resumed; clearing pending")

    def _on_quote_event(self, resp_code: int, event_code: int, info: str, event: str) -> None:
        try:
            now = timebase.now_s()
            self._last_quote_event_ts = now
            self._event_callback_registered = True
            if event_code in (1, 2, 3, 4, 12, 13):
                logger.info("Quote event", resp_code=resp_code, event_code=event_code, info=info, event_name=event)
            if event_code == 12:
                self._note_quote_flap(now)
                if self.metrics:
                    self.metrics.shioaji_keepalive_failures_total.inc()
                self._mark_quote_pending("event_12")
                if self.tick_callback:
                    self._callbacks_registered = False
                    self._ensure_callbacks(self.tick_callback)
            elif event_code == 13:
                if self._pending_quote_resubscribe:
                    self._clear_quote_pending()
                if self.tick_callback:
                    self._callbacks_registered = False
                    self._ensure_callbacks(self.tick_callback)
                self._resubscribe_all()
                if self.metrics:
                    self.metrics.feed_resubscribe_total.labels(result="event_13").inc()
            elif event_code == 4:
                if self._pending_quote_resubscribe:
                    self._clear_quote_pending()
                if self.tick_callback:
                    self._callbacks_registered = False
                    self._ensure_callbacks(self.tick_callback)
                self._resubscribe_all()
                if self.metrics:
                    self.metrics.feed_resubscribe_total.labels(result="event_4").inc()
        except Exception as exc:
            logger.error(
                "Quote event handler failed",
                resp_code=resp_code,
                event_code=event_code,
                info=info,
                event_name=event,
                error=str(exc),
            )

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        if not self.api:
            return False
        now = timebase.now_s()
        cooldown = float(os.getenv("HFT_RECONNECT_COOLDOWN", "30"))
        if not force and now - self._last_reconnect_ts < max(cooldown, self._reconnect_backoff_s):
            return False
        if not self._reconnect_lock.acquire(blocking=False):
            return False
        try:
            self._last_reconnect_ts = now
            logger.warning("Reconnecting Shioaji", reason=reason, force=force)
            try:
                self.api.logout()
            except Exception as exc:
                logger.warning("Logout failed during reconnect", error=str(exc))
            self.logged_in = False
            self._callbacks_registered = False
            self._pending_quote_resubscribe = False
            self.subscribed_codes = set()
            self.subscribed_count = 0

            self.login()
            if self.logged_in and self.tick_callback:
                self._ensure_callbacks(self.tick_callback)
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
        self._ensure_callbacks(self.tick_callback)
        now = timebase.now_s()
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
            v = self._get_quote_version()
            if v is None:
                self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.Tick)
                self.api.quote.subscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
            else:
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
            v = self._get_quote_version()
            if v is None:
                self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick)
                self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk)
            else:
                self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.Tick, version=v)
                self.api.quote.unsubscribe(contract, quote_type=sj.constant.QuoteType.BidAsk, version=v)
            self._record_api_latency("unsubscribe", start_ns, ok=True)
        except Exception as e:
            self._record_api_latency("unsubscribe", start_ns, ok=False)
            logger.warning(f"Unsubscribe failed for {code}: {e}")

    def reload_symbols(self) -> None:
        old_map: dict[str, Dict[str, Any]] = {}
        for sym in self.symbols:
            code = sym.get("code")
            if not code:
                continue
            old_map[str(code)] = sym
        self._load_config()
        self.code_exchange_map = {s["code"]: s["exchange"] for s in self.symbols if s.get("code") and s.get("exchange")}

        new_map: dict[str, Dict[str, Any]] = {}
        for sym in self.symbols:
            code = sym.get("code")
            if not code:
                continue
            new_map[str(code)] = sym
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

    def _wrapped_tick_cb(self, *args, **kwargs):
        """Persistent callback wrapper."""
        try:
            if hasattr(self, "tick_callback") and self.tick_callback:
                self.tick_callback(*args, **kwargs)
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
        raw_code = str(code or "").strip().upper()

        if prod in {"index", "idx"} or exch in {"IDX", "INDEX"}:
            idx_exch = exch if exch in {"TSE", "OTC"} else self.index_exchange
            idx_group = getattr(self.api.Contracts.Indexs, idx_exch, None)
            return self._lookup_contract(
                idx_group, code, allow_symbol_fallback=self.allow_symbol_fallback, label="index"
            )

        if prod in {"stock", "stk"} or exch in {"TSE", "OTC", "OES"}:
            stocks = getattr(self.api.Contracts, "Stocks", None)
            tse_group = getattr(stocks, "TSE", None) if stocks is not None else None
            otc_group = getattr(stocks, "OTC", None) if stocks is not None else None
            oes_group = getattr(stocks, "OES", None) if stocks is not None else None
            if isinstance(stocks, dict):
                tse_group = stocks.get("TSE", tse_group)
                otc_group = stocks.get("OTC", otc_group)
                oes_group = stocks.get("OES", oes_group)

            if exch == "TSE" and tse_group is not None:
                return self._lookup_contract(
                    tse_group,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OTC" and otc_group is not None:
                return self._lookup_contract(
                    otc_group,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
            if exch == "OES" and oes_group is not None:
                return self._lookup_contract(
                    oes_group,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )

            for group in (tse_group, otc_group, oes_group):
                if group is None:
                    continue
                contract = self._lookup_contract(
                    group,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )
                if contract:
                    return contract

            if stocks is not None:
                return self._lookup_contract(
                    stocks,
                    code,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="stock",
                )

        if prod in {"future", "futures"} or exch in {"FUT", "FUTURES", "TAIFEX"}:
            for candidate in self._expand_future_codes(raw_code):
                contract = self._lookup_contract(
                    self.api.Contracts.Futures,
                    candidate,
                    allow_symbol_fallback=self.allow_symbol_fallback,
                    label="future",
                )
                if contract:
                    return contract

        if prod in {"option", "options"} or exch in {"OPT", "OPTIONS"}:
            contract = self._lookup_contract(
                self.api.Contracts.Options,
                raw_code,
                allow_symbol_fallback=self.allow_symbol_fallback,
                label="option",
            )
            if contract:
                return contract

        if allow_synthetic and sj:
            return self._build_synthetic_contract(exch, raw_code)

        return None

    def _expand_future_codes(self, code: str) -> list[str]:
        """Expand legacy futures month codes (e.g., TXFD6) to YYYYMM form (TXF202604)."""
        code = str(code or "").strip().upper()
        if not code:
            return []
        candidates = [code]
        # Legacy format: ROOT + month_code + year_digit (e.g., TXFD6)
        if len(code) >= 5:
            month_code = code[-2]
            year_digit = code[-1]
            month_map = {
                "A": "01",
                "B": "02",
                "C": "03",
                "D": "04",
                "E": "05",
                "F": "06",
                "G": "07",
                "H": "08",
                "I": "09",
                "J": "10",
                "K": "11",
                "L": "12",
            }
            if year_digit.isdigit() and month_code in month_map:
                root = code[:-2]
                year = self._resolve_year_from_digit(int(year_digit))
                alt = f"{root}{year}{month_map[month_code]}"
                if alt not in candidates:
                    candidates.append(alt)
        return candidates

    def _resolve_year_from_digit(self, digit: int) -> int:
        now_year = dt.datetime.now(timebase.TZINFO).year
        base = (now_year // 10) * 10 + digit
        # If the computed year is too far in the past, roll to next decade.
        if base < now_year - 1:
            base += 10
        return base

    def _lookup_contract(self, container: Any, code: str, allow_symbol_fallback: bool, label: str) -> Any | None:
        if not container:
            return None

        try:
            return container[code]
        except Exception as exc:
            logger.debug("Direct contract lookup failed", code=code, label=label, error=str(exc))

        def iter_contracts(value: Any):
            iterable = value.values() if isinstance(value, dict) else value
            for item in iterable:
                yield item
                try:
                    if hasattr(item, "__iter__") and not hasattr(item, "code"):
                        for sub in item:
                            yield sub
                except Exception as exc:
                    logger.debug("Error iterating contract sub-items", error=str(exc))
                    continue

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "code", None) == code:
                    return contract
        except Exception as exc:
            logger.warning("Error searching contracts by code", code=code, label=label, error=str(exc))
            return None

        if not allow_symbol_fallback:
            return None

        try:
            for contract in iter_contracts(container):
                if getattr(contract, "symbol", None) == code:
                    logger.warning("Symbol fallback used for contract", code=code, type=label)
                    return contract
        except Exception as exc:
            logger.warning("Error searching contracts by symbol fallback", code=code, label=label, error=str(exc))
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
            return {"seq_no": f"sim-{int(timebase.now_s() * 1000)}"}

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
        if fallback_cls is None:
            raise RuntimeError("Shioaji Order class unavailable")

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
