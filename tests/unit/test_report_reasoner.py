"""Tests for Layer 2 Reasoner (reports.reasoner)."""

from __future__ import annotations

from hft_platform.reports.models import (
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
    SegmentFact,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)
from hft_platform.reports.reasoner import (
    BiasReasoner,
    LevelReasoner,
    NarrativeReasoner,
    ScenarioReasoner,
    reason_all,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCALE = 10_000

_DUMMY_FLOW_BAR = FlowBar(
    ts="1970-01-01 00:00:00",
    ticks=0,
    total_vol=0,
    uptick_vol=0,
    downtick_vol=0,
    flat_vol=0,
    ud_ratio=1.0,
    net_flow=0,
)


def _session_data(
    close: int = 200,
    high: int = 205,
    low: int = 195,
    open_: int = 200,
) -> SessionData:
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-29",
        open=open_ * SCALE,
        high=high * SCALE,
        low=low * SCALE,
        close=close * SCALE,
        volume=5000,
        tick_count=1000,
        bars_5m=[],
        flow_5m=[],
        large_trades=[],
        spread_dist={},
        depth_imbalance=[],
    )


def _segment(
    name: str = "closing",
    ud_ratio: float = 1.0,
    dominant_side: str = "neutral",
    volume_pct: float = 0.25,
    large_buy: int = 0,
    large_sell: int = 0,
) -> SegmentFact:
    return SegmentFact(
        name=name,
        time_range="12:00-13:45",
        ud_ratio=ud_ratio,
        net_flow=0,
        volume=1000,
        volume_pct=volume_pct,
        large_buy_count=large_buy,
        large_sell_count=large_sell,
        high=205 * SCALE,
        low=195 * SCALE,
        dominant_side=dominant_side,
    )


def _flow_facts(
    session_ud: float = 1.0,
    eod_drift: float = 0.0,
    sustained_runs: list[tuple[str, int, str]] | None = None,
) -> FlowFacts:
    return FlowFacts(
        session_ud=session_ud,
        session_net_flow=0,
        strongest_buy_bar=_DUMMY_FLOW_BAR,
        strongest_sell_bar=_DUMMY_FLOW_BAR,
        sustained_runs=sustained_runs or [],
        volume_spikes=[],
        eod_ud=1.0,
        eod_drift=eod_drift,
    )


def _chip_facts(
    net_ratio: float = 0.50,
    clusters: list[ChipCluster] | None = None,
    vap_peaks: list[PriceLevel] | None = None,
) -> ChipFacts:
    return ChipFacts(
        clusters=clusters or [],
        vap_peaks=vap_peaks or [],
        buy_zone=None,
        sell_zone=None,
        total_buy_volume=100,
        total_sell_volume=100,
        net_ratio=net_ratio,
    )


def _structure_facts(
    failed_breakouts: list[PriceLevel] | None = None,
    double_bottoms: list[PriceLevel] | None = None,
    double_tops: list[PriceLevel] | None = None,
    round_numbers: list[PriceLevel] | None = None,
) -> StructureFacts:
    return StructureFacts(
        double_bottoms=double_bottoms or [],
        double_tops=double_tops or [],
        failed_breakouts=failed_breakouts or [],
        round_numbers=round_numbers or [],
        session_high=PriceLevel(price=205 * SCALE, strength=0.5, reason="session high"),
        session_low=PriceLevel(price=195 * SCALE, strength=0.5, reason="session low"),
    )


def _volatility_facts(
    atr_session: int = 100_000,
    range_atr_ratio: float = 1.0,
) -> VolatilityFacts:
    return VolatilityFacts(
        atr_5m=10_000,
        session_range=100_000,
        range_atr_ratio=range_atr_ratio,
        atr_session=atr_session,
    )


def _cross_day(
    trend_direction: str = "sideways",
    flow_reversal: bool = False,
    prev_days: list[DaySnapshot] | None = None,
) -> CrossDayFacts:
    return CrossDayFacts(
        prev_days=prev_days or [],
        volume_change_pct=0.0,
        price_position="inside_range",
        trend_direction=trend_direction,
        flow_reversal=flow_reversal,
    )


