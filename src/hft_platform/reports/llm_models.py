"""LLM-facing contracts for decision report generation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise ValueError(msg)
    if not value.strip():
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)


def _require_string(value: object, field_name: str) -> None:
    if not isinstance(value, str):
        msg = f"{field_name} must be a string"
        raise ValueError(msg)


def _require_non_empty(value: object, field_name: str) -> None:
    if not value:
        msg = f"{field_name} must be non-empty"
        raise ValueError(msg)


def _require_text_items(values: tuple[str, ...], field_name: str) -> None:
    for index, value in enumerate(values):
        _require_text(value, f"{field_name}[{index}]")


def _coerce_sequence_items(
    value: object,
    field_name: str,
    *,
    item_type: type[str] | type["EvidenceRef"],
) -> tuple[object, ...]:
    if isinstance(value, (str, bytes)):
        msg = f"{field_name} must be a sequence, not a scalar string"
        raise ValueError(msg)
    if not isinstance(value, Sequence):
        msg = f"{field_name} must be a sequence"
        raise ValueError(msg)
    coerced = tuple(value)
    for index, item in enumerate(coerced):
        if not isinstance(item, item_type):
            msg = f"{field_name}[{index}] must be {item_type.__name__}"
            raise ValueError(msg)
    return coerced


def _validate_nested_plan(value: object, field_name: str) -> None:
    if type(value) is not TradePlan:
        msg = f"{field_name} must be a valid trade plan"
        raise ValueError(msg)
    value.validate()


def canonical_level_label(side: str, index: int) -> str:
    """Return the canonical label for a support or resistance level."""

    _require_text(side, "side")
    if type(index) is not int:
        msg = "index must be an int"
        raise ValueError(msg)
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
        _require_string(self.trigger, "trigger")
        _require_string(self.stop, "stop")
        _require_string(self.target_1, "target_1")
        _require_string(self.target_2, "target_2")
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
        object.__setattr__(
            self,
            "narrative",
            _coerce_sequence_items(self.narrative, "narrative", item_type=str),
        )

    def validate(self) -> None:
        _require_text(self.symbol, "symbol")
        _require_text(self.session, "session")
        _require_text(self.date, "date")
        _require_non_empty(self.evidence, "evidence")
        _require_non_empty(self.narrative, "narrative")
        for key, value in self.evidence.items():
            _require_text(key, "evidence key")
            _require_text(value, f"evidence[{key!r}]")
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
        object.__setattr__(
            self,
            "key_levels",
            _coerce_sequence_items(self.key_levels, "key_levels", item_type=str),
        )
        object.__setattr__(
            self,
            "invalidations",
            _coerce_sequence_items(self.invalidations, "invalidations", item_type=str),
        )
        object.__setattr__(
            self,
            "execution_notes",
            _coerce_sequence_items(self.execution_notes, "execution_notes", item_type=str),
        )
        object.__setattr__(
            self,
            "evidence_refs",
            _coerce_sequence_items(self.evidence_refs, "evidence_refs", item_type=EvidenceRef),
        )

    def validate(self) -> None:
        _require_text(self.market_verdict, "market_verdict")
        for marker in _GENERIC_VERDICT_MARKERS:
            if marker in self.market_verdict:
                msg = "market_verdict contains generic guidance"
                raise ValueError(msg)
        _validate_nested_plan(self.intraday_plan, "intraday_plan")
        _validate_nested_plan(self.swing_plan, "swing_plan")
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
        if type(self.confidence) is not int:
            msg = "confidence must be an int"
            raise ValueError(msg)
        if not 0 <= self.confidence <= 100:
            msg = "confidence must be between 0 and 100"
            raise ValueError(msg)
