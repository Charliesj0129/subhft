"""Tests for Layer 3 ReportComposer."""

from __future__ import annotations

import dataclasses

import pytest

from hft_platform.reports.composer import (
    TELEGRAM_MAX_LEN,
    ReportComposer,
    _median_spread,
    _p,
    _pct,
    _split_message,
    _stars,
)
from hft_platform.reports.llm_models import EvidenceRef, LLMDecisionReport, TradePlan
from hft_platform.reports.models import (
    Bar5m,
    BiasJudgment,
    ChipCluster,
    ChipFacts,
    ComposedReport,
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_flow_bar(
    ts: str = "2026-03-28 09:00:00",
    *,
    ud_ratio: float = 1.0,
    net_flow: int = 0,
    total_vol: int = 100,
    uptick_vol: int = 50,
    downtick_vol: int = 50,
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


def _make_bar5m(
    ts: str = "2026-03-28 09:00:00",
    *,
    open_: int = 200_000_000,
    high: int = 200_500_000,
    low: int = 199_500_000,
    close: int = 200_200_000,
) -> Bar5m:
    return Bar5m(ts=ts, open=open_, high=high, low=low, close=close, volume=500, ticks=120)


def _make_session_data() -> SessionData:
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-28",
        open=200_000_000,
        high=201_000_000,
        low=199_000_000,
        close=200_500_000,
        volume=35000,
        tick_count=18000,
        bars_5m=[
            _make_bar5m("2026-03-28 09:00:00"),
            _make_bar5m("2026-03-28 09:05:00"),
        ],
        flow_5m=[
            _make_flow_bar("2026-03-28 09:00:00", ud_ratio=1.2, net_flow=50, uptick_vol=60, downtick_vol=50),
            _make_flow_bar("2026-03-28 09:05:00", ud_ratio=0.8, net_flow=-30, uptick_vol=40, downtick_vol=50),
        ],
        large_trades=[],
        spread_dist={1: 100, 2: 200, 3: 50},
        depth_imbalance=[],
    )


def _make_fact_report(*, with_cross_day: bool = True) -> FactReport:
    sd = _make_session_data()

    segments = [
        SegmentFact(
            name="opening",
            time_range="08:45-09:30",
            ud_ratio=1.2,
            net_flow=100,
            volume=5000,
            volume_pct=0.30,
            large_buy_count=2,
            large_sell_count=1,
            high=201_000_000,
            low=199_500_000,
            dominant_side="bull",
        ),
        SegmentFact(
            name="midday",
            time_range="09:30-12:00",
            ud_ratio=0.9,
            net_flow=-20,
            volume=8000,
            volume_pct=0.40,
            large_buy_count=1,
            large_sell_count=2,
            high=200_800_000,
            low=199_200_000,
            dominant_side="neutral",
        ),
        SegmentFact(
            name="closing",
            time_range="12:00-13:45",
            ud_ratio=1.1,
            net_flow=30,
            volume=6000,
            volume_pct=0.30,
            large_buy_count=1,
            large_sell_count=0,
            high=200_600_000,
            low=199_800_000,
            dominant_side="bull",
        ),
    ]

    chips = ChipFacts(
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
            ),
        ],
        vap_peaks=[],
        buy_zone=(199_500_000, 200_200_000),
        sell_zone=None,
        total_buy_volume=200,
        total_sell_volume=120,
        net_ratio=0.625,
    )

    flow = FlowFacts(
        session_ud=1.15,
        session_net_flow=500,
        strongest_buy_bar=_make_flow_bar("2026-03-28 09:00:00", ud_ratio=2.0),
        strongest_sell_bar=_make_flow_bar("2026-03-28 11:30:00", ud_ratio=0.5),
        sustained_runs=[("bull", 5, "09:00-09:25")],
        volume_spikes=[(_make_flow_bar("2026-03-28 09:00:00", total_vol=500), 3.2)],
        eod_ud=1.25,
        eod_drift=0.10,
    )

    structure = StructureFacts(
        double_bottoms=[],
        double_tops=[],
        failed_breakouts=[],
        round_numbers=[PriceLevel(price=200_000_000, strength=0.6, reason="整數關卡 20000")],
        session_high=PriceLevel(price=201_000_000, strength=0.5, reason="session high"),
        session_low=PriceLevel(price=199_000_000, strength=0.5, reason="session low"),
    )

    volatility = VolatilityFacts(
        atr_5m=50_000,
        session_range=2_000_000,
        range_atr_ratio=1.5,
        atr_session=200_000,
    )

    prev_days: list[DaySnapshot] = []
    if with_cross_day:
        prev_days = [
            DaySnapshot(
                date="2026-03-27",
                session="day",
                open=199_000_000,
                high=200_500_000,
                low=198_500_000,
                close=200_000_000,
                volume=30000,
                ud_ratio=1.05,
                net_flow=200,
            ),
            DaySnapshot(
                date="2026-03-26",
                session="day",
                open=198_000_000,
                high=199_500_000,
                low=197_500_000,
                close=199_000_000,
                volume=28000,
                ud_ratio=1.10,
                net_flow=300,
            ),
        ]

    cross_day = CrossDayFacts(
        prev_days=prev_days,
        volume_change_pct=16.7,
        price_position="above_prev_high" if with_cross_day else "inside_range",
        trend_direction="up" if with_cross_day else "sideways",
        flow_reversal=False,
    )

    return FactReport(
        session_data=sd,
        segments=segments,
        chips=chips,
        flow=flow,
        structure=structure,
        volatility=volatility,
        cross_day=cross_day,
    )


