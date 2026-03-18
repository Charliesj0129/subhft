"""Main engine: state machine + async polling loop + rich.live rendering."""

from __future__ import annotations

import asyncio
import datetime as dt
import signal
import time
from typing import Any

from structlog import get_logger

from hft_platform.monitor._alpha_dispatcher import AlphaDispatcher
from hft_platform.monitor._ch_poller import CHPoller
from hft_platform.monitor._config_loader import load_watchlist
from hft_platform.monitor._data_source import (
    CHDataSource,
    DataSource,
    HybridDataSource,
    RedisHybridSource,
    ShmDataSource,
)
from hft_platform.monitor._enrichment import enrich_tick, validate_l1_row
from hft_platform.monitor._events import (
    compute_opportunity_score,
    detect_events,
    format_event_label,
    snapshot_prev,
)
from hft_platform.monitor._renderer import build_header, build_table
from hft_platform.monitor._session import _TZ_TAIPEI, format_next_open, get_session_info, get_session_start
from hft_platform.monitor._types import (
    _EVENT_RING_SIZE,
    EventFlag,
    HeaderContext,
    MonitorConfig,
    MonitorEvent,
    MonitorState,
    RowView,
    SymbolState,
)

logger = get_logger("monitor.engine")

# Sort mode constants
SORT_OPPORTUNITY = 0
SORT_COMPOSITE = 1
SORT_CONFIG = 2
_SORT_LABELS = ("opportunity", "composite", "config")


