"""Tests for Layer 1 FactExtractor (reports.facts)."""

from __future__ import annotations

import math

import pytest

from hft_platform.reports.facts import (
    extract_all,
    extract_chip_facts,
    extract_cross_day_facts,
    extract_flow_facts,
    extract_structure_facts,
    extract_time_segments,
    extract_volatility_facts,
)
from hft_platform.reports.models import (
    Bar5m,
    ChipFacts,
    CrossDayFacts,
    DaySnapshot,
    FactReport,
    FlowBar,
    FlowFacts,
    LargeTrade,
    SessionData,
    StructureFacts,
    VolatilityFacts,
)

# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

SCALE = 10_000


def _bar5m(
    ts: str,
    o: int = 200,
    h: int = 205,
    l: int = 198,
    c: int = 202,
    vol: int = 100,
    ticks: int = 50,
) -> Bar5m:
    return Bar5m(
        ts=ts,
        open=o * SCALE,
        high=h * SCALE,
        low=l * SCALE,
        close=c * SCALE,
        volume=vol,
        ticks=ticks,
    )


def _flow(
    ts: str,
    total: int = 100,
    up: int = 60,
    down: int = 40,
    flat: int = 0,
    ticks: int = 10,
) -> FlowBar:
    ud = up / down if down > 0 else float("inf") if up > 0 else 1.0
    return FlowBar(
        ts=ts,
        ticks=ticks,
        total_vol=total,
        uptick_vol=up,
        downtick_vol=down,
        flat_vol=flat,
        ud_ratio=ud,
        net_flow=up - down,
    )


def _trade(ts: str, price: int, vol: int, direction: str) -> LargeTrade:
    return LargeTrade(ts=ts, price=price * SCALE, volume=vol, direction=direction)


def _session_data(
    bars: list[Bar5m] | None = None,
    flow: list[FlowBar] | None = None,
    trades: list[LargeTrade] | None = None,
    session: str = "day",
    high: int = 205,
    low: int = 195,
    close: int = 200,
) -> SessionData:
    return SessionData(
        session=session,
        symbol="TXFD6",
        date="2026-03-29",
        open=200 * SCALE,
        high=high * SCALE,
        low=low * SCALE,
        close=close * SCALE,
        volume=5000,
        tick_count=1000,
        bars_5m=bars or [],
        flow_5m=flow or [],
        large_trades=trades or [],
        spread_dist={},
        depth_imbalance=[],
    )


# ---------------------------------------------------------------------------
# 1. extract_time_segments
# ---------------------------------------------------------------------------


