"""Rich-based table renderer for Signal Monitor TUI."""

from __future__ import annotations

import math
import time
from typing import Sequence

from rich.style import Style
from rich.table import Table
from rich.text import Text

from hft_platform.monitor._events import _ALPHA_SHORT, dominant_alpha_label
from hft_platform.monitor._types import AlphaState, HeaderContext, MonitorConfig, MonitorState, SymbolState

# Sparkline Unicode blocks (8 levels)
_SPARK_CHARS = "▁▂▃▄▅▆▇█"

# Color thresholds
_BRIGHT_GREEN = Style(color="bright_green", bold=True)
_GREEN = Style(color="green")
_WHITE = Style(color="white")
_RED = Style(color="red")
_BRIGHT_RED = Style(color="bright_red", bold=True)
_DIM = Style(dim=True)
_YELLOW = Style(color="yellow")
_ERROR_STYLE = Style(color="red", bold=True)
_CYAN = Style(color="cyan")
_BOLD_WHITE = Style(color="bright_white", bold=True)

# Source badge styles (module-level to avoid per-render allocation)
_SRC_STYLES: dict[str, Style] = {
    "CH": Style(dim=True),
    "SHM": Style(dim=True),
    "REDIS": Style(color="cyan"),
    "REDIS+CH": Style(color="yellow"),
    "SHM+CH": Style(color="yellow"),
}

# Row flash styles (Phase 4)
_FLASH_BOLD = Style(bold=True)
_FLASH_BRIGHT = Style(color="bright_white")

# Pre-built state badge styles (avoid per-render dict creation)
_STATE_STYLES: dict[MonitorState, Style] = {
    MonitorState.INITIALIZING: Style(color="cyan"),
    MonitorState.WARMING_UP: Style(color="yellow"),
    MonitorState.LIVE: Style(color="bright_green", bold=True),
    MonitorState.STALE: Style(color="yellow", bold=True),
    MonitorState.PAUSED: Style(color="blue"),
    MonitorState.DISCONNECTED: Style(color="red", bold=True),
    MonitorState.ERROR: Style(color="bright_red", bold=True),
}

# Selected row background (Phase 3)
_SELECTED_BG = Style(bgcolor="grey23")


def build_header(ctx: HeaderContext) -> Text:
    """Build the status header line from a HeaderContext."""
    parts = Text()

    # Line 1: State badge + session + time + CH + sort mode
    parts.append(f"[{ctx.state.name}]", _STATE_STYLES.get(ctx.state, _WHITE))
    parts.append(f" {ctx.session_display}  ", _WHITE)

    if ctx.extra:
        parts.append(f"{ctx.extra}  ", _DIM)

    parts.append(f"|  {ctx.time_str}  |  {ctx.ch_status}", _DIM)

    if ctx.stale_symbols:
        parts.append(f"  |  Stale: {', '.join(ctx.stale_symbols)}", _YELLOW)

    parts.append(f"  |  sorted: {ctx.sort_mode}", _DIM)

    # Source badge
    if ctx.source_label:
        src_style = _SRC_STYLES.get(ctx.source_label, _DIM)
        parts.append(f"  |  src: {ctx.source_label}", src_style)

    # Line 2: Event ticker (Phase 1)
    if ctx.event_ticker:
        parts.append(f"\n{ctx.event_ticker}")

    return parts


