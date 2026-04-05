"""Unit tests for hft_platform.reports.models data contracts."""

from __future__ import annotations

import pytest

from hft_platform.contracts.types import ScaledPrice
from hft_platform.reports.models import (
    Bar5m,
    BiasJudgment,
    ChannelConfig,
    ChipCluster,
    ChipFacts,
    ComposedReport,
    CrossDayFacts,
    DaySnapshot,
    DepthBar,
    EnrichedLevel,
    Evidence,
    FactReport,
    FlowBar,
    FlowFacts,
    KeyLevel,
    LargeTrade,
    MessagePart,
    NarrativeReport,
    PriceLevel,
    ReasoningReport,
    Scenario,
    ScenarioReport,
    SegmentFact,
    SessionData,
    SignalReport,
    StructureFacts,
    VolatilityFacts,
)

# ---------------------------------------------------------------------------
# Bar5m
# ---------------------------------------------------------------------------


class TestBar5m:
    def test_creation(self) -> None:
        bar = Bar5m(
            ts="2026-03-27T09:00:00",
            open=ScaledPrice(220_0000),
            high=ScaledPrice(221_0000),
            low=ScaledPrice(219_0000),
            close=ScaledPrice(220_5000),
            volume=1000,
            ticks=42,
        )
        assert bar.ts == "2026-03-27T09:00:00"
        assert bar.open == ScaledPrice(220_0000)
        assert bar.volume == 1000
        assert bar.ticks == 42

    def test_slots_prevents_extra_attrs(self) -> None:
        bar = Bar5m(
            ts="t",
            open=ScaledPrice(1),
            high=ScaledPrice(2),
            low=ScaledPrice(1),
            close=ScaledPrice(1),
            volume=0,
            ticks=0,
        )
        with pytest.raises((AttributeError, TypeError)):
            bar.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FlowBar
# ---------------------------------------------------------------------------


class TestFlowBar:
    def test_creation(self) -> None:
        fb = FlowBar(
            ts="2026-03-27T09:05:00",
            ticks=10,
            total_vol=500,
            uptick_vol=300,
            downtick_vol=150,
            flat_vol=50,
            ud_ratio=2.0,
            net_flow=150,
        )
        assert fb.ts == "2026-03-27T09:05:00"
        assert fb.ud_ratio == 2.0
        assert fb.net_flow == 150

    def test_slots_prevents_extra_attrs(self) -> None:
        fb = FlowBar(
            ts="t",
            ticks=0,
            total_vol=0,
            uptick_vol=0,
            downtick_vol=0,
            flat_vol=0,
            ud_ratio=1.0,
            net_flow=0,
        )
        with pytest.raises((AttributeError, TypeError)):
            fb.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# LargeTrade
# ---------------------------------------------------------------------------


class TestLargeTrade:
    def test_creation(self) -> None:
        lt = LargeTrade(
            ts="2026-03-27T10:00:00",
            price=ScaledPrice(220_0000),
            volume=200,
            direction="buy",
        )
        assert lt.direction == "buy"
        assert lt.price == ScaledPrice(220_0000)

    def test_direction_values(self) -> None:
        for direction in ("buy", "sell", "unknown"):
            lt = LargeTrade(ts="t", price=ScaledPrice(1), volume=1, direction=direction)
            assert lt.direction == direction

    def test_slots_prevents_extra_attrs(self) -> None:
        lt = LargeTrade(ts="t", price=ScaledPrice(1), volume=1, direction="buy")
        with pytest.raises((AttributeError, TypeError)):
            lt.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DepthBar
# ---------------------------------------------------------------------------


class TestDepthBar:
    def test_creation(self) -> None:
        db = DepthBar(
            hour=9,
            avg_bid_vol=500.5,
            avg_ask_vol=480.0,
            bid_ratio=0.51,
        )
        assert db.hour == 9
        assert db.bid_ratio == 0.51

    def test_slots_prevents_extra_attrs(self) -> None:
        db = DepthBar(hour=9, avg_bid_vol=1.0, avg_ask_vol=1.0, bid_ratio=0.5)
        with pytest.raises((AttributeError, TypeError)):
            db.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SessionData
