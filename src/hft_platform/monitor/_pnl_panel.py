"""Cost Attribution Panel for the Signal Monitor TUI.

Queries ``hft.fills`` for per-strategy daily fee/commission/tax breakdown
and renders a compact text table suitable for TUI display.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from structlog import get_logger

logger = get_logger("monitor.pnl_panel")

# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

_COST_QUERY = (
    "SELECT "
    "  strategy_id, "
    "  symbol, "
    "  count(*) AS fill_count, "
    "  sum(fee_scaled) AS total_fee_scaled, "
    "  sum(tax_scaled) AS total_tax_scaled "
    "FROM hft.fills "
    "WHERE toDate(toDateTime(ts_exchange / 1000000000)) = {date:String} "
    "GROUP BY strategy_id, symbol "
    "ORDER BY total_fee_scaled DESC"
)


@dataclass(frozen=True, slots=True)
class CostPanelData:
    """Per-strategy/symbol cost attribution snapshot."""

    strategy: str
    symbol: str
    fill_count: int
    total_fee_scaled: int
    tax_scaled: int

    @property
    def commission_scaled(self) -> int:
        """Commission = total_fee - tax."""
        return self.total_fee_scaled - self.tax_scaled


# ---------------------------------------------------------------------------
# ClickHouse fetch
# ---------------------------------------------------------------------------


def fetch_cost_attribution(ch_client: Any, date_str: str) -> list[CostPanelData]:
    """Query ``hft.fills`` for daily cost attribution.

    Parameters
    ----------
    ch_client:
        A ``clickhouse_connect`` client (or compatible duck-type with ``.query()``).
    date_str:
        Date string in ``YYYY-MM-DD`` format.

    Returns
    -------
    list[CostPanelData]
        Per-strategy/symbol breakdown, ordered by total_fee_scaled DESC.
        Empty list on failure.
    """
    try:
        result = ch_client.query(_COST_QUERY, parameters={"date": date_str})
        rows = getattr(result, "result_rows", None) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("cost_attribution_query_failed", error=str(exc))
        return []

    out: list[CostPanelData] = []
    for row in rows:
        out.append(
            CostPanelData(
                strategy=str(row[0]),
                symbol=str(row[1]),
                fill_count=int(row[2]),
                total_fee_scaled=int(row[3]),
                tax_scaled=int(row[4]),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------

_HDR_FMT = "{:<12s} {:<8s} {:>5s} {:>10s} {:>10s} {:>10s}"
_ROW_FMT = "{:<12s} {:<8s} {:>5d} {:>10s} {:>10s} {:>10s}"


def _ntd(scaled: int) -> str:
    """Convert scaled-int (x10000) to NTD display string."""
    return f"{scaled / 10000:,.1f}"


def render_cost_table(data: Sequence[CostPanelData]) -> list[str]:
    """Render cost attribution as pre-formatted text lines.

    Returns
    -------
    list[str]
        Lines suitable for TUI display.  Empty data yields
        ``["  No fills today"]``.
    """
    if not data:
        return ["  No fills today"]

    lines: list[str] = []
    lines.append(_HDR_FMT.format("Strategy", "Symbol", "Fills", "Comm", "Tax", "Total"))

    total_fee = 0
    total_tax = 0
    total_fills = 0
    for d in data:
        lines.append(
            _ROW_FMT.format(
                d.strategy[:12],
                d.symbol[:8],
                d.fill_count,
                _ntd(d.commission_scaled),
                _ntd(d.tax_scaled),
                _ntd(d.total_fee_scaled),
            )
        )
        total_fee += d.total_fee_scaled
        total_tax += d.tax_scaled
        total_fills += d.fill_count

    total_comm = total_fee - total_tax
    lines.append(
        _ROW_FMT.format(
            "TOTAL",
            "",
            total_fills,
            _ntd(total_comm),
            _ntd(total_tax),
            _ntd(total_fee),
        )
    )
    return lines


# ---------------------------------------------------------------------------
# Renderer helper (called from _renderer.py)
# ---------------------------------------------------------------------------


def build_cost_section(cost_lines: list[str], width: int) -> list[str]:
    """Build section lines with a header for the cost attribution panel.

    Parameters
    ----------
    cost_lines:
        Output of ``render_cost_table()``.
    width:
        Terminal width (reserved for future formatting).

    Returns
    -------
    list[str]
        Header + cost_lines.
    """
    return ["Cost Attribution:"] + cost_lines
