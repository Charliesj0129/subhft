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
from hft_platform.feed_adapter.shioaji.limits import DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN
from hft_platform.feed_adapter.shioaji.pool_health import (
    check_facade_health,
    get_healthy_feed_gap_s,
)

try:
    from prometheus_client import Gauge
except ImportError:
    Gauge = None  # type: ignore[assignment, misc]

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
_MAX_SUBSCRIPTIONS_PER_CONN = DEFAULT_MAX_SUBSCRIPTIONS_PER_CONN

# Default path for runtime-refreshed snapshot (TXO chain auto-rotation output).
# This MUST never collide with the canonical INPUT file pointed to by
# ``SYMBOLS_CONFIG`` — overwriting the canonical file destroys hand-curated
# metadata (product_type/tick_size/price_scale/point_value) and was the
# 2026-04-27 root cause of B2 metadata gap (``config/symbols.yaml`` truncated
# from 1868 → 370 lines, see commit log near this comment).
_DEFAULT_RUNTIME_SNAPSHOT_PATH = "data/live_with_options.yaml"


def _paths_collide(a: str, b: str) -> bool:
    """Return True iff ``a`` and ``b`` resolve to the same on-disk file.

    Uses ``realpath`` so symlinks / ``..`` segments / different but
    equivalent relative paths all collapse to the canonical form before
    comparison. Returns False if either path cannot be resolved (treated
    as non-colliding so the caller can proceed; the surrounding writer
    still validates the path before writing).
    """
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def _derive_sidecar_path(input_path: str) -> str:
    """Build a sidecar snapshot path from ``input_path``.

    Convention: ``<input_dir>/<input_stem>.runtime.yaml``. Lives next to
    the canonical file so operators can find it, and the ``.runtime``
    infix makes its transient nature obvious.
    """
    head, tail = os.path.split(input_path)
    stem, ext = os.path.splitext(tail)
    if not ext:
        ext = ".yaml"
    sidecar_name = f"{stem}.runtime{ext}"
    return os.path.join(head, sidecar_name) if head else sidecar_name


def _resolve_runtime_snapshot_path(symbols_input_path: str | None = None) -> str:
    """Return the output path for QuoteConnectionPool's auto-refreshed snapshot.

    Precedence:
      1. ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` env var (explicit operator override)
      2. ``_DEFAULT_RUNTIME_SNAPSHOT_PATH`` (``data/live_with_options.yaml``)

    NEVER falls back to ``SYMBOLS_CONFIG`` — that variable points to the
    INPUT file (canonical, hand-curated). Conflating input + output paths
    caused B2 in 2026-04-27 (canonical ``config/symbols.yaml`` overwritten
    with a 370-line transient snapshot lacking ``product_type`` etc.).

    Self-clobber protection (codex round-4 P2 #9):

    If ``symbols_input_path`` is provided and the resolved output path
    points at the same file, the function tries — in order — to find a
    path that is **guaranteed distinct** from the input:

      1. Default ``_DEFAULT_RUNTIME_SNAPSHOT_PATH``.
      2. Sidecar derived from the input path itself
         (``<input_dir>/<input_stem>.runtime.yaml``).
      3. ``RuntimeError`` — operator must set
         ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` to a different path.

    The previous implementation always returned the default on collision,
    which silently re-clobbered the input when the operator pointed
    ``SYMBOLS_CONFIG`` at ``data/live_with_options.yaml`` (i.e. used the
    generated snapshot AS the canonical input).
    """
    snapshot = os.getenv("HFT_SYMBOLS_RUNTIME_SNAPSHOT", "").strip()
    if not snapshot:
        snapshot = _DEFAULT_RUNTIME_SNAPSHOT_PATH

    if not symbols_input_path or not _paths_collide(symbols_input_path, snapshot):
        return snapshot

    # Stage 1: try the default sidecar.
    if snapshot != _DEFAULT_RUNTIME_SNAPSHOT_PATH and not _paths_collide(
        symbols_input_path, _DEFAULT_RUNTIME_SNAPSHOT_PATH
    ):
        logger.error(
            "runtime_snapshot_path_collision",
            requested=snapshot,
            canonical_input=symbols_input_path,
            fallback=_DEFAULT_RUNTIME_SNAPSHOT_PATH,
            stage="env_override_to_default",
        )
        return _DEFAULT_RUNTIME_SNAPSHOT_PATH

    # Stage 2: derive a sidecar adjacent to the input itself.
    sidecar = _derive_sidecar_path(symbols_input_path)
    if not _paths_collide(symbols_input_path, sidecar):
        logger.error(
            "runtime_snapshot_path_collision",
            requested=snapshot,
            canonical_input=symbols_input_path,
            fallback=sidecar,
            stage="default_to_sidecar",
        )
        return sidecar

    # Stage 3: refuse — every candidate collides with the input. This is
    # only reachable if the operator's canonical file is literally named
    # ``<x>.runtime.yaml`` AND ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` points at
    # the same file. Demand explicit operator action rather than
    # silently overwriting hand-curated metadata.
    raise RuntimeError(
        "Symbols input path collides with every snapshot output candidate "
        f"(input={symbols_input_path!r}, requested={snapshot!r}, "
        f"sidecar={sidecar!r}). Set HFT_SYMBOLS_RUNTIME_SNAPSHOT to a "
        "path that does not equal the canonical symbols file."
    )