class TestExtractTimeSegments:
    """Tests for time segment classification."""

    def test_day_session_has_four_segments(self) -> None:
        bars = [
            _flow("2026-03-29 07:30:00", total=10, up=6, down=4),
            _flow("2026-03-29 09:00:00", total=20, up=15, down=5),
            _flow("2026-03-29 10:00:00", total=30, up=10, down=20),
            _flow("2026-03-29 13:00:00", total=40, up=25, down=15),
        ]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        assert len(segments) == 4
        names = [s.name for s in segments]
        assert names == ["pre_open", "opening", "midday", "closing"]

    def test_volume_pcts_sum_to_one(self) -> None:
        bars = [
            _flow("2026-03-29 08:00:00", total=100),
            _flow("2026-03-29 09:00:00", total=200),
            _flow("2026-03-29 10:30:00", total=300),
            _flow("2026-03-29 13:30:00", total=400),
        ]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        total_pct = sum(s.volume_pct for s in segments)
        assert abs(total_pct - 1.0) < 1e-9

    def test_dominant_side_bull(self) -> None:
        bars = [_flow("2026-03-29 10:00:00", total=100, up=80, down=20)]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        midday = [s for s in segments if s.name == "midday"][0]
        assert midday.dominant_side == "bull"
        assert midday.ud_ratio == pytest.approx(4.0)

    def test_dominant_side_bear(self) -> None:
        bars = [_flow("2026-03-29 10:00:00", total=100, up=20, down=80)]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        midday = [s for s in segments if s.name == "midday"][0]
        assert midday.dominant_side == "bear"

    def test_dominant_side_neutral(self) -> None:
        bars = [_flow("2026-03-29 10:00:00", total=100, up=50, down=50)]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        midday = [s for s in segments if s.name == "midday"][0]
        assert midday.dominant_side == "neutral"
        assert midday.ud_ratio == pytest.approx(1.0)

    def test_empty_segments_have_zero_values(self) -> None:
        """Segments with no bars still appear with zeroed fields."""
        bars = [_flow("2026-03-29 10:00:00", total=100, up=50, down=50)]
        sd = _session_data(flow=bars)
        segments = extract_time_segments(sd)

        pre_open = [s for s in segments if s.name == "pre_open"][0]
        assert pre_open.volume == 0
        assert pre_open.net_flow == 0
        assert pre_open.ud_ratio == 1.0
        assert pre_open.dominant_side == "neutral"

    def test_large_trade_counts(self) -> None:
        bars = [_flow("2026-03-29 10:00:00", total=100)]
        trades = [
            _trade("2026-03-29 10:05:00", 200, 50, "buy"),
            _trade("2026-03-29 10:15:00", 201, 30, "buy"),
            _trade("2026-03-29 10:20:00", 199, 40, "sell"),
        ]
        sd = _session_data(flow=bars, trades=trades)
        segments = extract_time_segments(sd)

        midday = [s for s in segments if s.name == "midday"][0]
        assert midday.large_buy_count == 2
        assert midday.large_sell_count == 1

    def test_night_session_segments(self) -> None:
        bars = [
            _flow("2026-03-29 15:10:00", total=50),
            _flow("2026-03-29 20:00:00", total=100),
            _flow("2026-03-29 03:30:00", total=30),
        ]
        sd = _session_data(flow=bars, session="night")
        segments = extract_time_segments(sd)

        names = [s.name for s in segments]
        assert names == ["opening", "midday", "closing"]

    def test_segment_high_low_from_bars_5m(self) -> None:
        bars5m = [
            _bar5m("2026-03-29 10:00:00", h=210, l=195),
            _bar5m("2026-03-29 10:05:00", h=215, l=200),
        ]
        flow = [_flow("2026-03-29 10:00:00"), _flow("2026-03-29 10:05:00")]
        sd = _session_data(bars=bars5m, flow=flow)
        segments = extract_time_segments(sd)

        midday = [s for s in segments if s.name == "midday"][0]
        assert midday.high == 215 * SCALE
        assert midday.low == 195 * SCALE


# ---------------------------------------------------------------------------
# 2. extract_chip_facts
# ---------------------------------------------------------------------------