def _make_reasoning_report() -> ReasoningReport:
    bias = BiasJudgment(
        bias="bullish",
        confidence=0.65,
        evidences=[
            Evidence(source="flow.session_ud", fact_value="1.15", direction="bull", weight=0.20),
            Evidence(source="chips.net_ratio", fact_value="0.63", direction="bull", weight=0.20),
            Evidence(source="flow.eod_drift", fact_value="+0.10", direction="neutral", weight=0.15),
            Evidence(source="flow.sustained_runs", fact_value="bullx5", direction="bull", weight=0.15),
            Evidence(source="segments.closing", fact_value="bull", direction="bull", weight=0.10),
        ],
        summary="偏多 (65%): 全場 U/D=1.15 多方主導 + 連續上漲趨勢",
    )

    levels = [
        EnrichedLevel(
            price=201_500_000, side="resistance", strength=0.9, sources=["session high", "整數關卡"], confluence_count=2
        ),
        EnrichedLevel(price=200_000_000, side="pivot", strength=0.7, sources=["整數關卡 20000"], confluence_count=1),
        EnrichedLevel(
            price=199_000_000, side="support", strength=0.8, sources=["session low", "大單群聚"], confluence_count=2
        ),
    ]

    scenarios = [
        Scenario(
            id="hold_bounce",
            label="守支撐反彈",
            probability="中",
            condition="守住支撐 199000000",
            target=201_500_000,
            description="支撐守住後反彈，目標 201500000，停損 198800000。",
        ),
        Scenario(
            id="break_below",
            label="破底加速",
            probability="低",
            condition="跌破支撐 199000000",
            target=198_000_000,
            description="若跌破 S1 支撐，目標 198000000，停損 199200000。",
        ),
    ]

    narrative = NarrativeReport(
        storyline=[
            "opening（08:45-09:30）：多方主導，U/D=1.20，量能佔全場 30%。大單 3 筆（買 2/賣 1）",
            "midday（09:30-12:00）：多空拉鋸，U/D=0.90，量能佔全場 40%",
            "closing（12:00-13:45）：多方主導，U/D=1.10，量能佔全場 30%。大單 1 筆（買 1/賣 0）",
        ],
        turning_points=[("midday", "多方→中性")],
        conclusion="多方尾盤接管，連續第3日走強",
    )

    return ReasoningReport(
        bias=bias,
        levels=levels,
        scenarios=scenarios,
        narrative=narrative,
    )