def build_table(
    symbol_states: Sequence[SymbolState],
    config: MonitorConfig,
    state: MonitorState,
    alpha_cols: Sequence[str] | None = None,
    selected_idx: int = -1,
) -> Table:
    """Build the main signal table."""
    table = Table(
        show_header=True,
        header_style="bold cyan",
        border_style="dim",
        padding=(0, 1),
        expand=True,
    )

    # Define columns — Phase 2: add rank column, Phase 1: Action+Drivers replace individual alphas
    table.add_column("#", justify="right", width=2)
    table.add_column("Symbol", style="bold", width=8)
    table.add_column("Name", width=6)
    table.add_column("State", width=7)
    table.add_column("Last", justify="right", width=8)
    table.add_column("Sprd", justify="right", width=6)

    # Dynamic alpha columns — use pre-computed list when provided
    if alpha_cols is None:
        alpha_cols_list: list[str] = []
        if symbol_states:
            seen: set[str] = set()
            for ss in symbol_states:
                for aid in ss.symbol.alpha_ids:
                    if aid not in seen:
                        seen.add(aid)
                        alpha_cols_list.append(aid)
        alpha_cols = alpha_cols_list

    for aid in alpha_cols:
        label = _short_alpha_label(aid)
        table.add_column(label, justify="right", width=6)

    table.add_column("Comp", justify="right", width=6)
    table.add_column("Str", justify="right", width=5)
    table.add_column("Agree", justify="center", width=7)
    table.add_column("Action", justify="center", width=10)
    table.add_column("Drivers", justify="center", width=8)
    table.add_column("Spark", width=20)

    now_ns = time.time_ns()

    for rank, ss in enumerate(symbol_states, 1):
        row: list[Text] = []
        row_style = _get_row_style(ss, state, now_ns)
        is_selected = (rank - 1) == selected_idx

        # Rank
        row.append(Text(str(rank), style=_DIM))

        # Symbol + quality badges (Phase 4)
        sym_text = Text(ss.symbol.code)
        if ss.session_label:
            sym_text.append(f" {ss.session_label}", _DIM)
        if ss.invalid_row_count > 5:
            sym_text.append(" [!L1]", _YELLOW)
        row.append(sym_text)

        # Name (truncated to 6 chars)
        row.append(Text(ss.symbol.name[:6]))

        # State — compact badges (Phase 2)
        status_text, status_style = _render_symbol_status(ss, config, state, now_ns)
        row.append(Text(status_text, style=status_style))

        # Last price
        if ss.last_price > 0:
            row.append(Text(_format_price(ss.last_price)))
        else:
            row.append(Text("--", style=_DIM))

        # Spread
        if ss.spread_bps > 0:
            row.append(Text(f"{ss.spread_bps:.1f}", style=row_style))
        else:
            row.append(Text("--", style=_DIM))

        # Alpha signals — colored by z-score (Phase 4)
        for aid in alpha_cols:
            astate = ss.alpha_states.get(aid)
            row.append(_render_alpha_cell(astate, ss.tick_count, config.warmup_ticks))

        # Composite
        if ss.tick_count >= config.warmup_ticks:
            comp_text = _format_signal(ss.composite)
            row.append(Text(comp_text, style=_signal_style(ss.composite)))
        else:
            row.append(Text("--", style=_DIM))

        # Strength (sigma)
        strength = abs(ss.composite)
        if ss.tick_count >= config.warmup_ticks:
            row.append(Text(f"{strength:.1f}σ", style=_signal_style_sigma(strength)))
        else:
            row.append(Text("--", style=_DIM))

        # Count directions once for agreement, action, and drivers
        pos, neg, total = _count_directions(ss, alpha_cols, config.warmup_ticks)

        # Agreement
        agreement_text, agreement_style = _format_agreement(pos, neg, total, ss.tick_count, config.warmup_ticks)
        row.append(Text(agreement_text, style=agreement_style))

        # Action — Phase 1: actionable suggestion with alpha names
        action_text, action_style = _format_action(ss, pos, neg, total, config.warmup_ticks)
        row.append(Text(action_text, style=action_style))

        # Drivers — Phase 1: compact alpha direction indicators
        drivers_text = _format_drivers(ss, alpha_cols, config.warmup_ticks)
        row.append(drivers_text)

        # Sparkline
        row.append(Text(_render_sparkline(ss.sparkline_values())))

        if is_selected:
            table.add_row(*row, style=_SELECTED_BG)
        else:
            table.add_row(*row)

    return table


def _short_alpha_label(alpha_id: str) -> str:
    """Convert alpha_id to short column header."""
    return _ALPHA_SHORT.get(alpha_id, alpha_id[:5].upper())


def _format_price(price: float) -> str:
    """Format price for display."""
    if price >= 1000:
        return f"{price:,.0f}"
    if price >= 100:
        return f"{price:.0f}"
    return f"{price:.1f}"


def _format_signal(value: float) -> str:
    """Format signal value."""
    if math.isnan(value):
        return "--"
    return f"{value:+.2f}"


def _signal_style(value: float) -> Style:
    """Color-code signal value."""
    if math.isnan(value):
        return _DIM
    if value > 0.5:
        return _BRIGHT_GREEN
    if value > 0.2:
        return _GREEN
    if value < -0.5:
        return _BRIGHT_RED
    if value < -0.2:
        return _RED
    return _WHITE


def _signal_style_sigma(sigma: float) -> Style:
    """Color-code by sigma magnitude."""
    if sigma > 2.0:
        return _BRIGHT_GREEN
    if sigma > 1.0:
        return _GREEN
    return _WHITE


