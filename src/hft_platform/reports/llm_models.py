"""LLM-facing contracts for decision report generation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

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


def _require_non_empty(value: object, field_name: str) -> None:
    if not value:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)


def _require_text_items(values: tuple[str, ...], field_name: str) -> None:
    for index, value in enumerate(values):
        _require_text(value, f"{field_name}[{index}]")


def canonical_level_label(side: str, index: int) -> str:
    """Return the canonical label for a support or resistance level."""

    if index < 0:
        msg = f"level index must be non-negative: {index}"
        raise ValueError(msg)
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

    def validate(self) -> None:
        _require_text(self.key, "key")
        _require_text(self.detail, "detail")


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
        _require_text(self.premise, "premise")
        _require_text(self.execution_style, "execution_style")
        _require_text(self.risk_note, "risk_note")
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
    evidence: Mapping[str, str]
    narrative: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))
        object.__setattr__(self, "narrative", tuple(self.narrative))

    def validate(self) -> None:
        _require_text(self.symbol, "symbol")
        _require_text(self.session, "session")
        _require_text(self.date, "date")
        _require_non_empty(self.evidence, "evidence")
        _require_non_empty(self.narrative, "narrative")
        _require_text_items(self.narrative, "narrative")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_levels", tuple(self.key_levels))
        object.__setattr__(self, "invalidations", tuple(self.invalidations))
        object.__setattr__(self, "execution_notes", tuple(self.execution_notes))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))

    def validate(self) -> None:
        _require_text(self.market_verdict, "market_verdict")
        for marker in _GENERIC_VERDICT_MARKERS:
            if marker in self.market_verdict:
                msg = "market_verdict contains generic guidance"
                raise ValueError(msg)
        self.intraday_plan.validate()
        self.swing_plan.validate()
        _require_non_empty(self.key_levels, "key_levels")
        _require_non_empty(self.invalidations, "invalidations")
        _require_text(self.counter_case, "counter_case")
        _require_non_empty(self.execution_notes, "execution_notes")
        _require_non_empty(self.evidence_refs, "evidence_refs")
        _require_text_items(self.key_levels, "key_levels")
        _require_text_items(self.invalidations, "invalidations")
        _require_text_items(self.execution_notes, "execution_notes")
        for evidence_ref in self.evidence_refs:
            evidence_ref.validate()
        if not 0 <= self.confidence <= 100:
            msg = "confidence must be between 0 and 100"
            raise ValueError(msg)