# ---------------------------------------------------------------------------


class TestSessionData:
    def _make_session(self) -> SessionData:
        return SessionData(
            session="day",
            symbol="TMFD6",
            date="2026-03-27",
            open=ScaledPrice(220_0000),
            high=ScaledPrice(222_0000),
            low=ScaledPrice(219_0000),
            close=ScaledPrice(221_0000),
            volume=5000,
            tick_count=120,
            bars_5m=[],
            flow_5m=[],
            large_trades=[],
            spread_dist={},
            depth_imbalance=[],
        )

    def test_creation(self) -> None:
        sd = self._make_session()
        assert sd.session == "day"
        assert sd.symbol == "TMFD6"
        assert sd.date == "2026-03-27"
        assert sd.bars_5m == []

    def test_slots_prevents_extra_attrs(self) -> None:
        sd = self._make_session()
        with pytest.raises((AttributeError, TypeError)):
            sd.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# PriceLevel
# ---------------------------------------------------------------------------


class TestPriceLevel:
    def test_creation(self) -> None:
        pl = PriceLevel(
            price=ScaledPrice(220_0000),
            strength=0.75,
            reason="large trade cluster",
        )
        assert pl.strength == 0.75
        assert pl.reason == "large trade cluster"

    def test_slots_prevents_extra_attrs(self) -> None:
        pl = PriceLevel(price=ScaledPrice(1), strength=0.5, reason="test")
        with pytest.raises((AttributeError, TypeError)):
            pl.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# SignalReport
# ---------------------------------------------------------------------------


class TestSignalReport:
    def _make_signal(self) -> SignalReport:
        session_data = SessionData(
            session="day",
            symbol="TMFD6",
            date="2026-03-27",
            open=ScaledPrice(220_0000),
            high=ScaledPrice(222_0000),
            low=ScaledPrice(219_0000),
            close=ScaledPrice(221_0000),
            volume=5000,
            tick_count=120,
            bars_5m=[],
            flow_5m=[],
            large_trades=[],
            spread_dist={},
            depth_imbalance=[],
        )
        flow = FlowBar(
            ts="t",
            ticks=10,
            total_vol=500,
            uptick_vol=300,
            downtick_vol=100,
            flat_vol=100,
            ud_ratio=3.0,
            net_flow=200,
        )
        return SignalReport(
            session_data=session_data,
            total_net_flow=500,
            ud_ratio_session=2.5,
            strongest_sell=flow,
            strongest_buy=flow,
            large_buy_volume=1000,
            large_sell_volume=800,
            large_net=200,
            key_large_trades=[],
            supports=[],
            resistances=[],
            bias="bullish",
            bias_confidence=0.72,
            rule_scores={},
        )

    def test_creation(self) -> None:
        sr = self._make_signal()
        assert sr.bias == "bullish"
        assert sr.bias_confidence == 0.72
        assert sr.total_net_flow == 500

    def test_slots_prevents_extra_attrs(self) -> None:
        sr = self._make_signal()
        with pytest.raises((AttributeError, TypeError)):
            sr.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Scenario
# ---------------------------------------------------------------------------


class TestScenario:
    def test_creation(self) -> None:
        sc = Scenario(
            id="S1",
            label="Bull breakout",
            probability="40%",
            condition="close > 222",
            target=ScaledPrice(223_0000),
            description="Momentum continuation above resistance.",
        )
        assert sc.id == "S1"
        assert sc.probability == "40%"

    def test_slots_prevents_extra_attrs(self) -> None:
        sc = Scenario(
            id="S1",
            label="l",
            probability="50%",
            condition="c",
            target=ScaledPrice(1),
            description="d",
        )
        with pytest.raises((AttributeError, TypeError)):
            sc.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# KeyLevel
# ---------------------------------------------------------------------------


class TestKeyLevel:
    def test_creation(self) -> None:
        kl = KeyLevel(
            price=ScaledPrice(220_0000),
            label="POC",
            importance=3,
            reason="volume node",
        )
        assert kl.importance == 3
        assert kl.label == "POC"

    def test_slots_prevents_extra_attrs(self) -> None:
        kl = KeyLevel(price=ScaledPrice(1), label="L", importance=1, reason="r")
        with pytest.raises((AttributeError, TypeError)):
            kl.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ScenarioReport
