"""Unit tests for deterministic LLM dossier building."""

from __future__ import annotations

import dataclasses

from hft_platform.reports.llm_dossier import build_llm_dossier
from hft_platform.reports.models import EnrichedLevel, NarrativeReport
from tests.unit.test_report_composer import _make_fact_report, _make_reasoning_report


def test_build_llm_dossier_extracts_symbol_and_required_evidence() -> None:
    dossier = build_llm_dossier(_make_fact_report(), _make_reasoning_report())

    assert dossier.symbol == "TXFD6"
    assert dossier.evidence["flow.session_ud"] == "1.15"
    assert dossier.evidence["flow.session_net_flow"] == "500"
    assert dossier.evidence["flow.eod_drift"] == "0.1"
    assert dossier.evidence["chips.net_ratio"] == "0.625"
    assert dossier.evidence["cross_day.trend_direction"] == "up"
    assert dossier.evidence["levels.R1"] == "20,150"
    assert dossier.evidence["levels.S1"] == "19,900"
    assert dossier.evidence["rule.bias"] == "bullish"
    assert dossier.evidence["rule.confidence"] == "0.65"
    dossier.validate()


def test_build_llm_dossier_canonicalizes_level_labels_relative_to_session_close() -> None:
    fact_report = _make_fact_report()
    reasoning_report = dataclasses.replace(
        _make_reasoning_report(),
        levels=[
            EnrichedLevel(
                price=202_000_000,
                side="resistance",
                strength=0.4,
                sources=["far resistance"],
                confluence_count=1,
            ),
            EnrichedLevel(
                price=200_400_000,
                side="support",
                strength=0.8,
                sources=["near support"],
                confluence_count=1,
            ),
            EnrichedLevel(
                price=200_000_000,
                side="pivot",
                strength=0.7,
                sources=["pivot"],
                confluence_count=1,
            ),
            EnrichedLevel(
                price=201_000_000,
                side="resistance",
                strength=0.9,
                sources=["near resistance"],
                confluence_count=1,
            ),
            EnrichedLevel(
                price=198_500_000,
                side="support",
                strength=0.5,
                sources=["far support"],
                confluence_count=1,
            ),
        ],
    )

    dossier = build_llm_dossier(fact_report, reasoning_report)

    assert dossier.evidence["levels.R1"] == "20,100"
    assert dossier.evidence["levels.R2"] == "20,200"
    assert dossier.evidence["levels.S1"] == "20,040"
    assert dossier.evidence["levels.S2"] == "19,850"


def test_build_llm_dossier_keeps_compact_three_line_tuple_narrative() -> None:
    reasoning_report = _make_reasoning_report()
    narrative = NarrativeReport(
        storyline=[
            "  opening  " + ("bull " * 60),
            "midday   balance   holds   ",
            " closing   buyers   retake  tape ",
            "fourth line should be dropped",
        ],
        turning_points=reasoning_report.narrative.turning_points,
        conclusion=reasoning_report.narrative.conclusion,
    )
    reasoning_report = dataclasses.replace(reasoning_report, narrative=narrative)

    dossier = build_llm_dossier(_make_fact_report(), reasoning_report)

    assert isinstance(dossier.narrative, tuple)
    assert len(dossier.narrative) == 3
    assert all(line == line.strip() for line in dossier.narrative)
    assert all("  " not in line for line in dossier.narrative)
    assert all(len(line) < 200 for line in dossier.narrative)


def test_build_llm_dossier_uses_storyline_only_when_first_entries_are_blank() -> None:
    reasoning_report = _make_reasoning_report()
    narrative = NarrativeReport(
        storyline=["   ", "\t", "\n", "conclusion must not leak in"],
        turning_points=reasoning_report.narrative.turning_points,
        conclusion="this must not be used",
    )
    reasoning_report = dataclasses.replace(reasoning_report, narrative=narrative)

    dossier = build_llm_dossier(_make_fact_report(), reasoning_report)

    assert dossier.narrative == ("(blank storyline)",)