class MonitorEngine:
    """Signal Monitor state machine and main loop."""

    __slots__ = (
        "_config",
        "_state",
        "_state_before_pause",
        "_data_source",
        "_dispatcher",
        "_sym_states",
        "_sym_states_sorted",
        "_error_msg",
        "_init_step",
        "_running",
        "_paused_by_user",
        "_last_now",
        "_alpha_cols",
        "_cursor_buf",
        "_stale_buf",
        "_ref_product_type",
        "_session_cache",
        # Phase 1: event ring buffer
        "_event_ring",
        "_event_ring_idx",
        "_event_ring_len",
        # Phase 2: sort mode
        "_sort_mode",
        # Phase 3: selection + detail
        "_selected_idx",
        "_detail_visible",
    )

    def __init__(self, config: MonitorConfig) -> None:
        self._config = config
        self._state = MonitorState.INITIALIZING
        self._state_before_pause: MonitorState | None = None
        self._data_source: DataSource | None = None
        self._dispatcher = AlphaDispatcher()
        self._sym_states: list[SymbolState] = []
        self._sym_states_sorted: list[SymbolState] = []
        self._error_msg = ""
        self._init_step = 0
        self._running = True
        self._paused_by_user = False
        self._last_now: dt.datetime | None = None
        self._alpha_cols: list[str] = []
        self._cursor_buf: dict[str, int] = {}
        self._stale_buf: list[str] = []
        self._ref_product_type: str = "stock"
        self._session_cache: dict[str, tuple[bool, str, str]] = {}
        # Phase 1: event ring buffer
        self._event_ring: list[MonitorEvent] = [
            MonitorEvent(symbol="", label="", fired_ns=0) for _ in range(_EVENT_RING_SIZE)
        ]
        self._event_ring_idx: int = 0
        self._event_ring_len: int = 0
        # Phase 2: sort mode
        self._sort_mode: int = SORT_OPPORTUNITY
        # Phase 3: selection + detail
        self._selected_idx: int = 0
        self._detail_visible: bool = False

    @property
    def state(self) -> MonitorState:
        return self._state

    @property
    def error_msg(self) -> str:
        return self._error_msg

    def _create_data_source(self, symbols: tuple[str, ...]) -> DataSource:
        """Create the appropriate DataSource based on config.data_source."""
        import os

        cfg = self._config
        ds_mode = cfg.data_source  # "ch" | "shm" | "auto"

        if ds_mode == "shm":
            shm_name = os.getenv("HFT_MONITOR_SHM_NAME", "hft_monitor_snapshot")
            max_sym = int(os.getenv("HFT_MONITOR_SHM_MAX_SYMBOLS", "64"))
            return ShmDataSource(shm_name=shm_name, max_symbols=max_sym, symbols=symbols)

        # Build CH (or Redis / Hybrid) source — always needed for "ch" and "auto"
        if cfg.source == "redis":
            from hft_platform.monitor._redis_poller import RedisPoller

            poller: Any = RedisPoller(
                host=cfg.redis_host,
                port=cfg.redis_port,
                symbols=symbols,
                password=cfg.redis_password,
                key_prefix=cfg.redis_key_prefix,
                ring_size=cfg.redis_ring_size,
                batch_limit=cfg.batch_limit_per_symbol,
                max_retries=cfg.max_retries,
            )
        elif cfg.source == "hybrid":
            from hft_platform.monitor._redis_poller import RedisPoller

            redis_poller = RedisPoller(
                host=cfg.redis_host,
                port=cfg.redis_port,
                symbols=symbols,
                password=cfg.redis_password,
                key_prefix=cfg.redis_key_prefix,
                ring_size=cfg.redis_ring_size,
                batch_limit=cfg.batch_limit_per_symbol,
                max_retries=cfg.max_retries,
            )
            ch_poller = CHPoller(
                host=cfg.ch_host,
                port=cfg.ch_port,
                symbols=symbols,
                user=cfg.ch_user,
                password=cfg.ch_password,
                batch_limit=cfg.batch_limit_per_symbol,
                max_retries=cfg.max_retries,
            )
            ch_src = CHDataSource(ch_poller)
            return RedisHybridSource(
                redis_poller=redis_poller,
                ch_source=ch_src,
                backfill_interval_s=cfg.hybrid_backfill_interval_s,
            )
        else:
            poller = CHPoller(
                host=cfg.ch_host,
                port=cfg.ch_port,
                symbols=symbols,
                user=cfg.ch_user,
                password=cfg.ch_password,
                batch_limit=cfg.batch_limit_per_symbol,
                max_retries=cfg.max_retries,
            )
        ch_source = CHDataSource(poller)

        if ds_mode == "ch":
            return ch_source

        # "auto" → try hybrid (SHM + CH)
        shm_name = os.getenv("HFT_MONITOR_SHM_NAME", "hft_monitor_snapshot")
        max_sym = int(os.getenv("HFT_MONITOR_SHM_MAX_SYMBOLS", "64"))
        try:
            shm_source = ShmDataSource(shm_name=shm_name, max_symbols=max_sym, symbols=symbols)
            if shm_source.connected:
                logger.info("data_source_auto_selected_hybrid")
                return HybridDataSource(
                    shm_source,
                    ch_source,
                    backfill_interval_s=cfg.hybrid_backfill_interval_s,
                )
        except Exception as exc:
            logger.info("data_source_auto_shm_unavailable", reason=str(exc))

        logger.info("data_source_auto_fallback_ch")
        return ch_source

    def initialize(self) -> None:
        """Run initialization steps. Transitions to WARMING_UP or ERROR."""
        try:
            # Step 1: Validate config
            self._init_step = 1
            if not self._config.symbols:
                raise ValueError("No symbols in watchlist")

            # Step 2: Initialize symbol states
            self._init_step = 2
            self._sym_states = [SymbolState(symbol=ws) for ws in self._config.symbols]
            self._sym_states_sorted = list(self._sym_states)

            # Step 3: Connect data source
            self._init_step = 3
            symbols = tuple(ws.code for ws in self._config.symbols)
            self._data_source = self._create_data_source(symbols)
            self._data_source.connect()

            # Step 4: Load alphas
            self._init_step = 4
            all_alpha_ids: set[str] = set()
            for ws in self._config.symbols:
                all_alpha_ids.update(ws.alpha_ids)
            if not all_alpha_ids:
                raise ValueError("No alpha_ids configured")

            loaded = self._dispatcher.load_alphas(
                tuple(sorted(all_alpha_ids)),
                promotions_dir=self._config.promotions_dir,
            )
            if not loaded:
                raise ValueError("No requested alphas could be loaded")
            for sym_state in self._sym_states:
                self._dispatcher.bind_symbol(sym_state)

            # Compute stable alpha column order (once)
            seen_alpha: set[str] = set()
            for ws in self._config.symbols:
                for aid in ws.alpha_ids:
                    if aid not in seen_alpha:
                        seen_alpha.add(aid)
                        self._alpha_cols.append(aid)

            # Cache reference product_type for header display
            for ws in self._config.symbols:
                if ws.product_type in ("future", "option"):
                    self._ref_product_type = ws.product_type
                    break

            # Step 5: Done
            self._init_step = 5
            self._refresh_sessions()
            self._bootstrap_all_symbols()
            self._state = MonitorState.WARMING_UP
            self._update_state()
            logger.info("initialization_complete", n_symbols=len(self._sym_states))

        except Exception as exc:
            self._state = MonitorState.ERROR
            self._error_msg = f"Init step {self._init_step}: {exc}"
            logger.error("initialization_failed", step=self._init_step, error=str(exc))

    def poll_and_update(self) -> None:
        """Execute one poll cycle: fetch ticks, enrich, dispatch, update state."""
        if self._state == MonitorState.ERROR:
            return

        now = dt.datetime.now(_TZ_TAIPEI)
        self._last_now = now
        any_active = self._refresh_sessions(now)
        was_disconnected = self._state == MonitorState.DISCONNECTED
        self._bootstrap_new_sessions()
        if self._state == MonitorState.DISCONNECTED and not was_disconnected:
            return
        if self._paused_by_user:
            self._state = MonitorState.PAUSED
            return

        if not any_active:
            self._state = MonitorState.PAUSED
            return

        if self._state == MonitorState.PAUSED:
            self.reset_session()

        if self._state == MonitorState.DISCONNECTED:
            self._handle_reconnect()
            return

        if self._data_source is None:
            return

        try:
            self._cursor_buf.clear()
            for ss in self._sym_states:
                if ss.session_active:
                    self._cursor_buf[ss.symbol.code] = ss.cursor_ts_ns
            rows_by_sym = self._data_source.poll(self._cursor_buf)
        except ConnectionError:
            self._state = MonitorState.DISCONNECTED
            logger.warning("ch_disconnected", error=self._data_source.last_error)
            return

        # Process each symbol's rows
        now_ns = time.time_ns()
        warmup = self._config.warmup_ticks
        for ss in self._sym_states:
            # Phase 1: snapshot prev values before processing
            snapshot_prev(ss)

            sym_rows = rows_by_sym.get(ss.symbol.code, [])
            for row in sym_rows:
                self._process_row(ss, row)

            # Stale detection (only for active sessions)
            if ss.session_active and ss.tick_count > 0:
                age_s = (now_ns - ss.last_update_ns) / 1e9
                ss.is_stale = age_s > self._config.stale_threshold_s
            else:
                ss.is_stale = False

            # Phase 1: detect events + compute opportunity score
            detect_events(ss, now_ns)
            ss.opportunity_score = compute_opportunity_score(ss, warmup)

            # Push to event ring if any event fired
            if ss.event_flags != EventFlag.NONE:
                label = format_event_label(EventFlag(ss.event_flags), ss)
                if label:
                    self._push_event(ss.symbol.code, label, now_ns)

        # Phase 2: sort symbols
        self._sort_symbols()

        # State transitions
        self._update_state()

    def _push_event(self, symbol: str, label: str, fired_ns: int) -> None:
        """Push an event into the ring buffer."""
        entry = self._event_ring[self._event_ring_idx]
        entry.symbol = symbol
        entry.label = label
        entry.fired_ns = fired_ns
        self._event_ring_idx = (self._event_ring_idx + 1) % _EVENT_RING_SIZE
        if self._event_ring_len < _EVENT_RING_SIZE:
            self._event_ring_len += 1

    def _sort_symbols(self) -> None:
        """Sort _sym_states_sorted according to current sort mode."""
        if self._sort_mode == SORT_OPPORTUNITY:
            self._sym_states_sorted = sorted(
                self._sym_states,
                key=lambda ss: ss.opportunity_score,
                reverse=True,
            )
        elif self._sort_mode == SORT_COMPOSITE:
            self._sym_states_sorted = sorted(
                self._sym_states,
                key=lambda ss: abs(ss.composite),
                reverse=True,
            )
        else:
            # Config order = original order
            self._sym_states_sorted = list(self._sym_states)

    def cycle_sort_mode(self) -> None:
        """Cycle through sort modes: opportunity → composite → config."""
        self._sort_mode = (self._sort_mode + 1) % 3
        self._sort_symbols()

    def _update_state(self) -> None:
        """Evaluate state transitions after polling (single-pass, no list alloc)."""
        if self._state == MonitorState.ERROR:
            return

        has_active = False
        any_ready = False
        any_stale = False
        warmup = self._config.warmup_ticks
        for ss in self._sym_states:
            if not ss.session_active:
                continue
            has_active = True
            if ss.tick_count >= warmup:
                any_ready = True
                if ss.is_stale:
                    any_stale = True
                    break  # both flags set — no need to continue
            elif ss.tick_count > 0 and ss.is_stale:
                any_stale = True

        if not has_active and self._state != MonitorState.INITIALIZING:
            self._state = MonitorState.PAUSED
            return

        if any_ready:
            self._state = MonitorState.STALE if any_stale else MonitorState.LIVE
            return

        if has_active:
            self._state = MonitorState.WARMING_UP

    def _handle_reconnect(self) -> None:
        """Handle DISCONNECTED state: attempt reconnection."""
        if self._data_source is None:
            self._state = MonitorState.ERROR
            self._error_msg = "No poller available"
            return

        try:
            if self._data_source.try_reconnect():
                self._bootstrap_all_symbols()
                self._state = MonitorState.WARMING_UP
                self._update_state()
                logger.info("source_reconnected", source=self._config.source)
        except RuntimeError as exc:
            self._state = MonitorState.ERROR
            self._error_msg = str(exc)

    def toggle_pause(self) -> None:
        """Toggle user-initiated pause."""
        if self._state == MonitorState.ERROR:
            return
        self._paused_by_user = not self._paused_by_user
        if self._paused_by_user:
            self._state_before_pause = self._state
            self._state = MonitorState.PAUSED
        else:
            self._state = self._state_before_pause or MonitorState.WARMING_UP
            self._state_before_pause = None

    def reset_session(self) -> None:
        """Reset alpha state and cursors for session transition.

        Delegates to _bootstrap_all_symbols() which calls _reset_symbol_runtime()
        for each symbol — no need to duplicate the per-field resets here.
        """
        self._bootstrap_all_symbols()
        self._state = MonitorState.WARMING_UP
        self._update_state()
        logger.info("session_reset")

    def request_stop(self) -> None:
        self._running = False

    # ---- Phase 3: Navigation ---- #

    def move_selection(self, delta: int) -> None:
        """Move selection index by delta, clamped to valid range."""
        n = len(self._sym_states_sorted)
        if n == 0:
            return
        self._selected_idx = max(0, min(n - 1, self._selected_idx + delta))

    def toggle_detail(self) -> None:
        """Toggle detail panel visibility."""
        self._detail_visible = not self._detail_visible

    def close_detail(self) -> None:
        """Close detail panel and clear selection."""
        self._detail_visible = False

    # ------------------------------------------------------------------ #
    # Rendering helpers                                                    #
    # ------------------------------------------------------------------ #

    def get_header_context(self) -> HeaderContext:
        """Build data-only header context for rendering."""
        now = self._last_now or dt.datetime.now(_TZ_TAIPEI)
        time_str = now.strftime("%Y-%m-%d %H:%M:%S TST")

        # Session display (use cached ref_product_type)
        session_display = ""
        if self._sym_states:
            _, _, session_display = get_session_info(self._ref_product_type, now)

        # Source status — label reflects actual data source type
        ds = self._data_source
        if isinstance(ds, RedisHybridSource):
            source_label = ds.mode_label
        elif isinstance(ds, HybridDataSource):
            source_label = ds.mode_label
        elif isinstance(ds, ShmDataSource):
            source_label = "SHM"
        elif self._config.source == "redis":
            source_label = "Redis"
        else:
            source_label = "CH"

        if ds and ds.connected:
            # Check heartbeat staleness for Redis-backed sources
            hb_stale = False
            if isinstance(ds, CHDataSource) and hasattr(ds._poller, "heartbeat_stale"):
                hb_stale = ds._poller.heartbeat_stale
            elif isinstance(ds, RedisHybridSource) and hasattr(ds._redis, "heartbeat_stale"):
                hb_stale = ds._redis.heartbeat_stale
            if hb_stale:
                ch_status = f"{source_label}: STALE (no heartbeat)"
            else:
                ch_status = f"{source_label}: OK"
        elif self._state == MonitorState.DISCONNECTED:
            retry = ds.retry_count if ds else "?"
            ch_status = f"{source_label}: RETRY {retry}"
        else:
            ch_status = f"{source_label}: --"

        # Stale symbols (reuse pre-allocated list)
        self._stale_buf.clear()
        for ss in self._sym_states:
            if ss.is_stale:
                self._stale_buf.append(ss.symbol.code)
        stale = list(self._stale_buf)

        # Extra info
        extra = ""
        if self._state == MonitorState.INITIALIZING:
            extra = f"Step {self._init_step}/5"
        elif self._state in (MonitorState.WARMING_UP, MonitorState.LIVE, MonitorState.STALE):
            extra = self._format_runtime_summary()
        elif self._state == MonitorState.PAUSED:
            extra = format_next_open(self._ref_product_type, now)
        elif self._state == MonitorState.DISCONNECTED and self._data_source:
            backoff = self._data_source.remaining_backoff_seconds()
            extra = f"retry in {backoff:.0f}s"
        elif self._state == MonitorState.ERROR:
            extra = self._error_msg[:80]

        # Event ticker (Phase 1)
        event_ticker = self._build_event_ticker()

        return HeaderContext(
            state=self._state,
            session_display=session_display,
            time_str=time_str,
            ch_status=ch_status,
            stale_symbols=stale,
            extra=extra,
            sort_mode=_SORT_LABELS[self._sort_mode],
            event_ticker=event_ticker,
            source_label=source_label,
        )

    def _build_event_ticker(self) -> str:
        """Build event ticker string from ring buffer (most recent first, max 3)."""
        if self._event_ring_len == 0:
            return ""
        now_ns = time.time_ns()
        entries: list[str] = []
        n = min(self._event_ring_len, 3)
        for i in range(n):
            idx = (self._event_ring_idx - 1 - i) % _EVENT_RING_SIZE
            evt = self._event_ring[idx]
            if evt.fired_ns == 0:
                continue
            age_s = (now_ns - evt.fired_ns) / 1e9
            if age_s > 30:
                continue
            entries.append(f"\u26a1 {evt.symbol} {evt.label} {age_s:.0f}s ago")
        return "  |  ".join(entries)

    def get_header(self) -> Any:
        """Build header Text for current state."""
        return build_header(self.get_header_context())

    def get_table(self) -> Any:
        """Build the signal table."""
        return build_table(
            self._sym_states_sorted,
            self._config,
            self._state,
            self._alpha_cols or None,
            selected_idx=self._selected_idx if self._detail_visible else -1,
        )

    def get_selected_symbol_state(self) -> SymbolState | None:
        """Return the currently selected SymbolState, or None."""
        if not self._sym_states_sorted:
            return None
        idx = max(0, min(self._selected_idx, len(self._sym_states_sorted) - 1))
        return self._sym_states_sorted[idx]

    def _refresh_sessions(self, now: dt.datetime | None = None) -> bool:
        """Refresh per-symbol session flags. Queries once per product_type, not per symbol."""
        if now is None:
            now = dt.datetime.now(_TZ_TAIPEI)

        # Reuse pre-allocated session cache (typically 2-3 types)
        self._session_cache.clear()
        cache = self._session_cache
        any_active = False
        for ss in self._sym_states:
            ss.was_session_active = ss.session_active
            pt = ss.symbol.product_type
            info = cache.get(pt)
            if info is None:
                info = get_session_info(pt, now)
                cache[pt] = info
            is_active, label, display = info
            ss.session_active = is_active
            ss.session_label = label
            ss.session_display = display
            ss.is_closed = label == "[CLOSED]"
            any_active = any_active or is_active
            if not is_active:
                ss.is_stale = False
        return any_active

    def _bootstrap_symbol(self, ss: SymbolState) -> None:
        """Reset, fetch recent history, and replay a single symbol."""
        assert self._data_source is not None  # noqa: S101
        self._reset_symbol_runtime(ss)
        min_ingest_ts = self._session_min_ingest_ts(ss)
        try:
            rows = self._data_source.fetch_recent_valid(
                ss.symbol.code,
                limit=self._config.replay_ticks,
                min_ingest_ts=min_ingest_ts,
            )
        except NotImplementedError:
            rows = []
        if ss.session_active:
            ss.session_started_ns = min_ingest_ts or time.time_ns()
            ss.cursor_ts_ns = max(0, (min_ingest_ts - 1) if min_ingest_ts > 0 else 0)
        for row in rows:
            self._process_row(ss, row)

    def _bootstrap_all_symbols(self) -> None:
        """Warm symbols from recent valid history so ready rows appear immediately."""
        if self._data_source is None:
            return
        self._state = MonitorState.WARMING_UP
        for ss in self._sym_states:
            self._bootstrap_symbol(ss)

    def _bootstrap_new_sessions(self) -> None:
        """Reset and replay any symbol that just entered an active session."""
        if self._data_source is None:
            return
        for ss in self._sym_states:
            if ss.session_active and not ss.was_session_active:
                try:
                    self._bootstrap_symbol(ss)
                except ConnectionError:
                    self._state = MonitorState.DISCONNECTED
                    logger.warning("ch_disconnected", error=self._data_source.last_error)
                    return

    def _process_row(self, ss: SymbolState, row: RowView) -> None:
        """Advance cursor for a row and dispatch only when L1 data is valid."""
        ss.cursor_ts_ns = max(ss.cursor_ts_ns, row.ingest_ts)
        ss.last_seen_ts_ns = max(ss.last_seen_ts_ns, row.ingest_ts)

        invalid_reason = validate_l1_row(row)
        if invalid_reason is not None:
            ss.invalid_row_count += 1
            ss.last_invalid_reason = invalid_reason
            return

        try:
            payload = enrich_tick(row, ss)
            self._dispatcher.dispatch(ss, payload)
        except Exception as exc:
            ss.invalid_row_count += 1
            ss.last_invalid_reason = str(exc)
            logger.warning("row_processing_failed", symbol=ss.symbol.code, error=str(exc))

    def _reset_symbol_runtime(self, ss: SymbolState) -> None:
        """Reset a single symbol's runtime state."""
        ss.cursor_ts_ns = 0
        ss.tick_count = 0
        ss.last_update_ns = 0
        ss.last_seen_ts_ns = 0
        ss.session_started_ns = 0
        ss.ofi_l1_cum = 0.0
        ss.prev_bid_qty = 0.0
        ss.prev_ask_qty = 0.0
        ss.last_price = 0.0
        ss.spread_bps = 0.0
        ss.bid_qty = 0.0
        ss.ask_qty = 0.0
        ss.invalid_row_count = 0
        ss.last_invalid_reason = ""
        ss.is_stale = False
        # Phase 1: reset event tracking fields
        ss.prev_composite = 0.0
        ss.prev_agree_direction = 0
        ss.prev_spread_bps = 0.0
        ss.prev_is_stale = False
        ss.composite_delta = 0.0
        ss.composite_delta_abs = 0.0
        ss.event_flags = 0
        ss.last_event_ns = 0
        ss.opportunity_score = 0.0
        self._dispatcher.reset_symbol(ss)
        ss.sparkline_clear()

    def _session_min_ingest_ts(self, ss: SymbolState) -> int:
        """Return current-session lower bound for warmup replays when active."""
        if not ss.session_active:
            return 0
        start = get_session_start(ss.symbol.product_type)
        if start is None:
            return 0
        return int(start.timestamp() * 1_000_000_000)

    def _format_runtime_summary(self) -> str:
        """Build compact runtime summary for the header (single pass)."""
        warmup = self._config.warmup_ticks
        warn_s = self._config.no_data_warn_s
        now_ns = time.time_ns()
        ready = warming = no_data = stale_n = bad_rows = 0

        for ss in self._sym_states:
            if not ss.session_active:
                continue
            tc = ss.tick_count
            if tc >= warmup:
                ready += 1
            elif tc > 0:
                warming += 1
            elif ss.invalid_row_count > 0 or (
                ss.session_started_ns > 0 and (now_ns - ss.session_started_ns) / 1e9 >= warn_s
            ):
                no_data += 1
            if ss.is_stale:
                stale_n += 1
            bad_rows += ss.invalid_row_count

        return f"ready {ready} | warm {warming} | no-data {no_data} | stale {stale_n} | bad {bad_rows}"