def _make_llm_decision() -> LLMDecisionReport:
    decision = LLMDecisionReport(
        market_verdict="偏多延續",
        intraday_plan=TradePlan(
            "bullish",
            "closing flow held",
            "hold above S1",
            "buy pullback",
            "lose S1",
            "R1",
            "R2",
            "avoid chasing",
        ),
        swing_plan=TradePlan(
            "bullish",
            "trend remains up",
            "daily close above R1",
            "hold partial",
            "close below S1",
            "R2",
            "R3",
            "cut ahead of event risk",
        ),
        key_levels=("S1 22,300", "R1 22,440"),
        invalidations=("lose S1 with expanding sell flow",),
        counter_case="opening rejection and failed reclaim turns thesis wrong",
        execution_notes=("only buy pullbacks",),
        confidence=72,
        evidence_refs=(EvidenceRef(key="flow.session_ud", detail="1.18"),),
    )
    decision.validate()
    return decision


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_p_formats_scaled_price(self) -> None:
        assert _p(200_000_000) == "20,000"
        assert _p(10_000) == "1"

    def test_pct_positive(self) -> None:
        result = _pct(200_000_000, 202_000_000)
        assert "▲" in result
        assert "+1.00%" in result

    def test_pct_negative(self) -> None:
        result = _pct(200_000_000, 198_000_000)
        assert "▼" in result
        assert "-1.00%" in result

    def test_pct_zero_open(self) -> None:
        result = _pct(0, 100_000)
        assert "0.00%" in result

    def test_stars(self) -> None:
        assert _stars(1) == "★☆☆"
        assert _stars(2) == "★★☆"
        assert _stars(3) == "★★★"
        assert _stars(0) == "☆☆☆"

    def test_median_spread_empty(self) -> None:
        assert _median_spread({}) == 0

    def test_median_spread_basic(self) -> None:
        # 100 counts at 1pt, 200 at 2pt, 50 at 3pt  => total=350, mid=175 => median=2
        assert _median_spread({1: 100, 2: 200, 3: 50}) == 2


class TestSplitMessage:
    def test_short_message_not_split(self) -> None:
        parts = _split_message("hello", "free")
        assert len(parts) == 1
        assert parts[0].content == "hello"
        assert parts[0].min_tier == "free"

    def test_long_message_split_at_newline(self) -> None:
        # Create a message > 4096 chars
        line = "x" * 100 + "\n"
        content = line * 50  # 5050 chars
        parts = _split_message(content, "paid")
        assert len(parts) >= 2
        for part in parts:
            assert len(part.content) <= TELEGRAM_MAX_LEN
            assert part.min_tier == "paid"

    def test_preserves_tier(self) -> None:
        parts = _split_message("test", "paid")
        assert parts[0].min_tier == "paid"


# ---------------------------------------------------------------------------
# Composer integration tests
# ---------------------------------------------------------------------------


