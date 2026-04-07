"""Unit tests for LLM-facing report contracts."""

from __future__ import annotations

import pytest

from hft_platform.reports.llm_models import (
    EvidenceRef,
    LLMDecisionReport,
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


def test_canonical_level_label_for_resistance() -> None:
    assert canonical_level_label("resistance", 0) == "R1"


def test_canonical_level_label_for_support() -> None:
    assert canonical_level_label("support", 0) == "S1"


def test_canonical_level_label_rejects_invalid_side() -> None:
    with pytest.raises(ValueError):
        canonical_level_label("pivot", 0)


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


def test_llm_decision_report_validate_accepts_complete_report() -> None:
    report = _report()

    report.validate()


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
