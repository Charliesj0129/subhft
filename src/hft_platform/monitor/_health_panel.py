"""System health panel for the Signal Monitor TUI."""

from __future__ import annotations

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
    """Toggle health panel visibility."""
    global _health_visible  # noqa: PLW0603
    _health_visible = not _health_visible


def is_health_visible() -> bool:
    """Return whether the health panel should be displayed."""
    return _health_visible


def get_cached_health() -> HealthState | None:
    """Return the last fetched HealthState."""
    return _current_health


def poll_health(
    feed_live: int = 0,
    feed_stale: int = 0,
    feed_total: int = 0,
) -> HealthState | None:
    """Fetch health every 5th call (~10s at 2s poll interval)."""
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


def _parse_metric(
    lines: Sequence[str],
    name: str,
    labels: str = "",
) -> float | None:
    """Extract a single metric value from Prometheus text output."""
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


def _parse_counter_total(
    lines: Sequence[str],
    name: str,
    labels: str = "",
) -> float:
    """Parse a counter metric, handling _total suffix."""
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
    """Fetch system health from the HFT engine Prometheus endpoint."""
    host = host or os.getenv("HFT_HEALTH_HOST", "localhost")
    port = port or int(os.getenv("HFT_HEALTH_PORT", "9090"))
    url = f"http://{host}:{port}/metrics"
    state = HealthState(feed_total_count=feed_total)
    try:
        with urlopen(url, timeout=2.0) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, TimeoutError):
        return state
    state.engine_reachable = True
    state.last_fetch_ts = time.time()
    lines = raw.splitlines()
    sg = _parse_metric(lines, "stormguard_mode", 'strategy="system"')
    state.stormguard_state = int(sg) if sg is not None else -1
    pnl = _parse_metric(lines, "portfolio_total_pnl")
    state.pnl_total = pnl if pnl is not None else 0.0
    dd = _parse_metric(lines, "portfolio_drawdown_pct")
    state.drawdown_pct = dd if dd is not None else 0.0
    rc = _parse_metric(lines, "reconciliation_discrepancy_count")
    state.recon_discrepancy_count = int(rc) if rc is not None else 0
    rt = _parse_metric(lines, "reconciliation_last_success_ts")
    state.recon_last_success_ts = rt if rt is not None else 0.0
    rf = _parse_metric(lines, "reconciliation_consecutive_failures")
    state.recon_consecutive_failures = int(rf) if rf is not None else 0
    cb = _parse_metric(lines, "circuit_breaker_state")
    state.circuit_breaker_state = int(cb) if cb is not None else 0
    wd = _parse_metric(lines, "wal_disk_available_mb")
    state.wal_disk_available_mb = wd if wd is not None else -1.0
    wcb = _parse_metric(lines, "wal_disk_circuit_breaker_active")
    state.wal_circuit_breaker = int(wcb) if wcb is not None else 0
    return state


def build_health_panel(health: HealthState) -> Panel:
    """Render the 6-line system health panel."""
    lines = Text()
    if not health.engine_reachable:
        lines.append(" Engine: UNREACHABLE\n", _BRIGHT_RED)
        lines.append(" (check HFT engine on port 9090)\n", _DIM)
        lines.append(" StormGuard: --  PnL: --  DD: --\n", _DIM)
        return Panel(
            lines,
            title="System Health",
            border_style=_BRIGHT_RED,
            height=8,
        )
    sg_label, sg_style = _SG_LABELS.get(
        health.stormguard_state,
        ("??", _DIM),
    )
    lines.append(f" StormGuard: [{sg_label}]", sg_style)
    pnl_ntd = health.pnl_total / 10000.0
    pnl_s = _GREEN if pnl_ntd >= 0 else _RED
    lines.append(f"  PnL: {pnl_ntd:+,.0f} NTD", pnl_s)
    dd_pct = health.drawdown_pct * 100.0
    if dd_pct > -0.5:
        dd_s = _GREEN
    elif dd_pct > -1.0:
        dd_s = _YELLOW
    else:
        dd_s = _RED
    lines.append(f"  DD: {dd_pct:+.1f}%", dd_s)
    if health.stormguard_state == 3:
        lines.append("  HALTED!", _BRIGHT_RED)
    lines.append("\n")
    exp = health.exposure_notional / 10000.0
    lines.append(
        f" Pos: {health.position_count} open  Exp: {exp:,.0f} NTD\n",
        _WHITE,
    )
    stale = health.feed_stale_count
    live = health.feed_live_count
    tot = health.feed_total_count
    rc = health.feed_reconnects
    f_s = _GREEN if stale == 0 else _YELLOW
    lines.append(
        f" Feed: {live}/{tot} live  Stale: {stale}  Reconnects: {rc}\n",
        f_s,
    )
    cb_ok = health.circuit_breaker_state == 0
    ok_s = "OK" if cb_ok else "BLOCKED"
    cb_s = "CLOSED" if cb_ok else "OPEN"
    rej = health.order_rejects
    lines.append(
        f" Orders: {ok_s}  Rejects: {rej}  CB: {cb_s}\n",
        _GREEN if cb_ok else _BRIGHT_RED,
    )
    if health.recon_consecutive_failures > 0:
        rl = f"FAILING ({health.recon_consecutive_failures})"
        rs = _RED
    elif health.recon_discrepancy_count > 0:
        rl = f"DRIFT {health.recon_discrepancy_count}"
        rs = _YELLOW
    else:
        rl = "SYNCED"
        rs = _GREEN
    cf = health.recon_consecutive_failures
    lines.append(f" Recon: {rl}  Failures: {cf}\n", rs)
    disk_mb = health.wal_disk_available_mb
    if disk_mb >= 0:
        d_s = _GREEN if disk_mb > 500 else _RED
        lines.append(f" Disk: {disk_mb:,.0f}MB  ", d_s)
    wal_ok = not health.wal_circuit_breaker
    wal_txt = "OK" if wal_ok else "BLOCKED"
    lines.append(
        f"WAL: {wal_txt}\n",
        _GREEN if wal_ok else _BRIGHT_RED,
    )
    sg = health.stormguard_state
    if sg == 3:
        border = _HALT_BORDER
    elif sg == 2:
        border = _STORM_BORDER
    elif sg == 1:
        border = _WARN_BORDER
    else:
        border = _OK_BORDER
    return Panel(
        lines,
        title="System Health",
        subtitle=" [h] toggle ",
        border_style=border,
        height=8,
    )
