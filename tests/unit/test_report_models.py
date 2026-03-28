"""Unit tests for hft_platform.reports.models data contracts."""

from __future__ import annotations

import pytest

from hft_platform.contracts.types import ScaledPrice
from hft_platform.reports.models import (
    Bar5m,
    ChannelConfig,
    DepthBar,
    FlowBar,
    KeyLevel,
    LargeTrade,
    PriceLevel,
    Scenario,
    ScenarioReport,
    SessionData,
    SignalReport,
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
        with pytest.raises(AttributeError):
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