class TestExtractChipFacts:
    """Tests for chip structure extraction."""

    def test_empty_trades(self) -> None:
        sd = _session_data(trades=[])
        chips = extract_chip_facts(sd)

        assert chips.clusters == []
        assert chips.total_buy_volume == 0
        assert chips.total_sell_volume == 0
        assert chips.net_ratio == 0.5

    def test_cluster_with_timestamps(self) -> None:
        trades = [
            _trade("2026-03-29 09:00:00", 200, 50, "buy"),
            _trade("2026-03-29 09:05:00", 200, 30, "buy"),
            _trade("2026-03-29 09:10:00", 201, 40, "sell"),
        ]
        sd = _session_data(trades=trades)
        chips = extract_chip_facts(sd)

        assert len(chips.clusters) >= 1
        cluster = chips.clusters[0]
        assert cluster.first_ts == "2026-03-29 09:00:00"
        assert cluster.last_ts == "2026-03-29 09:10:00"
        assert "09:00" in cluster.time_range
        assert cluster.trade_count == 3

    def test_buy_sell_aggregation(self) -> None:
        trades = [
            _trade("2026-03-29 09:00:00", 200, 100, "buy"),
            _trade("2026-03-29 09:05:00", 200, 30, "sell"),
            _trade("2026-03-29 09:10:00", 200, 20, "unknown"),
        ]
        sd = _session_data(trades=trades)
        chips = extract_chip_facts(sd)

        assert chips.total_buy_volume == 100
        assert chips.total_sell_volume == 30
        assert chips.net_ratio == pytest.approx(100.0 / 130.0)

    def test_buy_zone_from_clusters(self) -> None:
        # 3 buy trades at ~200, 3 sell trades at ~210
        trades = [
            _trade("2026-03-29 09:00:00", 200, 50, "buy"),
            _trade("2026-03-29 09:01:00", 200, 50, "buy"),
            _trade("2026-03-29 09:02:00", 201, 50, "buy"),
            _trade("2026-03-29 09:10:00", 210, 50, "sell"),
            _trade("2026-03-29 09:11:00", 210, 50, "sell"),
            _trade("2026-03-29 09:12:00", 211, 50, "sell"),
        ]
        sd = _session_data(trades=trades)
        chips = extract_chip_facts(sd)

        assert chips.buy_zone is not None
        assert chips.sell_zone is not None

    def test_vap_peaks_from_bars(self) -> None:
        bars = [
            _bar5m("2026-03-29 09:00:00", vol=500),
            _bar5m("2026-03-29 09:05:00", vol=100),
        ]
        sd = _session_data(bars=bars)
        chips = extract_chip_facts(sd)

        assert len(chips.vap_peaks) >= 1


# ---------------------------------------------------------------------------
# 3. extract_flow_facts
# ---------------------------------------------------------------------------


class TestExtractFlowFacts:
    """Tests for flow facts extraction."""

    def test_empty_bars(self) -> None:
        sd = _session_data(flow=[])
        ff = extract_flow_facts(sd)

        assert ff.session_ud == 1.0
        assert ff.session_net_flow == 0
        assert ff.eod_ud == 1.0
        assert ff.eod_drift == 0.0
        assert ff.sustained_runs == []
        assert ff.volume_spikes == []

    def test_session_ud_calculation(self) -> None:
        bars = [
            _flow("2026-03-29 09:00:00", up=100, down=50),
            _flow("2026-03-29 09:05:00", up=80, down=40),
        ]
        sd = _session_data(flow=bars)
        ff = extract_flow_facts(sd)

        assert ff.session_ud == pytest.approx(180.0 / 90.0)
        assert ff.session_net_flow == (100 - 50) + (80 - 40)

    def test_strongest_buy_sell_bars(self) -> None:
        bars = [
            _flow("2026-03-29 09:00:00", up=90, down=10),  # ud=9.0
            _flow("2026-03-29 09:05:00", up=50, down=50),  # ud=1.0
            _flow("2026-03-29 09:10:00", up=10, down=90),  # ud=0.111
        ]
        sd = _session_data(flow=bars)
        ff = extract_flow_facts(sd)

        assert ff.strongest_buy_bar.ts == "2026-03-29 09:00:00"
        assert ff.strongest_sell_bar.ts == "2026-03-29 09:10:00"

    def test_sustained_run_detection(self) -> None:
        # 5 consecutive bullish bars (ud_ratio > 1.3)
        bars = [
            _flow(f"2026-03-29 09:{i * 5:02d}:00", up=80, down=20)
            for i in range(6)
        ]
        sd = _session_data(flow=bars)
        ff = extract_flow_facts(sd)

        assert len(ff.sustained_runs) == 1
        side, count, time_range = ff.sustained_runs[0]
        assert side == "bull"
        assert count == 6

    def test_sustained_run_minimum_four(self) -> None:
        # Only 3 consecutive — should NOT produce a run
        bars = [
            _flow("2026-03-29 09:00:00", up=80, down=20),
            _flow("2026-03-29 09:05:00", up=80, down=20),
            _flow("2026-03-29 09:10:00", up=80, down=20),
            _flow("2026-03-29 09:15:00", up=50, down=50),  # neutral break
        ]
        sd = _session_data(flow=bars)
        ff = extract_flow_facts(sd)

        assert ff.sustained_runs == []

    def test_volume_spike_detection(self) -> None:
        # One spike bar amid normal bars
        bars = [
            _flow("2026-03-29 09:00:00", total=100),
            _flow("2026-03-29 09:05:00", total=100),
            _flow("2026-03-29 09:10:00", total=100),
            _flow("2026-03-29 09:15:00", total=500),  # 5x mean
        ]
        sd = _session_data(flow=bars)
        ff = extract_flow_facts(sd)

        assert len(ff.volume_spikes) == 1
        spike_bar, ratio = ff.volume_spikes[0]
        assert spike_bar.ts == "2026-03-29 09:15:00"
        assert ratio == pytest.approx(500.0 / 200.0)  # mean = (100+100+100+500)/4 = 200

    def test_eod_drift(self) -> None:
        # Session is neutral (ud≈1), but last 6 bars are bullish
        neutral_bars = [
            _flow(f"2026-03-29 09:{i * 5:02d}:00", up=50, down=50)
            for i in range(10)
        ]
        # Make last 6 bullish
        for bar in neutral_bars[-6:]:
            bar.uptick_vol = 80  # type: ignore[misc]
            bar.downtick_vol = 20  # type: ignore[misc]
            bar.ud_ratio = 4.0  # type: ignore[misc]

        sd = _session_data(flow=neutral_bars)
        ff = extract_flow_facts(sd)

        assert ff.eod_ud > ff.session_ud
        assert ff.eod_drift > 0