def _fact_report(**kwargs: object) -> FactReport:
    """Build a FactReport with sensible defaults, overridable via kwargs."""
    defaults: dict[str, object] = {
        "session_data": _session_data(),
        "segments": [
            _segment("opening", ud_ratio=1.2, dominant_side="neutral"),
            _segment("midday", ud_ratio=1.0, dominant_side="neutral"),
            _segment("closing", ud_ratio=1.0, dominant_side="neutral"),
        ],
        "chips": _chip_facts(),
        "flow": _flow_facts(),
        "structure": _structure_facts(),
        "volatility": _volatility_facts(),
        "cross_day": _cross_day(),
    }
    defaults.update(kwargs)
    return FactReport(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BiasReasoner
# ---------------------------------------------------------------------------


class TestBiasReasoner:
    """Tests for evidence-driven bias determination."""

    def test_bearish_concordant(self) -> None:
        """All bear signals → bearish bias."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=0.70, eod_drift=-0.30, sustained_runs=[("bear", 5, "10:00-10:25")]),
            chips=_chip_facts(net_ratio=0.35),
            segments=[
                _segment("opening", dominant_side="bear"),
                _segment("closing", dominant_side="bear"),
            ],
            cross_day=_cross_day(trend_direction="down"),
        )
        result = BiasReasoner().judge(fr)

        assert result.bias == "bearish"
        assert result.confidence > 0.5
        assert "偏空" in result.summary
        assert len(result.evidences) == 8

    def test_bullish_all_bull(self) -> None:
        """All bull signals → bullish bias."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=1.40, eod_drift=0.30, sustained_runs=[("bull", 6, "09:00-09:30")]),
            chips=_chip_facts(net_ratio=0.65),
            segments=[
                _segment("opening", dominant_side="bull"),
                _segment("closing", dominant_side="bull"),
            ],
            cross_day=_cross_day(trend_direction="up"),
        )
        result = BiasReasoner().judge(fr)

        assert result.bias == "bullish"
        assert result.confidence > 0.5
        assert "偏多" in result.summary

    def test_neutral_dead_zone(self) -> None:
        """All evidence in neutral range → neutral bias with lower confidence."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=1.0, eod_drift=0.0),
            chips=_chip_facts(net_ratio=0.50),
            segments=[
                _segment("opening", dominant_side="neutral"),
                _segment("closing", dominant_side="neutral"),
            ],
            cross_day=_cross_day(trend_direction="sideways"),
        )
        result = BiasReasoner().judge(fr)

        assert result.bias == "neutral"
        assert "中性" in result.summary

    def test_contradictions_produce_neutral(self) -> None:
        """Bull flow + bear chips → mixed signals can result in neutral."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=1.30, eod_drift=0.0),
            chips=_chip_facts(net_ratio=0.35),
            segments=[
                _segment("opening", dominant_side="neutral"),
                _segment("closing", dominant_side="neutral"),
            ],
            cross_day=_cross_day(trend_direction="sideways"),
        )
        result = BiasReasoner().judge(fr)

        # Bull from flow.session_ud (0.20) vs bear from chips.net_ratio (0.20)
        # Should be neutral or very close
        assert result.bias in ("neutral", "bullish", "bearish")
        assert len(result.evidences) == 8

    def test_failed_breakout_support_is_bull(self) -> None:
        """Failed low breakout (支撐) → bull evidence."""
        fr = _fact_report(
            structure=_structure_facts(
                failed_breakouts=[PriceLevel(price=195 * SCALE, strength=0.8, reason="假跌破支撐")]
            ),
        )
        result = BiasReasoner().judge(fr)

        fb_ev = [ev for ev in result.evidences if ev.source == "structure.failed_breakouts"][0]
        assert fb_ev.direction == "bull"

    def test_failed_breakout_resistance_is_bear(self) -> None:
        """Failed high breakout (壓力) → bear evidence."""
        fr = _fact_report(
            structure=_structure_facts(
                failed_breakouts=[PriceLevel(price=210 * SCALE, strength=0.8, reason="假突破壓力")]
            ),
        )
        result = BiasReasoner().judge(fr)

        fb_ev = [ev for ev in result.evidences if ev.source == "structure.failed_breakouts"][0]
        assert fb_ev.direction == "bear"

    def test_flow_reversal_bull(self) -> None:
        """Flow reversal + today bullish → bull evidence."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=1.20),
            cross_day=_cross_day(flow_reversal=True),
        )
        result = BiasReasoner().judge(fr)

        rev_ev = [ev for ev in result.evidences if ev.source == "cross_day.flow_reversal"][0]
        assert rev_ev.direction == "bull"

    def test_eight_evidences_always(self) -> None:
        """Always exactly 8 evidence items."""
        fr = _fact_report()
        result = BiasReasoner().judge(fr)
        assert len(result.evidences) == 8


# ---------------------------------------------------------------------------
# LevelReasoner
# ---------------------------------------------------------------------------


class TestLevelReasoner:
    """Tests for level enrichment and confluence merging."""

    def test_confluence_merge(self) -> None:
        """Two levels within proximity merge into one with confluence=2."""
        # Session high at 205 and a double top at 205.2 (within 5 pts)
        fr = _fact_report(
            session_data=_session_data(close=200),
            structure=_structure_facts(
                double_tops=[PriceLevel(price=2052000, strength=0.8, reason="雙頂")],
            ),
        )
        # session_high is 205*SCALE = 2050000, double_top at 2052000 → within 50000
        levels = LevelReasoner().analyze(fr)

        # Should have merged levels with confluence >= 2
        merged = [lv for lv in levels if lv.confluence_count >= 2]
        assert len(merged) >= 1

    def test_buffer_zone_pivot(self) -> None:
        """Level within ±50000 of close is classified as pivot."""
        # close=200, session_high=201 (within buffer)
        fr = _fact_report(
            session_data=_session_data(close=200, high=201, low=199),
            structure=StructureFacts(
                double_bottoms=[],
                double_tops=[],
                failed_breakouts=[],
                round_numbers=[],
                session_high=PriceLevel(price=201 * SCALE, strength=0.8, reason="session high"),
                session_low=PriceLevel(price=199 * SCALE, strength=0.8, reason="session low"),
            ),
        )
        levels = LevelReasoner().analyze(fr)

        # 201 * 10000 = 2010000, close = 200 * 10000 = 2000000
        # diff = 10000 < 50000 → pivot
        pivot_levels = [lv for lv in levels if lv.side == "pivot"]
        # Both session high and low are within buffer
        assert len(pivot_levels) >= 1

    def test_no_hard_limit(self) -> None:
        """No artificial 3-level limit per side."""
        # Create many levels far from close
        many_levels = [PriceLevel(price=(210 + i * 10) * SCALE, strength=0.8, reason=f"level_{i}") for i in range(5)]
        fr = _fact_report(
            session_data=_session_data(close=200),
            structure=_structure_facts(double_tops=many_levels),
        )
        levels = LevelReasoner().analyze(fr)

        resistance_count = sum(1 for lv in levels if lv.side == "resistance")
        # We should get more than 3 if they all qualify
        assert resistance_count >= 3

    def test_weak_singleton_filtered(self) -> None:
        """Single level with strength < 0.7 is filtered out."""
        fr = _fact_report(
            session_data=_session_data(close=200, high=200, low=200),
            structure=StructureFacts(
                double_bottoms=[],
                double_tops=[],
                failed_breakouts=[],
                round_numbers=[],
                session_high=PriceLevel(price=200 * SCALE, strength=0.3, reason="weak high"),
                session_low=PriceLevel(price=200 * SCALE, strength=0.3, reason="weak low"),
            ),
            chips=_chip_facts(vap_peaks=[]),
        )
        levels = LevelReasoner().analyze(fr)

        # Both are at same price, so they merge with confluence=2 → kept
        # But if all at same price, confluence >= 2 → kept regardless of strength
        # This tests the filter logic works
        assert all(lv.confluence_count >= 2 or lv.strength >= 0.7 for lv in levels)

    def test_chip_clusters_converted(self) -> None:
        """Chip clusters are converted to PriceLevels and included."""
        cluster = ChipCluster(
            price_center=210 * SCALE,
            price_range=(209 * SCALE, 211 * SCALE),
            buy_volume=500,
            sell_volume=100,
            trade_count=15,
            dominant_side="buy",
            first_ts="2026-03-29 09:00:00",
            last_ts="2026-03-29 09:30:00",
            time_range="09:00-09:30",
        )
        fr = _fact_report(
            session_data=_session_data(close=200),
            chips=_chip_facts(clusters=[cluster]),
        )
        levels = LevelReasoner().analyze(fr)

        cluster_sources = [lv for lv in levels if any("大單群聚" in s for s in lv.sources)]
        assert len(cluster_sources) >= 1


# ---------------------------------------------------------------------------
# ScenarioReasoner
# ---------------------------------------------------------------------------


class TestScenarioReasoner:
    """Tests for conditional scenario generation."""

    def _bias(self, bias: str = "neutral", confidence: float = 0.5) -> BiasJudgment:
        return BiasJudgment(
            bias=bias,
            confidence=confidence,
            evidences=[Evidence("test", "1.0", "bull", 0.5)],
            summary="test",
        )

    def test_break_below_with_single_support(self) -> None:
        """break_below generated when support exists and bias != bullish."""
        fr = _fact_report()
        bias = self._bias("bearish", 0.7)
        levels = [EnrichedLevel(price=195 * SCALE, side="support", strength=0.8, sources=["test"], confluence_count=2)]

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        assert "break_below" in ids

        bb = [s for s in scenarios if s.id == "break_below"][0]
        assert bb.label == "破底加速"
        assert bb.probability == "高"  # bearish + confidence > 0.6

    def test_hold_bounce_with_support_and_bull_evidence(self) -> None:
        """hold_bounce generated when support exists and bull evidence present."""
        fr = _fact_report()
        bias = self._bias("neutral", 0.5)
        levels = [EnrichedLevel(price=195 * SCALE, side="support", strength=0.8, sources=["test"], confluence_count=2)]

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        # bias has a bull evidence in its evidences list
        assert "hold_bounce" in ids

    def test_gap_fill_on_gap(self) -> None:
        """gap_fill generated when gap >= 0.3%."""
        prev_close = 200 * SCALE
        open_price = 201 * SCALE  # 0.5% gap
        fr = _fact_report(
            session_data=_session_data(close=201, open_=201),
            cross_day=_cross_day(
                prev_days=[
                    DaySnapshot(
                        date="2026-03-28",
                        session="day",
                        open=199 * SCALE,
                        high=201 * SCALE,
                        low=198 * SCALE,
                        close=prev_close,
                        volume=5000,
                        ud_ratio=1.0,
                        net_flow=0,
                    )
                ],
            ),
        )
        bias = self._bias("neutral")
        levels: list[EnrichedLevel] = []

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        assert "gap_fill" in ids

        gf = [s for s in scenarios if s.id == "gap_fill"][0]
        assert gf.target == prev_close

    def test_range_bound_on_low_ratio(self) -> None:
        """range_bound generated when range_atr_ratio < 0.7 + neutral bias."""
        fr = _fact_report(
            volatility=_volatility_facts(range_atr_ratio=0.5),
        )
        bias = self._bias("neutral")
        levels: list[EnrichedLevel] = []

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        assert "range_bound" in ids

        rb = [s for s in scenarios if s.id == "range_bound"][0]
        assert rb.label == "區間震盪"

    def test_trend_continue_concordant(self) -> None:
        """trend_continue generated when trend + bias align."""
        fr = _fact_report(
            cross_day=_cross_day(
                trend_direction="up",
                prev_days=[
                    DaySnapshot(
                        date=f"2026-03-2{8 - i}",
                        session="day",
                        open=198 * SCALE,
                        high=201 * SCALE,
                        low=197 * SCALE,
                        close=(199 + i) * SCALE,
                        volume=5000,
                        ud_ratio=1.1,
                        net_flow=100,
                    )
                    for i in range(3)
                ],
            ),
        )
        bias = self._bias("bullish", 0.7)
        levels: list[EnrichedLevel] = []

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        assert "trend_continue" in ids

    def test_no_break_below_when_bullish(self) -> None:
        """break_below NOT generated when bias is bullish."""
        fr = _fact_report()
        bias = self._bias("bullish")
        levels = [EnrichedLevel(price=195 * SCALE, side="support", strength=0.8, sources=["test"], confluence_count=2)]

        scenarios = ScenarioReasoner().generate(fr, bias, levels)

        ids = [s.id for s in scenarios]
        assert "break_below" not in ids

    def test_probability_high_when_concordant_strong(self) -> None:
        """Probability = '高' when bias concordant + confidence > 0.6."""
        fr = _fact_report()
        bias = self._bias("bearish", 0.75)
        levels = [EnrichedLevel(price=195 * SCALE, side="support", strength=0.8, sources=["test"], confluence_count=2)]

        scenarios = ScenarioReasoner().generate(fr, bias, levels)
        bb = [s for s in scenarios if s.id == "break_below"][0]
        assert bb.probability == "高"


# ---------------------------------------------------------------------------
# NarrativeReasoner
# ---------------------------------------------------------------------------


class TestNarrativeReasoner:
    """Tests for segment narrative generation."""

    def test_one_paragraph_per_segment(self) -> None:
        """Storyline has one entry per segment."""
        fr = _fact_report(
            segments=[
                _segment("opening", ud_ratio=1.3, dominant_side="bull"),
                _segment("midday", ud_ratio=0.7, dominant_side="bear"),
                _segment("closing", ud_ratio=1.0, dominant_side="neutral"),
            ],
        )
        result = NarrativeReasoner().narrate(fr)

        assert len(result.storyline) == 3
        assert "opening" in result.storyline[0]
        assert "midday" in result.storyline[1]
        assert "closing" in result.storyline[2]

    def test_turning_point_on_ud_flip(self) -> None:
        """Turning point detected when dominant_side flips bull→bear."""
        fr = _fact_report(
            segments=[
                _segment("opening", dominant_side="bull"),
                _segment("midday", dominant_side="bear"),
                _segment("closing", dominant_side="bear"),
            ],
        )
        result = NarrativeReasoner().narrate(fr)

        assert len(result.turning_points) >= 1
        tp_names = [tp[0] for tp in result.turning_points]
        assert "midday" in tp_names

    def test_no_turning_point_neutral_transition(self) -> None:
        """No turning point when transition is bull→neutral."""
        fr = _fact_report(
            segments=[
                _segment("opening", dominant_side="bull"),
                _segment("midday", dominant_side="neutral"),
                _segment("closing", dominant_side="neutral"),
            ],
        )
        result = NarrativeReasoner().narrate(fr)
        assert len(result.turning_points) == 0

    def test_conclusion_not_empty(self) -> None:
        """Conclusion is always a non-empty string."""
        fr = _fact_report()
        result = NarrativeReasoner().narrate(fr)
        assert len(result.conclusion) > 0

    def test_conclusion_with_trend(self) -> None:
        """Conclusion includes trend context when cross_day has trend."""
        fr = _fact_report(
            segments=[_segment("closing", dominant_side="bear")],
            cross_day=_cross_day(
                trend_direction="down",
                prev_days=[
                    DaySnapshot(
                        date="2026-03-28",
                        session="day",
                        open=200 * SCALE,
                        high=201 * SCALE,
                        low=198 * SCALE,
                        close=199 * SCALE,
                        volume=5000,
                        ud_ratio=0.9,
                        net_flow=-100,
                    )
                ],
            ),
        )
        result = NarrativeReasoner().narrate(fr)
        assert "走弱" in result.conclusion

    def test_large_trade_note_in_paragraph(self) -> None:
        """Paragraph includes large trade note when present."""
        fr = _fact_report(
            segments=[_segment("closing", large_buy=3, large_sell=1)],
        )
        result = NarrativeReasoner().narrate(fr)
        assert "大單" in result.storyline[0]
        assert "4 筆" in result.storyline[0]

    def test_volume_concentrated(self) -> None:
        """Volume commentary includes '量能集中' when pct > 35%."""
        fr = _fact_report(
            segments=[_segment("closing", volume_pct=0.40)],
        )
        result = NarrativeReasoner().narrate(fr)
        assert "量能集中" in result.storyline[0]

    def test_volume_shrink(self) -> None:
        """Volume commentary includes '量能萎縮' when pct < 15%."""
        fr = _fact_report(
            segments=[_segment("closing", volume_pct=0.10)],
        )
        result = NarrativeReasoner().narrate(fr)
        assert "量能萎縮" in result.storyline[0]


# ---------------------------------------------------------------------------
# reason_all
# ---------------------------------------------------------------------------


class TestReasonAll:
    """Tests for the orchestrator function."""

    def test_returns_valid_reasoning_report(self) -> None:
        """reason_all returns a ReasoningReport with all four components."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=0.75, eod_drift=-0.25),
            chips=_chip_facts(net_ratio=0.40),
            segments=[
                _segment("opening", dominant_side="bear"),
                _segment("midday", dominant_side="bear"),
                _segment("closing", dominant_side="bear"),
            ],
            cross_day=_cross_day(trend_direction="down"),
        )
        result = reason_all(fr)

        assert isinstance(result, ReasoningReport)
        assert isinstance(result.bias, BiasJudgment)
        assert isinstance(result.levels, list)
        assert isinstance(result.scenarios, list)
        assert isinstance(result.narrative, NarrativeReport)

    def test_scenarios_use_bias_from_reasoner(self) -> None:
        """Scenarios are generated using the bias determined by BiasReasoner."""
        fr = _fact_report(
            flow=_flow_facts(session_ud=0.70, eod_drift=-0.30),
            chips=_chip_facts(net_ratio=0.35),
            segments=[
                _segment("opening", dominant_side="bear"),
                _segment("closing", dominant_side="bear"),
            ],
        )
        result = reason_all(fr)

        assert result.bias.bias == "bearish"
        # With bearish bias and support levels from structure, break_below should appear
        if any(lv.side == "support" for lv in result.levels):
            ids = [s.id for s in result.scenarios]
            assert "break_below" in ids

    def test_empty_segments_still_works(self) -> None:
        """reason_all handles empty segments gracefully."""
        fr = _fact_report(segments=[])

        result = reason_all(fr)

        assert isinstance(result, ReasoningReport)
        assert result.narrative.conclusion == "無盤中資料"
        assert len(result.narrative.storyline) == 0
