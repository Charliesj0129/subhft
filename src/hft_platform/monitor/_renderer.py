"""Rich-based table renderer for Signal Monitor TUI."""

from __future__ import annotations

import math
import unicodedata
from typing import Sequence

from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text

from hft_platform.core import timebase
from hft_platform.monitor._events import _ALPHA_SHORT, dominant_alpha_label
from hft_platform.monitor._types import (
    AlphaState,
    ColumnProfile,
    HeaderContext,
    MonitorConfig,
    MonitorState,
    Severity,
    SymbolState,
    Toast,
)

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

    # S1: Heartbeat indicator
    if ctx.poll_count > 0:
        age = ctx.poll_age_s
        if age < 1.0:
            hb_style = _GREEN
        elif age < 3.0:
            hb_style = _YELLOW
        else:
            hb_style = _BRIGHT_RED
        parts.append(f"  |  \u25cf poll #{ctx.poll_count} {age:.0f}s ago", hb_style)

    if ctx.stale_symbols:
        parts.append(f"  |  Stale: {', '.join(ctx.stale_symbols)}", _YELLOW)

    parts.append(f"  |  sorted: {ctx.sort_mode}", _DIM)

    # Source badge
    if ctx.source_label:
        src_style = _SRC_STYLES.get(ctx.source_label, _DIM)
        parts.append(f"  |  src: {ctx.source_label}", src_style)

    # Bad-data summary
    if ctx.bad_summary:
        _bad_style = Style.parse(ctx.bad_style) if ctx.bad_style else _DIM
        parts.append(f" | {ctx.bad_summary}", _bad_style)

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
    closed_collapsed: bool = True,
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
    # S2: Price delta column
    table.add_column("\u0394", justify="right", width=6)
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
    # S7: price sparkline instead of composite sparkline
    table.add_column("Spark", width=20)

    now_ns = timebase.now_ns()

    # S3: partition into active and closed for optional collapse
    active_states: list[SymbolState] = []
    closed_states: list[SymbolState] = []
    for ss in symbol_states:
        if ss.is_closed:
            closed_states.append(ss)
        else:
            active_states.append(ss)

    rank = 0
    for ss in active_states:
        rank += 1
        row = _build_symbol_row(ss, rank, config, state, alpha_cols, now_ns)
        is_selected = (rank - 1) == selected_idx
        if is_selected:
            table.add_row(*row, style=_SELECTED_BG)
        else:
            table.add_row(*row)

    # S3: closed symbols — collapsed or expanded
    if closed_states:
        if closed_collapsed:
            # Single summary row
            n_cols = len(table.columns)
            cells: list[Text] = [Text("", style=_DIM)] * n_cols
            cells[0] = Text("", style=_DIM)
            cells[1] = Text(f"[{len(closed_states)} closed]", style=_DIM)
            cells[2] = Text("press c", style=_DIM)
            table.add_row(*cells)
        else:
            for ss in closed_states:
                rank += 1
                row = _build_symbol_row(ss, rank, config, state, alpha_cols, now_ns)
                table.add_row(*row)

    return table


