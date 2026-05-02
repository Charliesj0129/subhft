"""Main engine: state machine, polling loop, and rendering helpers."""

from __future__ import annotations

import datetime as dt
from typing import Any

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.monitor._alpha_dispatcher import AlphaDispatcher
from hft_platform.monitor._ch_poller import CHPoller
from hft_platform.monitor._data_source import (
    CHDataSource,
    DataSource,
    HybridDataSource,
    RedisHybridSource,
    ShmDataSource,
)
from hft_platform.monitor._enrichment import classify_problem, enrich_tick, validate_l1_row
from hft_platform.monitor._events import (
    compute_opportunity_score,
    detect_events,
    format_event_label,
    snapshot_prev,
)
from hft_platform.monitor._renderer import build_header_with_toast, build_table
from hft_platform.monitor._session import _TZ_TAIPEI, format_next_open, get_session_info, get_session_start
from hft_platform.monitor._types import (
    _EVENT_RING_SIZE,
    EventFlag,
    HeaderContext,
    MonitorConfig,
    MonitorEvent,
    MonitorState,
    ProblemEntry,
    RowView,
    Severity,
    SymbolState,
    Toast,
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
        # S1: heartbeat
        "_poll_count",
        "_last_poll_ns",
        # S3: collapsed closed symbols
        "_closed_collapsed",
        # Production readiness: toast, help, event log, warning filter, force poll, problem log
        "_toast",
        "_show_help",
        "_show_event_log",
        "_warning_filter",
        "_force_poll",
        "_show_problem_log",
        # Cost attribution panel
        "_cost_lines",
        "_cost_last_fetch_ns",
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
        # S1: heartbeat
        self._poll_count: int = 0
        self._last_poll_ns: int = 0
        # S3: collapsed closed symbols
        self._closed_collapsed: bool = True
        # Production readiness
        self._toast: Toast | None = None
        self._show_help: bool = False
        self._show_event_log: bool = False
        self._warning_filter: bool = False
        self._force_poll: bool = False
        self._show_problem_log: bool = False
        # Cost attribution panel
        self._cost_lines: list[str] = []
        self._cost_last_fetch_ns: int = 0

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

        # S1: heartbeat tracking
        self._poll_count += 1
        self._last_poll_ns = timebase.now_ns()

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
            logger.warning("source_disconnected", error=self._data_source.last_error)
            return

        # Process each symbol's rows
        now_ns = timebase.now_ns()
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

        # Refresh cost attribution (every 60s)
        self._maybe_refresh_cost(now_ns)

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
        self._set_toast(f"Sort: {_SORT_LABELS[self._sort_mode]} \u21bb", "cyan")

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
        self._set_toast("\u23f8 Paused" if self._paused_by_user else "\u25b6 Resumed", "cyan")

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

    # S3: Collapsed closed symbols
    def toggle_closed_collapse(self) -> None:
        """Toggle whether closed symbols are collapsed into a summary row."""
        self._closed_collapsed = not self._closed_collapsed
        self._set_toast("Closed: hidden" if self._closed_collapsed else "Closed: shown", "cyan")

    # ------------------------------------------------------------------ #
    # Production readiness: warnings, polling, reconnect, toggles          #
    # ------------------------------------------------------------------ #

    def clear_warnings(self) -> None:
        now = timebase.now_ns()
        for ss in self._sym_states:
            ss.invalid_row_count = 0
            ss.max_severity = Severity.INFO
            ss.problem_log.append(
                ProblemEntry(
                    ts_ns=now,
                    severity=Severity.INFO,
                    message="Warnings cleared by user",
                )
            )
        self._set_toast("\u2713 Warnings cleared", "green")

    def request_force_poll(self) -> None:
        self._force_poll = True
        self._set_toast("\u27f3 Polling...", "cyan")

    def request_reconnect(self) -> None:
        self._set_toast("\u27f3 Reconnecting...", "cyan")
        if self._data_source is not None:
            ok = self._data_source.try_reconnect()
            if ok:
                self.clear_warnings()
                self._force_poll = True
                self._set_toast("\u2713 Connected", "green")
            else:
                self._set_toast("\u2716 Reconnect failed", "red")

    def _set_toast(self, message: str, style: str) -> None:
        self._toast = Toast(message=message, style=style, expire_ns=timebase.now_ns() + 2_000_000_000)

    def toggle_help(self) -> None:
        self._show_help = not self._show_help

    def toggle_warning_filter(self) -> None:
        self._warning_filter = not self._warning_filter
        label = "warnings only" if self._warning_filter else "all symbols"
        self._set_toast(f"Filter: {label}", "cyan")

    def toggle_event_log(self) -> None:
        self._show_event_log = not self._show_event_log

    def toggle_problem_log(self) -> None:
        self._show_problem_log = not self._show_problem_log

    # ------------------------------------------------------------------ #
    # Cost attribution refresh                                             #
    # ------------------------------------------------------------------ #

    _COST_REFRESH_NS: int = 60_000_000_000  # 60s

    def _maybe_refresh_cost(self, now_ns: int) -> None:
        """Refresh cost attribution from ClickHouse every 60s."""
        if now_ns - self._cost_last_fetch_ns < self._COST_REFRESH_NS:
            return
        self._cost_last_fetch_ns = now_ns

        ch_client = self._get_ch_client()
        if ch_client is None:
            return

        from hft_platform.monitor._pnl_panel import fetch_cost_attribution, render_cost_table

        now = self._last_now or dt.datetime.now(_TZ_TAIPEI)
        date_str = now.strftime("%Y-%m-%d")
        data = fetch_cost_attribution(ch_client, date_str)
        self._cost_lines = render_cost_table(data)

    def _get_ch_client(self) -> Any:
        """Extract a ClickHouse client from the current data source, if available."""
        ds = self._data_source
        if ds is None:
            return None
        # CHDataSource wraps a CHPoller which owns the client
        if isinstance(ds, CHDataSource):
            poller = getattr(ds, "_poller", None)
            return getattr(poller, "_client", None)
        # HybridDataSource has a _ch_source
        ch_src = getattr(ds, "_ch_source", None)
        if ch_src is not None:
            poller = getattr(ch_src, "_poller", None)
            return getattr(poller, "_client", None)
        return None

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
            # Check heartbeat staleness via protocol property
            hb_stale = ds.heartbeat_stale
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

        # S1: heartbeat age
        now_ns = timebase.now_ns()
        poll_age_s = (now_ns - self._last_poll_ns) / 1e9 if self._last_poll_ns > 0 else 0.0

        # S3: count closed symbols
        n_closed = sum(1 for ss in self._sym_states if ss.is_closed)

        # Bad-data summary for header
        total_bad = sum(ss.invalid_row_count for ss in self._sym_states)
        total_rows = sum(ss.tick_count + ss.invalid_row_count for ss in self._sym_states)
        any_active_now = any(ss.session_active for ss in self._sym_states)

        if total_bad == 0:
            _bad_summary, _bad_style = "", ""
        elif not any_active_now:
            _bad_summary = f"{total_bad:,} pre-market rows skipped (normal)"
            _bad_style = "dim"
        else:
            ratio = total_bad / max(total_rows, 1) * 100
            if ratio < 5:
                _bad_summary = f"{total_bad:,} L1 gaps ({ratio:.1f}%) \u2014 within tolerance"
                _bad_style = "dim yellow"
            elif ratio < 20:
                _bad_summary = f"\u26a0 {total_bad:,} L1 gaps ({ratio:.0f}%) \u2014 feed degraded"
                _bad_style = "yellow"
            else:
                _bad_summary = f"\u2716 feed critically degraded ({ratio:.0f}% invalid)"
                _bad_style = "bright_red"

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
            poll_count=self._poll_count,
            poll_age_s=poll_age_s,
            closed_collapsed=self._closed_collapsed,
            n_closed=n_closed,
            bad_summary=_bad_summary,
            bad_style=_bad_style,
        )

    def _build_event_ticker(self) -> str:
        """Build event ticker string from ring buffer (most recent first, max 3)."""
        if self._event_ring_len == 0:
            return ""
        now_ns = timebase.now_ns()
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
        """Build header Text for current state (with toast if active)."""
        return build_header_with_toast(self.get_header_context(), self._toast)

    def get_table(self) -> Any:
        """Build the signal table."""
        return build_table(
            self._sym_states_sorted,
            self._config,
            self._state,
            self._alpha_cols or None,
            selected_idx=self._selected_idx if self._detail_visible else -1,
            closed_collapsed=self._closed_collapsed,
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
        if self._data_source is None:
            raise RuntimeError("_bootstrap_symbol called before data source initialized")
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
            ss.session_started_ns = min_ingest_ts or timebase.now_ns()
            ss.cursor_ts_ns = max(0, (min_ingest_ts - 1) if min_ingest_ts > 0 else 0)
        for row in rows:
            self._process_row(ss, row)

    def _bootstrap_all_symbols(self) -> None:
        """Warm symbols from recent valid history so ready rows appear immediately.

        S4: Only bootstrap symbols with active sessions to avoid blocking on
        closed-session symbols during init.
        """
        if self._data_source is None:
            return
        self._state = MonitorState.WARMING_UP
        for ss in self._sym_states:
            if ss.session_active:
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
                    logger.warning("source_disconnected", error=self._data_source.last_error)
                    return

    def _process_row(self, ss: SymbolState, row: RowView) -> None:
        """Advance cursor for a row and dispatch only when L1 data is valid."""
        ss.cursor_ts_ns = max(ss.cursor_ts_ns, row.ingest_ts)
        ss.last_seen_ts_ns = max(ss.last_seen_ts_ns, row.ingest_ts)

        invalid_reason = validate_l1_row(row)
        if invalid_reason is not None:
            ss.invalid_row_count += 1
            ss.last_invalid_reason = invalid_reason
            severity = classify_problem(
                invalid_reason,
                is_active=ss.session_active,
                session_label=ss.session_label,
            )
            if severity > ss.max_severity:
                ss.max_severity = severity
            ss.problem_log.append(
                ProblemEntry(
                    ts_ns=timebase.now_ns(),
                    severity=severity,
                    message=invalid_reason,
                )
            )
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
        ss.max_severity = Severity.INFO
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
        ss.prev_poll_price = 0.0
        self._dispatcher.reset_symbol(ss)
        ss.sparkline_clear()
        ss.price_sparkline_clear()

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
        now_ns = timebase.now_ns()
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
