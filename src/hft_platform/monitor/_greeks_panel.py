"""Portfolio Greeks panel for the Signal Monitor TUI."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from rich.style import Style
from rich.text import Text

_GREEN = Style(color="green")
_RED = Style(color="red")
_YELLOW = Style(color="yellow")
_WHITE = Style(color="white")
_DIM = Style(dim=True)


@dataclass(slots=True)
class PortfolioGreeksSnapshot:
    ts: int = 0
    net_delta_lots: float = 0.0
    net_gamma_lots: float = 0.0
    net_theta_ntd: float = 0.0
    net_vega_ntd: float = 0.0
    worst_pnl_ntd: float = 0.0
    eye_state: str = "UNKNOWN"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PortfolioGreeksSnapshot:
        return cls(
            ts=int(data.get("ts", 0)),
            net_delta_lots=float(data.get("net_delta_lots", 0.0)),
            net_gamma_lots=float(data.get("net_gamma_lots", 0.0)),
            net_theta_ntd=float(data.get("net_theta_ntd", 0.0)),
            net_vega_ntd=float(data.get("net_vega_ntd", 0.0)),
            worst_pnl_ntd=float(data.get("worst_pnl_ntd", 0.0)),
            eye_state=str(data.get("eye_state", "UNKNOWN")),
        )


def _format_value(value: float, limit: float | None = None) -> Text:
    sign = "+" if value >= 0 else ""
    text = f"{sign}{value:,.0f}" if abs(value) >= 1000 else f"{sign}{value:.1f}"
    if limit is not None and abs(value) > abs(limit) * 0.8:
        style = _RED if abs(value) > abs(limit) else _YELLOW
    else:
        style = _WHITE
    return Text(text, style=style)


def _state_style(state: str) -> Style:
    if state == "QUOTING":
        return _GREEN
    if state == "NARROW":
        return _YELLOW
    if state in ("RESTRICT", "HALT"):
        return _RED
    return _DIM


def render_greeks_panel(
    snap: PortfolioGreeksSnapshot | None,
    delta_limit: float = 50,
    gamma_limit: float = 20,
) -> list[Text]:
    if snap is None:
        return [Text("Greeks: unavailable", style=_DIM)]
    lines: list[Text] = []

    delta_line = Text("Net \u0394: ", style=_DIM)
    delta_line.append_text(_format_value(snap.net_delta_lots, delta_limit))
    delta_line.append(f"  (lim: {delta_limit:.0f})", style=_DIM)
    lines.append(delta_line)

    gamma_line = Text("Net \u0393: ", style=_DIM)
    gamma_line.append_text(_format_value(snap.net_gamma_lots, gamma_limit))
    gamma_line.append(f"  (lim: {gamma_limit:.0f})", style=_DIM)
    lines.append(gamma_line)

    theta_line = Text("Net \u0398: ", style=_DIM)
    theta_line.append_text(_format_value(snap.net_theta_ntd))
    theta_line.append(" NTD", style=_DIM)
    lines.append(theta_line)

    vega_line = Text("Net V: ", style=_DIM)
    vega_line.append_text(_format_value(snap.net_vega_ntd))
    vega_line.append(" NTD", style=_DIM)
    lines.append(vega_line)

    pnl_line = Text("Worst PnL: ", style=_DIM)
    pnl_line.append_text(_format_value(snap.worst_pnl_ntd))
    pnl_line.append(" NTD", style=_DIM)
    lines.append(pnl_line)

    state_line = Text("State: ", style=_DIM)
    state_line.append(snap.eye_state, style=_state_style(snap.eye_state))
    lines.append(state_line)

    return lines