def _z_score_style(z: float) -> Style:
    """Color alpha cell by z-score magnitude (Phase 4)."""
    az = abs(z)
    if z > 0:
        if az >= 2.0:
            return _BRIGHT_GREEN
        if az >= 1.0:
            return _GREEN
        if az >= 0.5:
            return Style(color="green", dim=True)
        return _WHITE
    if z < 0:
        if az >= 2.0:
            return _BRIGHT_RED
        if az >= 1.0:
            return _RED
        if az >= 0.5:
            return Style(color="red", dim=True)
        return _WHITE
    return _WHITE


def _render_alpha_cell(
    astate: AlphaState | None,
    tick_count: int,
    warmup_ticks: int,
) -> Text:
    """Render a single alpha signal cell — colored by z-score (Phase 4)."""
    if astate is None:
        return Text("--", style=_DIM)

    if astate.disabled:
        return Text("OFF", style=_ERROR_STYLE)

    if astate.error_count > 0 and math.isnan(astate.signal):
        return Text("ERR", style=_ERROR_STYLE)

    if tick_count < warmup_ticks:
        return Text("--", style=_DIM)

    if math.isnan(astate.signal):
        return Text("--", style=_DIM)

    val_str = _format_signal(astate.signal)
    return Text(val_str, style=_z_score_style(astate.z_score))


def _count_directions(
    ss: SymbolState,
    alpha_cols: Sequence[str],
    warmup_ticks: int,
) -> tuple[int, int, int]:
    """Count positive, negative, and total active alpha signals. Single iteration."""
    if ss.tick_count < warmup_ticks:
        return 0, 0, 0

    pos = 0
    neg = 0
    total = 0
    for aid in alpha_cols:
        astate = ss.alpha_states.get(aid)
        if astate is None or astate.disabled or math.isnan(astate.signal):
            continue
        total += 1
        if astate.signal > 0:
            pos += 1
        elif astate.signal < 0:
            neg += 1
    return pos, neg, total


def _format_agreement(
    pos: int,
    neg: int,
    total: int,
    tick_count: int,
    warmup_ticks: int,
) -> tuple[str, Style]:
    """Format directional agreement from pre-counted directions."""
    if tick_count < warmup_ticks or total == 0:
        return "--", _DIM

    dominant = max(pos, neg)
    direction = "▲" if pos >= neg else "▼"
    pct = dominant / total

    text = f"{dominant}/{total} {direction}"
    if pct >= 0.75:
        style = _GREEN if pos >= neg else _RED
    else:
        style = _YELLOW

    return text, style


def _format_action(
    ss: SymbolState,
    pos: int,
    neg: int,
    total: int,
    warmup_ticks: int,
) -> tuple[str, Style]:
    """Format actionable suggestion with driving alpha names (Phase 1).

    Replaces old _format_suggestion with richer context.
    """
    if ss.tick_count < warmup_ticks:
        return "--", _DIM

    if total == 0:
        return "--", _DIM

    dominant = max(pos, neg)
    agree_pct = dominant / total

    if agree_pct < 0.5:
        return "mixed", _YELLOW

    sigma = abs(ss.composite)
    is_buy = ss.composite > 0
    dom = dominant_alpha_label(ss)
    suffix = f":{dom}" if dom else ""

    if sigma >= 2.0 and agree_pct >= 0.75:
        label = f"BUY{suffix}" if is_buy else f"SELL{suffix}"
        return label, _BRIGHT_GREEN if is_buy else _BRIGHT_RED
    if sigma >= 1.5 and agree_pct >= 0.75:
        label = f"buy{suffix}" if is_buy else f"sell{suffix}"
        return label, _GREEN if is_buy else _RED
    if sigma >= 1.0 and agree_pct >= 0.75:
        label = f"lean{suffix}" if is_buy else f"lean{suffix}"
        return label, _GREEN if is_buy else _RED

    return "--", _WHITE


def _format_drivers(
    ss: SymbolState,
    alpha_cols: Sequence[str],
    warmup_ticks: int,
) -> Text:
    """Format compact driver indicators: QI↑MM↑ (Phase 1).

    Only alphas with |z_score|>=1.0 shown.
    """
    t = Text()
    if ss.tick_count < warmup_ticks:
        t.append("--", _DIM)
        return t

    for aid in alpha_cols:
        astate = ss.alpha_states.get(aid)
        if astate is None or astate.disabled or math.isnan(astate.signal):
            continue
        if abs(astate.z_score) < 1.0:
            continue
        short = _ALPHA_SHORT.get(aid, aid[:2].upper())
        arrow = "↑" if astate.signal > 0 else "↓"
        style = _GREEN if astate.signal > 0 else _RED
        t.append(f"{short}{arrow}", style)

    if not t.plain:
        t.append("--", _DIM)
    return t