class TestReportComposer:
    @pytest.fixture()
    def composer(self) -> ReportComposer:
        return ReportComposer()

    @pytest.fixture()
    def fr(self) -> FactReport:
        return _make_fact_report(with_cross_day=True)

    @pytest.fixture()
    def rr(self) -> ReasoningReport:
        return _make_reasoning_report()

    def test_compose_returns_composed_report(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        assert isinstance(result, ComposedReport)

    def test_compose_has_at_least_7_messages(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        # 6 text + disclaimer = 7 minimum (heatmap may or may not be present)
        assert len(result.messages) >= 7

    def test_summary_is_free_tier(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        summary = result.messages[0]
        assert summary.min_tier == "free"

    def test_summary_contains_symbol(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        assert "TXFD6" in result.messages[0].content

    def test_summary_contains_bias(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        content = result.messages[0].content
        assert "偏多" in content
        assert "65%" in content

    def test_summary_contains_cross_day_info(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        content = result.messages[0].content
        assert "vs 前日" in content
        assert "vs 前 3 日" in content

    def test_summary_skips_cross_day_when_no_prev_days(
        self,
        composer: ReportComposer,
        rr: ReasoningReport,
    ) -> None:
        fr_no_cd = _make_fact_report(with_cross_day=False)
        result = composer.compose(fr_no_cd, rr)
        content = result.messages[0].content
        assert "vs 前日" not in content

    def test_disclaimer_is_free_tier(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        disclaimer = result.messages[-1]
        assert disclaimer.min_tier == "free"
        assert "⚠️" in disclaimer.content

    def test_paid_messages_present(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        paid = [m for m in result.messages if m.min_tier == "paid"]
        assert len(paid) >= 5

    def test_no_message_exceeds_telegram_limit(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        for msg in result.messages:
            if msg.kind == "text":
                assert len(msg.content) <= TELEGRAM_MAX_LEN, (
                    f"Message exceeds {TELEGRAM_MAX_LEN} chars: {len(msg.content)}"
                )

    def test_narrative_message_contains_storyline(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        # Narrative is the second message (index 1)
        narr = result.messages[1]
        assert "📖 時段敘事" in narr.content
        assert "opening" in narr.content

    def test_flow_message_contains_ud(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        # Flow is the third message (index 2)
        flow_msg = result.messages[2]
        assert "🔍 流向深度分析" in flow_msg.content
        assert "U/D" in flow_msg.content

    def test_chips_message_present(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        chips_msg = result.messages[3]
        assert "🏦 籌碼結構" in chips_msg.content
        assert "買" in chips_msg.content


class TestComposeWithLLM:
    @pytest.fixture()
    def composer(self) -> ReportComposer:
        return ReportComposer()

    @pytest.fixture()
    def fr(self) -> FactReport:
        return _make_fact_report(with_cross_day=True)

    @pytest.fixture()
    def rr(self) -> ReasoningReport:
        return _make_reasoning_report()

    def test_inserts_llm_sections_before_disclaimer(self) -> None:
        report = ReportComposer().compose(
            _make_fact_report(),
            _make_reasoning_report(),
            llm_decision=_make_llm_decision(),
        )

        text_parts = [message.content for message in report.messages if message.kind == "text"]
        joined = "\n".join(text_parts)

        assert "LLM 市場裁決" in joined
        assert "當日交易計畫" in joined
        assert "1-3 日波段觀點" in joined
        assert "失效條件" in joined
        assert joined.index("LLM 市場裁決") < joined.index("⚠️")

    def test_works_without_llm_decision(self) -> None:
        report = ReportComposer().compose(
            _make_fact_report(),
            _make_reasoning_report(),
            llm_decision=None,
        )

        text_parts = [message.content for message in report.messages if message.kind == "text"]
        joined = "\n".join(text_parts)
        assert "LLM 市場裁決" not in joined

    def test_localizes_trade_plan_stance_labels(self) -> None:
        report = ReportComposer().compose(
            _make_fact_report(),
            _make_reasoning_report(),
            llm_decision=_make_llm_decision(),
        )

        joined = "\n".join(message.content for message in report.messages if message.kind == "text")
        assert "方向：偏多" in joined
        assert "方向：bullish" not in joined

    def test_splits_large_llm_block_within_telegram_limit(self) -> None:
        decision = dataclasses.replace(
            _make_llm_decision(),
            execution_notes=tuple(f"note-{index} " + ("x" * 240) for index in range(30)),
        )
        decision.validate()

        report = ReportComposer().compose(
            _make_fact_report(),
            _make_reasoning_report(),
            llm_decision=decision,
        )

        llm_start = next(i for i, message in enumerate(report.messages) if "LLM 市場裁決" in message.content)
        narrative_start = next(i for i, message in enumerate(report.messages) if "📖 時段敘事" in message.content)
        llm_parts = report.messages[llm_start:narrative_start]

        assert len(llm_parts) >= 2
        for message in llm_parts:
            assert message.kind == "text"
            assert len(message.content) <= TELEGRAM_MAX_LEN

    def test_levels_message_groups_by_side(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        levels_msg = result.messages[4]
        assert "🎯 關鍵點位" in levels_msg.content
        assert "壓力" in levels_msg.content
        assert "支撐" in levels_msg.content

    def test_scenarios_message_format(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        scenarios_msg = result.messages[5]
        assert "📋 情境規劃" in scenarios_msg.content
        assert "情境 A" in scenarios_msg.content
        assert "情境 B" in scenarios_msg.content

    def test_all_text_messages_have_kind_text(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        for msg in result.messages:
            assert msg.kind in ("text", "image")
            if msg.kind == "text":
                assert len(msg.content) > 0

    def test_session_label_day(
        self,
        composer: ReportComposer,
        fr: FactReport,
        rr: ReasoningReport,
    ) -> None:
        result = composer.compose(fr, rr)
        assert "日盤報告" in result.messages[0].content

    def test_session_label_night(
        self,
        composer: ReportComposer,
        rr: ReasoningReport,
    ) -> None:
        fr = _make_fact_report(with_cross_day=True)
        night_sd = dataclasses.replace(fr.session_data, session="night")
        fr_night = dataclasses.replace(fr, session_data=night_sd)
        result = composer.compose(fr_night, rr)
        assert "夜盤報告" in result.messages[0].content
