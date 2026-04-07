"""Unit tests for the LLM report reasoner boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hft_platform.reports.llm_models import EvidenceRef, LLMDecisionReport, LLMDossier, TradePlan
from hft_platform.reports.llm_reasoner import LLMReportReasoner


class _FakeClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.prompts: list[str] = []

    async def complete_json(self, prompt: str) -> dict[str, object]:
        self.prompts.append(prompt)
        return self._payload


def _dossier() -> LLMDossier:
    return LLMDossier(
        symbol="TXF",
        session="day",
        date="2026-04-07",
        evidence={
            "flow.session_ud": "1.18",
            "flow.session_net_flow": "420",
            "levels.R1": "22,100",
            "levels.S1": "21,850",
            "rule.bias": "bullish",
        },
        narrative=(
            "Opening flow stayed net positive.",
            "Price held above the opening range midline.",
        ),
    )


def _plan(*, stance: str, premise: str) -> dict[str, str]:
    return {
        "stance": stance,
        "premise": premise,
        "trigger": "Break above R1",
        "execution_style": "breakout",
        "stop": "Back below VWAP",
        "target_1": "R2",
        "target_2": "R3",
        "risk_note": "Abort if delta fades.",
    }


def _decision() -> LLMDecisionReport:
    report = LLMDecisionReport(
        market_verdict="Bullish while opening flow stays in control above R1.",
        intraday_plan=TradePlan(
            stance="bullish",
            premise="Opening buyers kept initiative.",
            trigger="Break above R1",
            execution_style="breakout",
            stop="Back below VWAP",
            target_1="R2",
            target_2="R3",
            risk_note="Abort if delta fades.",
        ),
        swing_plan=TradePlan(
            stance="neutral",
            premise="Daily structure is constructive but not confirmed.",
            trigger="Hold daily close above R1",
            execution_style="hold on confirmation",
            stop="Daily close back below S1",
            target_1="R2",
            target_2="R3",
            risk_note="Reduce size ahead of event risk.",
        ),
        key_levels=("R1 22,100", "S1 21,850"),
        invalidations=("Lose S1 with expanding sell pressure",),
        counter_case="If price rejects R1 twice, momentum may fail.",
        execution_notes=("Use only confirmed breakouts.",),
        confidence=76,
        evidence_refs=(
            EvidenceRef(key="flow.session_ud", detail="Session U/D stayed above 1.1."),
            EvidenceRef(key="levels.R1", detail="R1 remains the breakout pivot."),
        ),
    )
    report.validate()
    return report


@pytest.mark.asyncio
async def test_generate_converts_client_payload_into_validated_report() -> None:
    client = AsyncMock()
    client.complete_json.return_value = {
        "market_verdict": "Bullish while opening flow stays in control above R1.",
        "intraday_plan": _plan(stance="long", premise="Opening buyers kept initiative."),
        "swing_plan": _plan(stance="neutral", premise="Daily structure is constructive but not confirmed."),
        "key_levels": ["R1 22,100", "S1 21,850"],
        "invalidations": ["Lose S1 with expanding sell pressure"],
        "counter_case": "If price rejects R1 twice, momentum may fail.",
        "execution_notes": ["Use only confirmed breakouts."],
        "confidence": 76,
        "evidence_refs": [
            {"key": "flow.session_ud", "detail": "Session U/D stayed above 1.1."},
            {"key": "levels.R1", "detail": "R1 remains the breakout pivot."},
        ],
    }
    reasoner = LLMReportReasoner(client=client)

    report = await reasoner.generate(_dossier())

    assert isinstance(report, LLMDecisionReport)
    assert isinstance(report.intraday_plan, TradePlan)
    assert isinstance(report.evidence_refs[0], EvidenceRef)
    assert report.market_verdict == "Bullish while opening flow stays in control above R1."
    assert report.evidence_refs[1].key == "levels.R1"
    client.complete_json.assert_awaited_once()
    prompt = client.complete_json.await_args.args[0]
    assert "Use only the dossier evidence provided below." in prompt
    assert "Return JSON only." in prompt
    assert "every key must come from dossier.evidence." in prompt
    assert '"flow.session_ud": "1.18"' in prompt
    assert '"session": "day"' in prompt


@pytest.mark.asyncio
async def test_generate_rejects_unknown_evidence_ref_key() -> None:
    client = _FakeClient(
        {
            "market_verdict": "Bullish above R1 unless flow flips.",
            "intraday_plan": _plan(stance="long", premise="Flow and structure align."),
            "swing_plan": _plan(stance="neutral", premise="Wait for higher timeframe confirmation."),
            "key_levels": ["R1 22,100", "S1 21,850"],
            "invalidations": ["Lose S1 with strong sell pressure"],
            "counter_case": "Repeated rejection at R1 weakens the thesis.",
            "execution_notes": ["Do not chase extended candles."],
            "confidence": 68,
            "evidence_refs": [
                {"key": "levels.R99", "detail": "Model cited a non-existent resistance."},
            ],
        }
    )
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="levels.R99"):
        await reasoner.generate(_dossier())


@pytest.mark.asyncio
async def test_generate_rejects_scalar_string_for_array_field() -> None:
    client = _FakeClient(
        {
            "market_verdict": "Bullish above R1 while buyers keep control.",
            "intraday_plan": _plan(stance="long", premise="Flow and structure align."),
            "swing_plan": _plan(stance="neutral", premise="Wait for higher timeframe confirmation."),
            "key_levels": "R1 22,100",
            "invalidations": ["Lose S1 with strong sell pressure"],
            "counter_case": "Repeated rejection at R1 weakens the thesis.",
            "execution_notes": ["Do not chase extended candles."],
            "confidence": 68,
            "evidence_refs": [
                {"key": "levels.R1", "detail": "R1 remains the breakout pivot."},
            ],
        }
    )
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="key_levels must be a JSON array"):
        await reasoner.generate(_dossier())


@pytest.mark.asyncio
async def test_generate_rejects_missing_nested_plan_field() -> None:
    broken_plan = _plan(stance="long", premise="Opening buyers kept initiative.")
    broken_plan.pop("trigger")
    client = _FakeClient(
        {
            "market_verdict": "Bullish while opening flow stays in control above R1.",
            "intraday_plan": broken_plan,
            "swing_plan": _plan(stance="neutral", premise="Daily structure is constructive but not confirmed."),
            "key_levels": ["R1 22,100", "S1 21,850"],
            "invalidations": ["Lose S1 with expanding sell pressure"],
            "counter_case": "If price rejects R1 twice, momentum may fail.",
            "execution_notes": ["Use only confirmed breakouts."],
            "confidence": 76,
            "evidence_refs": [
                {"key": "flow.session_ud", "detail": "Session U/D stayed above 1.1."},
            ],
        }
    )
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="intraday_plan.trigger must be a string"):
        await reasoner.generate(_dossier())


@pytest.mark.asyncio
async def test_generate_rejects_non_string_text_field() -> None:
    client = _FakeClient(
        {
            "market_verdict": "Bullish while opening flow stays in control above R1.",
            "intraday_plan": _plan(stance="long", premise="Opening buyers kept initiative."),
            "swing_plan": _plan(stance="neutral", premise="Daily structure is constructive but not confirmed."),
            "key_levels": ["R1 22,100", "S1 21,850"],
            "invalidations": ["Lose S1 with expanding sell pressure"],
            "counter_case": "If price rejects R1 twice, momentum may fail.",
            "execution_notes": [123],
            "confidence": 76,
            "evidence_refs": [
                {"key": "flow.session_ud", "detail": "Session U/D stayed above 1.1."},
            ],
        }
    )
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="execution_notes\\[0\\] must be a string"):
        await reasoner.generate(_dossier())


@pytest.mark.asyncio
async def test_generate_rejects_non_int_confidence() -> None:
    client = _FakeClient(
        {
            "market_verdict": "Bullish while opening flow stays in control above R1.",
            "intraday_plan": _plan(stance="long", premise="Opening buyers kept initiative."),
            "swing_plan": _plan(stance="neutral", premise="Daily structure is constructive but not confirmed."),
            "key_levels": ["R1 22,100", "S1 21,850"],
            "invalidations": ["Lose S1 with expanding sell pressure"],
            "counter_case": "If price rejects R1 twice, momentum may fail.",
            "execution_notes": ["Use only confirmed breakouts."],
            "confidence": "76",
            "evidence_refs": [
                {"key": "flow.session_ud", "detail": "Session U/D stayed above 1.1."},
            ],
        }
    )
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="confidence must be an int"):
        await reasoner.generate(_dossier())


@pytest.mark.asyncio
async def test_generate_validates_input_dossier_before_calling_client() -> None:
    client = AsyncMock()
    reasoner = LLMReportReasoner(client=client)
    invalid_dossier = LLMDossier(
        symbol="TXF",
        session="day",
        date="2026-04-07",
        evidence={"flow.session_ud": "1.18"},
        narrative=(),
    )

    with pytest.raises(ValueError, match="narrative must be non-empty"):
        await reasoner.generate(invalid_dossier)

    client.complete_json.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_followup_uses_full_decision_context_and_returns_answer() -> None:
    client = AsyncMock()
    client.complete_json.return_value = {
        "answer": "Treat S1 as the key invalidation and do not chase above R1.",
        "evidence_refs": ["levels.S1", "levels.R1"],
    }
    reasoner = LLMReportReasoner(client=client)

    answer = await reasoner.answer_followup(_dossier(), _decision(), "現在還能追嗎？")

    assert answer == "Treat S1 as the key invalidation and do not chase above R1."
    client.complete_json.assert_awaited_once()
    prompt = client.complete_json.await_args.args[0]
    assert '"invalidations": ["Lose S1 with expanding sell pressure"]' in prompt
    assert '"execution_notes": ["Use only confirmed breakouts."]' in prompt
    assert '"counter_case": "If price rejects R1 twice, momentum may fail."' in prompt


@pytest.mark.asyncio
async def test_answer_followup_rejects_unknown_evidence_ref_key() -> None:
    client = AsyncMock()
    client.complete_json.return_value = {
        "answer": "This answer cites a non-existent level.",
        "evidence_refs": ["levels.R99"],
    }
    reasoner = LLMReportReasoner(client=client)

    with pytest.raises(ValueError, match="levels.R99"):
        await reasoner.answer_followup(_dossier(), _decision(), "現在還能追嗎？")