def _build_symbol_row(
    ss: SymbolState,
    rank: int,
    config: MonitorConfig,
    state: MonitorState,
    alpha_cols: Sequence[str],
    now_ns: int,
) -> list[Text]:
    """Build a single symbol row for the table."""
    row: list[Text] = []
    row_style = _get_row_style(ss, state, now_ns)

    # Rank
    row.append(Text(str(rank), style=_DIM))

    # Symbol + quality badges (Phase 4)
    sym_text = Text(ss.symbol.code)
    if ss.session_label:
        sym_text.append(f" {ss.session_label}", _DIM)
    if ss.max_severity >= Severity.CRIT:
        sym_text.append(" \u2716", _BRIGHT_RED)
    elif ss.max_severity >= Severity.WARN:
        sym_text.append(" \u26a0", _YELLOW)
    row.append(sym_text)

    # Name -- CJK-aware truncation + futures contract labels
    if ss.symbol.product_type == "future":
        name_display = format_contract_name(ss.symbol.code, ss.symbol.name)
    else:
        name_display = truncate_display(ss.symbol.name, 10)
    row.append(Text(name_display))

    # State — compact badges (Phase 2)
    status_text, status_style = _render_symbol_status(ss, config, state, now_ns)
    row.append(Text(status_text, style=status_style))

    # Last price
    if ss.last_price > 0:
        row.append(Text(_format_price(ss.last_price)))
    else:
        row.append(Text("--", style=_DIM))

    # S2: Price delta column
    if ss.prev_poll_price > 0 and ss.last_price > 0:
        delta = ss.last_price - ss.prev_poll_price
        if abs(delta) > 1e-8:
            arrow = "\u25b2" if delta > 0 else "\u25bc"
            delta_style = _GREEN if delta > 0 else _RED
            row.append(Text(f"{delta:+.1f}{arrow}", style=delta_style))
        else:
            row.append(Text("0", style=_DIM))
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
        row.append(Text(f"{strength:.1f}\u03c3", style=_signal_style_sigma(strength)))
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

    # S7: price sparkline instead of composite sparkline
    row.append(Text(_render_sparkline(ss.price_sparkline_values())))

    return row


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
    Falls back to ``timebase.now_ns()`` when not provided (backward compat).
    """
    if state == MonitorState.DISCONNECTED:
        return "!CH", _ERROR_STYLE
    if ss.is_closed:
        return "---", _DIM
    if ss.session_label == "[PRE]":
        return "PRE", _DIM
    if ss.is_stale and ss.last_update_ns > 0:
        ts = now_ns or timebase.now_ns()
        age_s = max(0.0, (ts - ss.last_update_ns) / 1e9)
        return f"S{age_s:.0f}s", Style(color="yellow", bold=True)
    if ss.tick_count >= config.warmup_ticks:
        return "✓", _GREEN
    if ss.tick_count > 0:
        return f"W{ss.tick_count}/{config.warmup_ticks}", _CYAN
    if ss.session_active and ss.max_severity >= Severity.CRIT:
        return "\u2716L1", _BRIGHT_RED
    if ss.session_active and ss.max_severity >= Severity.WARN:
        return "\u26a0L1", _YELLOW
    if ss.session_active and ss.session_started_ns > 0:
        ts = now_ns or timebase.now_ns()
        age_s = (ts - ss.session_started_ns) / 1e9
        if age_s >= config.no_data_warn_s:
            return "NO DATA", _YELLOW
    return "WAIT", _DIM


# ------------------------------------------------------------------ #
# Task 8: Footer help bar                                             #
# ------------------------------------------------------------------ #


def build_footer(
    *,
    detail_visible: bool,
    paused: bool,
    has_warnings: bool,
    show_help: bool,
) -> Text:
    """Build context-aware footer help bar."""
    if show_help:
        return Text("Press any key to close", style=_DIM)
    t = Text()
    if paused:
        t.append("[p]", _WHITE)
        t.append(" resume  ", _DIM)
        t.append("[Space]", _WHITE)
        t.append(" single poll  ", _DIM)
    elif detail_visible:
        t.append("[l]", _WHITE)
        t.append(" problem log  ", _DIM)
        t.append("[e]", _WHITE)
        t.append(" events  ", _DIM)
        t.append("[x]", _WHITE)
        t.append(" clear warns  ", _DIM)
        t.append("[ESC]", _WHITE)
        t.append(" close  ", _DIM)
    elif has_warnings:
        t.append("[w]", _WHITE)
        t.append(" warnings only  ", _DIM)
        t.append("[x]", _WHITE)
        t.append(" clear  ", _DIM)
        t.append("[R]", _WHITE)
        t.append(" reconnect  ", _DIM)
    else:
        t.append("[Space]", _WHITE)
        t.append(" refresh  ", _DIM)
        t.append("[j/k]", _WHITE)
        t.append(" nav  ", _DIM)
        t.append("[d]", _WHITE)
        t.append(" detail  ", _DIM)
        t.append("[s]", _WHITE)
        t.append(" sort  ", _DIM)
        t.append("[h]", _WHITE)
        t.append(" health  ", _DIM)
    t.append("[?]", _WHITE)
    t.append(" all keys", _DIM)
    return t


# ------------------------------------------------------------------ #
# Task 9: Help overlay (? key)                                        #
# ------------------------------------------------------------------ #


def build_help_overlay() -> Panel:
    """Build full-screen help overlay with all keybindings."""
    content = Text()
    sections = [
        ("Navigation", [
            ("j/k \u2191/\u2193", "Navigate symbols"),
            ("d/Enter", "Toggle detail panel"),
            ("ESC", "Close panel / clear"),
        ]),
        ("Data", [
            ("Space", "Force poll now"),
            ("s", "Cycle sort mode"),
            ("w", "Filter warnings only"),
            ("c", "Toggle closed symbols"),
            ("e", "Event log overlay"),
        ]),
        ("System", [
            ("x", "Clear warnings"),
            ("R", "Reconnect data source"),
            ("r/Ctrl+R", "Full reset (replay warmup)"),
            ("h", "Toggle health panel"),
            ("p", "Pause / resume"),
            ("q", "Quit"),
        ]),
        ("Detail Panel", [
            ("l", "Toggle problem log"),
        ]),
    ]
    for title, keys in sections:
        content.append(f"\u2500\u2500\u2500 {title} ", _DIM)
        content.append("\u2500" * (30 - len(title)) + "\n", _DIM)
        for key, desc in keys:
            content.append(f"  {key:<12}", _WHITE)
            content.append(f"{desc}\n", _DIM)
        content.append("\n")
    content.append("Press any key to close", _DIM)
    return Panel(content, title="Keyboard Shortcuts", border_style="dim")


# ------------------------------------------------------------------ #
# Task 10: Toast rendering in header                                  #
# ------------------------------------------------------------------ #


def build_header_with_toast(ctx: HeaderContext, toast: Toast | None = None) -> Text:
    """Build header text, appending toast notification if active."""
    parts = build_header(ctx)
    if toast is not None:
        now_ns = timebase.now_ns()
        if toast.expire_ns > now_ns:
            parts.append("  ", _DIM)
            parts.append(f" {toast.message} ", Style.parse(toast.style))
    return parts


# ------------------------------------------------------------------ #
# Task 15: CJK-aware truncation                                      #
# ------------------------------------------------------------------ #


def truncate_display(text: str, max_width: int) -> str:
    """Truncate string to max_width display columns, CJK-aware."""
    if not text:
        return text
    width = 0
    for i, ch in enumerate(text):
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > max_width:
            # Need to truncate: go back and add ellipsis
            # Find the cut point where we can fit ellipsis (1 col)
            cut_width = 0
            for j, c in enumerate(text):
                cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
                if cut_width + cw > max_width - 1:
                    return text[:j] + "\u2026"
                cut_width += cw
            return text[:i] + "\u2026"
        width += w
    return text


# ------------------------------------------------------------------ #
# Task 16: Contract month readable labels                             #
# ------------------------------------------------------------------ #

_PRODUCT_NAMES: dict[str, str] = {
    "TXF": "\u53f0\u6307\u671f",
    "MXF": "\u5c0f\u53f0\u6307",
    "TMF": "\u5fae\u53f0\u6307",
    "EXF": "\u96fb\u5b50\u671f",
    "FXF": "\u91d1\u878d\u671f",
    "TGF": "\u53f0\u91d1\u671f",
}
_MONTH_MAP: dict[str, str] = {chr(ord("A") + i): f"{i + 1:02d}" for i in range(12)}


def format_contract_name(code: str, raw_name: str) -> str:
    """Convert futures contract code to human-readable name."""
    if len(code) >= 4:
        prefix = code[:3]
        month_char = code[3] if len(code) > 3 else ""
        product = _PRODUCT_NAMES.get(prefix)
        month = _MONTH_MAP.get(month_char)
        if product and month:
            return f"{product} {month}\u6708"
    return truncate_display(raw_name, 10)


# ------------------------------------------------------------------ #
# Task 17: Adaptive column widths                                     #
# ------------------------------------------------------------------ #


def compute_column_profile(terminal_width: int) -> ColumnProfile:
    """Compute column visibility based on terminal width."""
    if terminal_width < 120:
        return ColumnProfile(name_width=8, show_drivers=False, show_spark=False, spark_width=0)
    if terminal_width > 180:
        return ColumnProfile(name_width=20, show_drivers=True, show_spark=True, spark_width=30)
    return ColumnProfile(name_width=10, show_drivers=True, show_spark=True, spark_width=20)
