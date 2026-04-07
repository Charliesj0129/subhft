"""LLM-facing contracts for decision report generation."""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "EvidenceRef",
    "LLMDecisionReport",
    "LLMDossier",
    "TradePlan",
    "canonical_level_label",
]

_GENERIC_VERDICT_MARKERS = (
    "\u8acb\u81ea\u884c\u5224\u65b7",
    "\u6709\u6f32\u6709\u8dcc",
    "\u6ce8\u610f\u98a8\u96aa",
)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)


def canonical_level_label(side: str, index: int) -> str:
    """Return the canonical label for a support or resistance level."""

    prefix_by_side = {
        "support": "S",
        "resistance": "R",
    }
    prefix = prefix_by_side.get(side.strip().lower())
    if prefix is None:
        msg = f"unsupported level side: {side}"
        raise ValueError(msg)
    return f"{prefix}{index + 1}"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    key: str
    detail: str


@dataclass(frozen=True, slots=True)
class TradePlan:
    stance: str
    premise: str
    trigger: str
    execution_style: str
    stop: str
    target_1: str
    target_2: str
    risk_note: str

    def validate(self) -> None:
        _require_text(self.stance, "stance")
        if self.stance.strip().lower() == "neutral":
            return
        _require_text(self.trigger, "trigger")
        _require_text(self.stop, "stop")
        _require_text(self.target_1, "target_1")
        _require_text(self.target_2, "target_2")


@dataclass(frozen=True, slots=True)
class LLMDossier:
    symbol: str
    session: str
    date: str
    evidence: dict[str, str]
    narrative: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LLMDecisionReport:
    market_verdict: str
    intraday_plan: TradePlan
    swing_plan: TradePlan
    key_levels: tuple[str, ...]
    invalidations: tuple[str, ...]
    counter_case: str
    execution_notes: tuple[str, ...]
    confidence: int
    evidence_refs: tuple[EvidenceRef, ...]

    def validate(self) -> None:
        _require_text(self.market_verdict, "market_verdict")
        for marker in _GENERIC_VERDICT_MARKERS:
            if marker in self.market_verdict:
                msg = "market_verdict contains generic guidance"
                raise ValueError(msg)
        self.intraday_plan.validate()
        self.swing_plan.validate()
        if not self.invalidations:
            msg = "invalidations must be non-empty"
            raise ValueError(msg)
        if not 0 <= self.confidence <= 100:
            msg = "confidence must be between 0 and 100"
            raise ValueError(msg)