async def run_monitor(
    watchlist_path: str | None = None,
    symbols_path: str | None = None,
    source: str | None = None,
) -> int:
    """Main async entry point for the Signal Monitor TUI."""
    from dataclasses import replace

    from rich.console import Console
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel

    from hft_platform.monitor._detail_panel import build_detail_panel

    config = load_watchlist(watchlist_path, symbols_path)
    if source is not None:
        _SOURCE_MAP = {"ch": "clickhouse", "clickhouse": "clickhouse", "redis": "redis", "hybrid": "hybrid"}
        effective = _SOURCE_MAP.get(source, "clickhouse")
        config = replace(config, source=effective)
    engine = MonitorEngine(config)
    console = Console()

    # Signal handling for graceful exit
    stop_event = asyncio.Event()

    def _on_signal(signum: int, frame: Any) -> None:
        engine.request_stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Initialize (blocking CH connect — run in executor to avoid blocking the event loop)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, engine.initialize)
    if engine.state == MonitorState.ERROR:
        console.print(f"[red bold]ERROR:[/] {engine.error_msg}")
        console.print("Press q to exit")
        return 1

    def _make_display() -> Layout:
        layout = Layout()
        header_size = 4 if engine.get_header_context().event_ticker else 3

        parts = [
            Layout(Panel(engine.get_header(), style="dim"), size=header_size, name="header"),
            Layout(engine.get_table(), name="table"),
        ]

        # Phase 3: detail panel
        if engine._detail_visible:
            ss = engine.get_selected_symbol_state()
            detail = build_detail_panel(ss, config, engine._dispatcher.weights)
            parts.append(Layout(detail, size=8, name="detail"))

        layout.split_column(*parts)
        return layout

    with Live(
        _make_display(),
        console=console,
        refresh_per_second=2,
        screen=True,
    ) as live:
        # Non-blocking key reader
        key_task = asyncio.create_task(_key_listener(engine, stop_event))

        while not stop_event.is_set():
            try:
                # Offload to thread executor: _poller.poll() does a synchronous CH HTTP
                # query (100ms–2s); running it in a thread keeps the event loop free for
                # keyboard input and display refresh.
                await loop.run_in_executor(None, engine.poll_and_update)
            except Exception as exc:
                logger.error("poll_error", error=str(exc))

            live.update(_make_display())

            if engine.state == MonitorState.ERROR:
                live.update(_make_display())
                break

            # Wait for next poll or stop
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=config.poll_interval_s,
                )
            except asyncio.TimeoutError:
                pass

        key_task.cancel()
        try:
            await key_task
        except asyncio.CancelledError:
            pass

    return 0