# ---------------------------------------------------------------------------
# 4. extract_structure_facts
# ---------------------------------------------------------------------------


class TestExtractStructureFacts:
    """Tests for structure facts extraction."""

    def test_empty_bars(self) -> None:
        sd = _session_data(bars=[], high=210, low=190)
        sf = extract_structure_facts(sd)

        assert isinstance(sf, StructureFacts)
        assert sf.session_high.price == 210 * SCALE
        assert sf.session_low.price == 190 * SCALE
        assert sf.double_bottoms == []
        assert sf.double_tops == []

    def test_session_extremes(self) -> None:
        bars = [
            _bar5m("2026-03-29 09:00:00", h=205, l=195),
            _bar5m("2026-03-29 09:05:00", h=210, l=200),
        ]
        sd = _session_data(bars=bars, high=210, low=195)
        sf = extract_structure_facts(sd)

        assert sf.session_high.price == 210 * SCALE
        assert sf.session_low.price == 195 * SCALE

    def test_round_numbers(self) -> None:
        sd = _session_data(high=22000, low=21000)
        sf = extract_structure_facts(sd)

        # Should have at least the 1000-pt multiples
        prices = {lv.price for lv in sf.round_numbers}
        assert 21000 * SCALE in prices
        assert 22000 * SCALE in prices

    def test_double_bottom_detection(self) -> None:
        # Two bars with similar lows separated by a higher-low bar
        bars = [
            _bar5m("2026-03-29 09:00:00", h=210, l=195, c=200),
            _bar5m("2026-03-29 09:05:00", h=215, l=205, c=210),  # higher low
            _bar5m("2026-03-29 09:10:00", h=210, l=195, c=200),
        ]
        sd = _session_data(bars=bars, high=215, low=195)
        sf = extract_structure_facts(sd)

        # The detector should find a double bottom near 195
        assert len(sf.double_bottoms) >= 1


# ---------------------------------------------------------------------------
# 5. extract_volatility_facts
# ---------------------------------------------------------------------------


