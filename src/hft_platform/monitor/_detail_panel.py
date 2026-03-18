"""Detail panel for single-symbol deep-dive view (Phase 3)."""

from __future__ import annotations

import math

from rich.panel import Panel
from rich.style import Style
from rich.text import Text

from hft_platform.core import timebase
from hft_platform.monitor._events import _ALPHA_SHORT, dominant_alpha_label
from hft_platform.monitor._types import MonitorConfig, SymbolState

_SPARK_CHARS = "▁▂▃▄▅▆▇█"

_GREEN = Style(color="green")
_RED = Style(color="red")
_BRIGHT_GREEN = Style(color="bright_green", bold=True)
_BRIGHT_RED = Style(color="bright_red", bold=True)
_DIM = Style(dim=True)
_WHITE = Style(color="white")
_CYAN = Style(color="cyan")
_YELLOW = Style(color="yellow")


def build_detail_panel(
    ss: SymbolState | None,
    config: MonitorConfig,
    weights: dict[str, float],
) -> Panel:
    """Build a detail panel for the selected symbol."""
    if ss is None:
        return Panel(
            Text("No symbol selected. Press j/k to navigate, d to open.", style=_DIM),
            title="Detail",
            border_style="dim",
            height=8,
        )

    now_ns = timebase.now_ns()
    lines = Text()

    # Line 1: L1 quote
    bid_str = f"{ss.last_price - ss.spread_bps * ss.last_price / 20000:.1f}" if ss.last_price > 0 else "--"
    ask_str = f"{ss.last_price + ss.spread_bps * ss.last_price / 20000:.1f}" if ss.last_price > 0 else "--"
    mid_str = f"{ss.last_price:.1f}" if ss.last_price > 0 else "--"
    lines.append(f" Bid: {bid_str} ({ss.bid_qty:.0f})  Ask: {ask_str} ({ss.ask_qty:.0f})  Mid: {mid_str}", _WHITE)
    lines.append(f"  Imb: {(ss.bid_qty - ss.ask_qty) / max(ss.bid_qty + ss.ask_qty, 1):.2f}", _DIM)
    lines.append(f"  OFI: {ss.ofi_l1_cum:+.0f}\n", _DIM)

    # Lines 2-N: Per-alpha detail with sparkline
    for aid, astate in ss.alpha_states.items():
        short = _ALPHA_SHORT.get(aid, aid[:4].upper())
        if astate.disabled:
            lines.append(f" {short:>4}: OFF\n", _DIM)
            continue
        if math.isnan(astate.signal):
            lines.append(f" {short:>4}: --\n", _DIM)
            continue

        z = astate.z_score
        sig = astate.signal
        z_style = _style_for_z(z)

        lines.append(f" {short:>4}: ", _WHITE)
        lines.append(f"{sig:+.3f}", z_style)
        lines.append(f" [z={abs(z):.1f}σ]  ", _DIM)

        # Mini sparkline from per-alpha buffer
        spark_vals = astate.signal_sparkline_values()
        spark_str = _mini_sparkline(spark_vals) if spark_vals else ""
        lines.append(spark_str, z_style)

        # Trend
        if len(spark_vals) >= 4:
            recent = spark_vals[-4:]
            if all(recent[i] <= recent[i + 1] for i in range(len(recent) - 1)):
                lines.append("  trend: rising", _GREEN)
            elif all(recent[i] >= recent[i + 1] for i in range(len(recent) - 1)):
                lines.append("  trend: falling", _RED)
            else:
                lines.append("  trend: flat", _DIM)
        lines.append("\n")

    # Composite line
    comp = ss.composite
    comp_style = _style_for_z(comp)
    dom = dominant_alpha_label(ss)
    lines.append(f" Composite: {comp:+.2f} [{abs(comp):.1f}σ]", comp_style)
    if dom:
        lines.append(f"  Dominant: {dom}", _WHITE)
    lines.append("\n")

    # WHY summary
    why_parts: list[str] = []
    pos = neg = total = 0
    for a in ss.alpha_states.values():
        if not a.disabled and not math.isnan(a.signal):
            total += 1
            if a.signal > 0:
                pos += 1
            elif a.signal < 0:
                neg += 1
    if total > 0:
        dominant = max(pos, neg)
        direction = "bullish" if pos > neg else "bearish" if neg > pos else "mixed"
        why_parts.append(f"{dominant}/{total} {direction}")
    if dom:
        # Find strongest alpha z
        max_z = 0.0
        max_aid = ""
        for a in ss.alpha_states.values():
            if not a.disabled and not math.isnan(a.signal) and abs(a.z_score) > max_z:
                max_z = abs(a.z_score)
                max_aid = _ALPHA_SHORT.get(a.alpha_id, a.alpha_id[:3])
        if max_aid:
            why_parts.append(f"{max_aid} strongest {max_z:.1f}σ")
    if ss.spread_bps > 0:
        why_parts.append(f"spread {ss.spread_bps:.0f}bps")
    lines.append(f" WHY: {' | '.join(why_parts)}\n", _CYAN)

    # Status bar
    age_s = (now_ns - ss.last_update_ns) / 1e9 if ss.last_update_ns > 0 else 0.0
    status = "✓ LIVE" if not ss.is_stale and not ss.is_closed else ("STALE" if ss.is_stale else "CLOSED")

    title = f" {ss.symbol.code} {ss.symbol.name}  {status}  {ss.tick_count:,} ticks  last {age_s:.1f}s ago "
    subtitle = " [j/k] nav  [d] close  [s] sort  [ESC] clear "

    return Panel(
        lines,
        title=title,
        subtitle=subtitle,
        border_style="cyan",
        height=8,
    )


def _style_for_z(z: float) -> Style:
    """Style based on z-score magnitude and direction."""
    az = abs(z)
    if z > 0:
        if az >= 2.0:
            return _BRIGHT_GREEN
        if az >= 1.0:
            return _GREEN
        return _WHITE
    if z < 0:
        if az >= 2.0:
            return _BRIGHT_RED
        if az >= 1.0:
            return _RED
        return _WHITE
    return _DIM


def _mini_sparkline(values: list[float]) -> str:
    """Render a compact sparkline."""
    if not values:
        return ""
    mn = min(values)
    mx = max(values)
    rng = mx - mn
    if rng < 1e-10:
        return _SPARK_CHARS[3] * len(values)
    scale = 7.0 / rng
    return "".join(_SPARK_CHARS[max(0, min(7, int((v - mn) * scale)))] for v in values)
