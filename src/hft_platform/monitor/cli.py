"""CLI wrapper for the signal monitor TUI."""

from __future__ import annotations

import asyncio


def run_cli(
    watchlist_path: str | None = None,
    symbols_path: str | None = None,
    source: str | None = None,
) -> int:
    """Run the monitor TUI from the main CLI."""
    from hft_platform.monitor._engine import run_monitor

    return asyncio.run(run_monitor(
        watchlist_path=watchlist_path,
        symbols_path=symbols_path,
        source=source,
    ))