# ---------------------------------------------------------------------------


class TestScenarioReport:
    def _make_report(self) -> ScenarioReport:
        session_data = SessionData(
            session="day",
            symbol="TMFD6",
            date="2026-03-27",
            open=ScaledPrice(220_0000),
            high=ScaledPrice(222_0000),
            low=ScaledPrice(219_0000),
            close=ScaledPrice(221_0000),
            volume=5000,
            tick_count=120,
            bars_5m=[],
            flow_5m=[],
            large_trades=[],
            spread_dist={},
            depth_imbalance=[],
        )
        flow = FlowBar(
            ts="t",
            ticks=0,
            total_vol=0,
            uptick_vol=0,
            downtick_vol=0,
            flat_vol=0,
            ud_ratio=1.0,
            net_flow=0,
        )
        signal = SignalReport(
            session_data=session_data,
            total_net_flow=0,
            ud_ratio_session=1.0,
            strongest_sell=flow,
            strongest_buy=flow,
            large_buy_volume=0,
            large_sell_volume=0,
            large_net=0,
            key_large_trades=[],
            supports=[],
            resistances=[],
            bias="neutral",
            bias_confidence=0.5,
            rule_scores={},
        )
        return ScenarioReport(
            signal=signal,
            direction="long",
            confidence_pct=65,
            entry_zone=(ScaledPrice(220_0000), ScaledPrice(220_5000)),
            target=ScaledPrice(222_0000),
            stop_loss=ScaledPrice(219_0000),
            scenarios=[],
            key_levels=[],
        )

    def test_creation(self) -> None:
        rpt = self._make_report()
        assert rpt.direction == "long"
        assert rpt.confidence_pct == 65
        assert rpt.entry_zone == (ScaledPrice(220_0000), ScaledPrice(220_5000))

    def test_slots_prevents_extra_attrs(self) -> None:
        rpt = self._make_report()
        with pytest.raises((AttributeError, TypeError)):
            rpt.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ChannelConfig — frozen=True
# ---------------------------------------------------------------------------