class TestExtractVolatilityFacts:
    """Tests for volatility facts extraction."""

    def test_empty_bars(self) -> None:
        sd = _session_data(bars=[])
        vf = extract_volatility_facts(sd)

        assert vf.atr_5m == 0
        assert vf.atr_session == 0
        assert vf.session_range == 0
        assert vf.range_atr_ratio == 0.0

    def test_single_bar(self) -> None:
        bars = [_bar5m("2026-03-29 09:00:00", h=210, l=200)]
        sd = _session_data(bars=bars, high=210, low=200)
        vf = extract_volatility_facts(sd)

        assert vf.atr_5m == 10 * SCALE
        assert vf.session_range == 10 * SCALE

    def test_atr_from_multiple_bars(self) -> None:
        bars = [
            _bar5m("2026-03-29 09:00:00", o=200, h=205, l=195, c=202),
            _bar5m("2026-03-29 09:05:00", o=202, h=210, l=198, c=208),
            _bar5m("2026-03-29 09:10:00", o=208, h=215, l=205, c=212),
            _bar5m("2026-03-29 09:15:00", o=212, h=218, l=210, c=215),
        ]
        sd = _session_data(bars=bars, high=218, low=195)
        vf = extract_volatility_facts(sd)

        assert vf.atr_5m > 0
        assert vf.atr_session > vf.atr_5m  # session ATR scaled by sqrt(n)
        assert vf.session_range == (218 - 195) * SCALE
        assert vf.range_atr_ratio > 0

    def test_atr_session_scaling(self) -> None:
        """atr_session should be atr_5m * sqrt(len(bars))."""
        bars = [
            _bar5m("2026-03-29 09:00:00", o=100, h=110, l=90, c=105),
            _bar5m("2026-03-29 09:05:00", o=105, h=115, l=95, c=108),
            _bar5m("2026-03-29 09:10:00", o=108, h=118, l=98, c=110),
            _bar5m("2026-03-29 09:15:00", o=110, h=120, l=100, c=112),
        ]
        sd = _session_data(bars=bars, high=120, low=90)
        vf = extract_volatility_facts(sd)

        # Approximate check: atr_session ≈ atr_5m * sqrt(4)
        expected_session = vf.atr_5m * math.sqrt(len(bars))
        assert abs(vf.atr_session - int(round(expected_session))) <= 1


# ---------------------------------------------------------------------------
# 6. extract_cross_day_facts
# ---------------------------------------------------------------------------


def _day_snap(
    close: int = 200,
    high: int = 210,
    low: int = 190,
    volume: int = 5000,
    ud: float = 1.0,
) -> DaySnapshot:
    return DaySnapshot(
        date="2026-03-28",
        session="day",
        open=195 * SCALE,
        high=high * SCALE,
        low=low * SCALE,
        close=close * SCALE,
        volume=volume,
        ud_ratio=ud,
        net_flow=0,
    )


