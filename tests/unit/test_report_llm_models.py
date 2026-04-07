"""Unit tests for LLM-facing report contracts."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from hft_platform.reports.llm_models import (
    EvidenceRef,
    LLMDecisionReport,
    LLMDossier,
    TradePlan,
    canonical_level_label,
)


def _directional_plan(*, stance: str = "long", trigger: str = "Break above R1") -> TradePlan:
    return TradePlan(
        stance=stance,
        premise="Momentum expands after opening balance.",
        trigger=trigger,
        execution_style="breakout",
        stop="Back below VWAP",
        target_1="R2",
        target_2="R3",
        risk_note="Abort if breadth fades.",
    )


def _report(
    *,
    market_verdict: str = (
        "\u504f\u591a\uff0c\u4f46\u9700\u7ad9\u7a69 R1 \u624d\u80fd\u5ef6\u7e8c\u3002"
    ),
) -> LLMDecisionReport:
    return LLMDecisionReport(
        market_verdict=market_verdict,
        intraday_plan=_directional_plan(),
        swing_plan=_directional_plan(stance="short", trigger="Lose S1 on close"),
        key_levels=("R1 22000", "S1 21800"),
        invalidations=("Lose follow-through after breakout",),
        counter_case="If sellers fail to press below VWAP, short thesis weakens.",
        execution_notes=("Wait for confirmation candle",),
        confidence=72,
        evidence_refs=(EvidenceRef(key="flow", detail="Opening flow stayed net positive."),),
    )


def _dossier(
    *,
    symbol: str = "TXF",
    session: str = "day",
    date: str = "2026-04-07",
    evidence: dict[str, str] | None = None,
    narrative: tuple[str, ...] = ("Opening flow stayed net positive.",),
) -> LLMDossier:
    return LLMDossier(
        symbol=symbol,
        session=session,
        date=date,
        evidence={"flow": "Opening flow stayed net positive."} if evidence is None else evidence,
        narrative=narrative,
    )


def test_canonical_level_label_for_resistance() -> None:
    assert canonical_level_label("resistance", 0) == "R1"


def test_canonical_level_label_for_support() -> None:
    assert canonical_level_label("support", 0) == "S1"


def test_canonical_level_label_rejects_invalid_side() -> None:
    with pytest.raises(ValueError):
        canonical_level_label("pivot", 0)


def test_canonical_level_label_rejects_negative_index() -> None:
    with pytest.raises(ValueError):
        canonical_level_label("resistance", -1)


def test_trade_plan_can_be_created_with_full_directional_fields() -> None:
    plan = _directional_plan()

    assert plan.stance == "long"
    assert plan.trigger == "Break above R1"
    assert plan.stop == "Back below VWAP"
    assert plan.target_1 == "R2"
    assert plan.target_2 == "R3"


def test_trade_plan_validate_rejects_missing_trigger_for_directional_plan() -> None:
    plan = _directional_plan(trigger="")

    with pytest.raises(ValueError):
        plan.validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("premise", ""),
        ("execution_style", ""),
        ("risk_note", ""),
    ),
)
def test_trade_plan_validate_rejects_missing_required_text_for_directional_plan(
    field_name: str,
    value: str,
) -> None:
    kwargs = {
        "stance": "long",
        "premise": "Momentum expands after opening balance.",
        "trigger": "Break above R1",
        "execution_style": "breakout",
        "stop": "Back below VWAP",
        "target_1": "R2",
        "target_2": "R3",
        "risk_note": "Abort if breadth fades.",
    }
    kwargs[field_name] = value
    plan = TradePlan(**kwargs)

    with pytest.raises(ValueError):
        plan.validate()


def test_llm_dossier_coerces_mapping_and_sequence_to_immutable_types() -> None:
    evidence = {"flow": "Opening flow stayed net positive."}
    narrative = ["Line 1", "Line 2"]

    dossier = LLMDossier(
        symbol="TXF",
        session="day",
        date="2026-04-07",
        evidence=evidence,
        narrative=narrative,
    )

    evidence["flow"] = "mutated"
    narrative.append("Line 3")

    assert isinstance(dossier.evidence, MappingProxyType)
    assert dossier.evidence["flow"] == "Opening flow stayed net positive."
    assert dossier.narrative == ("Line 1", "Line 2")
    with pytest.raises(TypeError):
        dossier.evidence["new"] = "value"  # type: ignore[index]


def test_llm_dossier_rejects_scalar_string_narrative_at_construction() -> None:
    with pytest.raises(ValueError):
        LLMDossier(
            symbol="TXF",
            session="day",
            date="2026-04-07",
            evidence={"flow": "Opening flow stayed net positive."},
            narrative="single line",  # type: ignore[arg-type]
        )


def test_llm_dossier_validate_accepts_complete_dossier() -> None:
    dossier = _dossier()

    dossier.validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("symbol", ""),
        ("session", "   "),
        ("date", ""),
    ),
)
def test_llm_dossier_validate_rejects_blank_identity_fields(
    field_name: str,
    value: str,
) -> None:
    kwargs = {
        "symbol": "TXF",
        "session": "day",
        "date": "2026-04-07",
        "evidence": {"flow": "Opening flow stayed net positive."},
        "narrative": ("Opening flow stayed net positive.",),
    }
    kwargs[field_name] = value
    dossier = LLMDossier(**kwargs)

    with pytest.raises(ValueError):
        dossier.validate()


def test_llm_dossier_validate_rejects_empty_evidence() -> None:
    dossier = _dossier(evidence={})

    with pytest.raises(ValueError):
        dossier.validate()


@pytest.mark.parametrize(
    "evidence",
    (
        {"": "Opening flow stayed net positive."},
        {"flow": "   "},
    ),
)
def test_llm_dossier_validate_rejects_blank_evidence_entries(
    evidence: dict[str, str],
) -> None:
    dossier = _dossier(evidence=evidence)

    with pytest.raises(ValueError):
        dossier.validate()


@pytest.mark.parametrize("narrative", ((), ("",), ("Valid", "   ")))
def test_llm_dossier_validate_rejects_blank_narrative_entries(
    narrative: tuple[str, ...],
) -> None:
    dossier = _dossier(narrative=narrative)

    with pytest.raises(ValueError):
        dossier.validate()


def test_llm_decision_report_validate_accepts_complete_report() -> None:
    report = _report()

    report.validate()


def test_llm_decision_report_coerces_sequences_to_tuples() -> None:
    key_levels = ["R1 22000", "S1 21800"]
    invalidations = ["Lose follow-through after breakout"]
    execution_notes = ["Wait for confirmation candle"]
    evidence_refs = [EvidenceRef(key="flow", detail="Opening flow stayed net positive.")]

    report = LLMDecisionReport(
        market_verdict=_report().market_verdict,
        intraday_plan=_directional_plan(),
        swing_plan=_directional_plan(stance="short", trigger="Lose S1 on close"),
        key_levels=key_levels,
        invalidations=invalidations,
        counter_case="If sellers fail to press below VWAP, short thesis weakens.",
        execution_notes=execution_notes,
        confidence=72,
        evidence_refs=evidence_refs,
    )

    key_levels.append("R2 22100")
    invalidations.append("Late squeeze invalidates fade")
    execution_notes.append("Do not chase")
    evidence_refs.append(EvidenceRef(key="breadth", detail="Advance line improved."))

    assert report.key_levels == ("R1 22000", "S1 21800")
    assert report.invalidations == ("Lose follow-through after breakout",)
    assert report.execution_notes == ("Wait for confirmation candle",)
    assert report.evidence_refs == (
        EvidenceRef(key="flow", detail="Opening flow stayed net positive."),
    )


def test_llm_decision_report_rejects_scalar_string_key_levels_at_construction() -> None:
    with pytest.raises(ValueError):
        LLMDecisionReport(
            market_verdict=_report().market_verdict,
            intraday_plan=_directional_plan(),
            swing_plan=_directional_plan(stance="short", trigger="Lose S1 on close"),
            key_levels="R1 22000",  # type: ignore[arg-type]
            invalidations=("Lose follow-through after breakout",),
            counter_case="If sellers fail to press below VWAP, short thesis weakens.",
            execution_notes=("Wait for confirmation candle",),
            confidence=72,
            evidence_refs=(EvidenceRef(key="flow", detail="Opening flow stayed net positive."),),
        )


def test_llm_decision_report_validate_rejects_missing_invalidations() -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels,
        invalidations=(),
        counter_case=report.counter_case,
        execution_notes=report.execution_notes,
        confidence=report.confidence,
        evidence_refs=report.evidence_refs,
    )

    with pytest.raises(ValueError):
        report.validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("counter_case", ""),
        ("key_levels", ()),
        ("execution_notes", ()),
        ("evidence_refs", ()),
    ),
)
def test_llm_decision_report_validate_rejects_missing_required_content(
    field_name: str,
    value: object,
) -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels if field_name != "key_levels" else value,
        invalidations=report.invalidations,
        counter_case=report.counter_case if field_name != "counter_case" else value,
        execution_notes=report.execution_notes if field_name != "execution_notes" else value,
        confidence=report.confidence,
        evidence_refs=report.evidence_refs if field_name != "evidence_refs" else value,
    )

    with pytest.raises(ValueError):
        report.validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("key_levels", ("R1 22000", "")),
        ("invalidations", ("",)),
        ("execution_notes", ("   ",)),
    ),
)
def test_llm_decision_report_validate_rejects_blank_sequence_items(
    field_name: str,
    value: tuple[str, ...],
) -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels if field_name != "key_levels" else value,
        invalidations=report.invalidations if field_name != "invalidations" else value,
        counter_case=report.counter_case,
        execution_notes=report.execution_notes if field_name != "execution_notes" else value,
        confidence=report.confidence,
        evidence_refs=report.evidence_refs,
    )

    with pytest.raises(ValueError):
        report.validate()


@pytest.mark.parametrize(
    "evidence_ref",
    (
        EvidenceRef(key="", detail="Opening flow stayed net positive."),
        EvidenceRef(key="flow", detail="   "),
    ),
)
def test_llm_decision_report_validate_rejects_blank_evidence_ref_fields(
    evidence_ref: EvidenceRef,
) -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels,
        invalidations=report.invalidations,
        counter_case=report.counter_case,
        execution_notes=report.execution_notes,
        confidence=report.confidence,
        evidence_refs=(evidence_ref,),
    )

    with pytest.raises(ValueError):
        report.validate()


def test_llm_decision_report_validate_rejects_non_int_confidence() -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels,
        invalidations=report.invalidations,
        counter_case=report.counter_case,
        execution_notes=report.execution_notes,
        confidence=10.5,  # type: ignore[arg-type]
        evidence_refs=report.evidence_refs,
    )

    with pytest.raises(ValueError):
        report.validate()


@pytest.mark.parametrize("confidence", (-1, 101))
def test_llm_decision_report_validate_rejects_out_of_range_confidence(confidence: int) -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=report.market_verdict,
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels,
        invalidations=report.invalidations,
        counter_case=report.counter_case,
        execution_notes=report.execution_notes,
        confidence=confidence,
        evidence_refs=report.evidence_refs,
    )

    with pytest.raises(ValueError):
        report.validate()


def test_llm_decision_report_validate_rejects_generic_verdict_text() -> None:
    report = _report(
        market_verdict=(
            "\u5e02\u5834\u6709\u6f32\u6709\u8dcc\uff0c"
            "\u8acb\u81ea\u884c\u5224\u65b7\u98a8\u96aa"
        )
    )

    with pytest.raises(ValueError):
        report.validate()


def test_llm_decision_report_validate_rejects_empty_market_verdict() -> None:
    report = _report(market_verdict="")

    with pytest.raises(ValueError):
        report.validate()


def test_llm_decision_report_validate_rejects_non_string_market_verdict_with_value_error() -> None:
    report = _report()
    report = LLMDecisionReport(
        market_verdict=None,  # type: ignore[arg-type]
        intraday_plan=report.intraday_plan,
        swing_plan=report.swing_plan,
        key_levels=report.key_levels,
        invalidations=report.invalidations,
        counter_case=report.counter_case,
        execution_notes=report.execution_notes,
        confidence=report.confidence,
        evidence_refs=report.evidence_refs,
    )

    with pytest.raises(ValueError):
        report.validate()