class TestChannelConfig:
    def test_creation(self) -> None:
        cfg = ChannelConfig(
            name="telegram_main",
            chat_id="-1001234567890",
            tier="standard",
            enabled=True,
        )
        assert cfg.name == "telegram_main"
        assert cfg.enabled is True

    def test_frozen_prevents_mutation(self) -> None:
        cfg = ChannelConfig(
            name="telegram_main",
            chat_id="-1001234567890",
            tier="standard",
            enabled=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            cfg.enabled = False  # type: ignore[misc]

    def test_slots_prevents_extra_attrs(self) -> None:
        # frozen=True + slots=True: Python 3.12 raises TypeError for unknown
        # attribute assignment (slots prevent __dict__; frozen blocks __setattr__)
        cfg = ChannelConfig(name="n", chat_id="c", tier="t", enabled=False)
        with pytest.raises((AttributeError, TypeError)):
            cfg.extra_field = "nope"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helpers for three-layer models
# ---------------------------------------------------------------------------


def _make_flow_bar(**overrides: object) -> FlowBar:
    defaults: dict[str, object] = dict(
        ts="09:00", ticks=100, total_vol=5000, uptick_vol=3000,
        downtick_vol=2000, flat_vol=0, ud_ratio=1.5, net_flow=1000,
    )
    defaults.update(overrides)
    return FlowBar(**defaults)  # type: ignore[arg-type]


def _make_price_level(**overrides: object) -> PriceLevel:
    defaults: dict[str, object] = dict(price=200000, strength=0.8, reason="vap_peak")
    defaults.update(overrides)
    return PriceLevel(**defaults)  # type: ignore[arg-type]


def _make_session_data_simple() -> SessionData:
    return SessionData(
        session="day", symbol="TXFD6", date="2026-03-29",
        open=220000, high=221000, low=219000, close=220500,
        volume=50000, tick_count=10000,
        bars_5m=[], flow_5m=[], large_trades=[],
        spread_dist={}, depth_imbalance=[],
    )


def _make_scenario_simple(**overrides: object) -> Scenario:
    defaults: dict[str, object] = dict(
        id="s1", label="bullish", probability="60%",
        condition="break above 221000", target=222000,
        description="continuation",
    )
    defaults.update(overrides)
    return Scenario(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Layer 1: Facts — Three-Layer Architecture
# ---------------------------------------------------------------------------


class TestSegmentFact:
    def test_construction(self) -> None:
        sf = SegmentFact(
            name="opening", time_range="09:00-09:30", ud_ratio=1.2,
            net_flow=500, volume=10000, volume_pct=0.25,
            large_buy_count=3, large_sell_count=1,
            high=221000, low=219500, dominant_side="bull",
        )
        assert sf.name == "opening"
        assert sf.dominant_side == "bull"
        assert sf.volume_pct == 0.25

    def test_slots(self) -> None:
        sf = SegmentFact(
            name="closing", time_range="13:00-13:30", ud_ratio=0.8,
            net_flow=-200, volume=8000, volume_pct=0.15,
            large_buy_count=1, large_sell_count=2,
            high=220500, low=219800, dominant_side="bear",
        )
        assert hasattr(sf, "__slots__")
        with pytest.raises((AttributeError, TypeError)):
            sf.extra = 1  # type: ignore[attr-defined]


class TestChipCluster:
    def test_construction(self) -> None:
        cc = ChipCluster(
            price_center=220000, price_range=(219500, 220500),
            buy_volume=3000, sell_volume=1000, trade_count=15,
            dominant_side="bull", first_ts="09:05", last_ts="09:20",
            time_range="09:05-09:20",
        )
        assert cc.price_center == 220000
        assert cc.price_range == (219500, 220500)
        assert cc.dominant_side == "bull"

    def test_slots(self) -> None:
        cc = ChipCluster(
            price_center=220000, price_range=(219500, 220500),
            buy_volume=3000, sell_volume=1000, trade_count=15,
            dominant_side="bull", first_ts="09:05", last_ts="09:20",
            time_range="09:05-09:20",
        )
        with pytest.raises((AttributeError, TypeError)):
            cc.extra = 1  # type: ignore[attr-defined]


class TestChipFacts:
    def test_construction(self) -> None:
        cluster = ChipCluster(
            price_center=220000, price_range=(219500, 220500),
            buy_volume=3000, sell_volume=1000, trade_count=15,
            dominant_side="bull", first_ts="09:05", last_ts="09:20",
            time_range="09:05-09:20",
        )
        cf = ChipFacts(
            clusters=[cluster],
            vap_peaks=[_make_price_level()],
            buy_zone=(219000, 220000),
            sell_zone=(221000, 222000),
            total_buy_volume=5000,
            total_sell_volume=3000,
            net_ratio=0.625,
        )
        assert len(cf.clusters) == 1
        assert cf.buy_zone == (219000, 220000)
        assert cf.net_ratio == 0.625

    def test_none_zones(self) -> None:
        cf = ChipFacts(
            clusters=[], vap_peaks=[], buy_zone=None, sell_zone=None,
            total_buy_volume=0, total_sell_volume=0, net_ratio=0.0,
        )
        assert cf.buy_zone is None
        assert cf.sell_zone is None


class TestFlowFacts:
    def test_construction(self) -> None:
        ff = FlowFacts(
            session_ud=1.3, session_net_flow=2000,
            strongest_buy_bar=_make_flow_bar(ud_ratio=2.0),
            strongest_sell_bar=_make_flow_bar(ud_ratio=0.5),
            sustained_runs=[("bull", 3, "09:00-09:15")],
            volume_spikes=[(_make_flow_bar(), 2.5)],
            eod_ud=1.1, eod_drift=0.05,
        )
        assert ff.session_ud == 1.3
        assert len(ff.sustained_runs) == 1
        assert ff.eod_drift == 0.05


class TestStructureFacts:
    def test_construction(self) -> None:
        level = _make_price_level()
        sf = StructureFacts(
            double_bottoms=[level], double_tops=[],
            failed_breakouts=[], round_numbers=[level],
            session_high=_make_price_level(price=221000),
            session_low=_make_price_level(price=219000),
        )
        assert len(sf.double_bottoms) == 1
        assert sf.session_high.price == 221000


class TestVolatilityFacts:
    def test_construction(self) -> None:
        vf = VolatilityFacts(
            atr_5m=500, session_range=2000,
            range_atr_ratio=4.0, atr_session=1800,
        )
        assert vf.atr_5m == 500
        assert vf.range_atr_ratio == 4.0


class TestDaySnapshot:
    def test_construction(self) -> None:
        ds = DaySnapshot(
            date="2026-03-28", session="day",
            open=219000, high=220500, low=218500, close=220000,
            volume=45000, ud_ratio=1.1, net_flow=500,
        )
        assert ds.date == "2026-03-28"
        assert ds.close == 220000

    def test_slots(self) -> None:
        ds = DaySnapshot(
            date="2026-03-28", session="day",
            open=219000, high=220500, low=218500, close=220000,
            volume=45000, ud_ratio=1.1, net_flow=500,
        )
        with pytest.raises((AttributeError, TypeError)):
            ds.extra = 1  # type: ignore[attr-defined]


class TestCrossDayFacts:
    def test_construction(self) -> None:
        snap = DaySnapshot(
            date="2026-03-28", session="day",
            open=219000, high=220500, low=218500, close=220000,
            volume=45000, ud_ratio=1.1, net_flow=500,
        )
        cdf = CrossDayFacts(
            prev_days=[snap],
            volume_change_pct=0.15,
            price_position="above_prev_close",
            trend_direction="up",
            flow_reversal=False,
        )
        assert len(cdf.prev_days) == 1
        assert cdf.trend_direction == "up"
        assert cdf.flow_reversal is False


class TestFactReport:
    def test_construction(self) -> None:
        seg = SegmentFact(
            name="opening", time_range="09:00-09:30", ud_ratio=1.2,
            net_flow=500, volume=10000, volume_pct=0.25,
            large_buy_count=3, large_sell_count=1,
            high=221000, low=219500, dominant_side="bull",
        )
        chips = ChipFacts(
            clusters=[], vap_peaks=[], buy_zone=None, sell_zone=None,
            total_buy_volume=0, total_sell_volume=0, net_ratio=0.0,
        )
        flow = FlowFacts(
            session_ud=1.3, session_net_flow=2000,
            strongest_buy_bar=_make_flow_bar(),
            strongest_sell_bar=_make_flow_bar(),
            sustained_runs=[], volume_spikes=[],
            eod_ud=1.1, eod_drift=0.05,
        )
        structure = StructureFacts(
            double_bottoms=[], double_tops=[],
            failed_breakouts=[], round_numbers=[],
            session_high=_make_price_level(price=221000),
            session_low=_make_price_level(price=219000),
        )
        vol = VolatilityFacts(
            atr_5m=500, session_range=2000,
            range_atr_ratio=4.0, atr_session=1800,
        )
        cross = CrossDayFacts(
            prev_days=[], volume_change_pct=0.0,
            price_position="at_prev_close",
            trend_direction="flat", flow_reversal=False,
        )
        fr = FactReport(
            session_data=_make_session_data_simple(),
            segments=[seg], chips=chips, flow=flow,
            structure=structure, volatility=vol, cross_day=cross,
        )
        assert fr.session_data.symbol == "TXFD6"
        assert len(fr.segments) == 1


# ---------------------------------------------------------------------------
# Layer 2: Reasoning — Three-Layer Architecture
# ---------------------------------------------------------------------------


class TestEvidence:
    def test_construction(self) -> None:
        e = Evidence(
            source="flow", fact_value="ud_ratio=1.5",
            direction="bull", weight=0.8,
        )
        assert e.source == "flow"
        assert e.weight == 0.8

    def test_slots(self) -> None:
        e = Evidence(
            source="flow", fact_value="ud_ratio=1.5",
            direction="bull", weight=0.8,
        )
        with pytest.raises((AttributeError, TypeError)):
            e.extra = 1  # type: ignore[attr-defined]


class TestBiasJudgment:
    def test_construction(self) -> None:
        ev = Evidence(
            source="flow", fact_value="ud_ratio=1.5",
            direction="bull", weight=0.8,
        )
        bj = BiasJudgment(
            bias="bullish", confidence=0.75,
            evidences=[ev], summary="Flow supports bullish bias.",
        )
        assert bj.bias == "bullish"
        assert bj.confidence == 0.75
        assert len(bj.evidences) == 1


class TestEnrichedLevel:
    def test_construction(self) -> None:
        el = EnrichedLevel(
            price=220000, side="support", strength=0.9,
            sources=["vap", "round_number"], confluence_count=2,
        )
        assert el.price == 220000
        assert el.confluence_count == 2
        assert len(el.sources) == 2


class TestNarrativeReport:
    def test_construction(self) -> None:
        nr = NarrativeReport(
            storyline=["Opening was bullish.", "Midday saw profit-taking."],
            turning_points=[("10:15", "Large sell cluster")],
            conclusion="Net bullish with caution near resistance.",
        )
        assert len(nr.storyline) == 2
        assert nr.turning_points[0][0] == "10:15"


class TestReasoningReport:
    def test_construction(self) -> None:
        ev = Evidence(source="flow", fact_value="1.5", direction="bull", weight=0.8)
        bias = BiasJudgment(
            bias="bullish", confidence=0.75,
            evidences=[ev], summary="Bullish.",
        )
        level = EnrichedLevel(
            price=220000, side="support", strength=0.9,
            sources=["vap"], confluence_count=1,
        )
        scenario = _make_scenario_simple()
        narrative = NarrativeReport(
            storyline=["Bullish opening."],
            turning_points=[], conclusion="Bullish.",
        )
        rr = ReasoningReport(
            bias=bias, levels=[level],
            scenarios=[scenario], narrative=narrative,
        )
        assert rr.bias.bias == "bullish"
        assert len(rr.levels) == 1
        assert len(rr.scenarios) == 1


# ---------------------------------------------------------------------------
# Layer 3: Composition — Three-Layer Architecture
# ---------------------------------------------------------------------------


class TestMessagePart:
    def test_defaults(self) -> None:
        mp = MessagePart(kind="text", content="Hello")
        assert mp.image is None
        assert mp.caption == ""
        assert mp.min_tier == "free"

    def test_with_image(self) -> None:
        mp = MessagePart(
            kind="image", content="", image=b"\x89PNG",
            caption="Chart", min_tier="premium",
        )
        assert mp.image == b"\x89PNG"
        assert mp.min_tier == "premium"

    def test_slots(self) -> None:
        mp = MessagePart(kind="text", content="Hello")
        with pytest.raises((AttributeError, TypeError)):
            mp.extra = 1  # type: ignore[attr-defined]


class TestComposedReport:
    def test_construction(self) -> None:
        parts = [
            MessagePart(kind="text", content="Summary"),
            MessagePart(kind="image", content="", image=b"\x89PNG", caption="Chart"),
        ]
        cr = ComposedReport(messages=parts)
        assert len(cr.messages) == 2
        assert cr.messages[0].kind == "text"
        assert cr.messages[1].image is not None

    def test_empty(self) -> None:
        cr = ComposedReport(messages=[])
        assert len(cr.messages) == 0


# ---------------------------------------------------------------------------
# __all__ coverage for new models
# ---------------------------------------------------------------------------


class TestThreeLayerAllExports:
    def test_new_models_in_all(self) -> None:
        from hft_platform.reports import models
        expected = {
            "SegmentFact", "ChipCluster", "ChipFacts", "FlowFacts",
            "StructureFacts", "VolatilityFacts", "DaySnapshot",
            "CrossDayFacts", "FactReport", "Evidence", "BiasJudgment",
            "EnrichedLevel", "NarrativeReport", "ReasoningReport",
            "MessagePart", "ComposedReport",
        }
        assert expected.issubset(set(models.__all__))