class TestExtractCrossDayFacts:
    """Tests for cross-day comparison extraction."""

    def test_empty_prev_days(self) -> None:
        sd = _session_data()
        cdf = extract_cross_day_facts(sd, [])

        assert cdf.volume_change_pct == 0.0
        assert cdf.price_position == "inside_range"
        assert cdf.trend_direction == "sideways"
        assert cdf.flow_reversal is False

    def test_volume_change(self) -> None:
        prev = _day_snap(volume=4000)
        sd = _session_data()  # volume=5000
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.volume_change_pct == pytest.approx(25.0)

    def test_price_above_prev_high(self) -> None:
        prev = _day_snap(high=199)  # prev high = 199
        sd = _session_data(close=200)  # close = 200 > 199
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.price_position == "above_prev_high"

    def test_price_below_prev_low(self) -> None:
        prev = _day_snap(low=201)  # prev low = 201
        sd = _session_data(close=200)  # close = 200 < 201
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.price_position == "below_prev_low"

    def test_price_inside_range(self) -> None:
        prev = _day_snap(high=210, low=190)
        sd = _session_data(close=200)
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.price_position == "inside_range"

    def test_trend_direction_up(self) -> None:
        prevs = [
            _day_snap(close=210),  # most recent
            _day_snap(close=205),
            _day_snap(close=200),  # oldest
        ]
        sd = _session_data()
        cdf = extract_cross_day_facts(sd, prevs)

        assert cdf.trend_direction == "up"

    def test_trend_direction_down(self) -> None:
        prevs = [
            _day_snap(close=190),  # most recent
            _day_snap(close=195),
            _day_snap(close=200),  # oldest
        ]
        sd = _session_data()
        cdf = extract_cross_day_facts(sd, prevs)

        assert cdf.trend_direction == "down"

    def test_trend_direction_sideways(self) -> None:
        prevs = [
            _day_snap(close=200),
            _day_snap(close=210),
            _day_snap(close=200),
        ]
        sd = _session_data()
        cdf = extract_cross_day_facts(sd, prevs)

        assert cdf.trend_direction == "sideways"

    def test_flow_reversal_bear_to_bull(self) -> None:
        prev = _day_snap(ud=0.8)  # previous was bearish
        # Today is bullish
        flow = [_flow("2026-03-29 09:00:00", up=80, down=20)]
        sd = _session_data(flow=flow)
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.flow_reversal is True

    def test_flow_reversal_bull_to_bear(self) -> None:
        prev = _day_snap(ud=1.2)  # previous was bullish
        # Today is bearish
        flow = [_flow("2026-03-29 09:00:00", up=20, down=80)]
        sd = _session_data(flow=flow)
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.flow_reversal is True

    def test_no_flow_reversal(self) -> None:
        prev = _day_snap(ud=1.2)  # bullish
        flow = [_flow("2026-03-29 09:00:00", up=80, down=20)]  # also bullish
        sd = _session_data(flow=flow)
        cdf = extract_cross_day_facts(sd, [prev])

        assert cdf.flow_reversal is False

    def test_single_prev_day_sideways(self) -> None:
        """With only 1 prev day, trend_direction should be 'sideways'."""
        sd = _session_data()
        cdf = extract_cross_day_facts(sd, [_day_snap()])

        assert cdf.trend_direction == "sideways"


# ---------------------------------------------------------------------------
# extract_all orchestrator
# ---------------------------------------------------------------------------


class TestExtractAll:
    """Tests for the extract_all orchestrator."""

    def test_returns_fact_report(self) -> None:
        bars = [
            _bar5m("2026-03-29 09:00:00", h=210, l=195),
            _bar5m("2026-03-29 09:05:00", h=215, l=200),
            _bar5m("2026-03-29 09:10:00", h=208, l=198),
        ]
        flow = [
            _flow("2026-03-29 09:00:00"),
            _flow("2026-03-29 09:05:00"),
            _flow("2026-03-29 09:10:00"),
        ]
        sd = _session_data(bars=bars, flow=flow, high=215, low=195)
        report = extract_all(sd)

        assert isinstance(report, FactReport)
        assert report.session_data is sd
        assert len(report.segments) == 4
        assert isinstance(report.chips, ChipFacts)
        assert isinstance(report.flow, FlowFacts)
        assert isinstance(report.structure, StructureFacts)
        assert isinstance(report.volatility, VolatilityFacts)
        assert isinstance(report.cross_day, CrossDayFacts)

    def test_with_prev_days(self) -> None:
        sd = _session_data()
        prevs = [_day_snap()]
        report = extract_all(sd, prev_days=prevs)

        assert report.cross_day.prev_days == prevs

    def test_empty_session(self) -> None:
        """extract_all should not raise on a completely empty session."""
        sd = _session_data(bars=[], flow=[], trades=[], high=0, low=0)
        report = extract_all(sd)

        assert isinstance(report, FactReport)
        assert report.flow.session_ud == 1.0
        assert report.volatility.atr_5m == 0
