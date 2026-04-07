"""Unit tests for the LLM report reasoner boundary."""

from __future__ import annotations

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
    dossier = LLMDossier(
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
    dossier.validate()
    return dossier


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


@pytest.mark.asyncio
async def test_generate_converts_client_payload_into_validated_report() -> None:
    client = _FakeClient(
        {
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
    )
    reasoner = LLMReportReasoner(client=client)

    report = await reasoner.generate(_dossier())

    assert isinstance(report, LLMDecisionReport)
    assert isinstance(report.intraday_plan, TradePlan)
    assert isinstance(report.evidence_refs[0], EvidenceRef)
    assert report.market_verdict == "Bullish while opening flow stays in control above R1."
    assert report.evidence_refs[1].key == "levels.R1"
    assert client.prompts


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
