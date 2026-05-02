"""Unit tests for deterministic LLM dossier building."""

from __future__ import annotations

import dataclasses

import pytest

from hft_platform.reports.llm_dossier import build_llm_dossier
from hft_platform.reports.models import (
    Bar5m,
    BiasJudgment,
    ChipCluster,
    ChipFacts,
    CrossDayFacts,
    DaySnapshot,
    EnrichedLevel,
    Evidence,
    FactReport,
    FlowBar,
    FlowFacts,
    NarrativeReport,
    PriceLevel,
    ReasoningReport,
    Scenario,
    SegmentFact,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)


def _make_flow_bar(
    ts: str,
    *,
    ud_ratio: float,
    net_flow: int,
    total_vol: int,
    uptick_vol: int,
    downtick_vol: int,
) -> FlowBar:
    return FlowBar(
        ts=ts,
        ticks=10,
        total_vol=total_vol,
        uptick_vol=uptick_vol,
        downtick_vol=downtick_vol,
        flat_vol=0,
        ud_ratio=ud_ratio,
        net_flow=net_flow,
    )


def _make_session_data() -> SessionData:
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-28",
        open=200_000_000,
        high=201_000_000,
        low=199_000_000,
        close=200_500_000,
        volume=35_000,
        tick_count=18_000,
        bars_5m=[
            Bar5m(
                ts="2026-03-28 09:00:00",
                open=200_000_000,
                high=200_500_000,
                low=199_500_000,
                close=200_200_000,
                volume=500,
                ticks=120,
            )
        ],
        flow_5m=[],
        large_trades=[],
        spread_dist={1: 100, 2: 200},
        depth_imbalance=[],
    )


def _make_fact_report() -> FactReport:
    return FactReport(
        session_data=_make_session_data(),
        segments=[
            SegmentFact(
                name="opening",
                time_range="08:45-09:30",
                ud_ratio=1.2,
                net_flow=100,
                volume=5_000,
                volume_pct=0.30,
                large_buy_count=2,
                large_sell_count=1,
                high=201_000_000,
                low=199_500_000,
                dominant_side="bull",
            ),
            SegmentFact(
                name="closing",
                time_range="12:00-13:45",
                ud_ratio=1.1,
                net_flow=30,
                volume=6_000,
                volume_pct=0.30,
                large_buy_count=1,
                large_sell_count=0,
                high=200_600_000,
                low=199_800_000,
                dominant_side="bull",
            ),
        ],
        chips=ChipFacts(
            clusters=[
                ChipCluster(
                    price_center=200_000_000,
                    price_range=(199_500_000, 200_500_000),
                    buy_volume=150,
                    sell_volume=80,
                    trade_count=5,
                    dominant_side="buy",
                    first_ts="2026-03-28 09:10:00",
                    last_ts="2026-03-28 10:30:00",
                    time_range="09:10-10:30",
                )
            ],
            vap_peaks=[],
            buy_zone=(199_500_000, 200_200_000),
            sell_zone=(200_800_000, 201_200_000),
            total_buy_volume=200,
            total_sell_volume=120,
            net_ratio=0.625,
        ),
        flow=FlowFacts(
            session_ud=1.15,
            session_net_flow=500,
            strongest_buy_bar=_make_flow_bar(
                "2026-03-28 09:00:00",
                ud_ratio=2.0,
                net_flow=150,
                total_vol=500,
                uptick_vol=300,
                downtick_vol=150,
            ),
            strongest_sell_bar=_make_flow_bar(
                "2026-03-28 11:30:00",
                ud_ratio=0.5,
                net_flow=-120,
                total_vol=420,
                uptick_vol=120,
                downtick_vol=240,
            ),
            sustained_runs=[("bull", 5, "09:00-09:25")],
            volume_spikes=[],
            eod_ud=1.25,
            eod_drift=0.10,
        ),
        structure=StructureFacts(
            double_bottoms=[],
            double_tops=[],
            failed_breakouts=[PriceLevel(price=200_700_000, strength=0.4, reason="failed breakout")],
            round_numbers=[PriceLevel(price=200_000_000, strength=0.6, reason="round number")],
            session_high=PriceLevel(price=201_000_000, strength=0.5, reason="session high"),
            session_low=PriceLevel(price=199_000_000, strength=0.5, reason="session low"),
        ),
        volatility=VolatilityFacts(
            atr_5m=50_000,
            session_range=2_000_000,
            range_atr_ratio=1.5,
            atr_session=200_000,
        ),
        cross_day=CrossDayFacts(
            prev_days=[
                DaySnapshot(
                    date="2026-03-27",
                    session="day",
                    open=199_000_000,
                    high=200_500_000,
                    low=198_500_000,
                    close=200_000_000,
                    volume=30_000,
                    ud_ratio=1.05,
                    net_flow=200,
                )
            ],
            volume_change_pct=16.7,
            price_position="above_prev_high",
            trend_direction="up",
            flow_reversal=False,
        ),
    )


