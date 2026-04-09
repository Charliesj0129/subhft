"""QuoteConnectionPool — manages multiple ShioajiClient sessions for quote subscriptions.

Each client owns an independent sj.Shioaji() session with its own watchdog,
reconnect orchestrator, and subscription tracking. All clients share the same
callback function, funneling data into a single raw_queue.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from typing import Any, Callable

import yaml
from structlog import get_logger

from hft_platform.feed_adapter.shioaji.facade_slot import FacadeSlot, FacadeState
from hft_platform.feed_adapter.shioaji.pool_health import (
    check_facade_health,
    get_healthy_feed_gap_s,
)

try:
    from prometheus_client import Gauge
except ImportError:
    Gauge = None

try:
    from hft_platform.feed_adapter.shioaji.facade import ShioajiClientFacade
except ImportError:  # pragma: no cover
    ShioajiClientFacade = None  # type: ignore[assignment,misc]

logger = get_logger("feed_adapter.quote_connection_pool")

_METRIC_SUBSCRIBED = None
_METRIC_LOGGED_IN = None
_METRIC_LAST_DATA_AGE = None
_METRIC_CONN_STATE = None


def _ensure_metrics() -> None:
    global _METRIC_SUBSCRIBED, _METRIC_LOGGED_IN, _METRIC_LAST_DATA_AGE, _METRIC_CONN_STATE
    if Gauge is None or _METRIC_SUBSCRIBED is not None:
        return
    _METRIC_SUBSCRIBED = Gauge(
        "hft_quote_conn_subscribed_count",
        "Subscribed symbol count per quote connection",
        ["conn_id"],
    )
    _METRIC_LOGGED_IN = Gauge(
        "hft_quote_conn_logged_in",
        "Login state per quote connection",
        ["conn_id"],
    )
    _METRIC_LAST_DATA_AGE = Gauge(
        "hft_quote_conn_last_data_age_s",
        "Seconds since last quote data per connection",
        ["conn_id"],
    )
    _METRIC_CONN_STATE = Gauge(
        "hft_quote_conn_state",
        "Connection state per quote connection (0=connected, 1=degraded, 2=recovering, 3=disconnected)",
        ["conn_id"],
    )


_SHIOAJI_MAX_CONNECTIONS = 5
_MAX_QUOTE_CONNECTIONS = _SHIOAJI_MAX_CONNECTIONS - 1
# Shioaji SDK actual limit: ~256 topics per connection.
# Each symbol subscribes to Tick + BidAsk = 2 topics, so max ~128 symbols.
# Use 120 as conservative safety margin.
_MAX_SUBSCRIPTIONS_PER_CONN = 120


class QuoteConnectionPool:
    """Manages multiple ShioajiClientFacade instances for quote subscriptions.

    Duck-types as a single ShioajiClientFacade for MarketDataService compatibility.
    """

    __slots__ = (
        "_clients",
        "_shard_dir",
        "_shard_paths",
        "_num_conns",
        "_config",
        "_all_symbols",
        "_login_interval_s",
        "_options_expiry",
        "_options_refresh_running",
        "_refresh_lock",
        "_refresh_thread",
        "_refresh_stop_event",
        "_slots",
        "_lob",
        "_feature_engine",
        "_degraded_threshold_s",
        "_reconnect_trigger_s",
        "_per_facade_timeout_s",
        "_user_callback",
    )

    def __init__(self, symbols_path: str, shioaji_cfg: dict[str, Any], num_conns: int) -> None:
        if num_conns + 1 > _SHIOAJI_MAX_CONNECTIONS:
            raise ValueError(
                f"Total connections {num_conns + 1} (quote={num_conns} + order=1) "
                f"exceeds Shioaji limit of {_SHIOAJI_MAX_CONNECTIONS}"
            )
        if num_conns > _MAX_QUOTE_CONNECTIONS:
            raise ValueError(f"num_conns={num_conns} exceeds max quote connections {_MAX_QUOTE_CONNECTIONS}")

        self._num_conns = num_conns
        self._config = shioaji_cfg
        self._login_interval_s = float(os.getenv("HFT_QUOTE_LOGIN_INTERVAL_S", "2"))
        self._options_expiry: str | None = None
        self._options_refresh_running: bool = False
        self._refresh_lock: threading.Lock = threading.Lock()
        self._refresh_thread: threading.Thread | None = None
        self._refresh_stop_event: threading.Event = threading.Event()
        self._slots: list[FacadeSlot] = []
        self._lob: Any = None
        self._feature_engine: Any = None
        self._degraded_threshold_s = float(os.getenv("HFT_FACADE_DEGRADED_THRESHOLD_S", "3"))
        self._reconnect_trigger_s = float(os.getenv("HFT_FACADE_RECONNECT_TRIGGER_S", "10"))
        self._user_callback: Callable[..., Any] | None = None
        self._per_facade_timeout_s = float(os.getenv("HFT_PER_FACADE_TIMEOUT_S", "15"))

        with open(symbols_path, "r") as f:
            data = yaml.safe_load(f) or {}
        self._all_symbols: list[dict[str, Any]] = data.get("symbols", [])

        groups: dict[int, list[dict[str, Any]]] = {i: [] for i in range(num_conns)}
        for sym in self._all_symbols:
            g = sym.get("group", 0)
            if not isinstance(g, int) or g < 0 or g >= num_conns:
                raise ValueError(
                    f"Symbol {sym.get('code', '?')} has group={g} "
                    f"but only {num_conns} connections configured (valid: 0..{num_conns - 1})"
                )
            groups[g].append(sym)

        for g, syms in groups.items():
            if len(syms) > _MAX_SUBSCRIPTIONS_PER_CONN:
                raise ValueError(f"Group {g} has {len(syms)} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN} limit")
            if not syms:
                logger.warning("Empty symbol group", group=g, num_conns=num_conns)

        self._shard_dir = tempfile.mkdtemp(prefix="hft_quote_pool_")
        self._shard_paths: list[str] = []
        self._clients: list[Any] = []

        for group_id in range(num_conns):
            shard_path = os.path.join(self._shard_dir, f"symbols_group_{group_id}.yaml")
            with open(shard_path, "w") as f:
                yaml.safe_dump({"symbols": groups[group_id]}, f, sort_keys=False)
            self._shard_paths.append(shard_path)

        logger.info(
            "QuoteConnectionPool initialized",
            num_conns=num_conns,
            groups={g: len(s) for g, s in groups.items()},
            shard_dir=self._shard_dir,
        )

    @property
    def num_conns(self) -> int:
        return self._num_conns

    def cleanup_shards(self) -> None:
        if self._shard_dir and os.path.isdir(self._shard_dir):
            shutil.rmtree(self._shard_dir, ignore_errors=True)
            self._shard_dir = ""

    def create_facades(self) -> None:
        """Create a ShioajiClientFacade and FacadeSlot for each connection group.

        Only the first facade fetches the full contract universe (~55k contracts).
        Subsequent facades skip contract download to prevent 3x memory duplication.
        """
        self._clients = []
        self._slots = []
        for group_id in range(self._num_conns):
            per_conn_cfg = dict(self._config)
            per_conn_cfg["session_lock_suffix"] = f"_conn{group_id}"
            # Only first facade downloads contracts; others skip to save ~27MB each
            if group_id > 0:
                per_conn_cfg["fetch_contract"] = "0"
            facade = ShioajiClientFacade(
                config_path=self._shard_paths[group_id],
                shioaji_config=per_conn_cfg,
            )
            self._clients.append(facade)

            # Read shard YAML to extract symbol codes for the slot
            shard_path = self._shard_paths[group_id]
            symbol_codes: set[str] = set()
            try:
                with open(shard_path, "r") as f:
                    shard_data = yaml.safe_load(f) or {}
                for sym in shard_data.get("symbols", []):
                    code = sym.get("code")
                    if code:
                        symbol_codes.add(str(code))
            except Exception as exc:
                logger.warning("facade_slot_symbol_read_failed", conn_id=group_id, error=str(exc))

            slot = FacadeSlot(conn_id=str(group_id), facade=facade)
            slot.symbols = symbol_codes
            self._slots.append(slot)
            logger.info("Created facade for group", conn_id=group_id, symbols=len(symbol_codes))

    def login_all(self) -> None:
        """Sequentially login each connection with a configurable interval."""
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            try:
                ok = facade.login()
                if ok:
                    log.info("Connection logged in")
                else:
                    log.error("Connection login failed")
            except Exception as exc:
                log.error("Connection login exception", error=str(exc))
            if i < len(self._clients) - 1 and self._login_interval_s > 0:
                time.sleep(self._login_interval_s)

    def login(self, *args: Any, **kwargs: Any) -> bool:
        self.login_all()
        return self.partial_login

    @staticmethod
    def _make_callback_wrapper(slot: FacadeSlot, original_cb: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap a market data callback to update the slot's last_data_mono timestamp."""
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            slot.last_data_mono = time.monotonic()
            return original_cb(*args, **kwargs)
        return wrapper

    def subscribe_all(self, cb: Callable[..., Any]) -> None:
        """Subscribe each logged-in connection's symbol basket.

        Wraps the callback to update each slot's ``last_data_mono`` timestamp
        on every market data event, enabling per-facade feed-gap detection.
        After successful subscription, transitions slot from RECOVERING to CONNECTED.
        """
        self._user_callback = cb
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            if not facade.logged_in:
                log.warning("Skipping subscribe for unconnected facade")
                continue
            slot = self._slots[i] if i < len(self._slots) else None

            try:
                wrapped_cb = self._make_callback_wrapper(slot, cb) if slot is not None else cb
                facade.subscribe_basket(wrapped_cb)
                if slot is not None:
                    slot.state = FacadeState.CONNECTED
                    slot.last_data_mono = time.monotonic()
                log.info("Subscribed", count=facade.subscribed_count)
            except Exception as exc:
                log.error("Subscribe failed", error=str(exc))

        # Auto-start options expiry refresh if enabled
        if os.getenv("HFT_OPTIONS_AUTO_REFRESH", "1").lower() not in {"0", "false", "no", "off"}:
            self.start_options_refresh_thread(cb=cb)

    def subscribe_basket(self, cb: Callable[..., Any]) -> None:
        """Duck-type alias for MarketDataService compatibility."""
        self.subscribe_all(cb)

    def reconnect(self, reason: str = "", force: bool = False) -> bool:
        """Reconnect non-CONNECTED facades. Duck-type for MarketDataService compatibility.

        When ``force=True``, all facades are reconnected regardless of state.
        Otherwise, only facades not in CONNECTED state are targeted.
        Returns True if at least one facade reconnected successfully.

        Note: warmup resets are deferred via ``_pending_warmup_reset`` and applied
        on the event loop thread by ``_apply_pending_resets()`` to avoid thread-safety
        issues with LOBEngine.books and FeatureEngine dicts (C2 fix).
        """
        targets = [s for s in self._slots if force or s.state != FacadeState.CONNECTED]
        if not targets:
            return True
        any_ok = False
        for slot in targets:
            slot.state = FacadeState.RECOVERING
            slot.last_reconnect_mono = time.monotonic()
            log = logger.bind(conn_id=slot.conn_id)
            try:
                ok = slot.facade.reconnect(reason=reason, force=force)
                if ok:
                    slot.state = FacadeState.CONNECTED
                    slot.reconnect_failures = 0
                    slot.last_data_mono = time.monotonic()
                    slot._pending_warmup_reset = True
                    any_ok = True
                    log.info("facade_reconnected")
                else:
                    slot.reconnect_failures += 1
                    slot.state = FacadeState.DISCONNECTED
                    log.warning("facade_reconnect_failed")
            except Exception as exc:
                slot.reconnect_failures += 1
                slot.state = FacadeState.DISCONNECTED
                log.error("facade_reconnect_exception", error=str(exc))
        return any_ok

    def set_reset_targets(self, lob: Any, feature_engine: Any) -> None:
        """Register LOB and FeatureEngine instances for per-facade warmup resets."""
        self._lob = lob
        self._feature_engine = feature_engine

    def get_healthy_feed_gap_s(self) -> float:
        """Return the maximum feed gap across all CONNECTED facades."""
        return get_healthy_feed_gap_s(self._slots)

    def check_facade_health(self) -> None:
        """Evaluate per-facade health and drive FSM state transitions.

        Also applies any pending warmup resets from background reconnect threads.
        This method runs on the event loop thread (called from supervisor), so
        LOB/FE mutations are safe here.
        """
        self._apply_pending_resets()
        check_facade_health(
            self._slots,
            degraded_threshold_s=self._degraded_threshold_s,
            reconnect_trigger_s=self._reconnect_trigger_s,
            schedule_fn=self._schedule_reconnect,
        )

    def _apply_pending_resets(self) -> None:
        """Apply deferred warmup resets on the event loop thread.

        Must be called from the event loop thread to ensure thread-safe
        mutation of LOBEngine.books and FeatureEngine state dicts.
        """
        for slot in self._slots:
            if slot._pending_warmup_reset:
                slot._pending_warmup_reset = False
                self._notify_warmup_reset(slot.conn_id)

    def _schedule_reconnect(self, conn_id: str) -> None:
        """Schedule a reconnect for the given connection slot.

        Spawns a daemon thread that performs the actual reconnect for this
        single facade. On success, marks the slot for pending warmup reset
        (applied on the event loop by ``_apply_pending_resets``).
        """
        slot: FacadeSlot | None = None
        for s in self._slots:
            if s.conn_id == conn_id:
                slot = s
                break
        if slot is None:
            logger.warning("schedule_reconnect_unknown_conn_id", conn_id=conn_id)
            return
        if slot.state == FacadeState.RECOVERING:
            return
        slot.state = FacadeState.RECOVERING
        slot.last_reconnect_mono = time.monotonic()
        logger.warning("facade_reconnect_scheduled", conn_id=conn_id)

        def _do_reconnect() -> None:
            log = logger.bind(conn_id=conn_id)
            try:
                ok = slot.facade.reconnect(reason="health_check", force=False)
                if ok:
                    slot.state = FacadeState.CONNECTED
                    slot.reconnect_failures = 0
                    slot.last_data_mono = time.monotonic()
                    slot._pending_warmup_reset = True
                    log.info("facade_reconnected_via_health_check")
                else:
                    slot.reconnect_failures += 1
                    slot.state = FacadeState.DISCONNECTED
                    log.warning("facade_health_reconnect_failed")
            except Exception as exc:
                slot.reconnect_failures += 1
                slot.state = FacadeState.DISCONNECTED
                log.error("facade_health_reconnect_exception", error=str(exc))

        t = threading.Thread(
            target=_do_reconnect,
            name=f"facade-reconnect-{conn_id}",
            daemon=True,
        )
        t.start()

    def _notify_warmup_reset(self, conn_id: str) -> None:
        """Reset LOB books and feature engine state for symbols on a reconnected facade."""
        slot: FacadeSlot | None = None
        for s in self._slots:
            if s.conn_id == conn_id:
                slot = s
                break
        if slot is None:
            return
        symbols = slot.symbols
        if self._lob is not None and hasattr(self._lob, "reset_books_for_symbols"):
            self._lob.reset_books_for_symbols(symbols)
        if self._feature_engine is not None and hasattr(self._feature_engine, "reset_symbols"):
            self._feature_engine.reset_symbols(symbols)
        logger.info("facade_warmup_reset", conn_id=conn_id, symbols=len(symbols))

    def resubscribe(self) -> bool:
        """Resubscribe all connections. Duck-type for MarketDataService compatibility."""
        success = True
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            if not facade.logged_in:
                log.warning("Skipping resubscribe for unconnected facade")
                continue
            try:
                ok = facade.resubscribe()
                if ok:
                    log.info("Connection resubscribed")
                else:
                    log.warning("Connection resubscribe returned False")
                    success = False
            except Exception as exc:
                log.error("Connection resubscribe failed", error=str(exc))
                success = False
        return success

    def fetch_snapshots(self) -> list[Any]:
        """Fetch snapshots from all connections. Duck-type for MarketDataService compatibility."""
        result: list[Any] = []
        for i, facade in enumerate(self._clients):
            if not facade.logged_in:
                continue
            try:
                result.extend(facade.fetch_snapshots())
            except Exception as exc:
                logger.bind(conn_id=i).error("Fetch snapshots failed", error=str(exc))
        return result

    def reload_symbols(self) -> None:
        """Reload symbols on all connections. Duck-type for MarketDataService compatibility."""
        for i, facade in enumerate(self._clients):
            try:
                facade.reload_symbols()
            except Exception as exc:
                logger.bind(conn_id=i).error("Reload symbols failed", error=str(exc))

    def validate_symbols(self) -> list[str]:
        """Return merged list of invalid symbols across all logged-in connections.

        Duck-type for MarketDataService compatibility (mirrors ShioajiClientFacade).
        """
        result: list[str] = []
        for i, facade in enumerate(self._clients):
            if not facade.logged_in:
                continue
            try:
                result.extend(facade.validate_symbols())
            except Exception as exc:
                logger.bind(conn_id=i).error("Validate symbols failed", error=str(exc))
        return result

    def logout(self) -> None:
        """Logout and close all connections."""
        for i, facade in enumerate(self._clients):
            try:
                facade.close(logout=True)
                logger.bind(conn_id=i).info("Connection closed")
            except Exception as exc:
                logger.bind(conn_id=i).error("Close failed", error=str(exc))
        self.cleanup_shards()

    def close(self, logout: bool = False) -> None:
        self.stop_options_refresh()
        if logout:
            self.logout()
        else:
            for facade in self._clients:
                try:
                    facade.close(logout=False)
                except Exception:
                    pass
            self.cleanup_shards()

    def shutdown(self, logout: bool = False) -> None:
        self.close(logout=logout)

    def get_client(self, group: int) -> Any:
        if 0 <= group < len(self._clients):
            return self._clients[group]
        raise ValueError(f"Invalid group {group}, valid: 0..{len(self._clients) - 1}")

    @property
    def logged_in(self) -> bool:
        return bool(self._clients) and all(c.logged_in for c in self._clients)

    @property
    def partial_login(self) -> bool:
        return any(c.logged_in for c in self._clients)

    @property
    def subscribed_count(self) -> int:
        return sum(getattr(c, "subscribed_count", 0) for c in self._clients)

    @property
    def mode(self) -> str:
        if self._clients:
            return self._clients[0]._client.mode
        return "unknown"

    @property
    def symbols(self) -> list[dict[str, Any]]:
        with self._refresh_lock:
            result: list[dict[str, Any]] = []
            for c in self._clients:
                result.extend(c._client.symbols)
            return result

    def health(self) -> dict[int, dict[str, Any]]:
        return {
            i: {
                "logged_in": c.logged_in,
                "subscribed_count": getattr(c, "subscribed_count", 0),
                "last_quote_ts": getattr(c._client, "_last_quote_data_ts", 0.0),
            }
            for i, c in enumerate(self._clients)
        }

    def update_metrics(self) -> None:
        """Push per-connection metrics to Prometheus gauges."""
        _ensure_metrics()
        now = time.monotonic()
        for slot in self._slots:
            cid = str(slot.conn_id)
            if _METRIC_SUBSCRIBED is not None:
                _METRIC_SUBSCRIBED.labels(conn_id=cid).set(
                    getattr(slot.facade, "subscribed_count", 0)
                )
            if _METRIC_LOGGED_IN is not None:
                _METRIC_LOGGED_IN.labels(conn_id=cid).set(1 if slot.facade.logged_in else 0)
            if _METRIC_LAST_DATA_AGE is not None:
                _METRIC_LAST_DATA_AGE.labels(conn_id=cid).set(now - slot.last_data_mono)
            if _METRIC_CONN_STATE is not None:
                _METRIC_CONN_STATE.labels(conn_id=cid).set(int(slot.state))

    # ── Options auto-refresh ─────────────────────────────────────────────

    def _load_options_from_cache(self) -> list[dict[str, Any]]:
        """Load TXO option contracts from the local contract cache.

        Falls back to live Shioaji API if cache is empty or stale.
        """
        cache_path = os.getenv("HFT_CONTRACT_CACHE_PATH", "config/contracts.json")
        try:
            import json

            with open(cache_path) as f:
                data = json.load(f)
            contracts = data.get("contracts", [])
            opts = [c for c in contracts if c.get("type") == "option" and c.get("root") == "TXO"]
            if opts:
                logger.debug("options_loaded_from_cache", count=len(opts))
                return opts
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

        # Fallback: query Shioaji API directly
        for c in self._clients:
            if not c.logged_in or not getattr(c._client, "api", None):
                continue
            try:
                raw = list(c._client.api.Contracts.Options.TXO)
                return [
                    {
                        "code": getattr(o, "code", ""),
                        "delivery_date": getattr(o, "delivery_date", ""),
                        "strike": getattr(o, "strike_price", None),
                        "right": getattr(o.option_right, "value", None),
                        "reference": getattr(o, "reference", None),
                    }
                    for o in raw
                ]
            except Exception as exc:
                logger.warning("options_api_fallback_failed", error=str(exc))
        return []

    def refresh_options_symbols(self, cb: Callable[..., Any] | None = None) -> bool:
        """Regenerate the options YAML if the nearest TXO expiry has changed.

        Uses ``self._user_callback`` (set by ``subscribe_all``) for resubscription.
        The ``cb`` parameter is accepted for backward compatibility but ignored
        in favor of the instance-level callback to avoid stale closure captures.

        Reads from contract cache first (no API call needed), falls back
        to live API.  Uses ``reference`` price to compute ATM and filters
        strikes to ATM ± ``HFT_OPTIONS_STRIKE_RANGE`` (default: all).

        Returns True if symbols were updated and resubscribed.
        """
        if not self._clients:
            return False

        with self._refresh_lock:
            opts = self._load_options_from_cache()
            if not opts:
                logger.warning("options_refresh_no_contracts")
                return False

            # Find nearest expiry
            dates = sorted(set(str(c.get("delivery_date", "")) for c in opts if c.get("delivery_date")))
            if not dates:
                return False
            nearest_date = dates[0]

            if self._options_expiry == nearest_date:
                logger.debug("options_refresh_no_change", expiry=nearest_date)
                return False

            logger.info(
                "options_expiry_changed",
                old_expiry=self._options_expiry,
                new_expiry=nearest_date,
            )

            nearest = [c for c in opts if str(c.get("delivery_date", "")) == nearest_date]

            # Optional ATM filtering via HFT_OPTIONS_STRIKE_RANGE
            strike_range = int(os.getenv("HFT_OPTIONS_STRIKE_RANGE", "0"))  # 0 = all
            if strike_range > 0:
                # Find ATM from reference prices
                refs = [float(c["reference"]) for c in nearest if c.get("reference")]
                if refs:
                    atm = sum(refs) / len(refs)
                    strikes = sorted(set(float(c["strike"]) for c in nearest if c.get("strike")))
                    if strikes:
                        atm_idx = min(range(len(strikes)), key=lambda i: abs(strikes[i] - atm))
                        lo = max(atm_idx - strike_range, 0)
                        hi = min(atm_idx + strike_range, len(strikes) - 1)
                        allowed = set(strikes[lo : hi + 1])
                        nearest = [c for c in nearest if c.get("strike") is not None and float(c["strike"]) in allowed]
                        logger.info(
                            "options_strike_filter",
                            atm=atm,
                            range=strike_range,
                            strikes_before=len(strikes),
                            strikes_after=len(allowed),
                        )

            calls = sorted(
                [c for c in nearest if c.get("right") == "C"],
                key=lambda c: float(c.get("strike", 0)),
            )
            puts = sorted(
                [c for c in nearest if c.get("right") == "P"],
                key=lambda c: float(c.get("strike", 0)),
            )

            # Preserve group 0 (non-option symbols)
            base_symbols = [s for s in self._all_symbols if s.get("group", 0) == 0]
            symbols: list[dict[str, Any]] = []
            for s in base_symbols:
                entry = dict(s)
                entry["group"] = 0
                symbols.append(entry)

            # Round-robin options across all non-zero groups by strike.
            # Interleave call/put pairs per strike for balanced coverage.
            option_groups = list(range(1, self._num_conns))
            if not option_groups:
                option_groups = [0]  # fallback: single connection
            all_options_by_strike: list[dict[str, Any]] = []
            # Use string keys to avoid float equality hazards (safe: TAIFEX strikes are integers)
            calls_by_strike: dict[str, list[dict[str, Any]]] = {}
            for opt in calls:
                k = str(opt.get("strike", "0"))
                calls_by_strike.setdefault(k, []).append(opt)
            puts_by_strike: dict[str, list[dict[str, Any]]] = {}
            for opt in puts:
                k = str(opt.get("strike", "0"))
                puts_by_strike.setdefault(k, []).append(opt)
            all_strike_keys = set(calls_by_strike.keys()) | set(puts_by_strike.keys())
            strike_keys = sorted(all_strike_keys, key=lambda s: float(s))
            for sk in strike_keys:
                all_options_by_strike.extend(calls_by_strike.get(sk, []))
                all_options_by_strike.extend(puts_by_strike.get(sk, []))
            group_counts: dict[int, int] = {g: 0 for g in option_groups}
            for i, opt in enumerate(all_options_by_strike):
                g = option_groups[i % len(option_groups)]
                symbols.append({"code": opt["code"], "exchange": "OPT", "group": g})
                group_counts[g] = group_counts.get(g, 0) + 1

            # Validate connection limits — auto-trim if overflow
            overflow = False
            for g in range(self._num_conns):
                count = sum(1 for s in symbols if s.get("group") == g)
                if count > _MAX_SUBSCRIPTIONS_PER_CONN:
                    overflow = True
                    break

            if overflow:
                # Auto-trim: compute max options that fit, then re-slice from ATM outward
                n_option_groups = max(len(option_groups), 1)
                max_options_total = n_option_groups * _MAX_SUBSCRIPTIONS_PER_CONN
                n_strikes_total = len(strike_keys)
                # Each strike produces up to 2 options (call + put)
                max_strikes = max_options_total // 2
                if max_strikes < n_strikes_total and max_strikes > 0:
                    # Find ATM index among sorted strikes
                    refs = [float(c["reference"]) for c in nearest if c.get("reference")]
                    atm = sum(refs) / len(refs) if refs else float(strike_keys[len(strike_keys) // 2])
                    atm_idx = min(range(n_strikes_total), key=lambda i: abs(float(strike_keys[i]) - atm))
                    half = max_strikes // 2
                    lo = max(atm_idx - half, 0)
                    hi = min(lo + max_strikes, n_strikes_total)
                    if hi == n_strikes_total:
                        lo = max(n_strikes_total - max_strikes, 0)
                    trimmed_keys = set(strike_keys[lo:hi])

                    logger.warning(
                        "options_auto_trim",
                        original_strikes=n_strikes_total,
                        trimmed_strikes=len(trimmed_keys),
                        max_per_group=_MAX_SUBSCRIPTIONS_PER_CONN,
                        n_option_groups=n_option_groups,
                        atm=atm,
                    )

                    # Rebuild options list with trimmed strikes
                    all_options_by_strike = []
                    for sk in strike_keys:
                        if sk in trimmed_keys:
                            all_options_by_strike.extend(calls_by_strike.get(sk, []))
                            all_options_by_strike.extend(puts_by_strike.get(sk, []))

                    # Rebuild symbols with round-robin
                    symbols = [s for s in symbols if s.get("exchange") != "OPT"]
                    group_counts = {g: 0 for g in option_groups}
                    for i, opt in enumerate(all_options_by_strike):
                        g = option_groups[i % len(option_groups)]
                        symbols.append({"code": opt["code"], "exchange": "OPT", "group": g})
                        group_counts[g] = group_counts.get(g, 0) + 1

                    # Final validation after trim
                    for g in range(self._num_conns):
                        count = sum(1 for s in symbols if s.get("group") == g)
                        if count > _MAX_SUBSCRIPTIONS_PER_CONN:
                            logger.error(
                                "options_refresh_group_overflow_after_trim",
                                group=g,
                                count=count,
                                limit=_MAX_SUBSCRIPTIONS_PER_CONN,
                            )
                            return False

            total_options = len(all_options_by_strike)
            out_path = os.getenv("SYMBOLS_CONFIG", "data/live_with_options.yaml")
            try:
                with open(out_path, "w") as f:
                    f.write("# Auto-refreshed by QuoteConnectionPool\n")
                    f.write(f"# TXO nearest expiry: {nearest_date}\n")
                    f.write(f"# Group 0: Base ({len(base_symbols)})\n")
                    for g in option_groups:
                        f.write(f"# Group {g}: TXO Options ({group_counts.get(g, 0)})\n")
                    f.write(
                        f"# Total options: {total_options} across"
                        f" {len(option_groups)} groups (round-robin by strike)\n\n"
                    )
                    yaml.dump({"symbols": symbols}, f, default_flow_style=False, allow_unicode=True)
            except Exception as exc:
                logger.error("options_refresh_write_failed", error=str(exc))
                return False

            self._options_expiry = nearest_date
            self._all_symbols = symbols

            # Rebuild shard files and reload each connection's config
            groups: dict[int, list[dict[str, Any]]] = {i: [] for i in range(self._num_conns)}
            for sym in symbols:
                g = sym.get("group", 0)
                if 0 <= g < self._num_conns:
                    groups[g].append(sym)

            for group_id in range(self._num_conns):
                shard_path = self._shard_paths[group_id] if group_id < len(self._shard_paths) else None
                if shard_path:
                    with open(shard_path, "w") as f:
                        yaml.safe_dump({"symbols": groups[group_id]}, f, sort_keys=False)

            # Update FacadeSlot symbol sets to reflect new option symbols
            for group_id in range(self._num_conns):
                if group_id < len(self._slots):
                    new_codes = {str(s.get("code", "")) for s in groups.get(group_id, []) if s.get("code")}
                    self._slots[group_id].symbols = new_codes

            # Reload config and resubscribe each connection (with callback wrapper).
            # Unsubscribe old symbols first to prevent broker-side subscription leak.
            active_cb = self._user_callback
            resubscribed = 0
            for i, facade in enumerate(self._clients):
                try:
                    # Unsubscribe old symbols before reloading config
                    if facade.logged_in:
                        sub_mgr = facade._client._subscriptions()
                        old_codes = set(facade._client.subscribed_codes)
                        for sym in facade._client.symbols:
                            code = sym.get("code")
                            if code and code in old_codes:
                                try:
                                    sub_mgr._unsubscribe_symbol(sym)
                                except Exception:
                                    pass
                    facade._client._load_config()
                    if facade.logged_in and active_cb is not None:
                        slot = self._slots[i] if i < len(self._slots) else None
                        wrapped_cb = self._make_callback_wrapper(slot, active_cb) if slot is not None else active_cb
                        facade.subscribe_basket(wrapped_cb)
                        if slot is not None:
                            slot.last_data_mono = time.monotonic()
                        resubscribed += 1
                except Exception as exc:
                    logger.error("options_refresh_reload_failed", conn_id=i, error=str(exc))

            logger.info(
                "options_refresh_complete",
                expiry=nearest_date,
                total_symbols=len(symbols),
                calls=len(calls),
                puts=len(puts),
                resubscribed=resubscribed,
            )
            return True

    def start_options_refresh_thread(
        self,
        cb: Callable[..., Any] | None = None,
        interval_s: float | None = None,
    ) -> None:
        """Start a background thread that checks for TXO expiry changes.

        Default interval: HFT_OPTIONS_REFRESH_S env var or 3600 (hourly).
        """
        if getattr(self, "_options_refresh_running", False):
            return
        if interval_s is None:
            interval_s = float(os.getenv("HFT_OPTIONS_REFRESH_S", "3600"))

        self._options_refresh_running = True
        self._refresh_stop_event.clear()

        def _loop() -> None:
            # Initial refresh on startup — wait for login to complete
            if self._refresh_stop_event.wait(timeout=30):
                return  # stop requested during initial wait
            try:
                self.refresh_options_symbols()
            except Exception as exc:
                logger.warning("options_refresh_initial_failed", error=str(exc))

            next_check = time.monotonic() + interval_s
            while not self._refresh_stop_event.is_set():
                # Sleep in short increments via event wait for responsive shutdown
                self._refresh_stop_event.wait(timeout=60)
                if self._refresh_stop_event.is_set():
                    break
                if time.monotonic() >= next_check:
                    try:
                        self.refresh_options_symbols()
                    except Exception as exc:
                        logger.warning("options_refresh_failed", error=str(exc))
                    next_check = time.monotonic() + interval_s

        t = threading.Thread(target=_loop, name="options-expiry-refresh", daemon=True)
        t.start()
        self._refresh_thread = t
        logger.info("options_refresh_thread_started", interval_s=interval_s)

    def stop_options_refresh(self) -> None:
        self._options_refresh_running = False
        self._refresh_stop_event.set()
        t = self._refresh_thread
        if t is not None:
            t.join(timeout=10)
            self._refresh_thread = None
