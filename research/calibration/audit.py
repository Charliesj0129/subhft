"""Data audit tool for calibration: inventory live fills across sources.

Produces a structured report of what fill data exists per instrument,
including quality flags indicating missing queue position, decision price,
or sparse coverage.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class InstrumentAuditResult:
    """Per-instrument audit summary from a single data source."""

    instrument: str
    source: str
    date_range: tuple[str, str]
    n_trading_days: int
    n_fills: int
    n_fills_with_queue_position: int
    n_fills_with_decision_price: int
    n_fills_with_latency: int
    fill_rate_per_day: float
    instruments_found: list[str] = field(default_factory=list)
    quality_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