def _make_reasoning_report() -> ReasoningReport:
    return ReasoningReport(
        bias=BiasJudgment(
            bias="bullish",
            confidence=0.65,
            evidences=[
                Evidence(source="flow.session_ud", fact_value="1.15", direction="bull", weight=0.20),
                Evidence(source="chips.net_ratio", fact_value="0.625", direction="bull", weight=0.20),
            ],
            summary="bullish bias",
        ),
        levels=[
            EnrichedLevel(
                price=201_500_000,
                side="resistance",
                strength=0.9,
                sources=["session high", "round number"],
                confluence_count=2,
            ),
            EnrichedLevel(
                price=200_000_000,
                side="pivot",
                strength=0.7,
                sources=["pivot"],
                confluence_count=1,
            ),
            EnrichedLevel(
                price=199_000_000,
                side="support",
                strength=0.8,
                sources=["session low", "chip cluster"],
                confluence_count=2,
            ),
        ],
        scenarios=[
            Scenario(
                id="hold_bounce",
                label="hold bounce",
                probability="medium",
                condition="hold support",
                target=201_500_000,
                description="bounce from support",
            )
        ],
        narrative=NarrativeReport(
            storyline=[
                "opening (08:45-09:30): buyers lead with U/D 1.20",
                "midday (09:30-12:00): balanced trade",
                "closing (12:00-13:45): buyers retake control",
            ],
            turning_points=[("midday", "bull to neutral")],
            conclusion="must not be used by dossier",
        ),
    )


def test_build_llm_dossier_extracts_symbol_and_required_evidence() -> None:
    dossier = build_llm_dossier(_make_fact_report(), _make_reasoning_report())

    assert dossier.symbol == "TXFD6"
    assert dossier.evidence["flow.session_ud"] == "1.15"
    assert dossier.evidence["flow.session_net_flow"] == "500"
    assert dossier.evidence["flow.eod_drift"] == "0.1"
    assert dossier.evidence["chips.net_ratio"] == "0.625"
    assert dossier.evidence["cross_day.trend_direction"] == "up"
    assert dossier.evidence["segments.closing"] == "bull|ud=1.1|net=30"
    assert dossier.evidence["flow.strongest_buy_bar"] == "2026-03-28 09:00:00|ud=2|net=150|vol=500"
    assert dossier.evidence["flow.strongest_sell_bar"] == "2026-03-28 11:30:00|ud=0.5|net=-120|vol=420"
    assert dossier.evidence["chips.buy_zone"] == "19,950-20,020"
    assert dossier.evidence["chips.sell_zone"] == "20,080-20,120"
    assert dossier.evidence["structure.failed_breakouts"] == "1"
    assert dossier.evidence["structure.session_high"] == "20,100"
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

    with pytest.raises(ValueError, match="narrative must be non-empty"):
        build_llm_dossier(_make_fact_report(), reasoning_report)