def _compute_agreement(
    ss: SymbolState,
    alpha_cols: list[str],
    warmup_ticks: int,
) -> tuple[str, Style]:
    """Compute directional agreement among alphas (backward compat)."""
    pos, neg, total = _count_directions(ss, alpha_cols, warmup_ticks)
    return _format_agreement(pos, neg, total, ss.tick_count, warmup_ticks)


def _format_suggestion(
    pos: int,
    neg: int,
    total: int,
    composite: float,
    tick_count: int,
    warmup_ticks: int,
) -> tuple[str, Style]:
    """Format trade suggestion from pre-counted directions (backward compat)."""
    if tick_count < warmup_ticks:
        return "--", _DIM

    if total == 0:
        return "flat", _DIM

    dominant = max(pos, neg)
    agree_pct = dominant / total

    if agree_pct < 0.5:
        return "mixed", _YELLOW

    sigma = abs(composite)
    is_buy = composite > 0

    if sigma >= 2.0 and agree_pct >= 0.75:
        label = "BUY↑↑" if is_buy else "SELL↓↓"
        return label, _BRIGHT_GREEN if is_buy else _BRIGHT_RED
    if sigma >= 1.5 and agree_pct >= 0.75:
        label = "BUY↑" if is_buy else "SELL↓"
        return label, _GREEN if is_buy else _RED
    if sigma >= 1.0 and agree_pct >= 0.75:
        label = "lean↑" if is_buy else "lean↓"
        return label, _GREEN if is_buy else _RED

    return "flat", _WHITE


def _compute_suggestion(
    ss: SymbolState,
    alpha_cols: list[str],
    warmup_ticks: int,
) -> tuple[str, Style]:
    """Compute trade suggestion (backward compat)."""
    pos, neg, total = _count_directions(ss, alpha_cols, warmup_ticks)
    return _format_suggestion(pos, neg, total, ss.composite, ss.tick_count, warmup_ticks)


def _render_sparkline(values: Sequence[float]) -> str:
    """Render sparkline from composite history values."""
    if not values:
        return ""

    mn = min(values)
    mx = max(values)
    rng = mx - mn

    if rng < 1e-10:
        return _SPARK_CHARS[3] * len(values)

    scale = 7.0 / rng
    return "".join(_SPARK_CHARS[max(0, min(7, int((v - mn) * scale)))] for v in values)


def _get_row_style(ss: SymbolState, state: MonitorState, now_ns: int = 0) -> Style:
    """Get base row style based on symbol/monitor state (Phase 4: flash for recent events)."""
    if state == MonitorState.DISCONNECTED:
        return _DIM
    if ss.is_closed:
        return _DIM

    # Phase 4: row flash for recent events
    if ss.last_event_ns > 0 and now_ns > 0:
        age_s = (now_ns - ss.last_event_ns) / 1e9
        if age_s < 2.0:
            return _FLASH_BOLD
        if age_s < 5.0:
            return _FLASH_BRIGHT

    if ss.is_stale:
        return _YELLOW
    return _WHITE


def _render_symbol_status(
    ss: SymbolState,
    config: MonitorConfig,
    state: MonitorState,
    now_ns: int = 0,
) -> tuple[str, Style]:
    """Render compact per-symbol runtime status (Phase 2: compact badges).

    ``now_ns`` is pre-computed once per render cycle to avoid per-row syscalls.
    Falls back to ``time.time_ns()`` when not provided (backward compat).
    """
    if state == MonitorState.DISCONNECTED:
        return "!CH", _ERROR_STYLE
    if ss.is_closed:
        return "---", _DIM
    if ss.session_label == "[PRE]":
        return "PRE", _DIM
    if ss.is_stale and ss.last_update_ns > 0:
        ts = now_ns or time.time_ns()
        age_s = max(0.0, (ts - ss.last_update_ns) / 1e9)
        return f"S{age_s:.0f}s", Style(color="yellow", bold=True)
    if ss.tick_count >= config.warmup_ticks:
        return "✓", _GREEN
    if ss.tick_count > 0:
        return f"W{ss.tick_count}/{config.warmup_ticks}", _CYAN
    if ss.session_active and ss.invalid_row_count > 0:
        return "!L1", _YELLOW
    if ss.session_active and ss.session_started_ns > 0:
        ts = now_ns or time.time_ns()
        age_s = (ts - ss.session_started_ns) / 1e9
        if age_s >= config.no_data_warn_s:
            return "NO DATA", _YELLOW
    return "WAIT", _DIM