def _clamp_max_subscriptions(config: dict[str, Any]) -> int:
    raw = config.get("max_subscriptions", _MAX_SUBSCRIPTIONS_PER_CONN)
    try:
        return max(1, min(int(raw), _MAX_SUBSCRIPTIONS_PER_CONN))
    except (TypeError, ValueError):
        return _MAX_SUBSCRIPTIONS_PER_CONN


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
        "_symbols_input_path",
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
        self._degraded_threshold_s = float(os.getenv("HFT_FACADE_DEGRADED_THRESHOLD_S", "10"))
        self._reconnect_trigger_s = float(os.getenv("HFT_FACADE_RECONNECT_TRIGGER_S", "10"))
        self._user_callback: Callable[..., Any] | None = None
        self._per_facade_timeout_s = float(os.getenv("HFT_PER_FACADE_TIMEOUT_S", "15"))
        # Remember the canonical input path so the auto-refresh writer can
        # detect (and refuse) self-clobber when an operator misconfigures
        # ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` (or legacy callers still expect
        # the writer to honour ``SYMBOLS_CONFIG``).
        self._symbols_input_path: str = str(symbols_path)

        with open(symbols_path, "r") as f:
            data = yaml.safe_load(f) or {}
        self._all_symbols: list[dict[str, Any]] = data.get("symbols", [])

        groups: dict[int, list[dict[str, Any]]] = {i: [] for i in range(num_conns)}
        unassigned: list[dict[str, Any]] = []
        for sym in self._all_symbols:
            g = sym.get("group")
            if g is None:
                unassigned.append(sym)
                continue
            if not isinstance(g, int) or g < 0 or g >= num_conns:
                raise ValueError(
                    f"Symbol {sym.get('code', '?')} has group={g} "
                    f"but only {num_conns} connections configured (valid: 0..{num_conns - 1})"
                )
            groups[g].append(sym)

        if unassigned:
            unassigned.sort(key=lambda s: (str(s.get("product_type") or ""), str(s.get("code") or "")))
            for i, sym in enumerate(unassigned):
                groups[i % num_conns].append(sym)
            logger.info(
                "auto_assigned_symbols_round_robin",
                count=len(unassigned),
                num_conns=num_conns,
                per_group={g: len(s) for g, s in groups.items()},
            )

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
            per_conn_cfg["max_subscriptions"] = _clamp_max_subscriptions(per_conn_cfg)
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
                    # Extract error detail from underlying client
                    _client = getattr(slot.facade, "_client", None)
                    _err = getattr(_client, "_last_reconnect_error", None) or getattr(
                        _client, "_last_login_error", None
                    )
                    log.warning(
                        "facade_reconnect_failed",
                        error=str(_err) if _err else "unknown (backoff guard or silent failure)",
                        consecutive_failures=slot.reconnect_failures,
                    )
                    # Alert on sustained failures (first alert at threshold, then every N)
                    _alert_every = int(os.environ.get("HFT_RECONNECT_ALERT_EVERY", "5"))
                    if slot.reconnect_failures == _alert_every or (
                        slot.reconnect_failures > _alert_every and slot.reconnect_failures % _alert_every == 0
                    ):
                        _self = self  # noqa: F841 — keep reference for structlog
                        logger.critical(
                            "reconnect_sustained_failure_alert",
                            conn_id=slot.conn_id,
                            consecutive_failures=slot.reconnect_failures,
                            last_error=str(_err) if _err else "unknown",
                            hint="Consider manual restart if this persists",
                        )
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

        Thread safety (P1, 2026-04-24): the slot's ``begin_reconnect`` /
        ``record_reconnect_success`` / ``record_reconnect_failure`` helpers
        guard compound state transitions under the slot's per-instance lock
        so the daemon thread and the event-loop supervisor never race on
        ``state`` + ``reconnect_failures``.
        """
        slot: FacadeSlot | None = None
        for s in self._slots:
            if s.conn_id == conn_id:
                slot = s
                break
        if slot is None:
            logger.warning("schedule_reconnect_unknown_conn_id", conn_id=conn_id)
            return
        if not slot.begin_reconnect():
            # Another thread already owns the reconnect slot.
            return
        logger.warning("facade_reconnect_scheduled", conn_id=conn_id)

        def _do_reconnect() -> None:
            log = logger.bind(conn_id=conn_id)
            try:
                ok = slot.facade.reconnect(reason="health_check", force=False)
                if ok:
                    slot.record_reconnect_success()
                    log.info("facade_reconnected_via_health_check")
                else:
                    slot.record_reconnect_failure()
                    log.warning("facade_health_reconnect_failed")
            except Exception as exc:
                slot.record_reconnect_failure()
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

    @property
    def subscribed_codes(self) -> set[str]:
        """Aggregate subscribed-codes set across all underlying clients.

        ``MarketDataService.get_active_feed_gap_s`` reads
        ``client.subscribed_codes`` to separate "expired contract" (latched
        but unsubscribed → safe to de-latch) from "partial outage" (still
        subscribed but silent → must surface).  When ``HFT_QUOTE_CONNECTIONS
        > 1`` the platform's ``client`` handle is this pool rather than a
        single facade; without this aggregation the de-latch path silently
        falls back to ``subscription_set=None`` and never engages.

        Snapshot semantics: each underlying client may mutate its own set
        concurrently (rollover refresh, contracts_runtime updates).  We
        materialise into a fresh ``set`` so callers (which iterate or
        membership-test) cannot tear with a concurrent writer.
        """
        result: set[str] = set()
        # Snapshot the client list once: the pool's reconnect path can
        # rebuild ``self._clients`` mid-iteration.
        clients_snapshot = list(self._clients)
        for client in clients_snapshot:
            codes = getattr(client, "subscribed_codes", None)
            if isinstance(codes, (set, frozenset)):
                # Snapshot each per-client set too, so a concurrent writer
                # on the underlying facade cannot mutate while we copy.
                try:
                    result.update(set(codes))
                except RuntimeError:
                    # set changed size during iteration on the underlying
                    # facade — skip this slice; the partial union still
                    # over-approximates "subscribed" which is the safer
                    # direction (false-positive subscription preserves the
                    # outage signal; false-negative would mask it).
                    continue
        return result

    @property
    def alias_to_actual(self) -> dict[str, str]:
        """Aggregate alias→resolved-code map across all underlying clients.

        Mirrors :pyattr:`subscribed_codes` for the rollover-alias bridge
        consumed by ``MarketDataService.get_active_feed_gap_s``.  Per-client
        maps are populated by ``ContractsRuntime.resolve_symbol_aliases``;
        when distinct clients carry the same alias key, the later client
        in the snapshot wins (deterministic, last-writer-wins; the resolved
        code is identical across clients in normal operation because the
        rollover machinery is single-source).
        """
        result: dict[str, str] = {}
        clients_snapshot = list(self._clients)
        for client in clients_snapshot:
            mapping = getattr(client, "alias_to_actual", None)
            if isinstance(mapping, dict):
                try:
                    result.update(dict(mapping))
                except RuntimeError:
                    # dict changed size during iteration on the underlying
                    # facade — skip this slice; the partial union is safe
                    # because every present mapping is itself authoritative.
                    continue
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
                _METRIC_SUBSCRIBED.labels(conn_id=cid).set(getattr(slot.facade, "subscribed_count", 0))
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

    def _option_target_groups(self) -> list[int]:
        groups = list(range(1, self._num_conns))
        return groups or [0]

    def _build_option_symbols(
        self,
        base_symbols: list[dict[str, Any]],
        calls: list[dict[str, Any]],
        puts: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        symbols: list[dict[str, Any]] = []
        for s in base_symbols:
            entry = dict(s)
            entry["group"] = 0
            symbols.append(entry)

        group_capacity = {g: _MAX_SUBSCRIPTIONS_PER_CONN for g in range(self._num_conns)}
        if 0 in group_capacity:
            group_capacity[0] -= len(base_symbols)
            if group_capacity[0] < 0:
                raise ValueError(
                    f"Group 0 has {len(base_symbols)} symbols, exceeds {_MAX_SUBSCRIPTIONS_PER_CONN} limit"
                )

        calls_by_strike = {float(c.get("strike", 0)): c for c in calls if c.get("strike") is not None}
        puts_by_strike = {float(c.get("strike", 0)): c for c in puts if c.get("strike") is not None}
        strikes = sorted(set(calls_by_strike) | set(puts_by_strike))
        target_groups = self._option_target_groups()
        trimmed = 0

        for idx, strike in enumerate(strikes):
            target_group = target_groups[idx % len(target_groups)]
            entries: list[dict[str, Any]] = []
            call = calls_by_strike.get(strike)
            put = puts_by_strike.get(strike)
            if call is not None:
                entries.append({"code": call["code"], "exchange": "OPT", "group": target_group})
            if put is not None:
                entries.append({"code": put["code"], "exchange": "OPT", "group": target_group})
            if not entries:
                continue
            if group_capacity[target_group] < len(entries):
                trimmed += len(entries)
                continue
            symbols.extend(entries)
            group_capacity[target_group] -= len(entries)

        return symbols, trimmed

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

            # Find nearest non-expired expiry. Drop dates < today so a stale
            # broker contract cache (containing already-expired chains) cannot
            # bait the auto-refresh into mass-subscribing dead options.
            # Cache uses 'YYYY/MM/DD' or 'YYYY-MM-DD'; parse before comparing.
            from datetime import date as _date

            today = _date.today()

            def _parse_delivery(value: str) -> _date | None:
                value = value.strip().replace("/", "-")
                if len(value) != 10:
                    return None
                try:
                    return _date.fromisoformat(value)
                except ValueError:
                    return None

            active: dict[str, _date] = {}
            for c in opts:
                raw = str(c.get("delivery_date", ""))
                parsed = _parse_delivery(raw)
                if parsed is not None and parsed >= today:
                    active[raw] = parsed
            if not active:
                logger.warning("options_refresh_no_active_expiry", cache_dates_seen=len(opts))
                return False
            dates = sorted(active.keys(), key=lambda k: active[k])
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

            # Preserve group 0 (non-option symbols) and spread option strike pairs
            # across the remaining quote connections. Each strike keeps its call/put
            # pair on the same group so individual groups stay mixed and balanced.
            base_symbols = [s for s in self._all_symbols if s.get("group", 0) == 0]
            try:
                symbols, trimmed = self._build_option_symbols(base_symbols, calls, puts)
            except ValueError as exc:
                logger.error("options_refresh_group_overflow", error=str(exc))
                return False

            # Validate connection limits
            for g in range(self._num_conns):
                count = sum(1 for s in symbols if s.get("group") == g)
                if count > _MAX_SUBSCRIPTIONS_PER_CONN:
                    logger.error(
                        "options_refresh_group_overflow",
                        group=g,
                        count=count,
                        limit=_MAX_SUBSCRIPTIONS_PER_CONN,
                    )
                    return False

            # Write the auto-refreshed snapshot to a sidecar path — never to
            # the canonical input file. ``_resolve_runtime_snapshot_path`` honours
            # ``HFT_SYMBOLS_RUNTIME_SNAPSHOT`` and refuses to clobber the
            # canonical INPUT (``self._symbols_input_path``) even if an operator
            # accidentally points it there. See module-level docstring on
            # ``_DEFAULT_RUNTIME_SNAPSHOT_PATH`` for the 2026-04-27 incident
            # context (B2 metadata gap).
            out_path = _resolve_runtime_snapshot_path(self._symbols_input_path)
            try:
                # Ensure parent dir exists (default sidecar lives under
                # gitignored ``data/`` which may not exist on a fresh clone).
                parent = os.path.dirname(out_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out_path, "w") as f:
                    f.write("# Auto-refreshed by QuoteConnectionPool\n")
                    f.write(f"# Source canonical: {self._symbols_input_path}\n")
                    f.write(f"# TXO nearest expiry: {nearest_date}\n")
                    f.write(f"# Group 0: Base ({len(base_symbols)})\n")
                    option_groups = self._option_target_groups()
                    f.write(f"# Option groups: {option_groups}\n")
                    f.write(f"# Trimmed options: {trimmed}\n\n")
                    yaml.dump({"symbols": symbols}, f, default_flow_style=False, allow_unicode=True)
            except Exception as exc:
                logger.error("options_refresh_write_failed", error=str(exc), out_path=out_path)
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
                    # Reset local subscription counters so subscribe_basket() doesn't
                    # immediately hit the MAX_SUBSCRIPTIONS guard on the second call.
                    # Mirrors what _resubscribe_all() does in subscription_manager.py.
                    # D2: in-place clear preserves object identity for peer readers.
                    facade._client.subscribed_codes.clear()
                    facade._client.subscribed_count = 0
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
                trimmed=trimmed,
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

        if interval_s <= 0:
            logger.info("options_refresh_thread_disabled", interval_s=interval_s)
            return

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
