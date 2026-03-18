"""TUI entry point: run_monitor() + _key_listener() for the Signal Monitor."""

from __future__ import annotations

import asyncio
import signal
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
                # query (100ms-2s); running it in a thread keeps the event loop free for
                # keyboard input and display refresh.
                await loop.run_in_executor(None, engine.poll_and_update)
            except Exception as exc:
                from structlog import get_logger

                get_logger("monitor.tui").error("poll_error", error=str(exc))

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