async def _key_listener(engine: MonitorEngine, stop_event: asyncio.Event) -> None:
    """Listen for keyboard input in a non-blocking way."""
    import sys
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setcbreak(fd)
        reader = asyncio.get_event_loop()

        while not stop_event.is_set():
            # Read one char with timeout
            try:
                char = await asyncio.wait_for(
                    reader.run_in_executor(None, lambda: sys.stdin.read(1)),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                continue

            if char in ("q", "Q"):
                engine.request_stop()
                stop_event.set()
                break
            elif char in ("p", "P"):
                engine.toggle_pause()
            elif char in ("r", "R"):
                engine.reset_session()
            elif char in ("s", "S"):
                engine.cycle_sort_mode()
            elif char in ("j",):
                engine.move_selection(1)
            elif char in ("k",):
                engine.move_selection(-1)
            elif char in ("d", "\r", "\n"):
                engine.toggle_detail()
            elif char == "\x1b":
                # Escape sequence — check for arrow keys
                try:
                    c2 = await asyncio.wait_for(
                        reader.run_in_executor(None, lambda: sys.stdin.read(1)),
                        timeout=0.05,
                    )
                    if c2 == "[":
                        c3 = await asyncio.wait_for(
                            reader.run_in_executor(None, lambda: sys.stdin.read(1)),
                            timeout=0.05,
                        )
                        if c3 == "A":  # Up arrow
                            engine.move_selection(-1)
                        elif c3 == "B":  # Down arrow
                            engine.move_selection(1)
                    else:
                        # Plain ESC — close detail
                        engine.close_detail()
                except asyncio.TimeoutError:
                    # Plain ESC
                    engine.close_detail()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
