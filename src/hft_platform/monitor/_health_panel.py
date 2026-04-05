"""System health panel for the Signal Monitor TUI."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Sequence
from urllib.error import URLError
from urllib.request import urlopen

from rich.panel import Panel
from rich.style import Style
from rich.text import Text

_GREEN = Style(color="green")
_BRIGHT_GREEN = Style(color="bright_green", bold=True)
_RED = Style(color="red")
_BRIGHT_RED = Style(color="bright_red", bold=True)
_YELLOW = Style(color="yellow")
_WHITE = Style(color="white")
_DIM = Style(dim=True)
_HALT_BORDER = Style(color="bright_red", bold=True)
_STORM_BORDER = Style(color="red")
_WARN_BORDER = Style(color="yellow")
_OK_BORDER = Style(color="green")
_SG_LABELS: dict[int, tuple[str, Style]] = {
    0: ("NORMAL", _BRIGHT_GREEN),
    1: ("WARM", _YELLOW),
    2: ("STORM", _RED),
    3: ("HALT", _BRIGHT_RED),
}


@dataclass(slots=True)
class HealthState:
    """Snapshot of system health metrics."""

    stormguard_state: int = -1
    pnl_total: float = 0.0
    drawdown_pct: float = 0.0
    position_count: int = 0
    exposure_notional: float = 0.0
    feed_live_count: int = 0
    feed_total_count: int = 0
    feed_stale_count: int = 0
    feed_reconnects: int = 0
    circuit_breaker_state: int = 0
    order_rejects: int = 0
    recon_discrepancy_count: int = 0
    recon_last_success_ts: float = 0.0
    recon_consecutive_failures: int = 0
    wal_disk_available_mb: float = -1.0
    wal_circuit_breaker: int = 0
    engine_reachable: bool = False
    last_fetch_ts: float = 0.0


_current_health: HealthState | None = None
_health_visible: bool = True
_poll_counter: int = 0


def toggle_health_visible() -> None:
    global _health_visible  # noqa: PLW0603
    _health_visible = not _health_visible


def is_health_visible() -> bool:
    return _health_visible


def get_cached_health() -> HealthState | None:
    return _current_health


def poll_health(feed_live: int = 0, feed_stale: int = 0, feed_total: int = 0) -> HealthState | None:
    """Synchronous poll (runs fetch on calling thread). Prefer poll_health_async() in async contexts."""
    global _poll_counter, _current_health  # noqa: PLW0603
    _poll_counter += 1
    if _poll_counter % 5 != 1:
        return _current_health
    health = _fetch_from_prometheus(feed_total=feed_total)
    health.feed_live_count = feed_live
    health.feed_stale_count = feed_stale
    health.feed_total_count = feed_total
    _current_health = health
    return health


async def poll_health_async(feed_live: int = 0, feed_stale: int = 0, feed_total: int = 0) -> HealthState | None:
    """Async poll — offloads the blocking urlopen call to a thread executor.

    Use this from async contexts (e.g. TUI event loop) to avoid stalling the
    event loop for up to 2 seconds on a Prometheus timeout.
    """
    global _poll_counter, _current_health  # noqa: PLW0603
    _poll_counter += 1
    if _poll_counter % 5 != 1:
        return _current_health
    health = await asyncio.to_thread(_fetch_from_prometheus, feed_total=feed_total)
    health.feed_live_count = feed_live
    health.feed_stale_count = feed_stale
    health.feed_total_count = feed_total
    _current_health = health
    return health


def _parse_metric(lines: Sequence[str], name: str, labels: str = "") -> float | None:
    prefix = f"{name}{{{labels}}}" if labels else f"{name} "
    for line in lines:
        if line.startswith("#"):
            continue
        if labels and line.startswith(prefix):
            parts = line.split("} ")
            if len(parts) >= 2:
                try:
                    return float(parts[1].split()[0])
                except (ValueError, IndexError):
                    continue
        elif not labels and line.startswith(prefix):
            try:
                return float(line.split()[1])
            except (ValueError, IndexError):
                continue
    return None


def _parse_counter_total(lines: Sequence[str], name: str, labels: str = "") -> float:
    val = _parse_metric(lines, f"{name}_total", labels)
    if val is not None:
        return val
    val = _parse_metric(lines, name, labels)
    return val if val is not None else 0.0


def _fetch_from_prometheus(
    host: str | None = None,
    port: int | None = None,
    feed_total: int = 0,
) -> HealthState:
    host = host or os.getenv("HFT_HEALTH_HOST", "localhost")
    port = port or int(os.getenv("HFT_HEALTH_PORT", "9090"))
    state = HealthState(feed_total_count=feed_total)
    try:
        with urlopen(f"http://{host}:{port}/metrics", timeout=2.0) as resp:  # noqa: S310  # nosec B310
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, TimeoutError):
        return state
    state.engine_reachable = True
    state.last_fetch_ts = time.monotonic()
    lines = raw.splitlines()
    sg = _parse_metric(lines, "stormguard_mode", 'strategy="system"')
    state.stormguard_state = int(sg) if sg is not None else -1
    for attr, metric in [
        ("pnl_total", "portfolio_total_pnl"),
        ("drawdown_pct", "portfolio_drawdown_pct"),
        ("wal_disk_available_mb", "wal_disk_available_mb"),
    ]:
        v = _parse_metric(lines, metric)
        if v is not None:
            setattr(state, attr, v)
    for attr, metric in [
        ("recon_discrepancy_count", "reconciliation_discrepancy_count"),
        ("recon_consecutive_failures", "reconciliation_consecutive_failures"),
        ("circuit_breaker_state", "circuit_breaker_state"),
        ("wal_circuit_breaker", "wal_disk_circuit_breaker_active"),
    ]:
        v = _parse_metric(lines, metric)
        if v is not None:
            setattr(state, attr, int(v))
    rt = _parse_metric(lines, "reconciliation_last_success_ts")
    if rt is not None:
        state.recon_last_success_ts = rt
    return state


def build_health_panel(health: HealthState) -> Panel:
    lines = Text()
    if not health.engine_reachable:
        lines.append(" Engine: UNREACHABLE\n", _BRIGHT_RED)
        lines.append(" (check HFT engine on port 9090)\n", _DIM)
        return Panel(lines, title="System Health", border_style=_BRIGHT_RED, height=8)
    sg_l, sg_s = _SG_LABELS.get(health.stormguard_state, ("??", _DIM))
    lines.append(f" StormGuard: [{sg_l}]", sg_s)
    pnl = health.pnl_total / 10000.0
    lines.append(f"  PnL: {pnl:+,.0f} NTD", _GREEN if pnl >= 0 else _RED)
    dd = health.drawdown_pct * 100.0
    dd_s = _GREEN if dd > -0.5 else (_YELLOW if dd > -1.0 else _RED)
    lines.append(f"  DD: {dd:+.1f}%", dd_s)
    if health.stormguard_state == 3:
        lines.append("  HALTED!", _BRIGHT_RED)
    lines.append("\n")
    exp = health.exposure_notional / 10000.0
    lines.append(f" Pos: {health.position_count}  Exp: {exp:,.0f} NTD\n", _WHITE)
    st = health.feed_stale_count
    f_s = _GREEN if st == 0 else _YELLOW
    lines.append(
        f" Feed: {health.feed_live_count}/{health.feed_total_count}  Stale: {st}  Recon: {health.feed_reconnects}\n",
        f_s,
    )
    cb = health.circuit_breaker_state == 0
    lines.append(
        f" Orders: {'OK' if cb else 'BLOCKED'}  Rej: {health.order_rejects}  CB: {'CLOSED' if cb else 'OPEN'}\n",
        _GREEN if cb else _BRIGHT_RED,
    )
    if health.recon_consecutive_failures > 0:
        rl, rs = f"FAIL({health.recon_consecutive_failures})", _RED
    elif health.recon_discrepancy_count > 0:
        rl, rs = f"DRIFT {health.recon_discrepancy_count}", _YELLOW
    else:
        rl, rs = "SYNCED", _GREEN
    lines.append(f" Recon: {rl}\n", rs)
    if health.wal_disk_available_mb >= 0:
        d_s = _GREEN if health.wal_disk_available_mb > 500 else _RED
        lines.append(f" Disk: {health.wal_disk_available_mb:,.0f}MB", d_s)
    w = not health.wal_circuit_breaker
    lines.append(f"  WAL: {'OK' if w else 'BLOCKED'}\n", _GREEN if w else _BRIGHT_RED)
    sg = health.stormguard_state
    bd = {3: _HALT_BORDER, 2: _STORM_BORDER, 1: _WARN_BORDER}.get(sg, _OK_BORDER)
    return Panel(lines, title="System Health", subtitle=" [h] toggle ", border_style=bd, height=8)
