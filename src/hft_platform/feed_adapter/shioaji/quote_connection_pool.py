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
from pathlib import Path
from typing import Any, Callable

import yaml
from structlog import get_logger

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


def _ensure_metrics() -> None:
    global _METRIC_SUBSCRIBED, _METRIC_LOGGED_IN, _METRIC_LAST_DATA_AGE
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


_SHIOAJI_MAX_CONNECTIONS = 5
_MAX_QUOTE_CONNECTIONS = _SHIOAJI_MAX_CONNECTIONS - 1
_MAX_SUBSCRIPTIONS_PER_CONN = 200


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
    )

    def __init__(self, symbols_path: str, shioaji_cfg: dict[str, Any], num_conns: int) -> None:
        if num_conns + 1 > _SHIOAJI_MAX_CONNECTIONS:
            raise ValueError(
                f"Total connections {num_conns + 1} (quote={num_conns} + order=1) "
                f"exceeds Shioaji limit of {_SHIOAJI_MAX_CONNECTIONS}"
            )
        if num_conns > _MAX_QUOTE_CONNECTIONS:
            raise ValueError(
                f"num_conns={num_conns} exceeds max quote connections {_MAX_QUOTE_CONNECTIONS}"
            )

        self._num_conns = num_conns
        self._config = shioaji_cfg
        self._login_interval_s = float(os.getenv("HFT_QUOTE_LOGIN_INTERVAL_S", "2"))
        self._options_expiry: str | None = None
        self._options_refresh_running: bool = False

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
                raise ValueError(
                    f"Group {g} has {len(syms)} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN} limit"
                )
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
        """Create a ShioajiClientFacade for each connection group."""
        self._clients = []
        for group_id in range(self._num_conns):
            per_conn_cfg = dict(self._config)
            per_conn_cfg["session_lock_suffix"] = f"_conn{group_id}"
            facade = ShioajiClientFacade(
                config_path=self._shard_paths[group_id],
                shioaji_config=per_conn_cfg,
            )
            self._clients.append(facade)
            logger.info("Created facade for group", conn_id=group_id)

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

    def subscribe_all(self, cb: Callable[..., Any]) -> None:
        """Subscribe each logged-in connection's symbol basket."""
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            if not facade.logged_in:
                log.warning("Skipping subscribe for unconnected facade")
                continue
            try:
                facade.subscribe_basket(cb)
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
        """Reconnect all connections. Duck-type for MarketDataService compatibility."""
        success = True
        for i, facade in enumerate(self._clients):
            log = logger.bind(conn_id=i)
            try:
                ok = facade.reconnect(reason=reason, force=force)
                if ok:
                    log.info("Connection reconnected")
                else:
                    log.warning("Connection reconnect returned False")
                    success = False
            except Exception as exc:
                log.error("Connection reconnect failed", error=str(exc))
                success = False
        return success

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
        if _METRIC_SUBSCRIBED is None:
            return
        from hft_platform.core import timebase

        now_s = timebase.now_s()
        for i, c in enumerate(self._clients):
            label = str(i)
            _METRIC_SUBSCRIBED.labels(conn_id=label).set(getattr(c, "subscribed_count", 0))
            _METRIC_LOGGED_IN.labels(conn_id=label).set(1 if c.logged_in else 0)
            last_ts = getattr(c._client, "_last_quote_data_ts", 0.0)
            age = now_s - last_ts if last_ts > 0 else -1
            _METRIC_LAST_DATA_AGE.labels(conn_id=label).set(age)

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
            opts = [
                c for c in contracts
                if c.get("type") == "option" and c.get("root") == "TXO"
            ]
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

        Reads from contract cache first (no API call needed), falls back
        to live API.  Uses ``reference`` price to compute ATM and filters
        strikes to ATM ± ``HFT_OPTIONS_STRIKE_RANGE`` (default: all).

        Returns True if symbols were updated and resubscribed.
        """
        if not self._clients:
            return False

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
                    allowed = set(strikes[lo:hi + 1])
                    nearest = [
                        c for c in nearest
                        if c.get("strike") is not None and float(c["strike"]) in allowed
                    ]
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
        for c in calls:
            symbols.append({"code": c["code"], "exchange": "OPT", "group": 1})
        for c in puts:
            symbols.append({"code": c["code"], "exchange": "OPT", "group": 2})

        # Validate connection limits
        for g in range(self._num_conns):
            count = sum(1 for s in symbols if s.get("group") == g)
            if count > _MAX_SUBSCRIPTIONS_PER_CONN:
                logger.error(
                    "options_refresh_group_overflow",
                    group=g, count=count, limit=_MAX_SUBSCRIPTIONS_PER_CONN,
                )
                return False

        out_path = os.getenv("SYMBOLS_CONFIG", "data/live_with_options.yaml")
        try:
            with open(out_path, "w") as f:
                f.write(f"# Auto-refreshed by QuoteConnectionPool\n")
                f.write(f"# TXO nearest expiry: {nearest_date}\n")
                f.write(f"# Group 0: Base ({len(base_symbols)})\n")
                f.write(f"# Group 1: TXO Calls ({len(calls)})\n")
                f.write(f"# Group 2: TXO Puts ({len(puts)})\n\n")
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

        # Reload config and resubscribe each connection
        resubscribed = 0
        for i, facade in enumerate(self._clients):
            try:
                facade._client._load_config()
                if facade.logged_in and cb is not None:
                    facade.subscribe_basket(cb)
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

        # Detect current expiry from the loaded symbols
        opt_codes = [s["code"] for s in self._all_symbols if s.get("exchange") == "OPT"]
        if opt_codes:
            # Extract expiry suffix from first option code (e.g. TXO31000D6)
            # We'll let the first refresh call set it properly
            pass

        def _loop() -> None:
            # Initial refresh on startup
            time.sleep(30)  # Wait for login to complete
            try:
                self.refresh_options_symbols(cb)
            except Exception as exc:
                logger.warning("options_refresh_initial_failed", error=str(exc))

            next_check = time.monotonic() + interval_s
            while self._options_refresh_running:
                time.sleep(60)
                if not self._options_refresh_running:
                    break
                if time.monotonic() >= next_check:
                    try:
                        self.refresh_options_symbols(cb)
                    except Exception as exc:
                        logger.warning("options_refresh_failed", error=str(exc))
                    next_check = time.monotonic() + interval_s

        t = threading.Thread(target=_loop, name="options-expiry-refresh", daemon=True)
        t.start()
        logger.info("options_refresh_thread_started", interval_s=interval_s)

    def stop_options_refresh(self) -> None:
        self._options_refresh_running = False
