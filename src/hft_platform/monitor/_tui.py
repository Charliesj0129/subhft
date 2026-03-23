"""TUI entry point: run_monitor() + _key_listener() for the Signal Monitor."""

from __future__ import annotations

import asyncio
import signal
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from hft_platform.monitor._config_loader import load_watchlist
from hft_platform.monitor._engine import MonitorEngine
from hft_platform.monitor._types import MonitorState


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

    # S6: Dedicated poll executor — single thread to avoid concurrent CH queries
    poll_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="monitor-poll")

    # Signal handling for graceful exit
    stop_event = asyncio.Event()

    def _on_signal(signum: int, frame: Any) -> None:
        engine.request_stop()
        stop_event.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Initialize (blocking CH connect — run in dedicated executor)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(poll_executor, engine.initialize)
    if engine.state == MonitorState.ERROR:
        poll_executor.shutdown(wait=False)
        console.print(f"[red bold]ERROR:[/] {engine.error_msg}")
        console.print("Press q to exit")
        return 1

    def _make_display() -> Layout:
        from hft_platform.monitor._health_panel import (
            build_health_panel,
            get_cached_health,
            is_health_visible,
            poll_health,
        )
        from hft_platform.monitor._renderer import build_footer, build_help_overlay
        from hft_platform.monitor._types import Severity as _Severity

        layout = Layout()
        header_size = 4 if engine.get_header_context().event_ticker else 3

        parts = [
            Layout(Panel(engine.get_header(), style="dim"), size=header_size, name="header"),
        ]

        # System health panel (self-contained module)
        if is_health_visible():
            _live = _stale = _total = 0
            for _ss in engine._sym_states:
                if _ss.session_active:
                    _total += 1
                    if _ss.is_stale:
                        _stale += 1
                    elif _ss.tick_count > 0:
                        _live += 1
            poll_health(feed_live=_live, feed_stale=_stale, feed_total=_total)
            _health = get_cached_health()
            if _health is not None:
                parts.append(Layout(build_health_panel(_health), size=8, name="health"))

        # Footer help bar (computed before help overlay check)
        footer_text = build_footer(
            detail_visible=engine._detail_visible,
            paused=engine._paused_by_user,
            has_warnings=any(ss.max_severity >= _Severity.WARN for ss in engine._sym_states),
            show_help=engine._show_help,
        )

        # Help overlay replaces main content when active
        if engine._show_help:
            layout.split_column(
                Layout(build_help_overlay(), name="help"),
                Layout(footer_text, size=1, name="footer"),
            )
            return layout

        parts.append(Layout(engine.get_table(), name="table"))

        # Phase 3: detail panel
        if engine._detail_visible:
            ss = engine.get_selected_symbol_state()
            detail = build_detail_panel(ss, config, engine._dispatcher.weights)
            parts.append(Layout(detail, size=8, name="detail"))

        parts.append(Layout(footer_text, size=1, name="footer"))

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
                # S6: Offload to dedicated single-thread executor for poll isolation.
                await loop.run_in_executor(poll_executor, engine.poll_and_update)
            except Exception as exc:
                from structlog import get_logger

                get_logger("monitor.tui").error("poll_error", error=str(exc))

            live.update(_make_display())

            if engine.state == MonitorState.ERROR:
                live.update(_make_display())
                break

            # Wait for next poll or stop (skip wait if force poll requested)
            if engine._force_poll:
                engine._force_poll = False
            else:
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

    # S6: shutdown dedicated executor
    poll_executor.shutdown(wait=False)

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
            elif char == "R":
                engine.request_reconnect()
            elif char in ("r", "\x12"):  # r or Ctrl+R = full reset
                engine.reset_session()
            elif char == " ":
                engine.request_force_poll()
            elif char == "x":
                engine.clear_warnings()
            elif char == "w":
                engine.toggle_warning_filter()
            elif char == "?":
                engine.toggle_help()
            elif char == "e":
                engine.toggle_event_log()
            elif char == "l":
                engine.toggle_problem_log()
            elif char in ("s", "S"):
                engine.cycle_sort_mode()
            elif char in ("c", "C"):
                engine.toggle_closed_collapse()
            elif char in ("h", "H"):
                from hft_platform.monitor._health_panel import toggle_health_visible

                toggle_health_visible()
            elif char in ("j",):
                engine.move_selection(1)
            elif char in ("k",):
                engine.move_selection(-1)
            elif char in ("d", "\r", "\n"):
                engine.toggle_detail()
            elif char == "\x1b":
                # Escape sequence -- check for arrow keys
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
                        # Plain ESC -- close detail
                        engine.close_detail()
                except asyncio.TimeoutError:
                    # Plain ESC
                    engine.close_detail()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
