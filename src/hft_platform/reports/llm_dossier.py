"""Deterministic dossier builder for downstream LLM report generation."""

from __future__ import annotations

from hft_platform.contracts.types import PLATFORM_SCALE
from hft_platform.reports.llm_models import LLMDossier, canonical_level_label
from hft_platform.reports.models import EnrichedLevel, FactReport, ReasoningReport

__all__ = ["build_llm_dossier"]


def _price_text(price: int) -> str:
    """Format platform-scaled prices as human-readable whole points."""

    return f"{price // PLATFORM_SCALE:,}"


def _float_text(value: float) -> str:
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _compact_line(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) < 200:
        return compact
    return f"{compact[:196].rstrip()}..."


def _canonical_levels(levels: list[EnrichedLevel], close_price: int) -> dict[str, str]:
    evidence: dict[str, str] = {}
    resistances = sorted(
        (level for level in levels if level.side == "resistance" and level.price > close_price),
        key=lambda level: level.price - close_price,
    )
    supports = sorted(
        (level for level in levels if level.side == "support" and level.price < close_price),
        key=lambda level: close_price - level.price,
    )
    for index, level in enumerate(resistances[:3]):
        evidence[f"levels.{canonical_level_label('resistance', index)}"] = _price_text(level.price)
    for index, level in enumerate(supports[:3]):
        evidence[f"levels.{canonical_level_label('support', index)}"] = _price_text(level.price)
    return evidence


def _narrative_lines(reasoning_report: ReasoningReport) -> tuple[str, ...]:
    lines = tuple(_compact_line(line) for line in reasoning_report.narrative.storyline[:3] if line.strip())
    if lines:
        return lines
    return (_compact_line(reasoning_report.narrative.conclusion),)


def build_llm_dossier(fact_report: FactReport, reasoning_report: ReasoningReport) -> LLMDossier:
    session_data = fact_report.session_data
    evidence = {
        "flow.session_ud": _float_text(fact_report.flow.session_ud),
        "flow.session_net_flow": str(fact_report.flow.session_net_flow),
        "flow.eod_drift": _float_text(fact_report.flow.eod_drift),
        "chips.net_ratio": _float_text(fact_report.chips.net_ratio),
        "cross_day.trend_direction": fact_report.cross_day.trend_direction,
        "rule.bias": reasoning_report.bias.bias,
        "rule.confidence": _float_text(reasoning_report.bias.confidence),
    }
    evidence.update(_canonical_levels(reasoning_report.levels, session_data.close))
    dossier = LLMDossier(
        symbol=session_data.symbol,
        session=session_data.session,
        date=session_data.date,
        evidence=evidence,
        narrative=_narrative_lines(reasoning_report),
    )
    dossier.validate()
    return dossier
