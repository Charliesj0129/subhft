"""Tests for informed flow rules IF-01 through IF-06."""
from __future__ import annotations

import pytest

from hft_platform.reports.models import FlowBar, LargeTrade
from hft_platform.reports.rules.informed_flow import (
    find_large_trade_clusters,
    score_end_of_session_drift,
    score_large_trade_net,
    score_session_ud,
    score_sustained_pressure,
    score_volume_spike,
)

# ---------------------------------------------------------------------------
# Test helper
# ---------------------------------------------------------------------------

def _fb(ud: float, net: int = 0, vol: int = 100, ticks: int = 50) -> FlowBar:
    up = int(vol * ud / (1 + ud)) if ud > 0 else 0
    dn = vol - up
    return FlowBar(
        ts="t",
        ticks=ticks,
        total_vol=vol,
        uptick_vol=up,
        downtick_vol=dn,
        flat_vol=0,
        ud_ratio=ud,
        net_flow=net,
    )


def _lt(price: int, volume: int, direction: str) -> LargeTrade:
    return LargeTrade(ts="t", price=price, volume=volume, direction=direction)


# ---------------------------------------------------------------------------
# IF-01  score_session_ud
# ---------------------------------------------------------------------------

class TestScoreSessionUD:
    def test_empty_bars_returns_zero(self) -> None:
        assert score_session_ud([]) == 0.0

    def test_neutral_ratio_returns_zero(self) -> None:
        # equal uptick / downtick → ratio 1.0 → score 0.0
        bars = [_fb(1.0)] * 4
        result = score_session_ud(bars)
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_bullish_ratio_positive_score(self) -> None:
        # ratio > 1.1 → positive score, clamped +1.0
        bars = [_fb(2.0)] * 4  # ud_ratio=2, ratio sum(up)/sum(dn) > 1.1
        result = score_session_ud(bars)
        assert result > 0.0
        assert result <= 1.0

    def test_bearish_ratio_negative_score(self) -> None:
        # ratio < 0.9 → negative score
        bars = [_fb(0.5)] * 4  # ud_ratio=0.5, ratio < 0.9
        result = score_session_ud(bars)
        assert result < 0.0
        assert result >= -1.0

    def test_very_bearish_clamped_to_minus_one(self) -> None:
        # ratio = 0.0 (no uptick) → should clamp to -1.0
        bars = [FlowBar(ts="t", ticks=10, total_vol=100, uptick_vol=0,
                        downtick_vol=100, flat_vol=0, ud_ratio=0.0, net_flow=-100)]
        result = score_session_ud(bars)
        assert result == pytest.approx(-1.0)

    def test_very_bullish_clamped_to_plus_one(self) -> None:
        # ratio >> 1.1 → clamp to +1.0
        bars = [FlowBar(ts="t", ticks=10, total_vol=100, uptick_vol=100,
                        downtick_vol=0, flat_vol=0, ud_ratio=100.0, net_flow=100)]
        result = score_session_ud(bars)
        assert result == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# IF-02  score_sustained_pressure
# ---------------------------------------------------------------------------

class TestScoreSustainedPressure:
    def test_no_sustained_run_returns_zero(self) -> None:
        # alternating, never 4 consecutive
        bars = [_fb(1.0), _fb(0.5), _fb(1.5), _fb(0.8)]
        result = score_sustained_pressure(bars)
        assert result == 0.0

    def test_four_consecutive_bearish(self) -> None:
        bars = [_fb(0.5)] * 4
        result = score_sustained_pressure(bars)
        assert result == pytest.approx(-1.0)

    def test_four_consecutive_bullish(self) -> None:
        bars = [_fb(1.5)] * 4
        result = score_sustained_pressure(bars)
        assert result == pytest.approx(1.0)

    def test_three_consecutive_bearish_no_score(self) -> None:
        bars = [_fb(0.5)] * 3 + [_fb(1.2)]
        result = score_sustained_pressure(bars)
        assert result == 0.0

    def test_six_consecutive_bullish_still_capped(self) -> None:
        bars = [_fb(2.0)] * 6
        result = score_sustained_pressure(bars)
        assert result == pytest.approx(1.0)

    def test_empty_returns_zero(self) -> None:
        assert score_sustained_pressure([]) == 0.0


# ---------------------------------------------------------------------------
# IF-03  score_large_trade_net
# ---------------------------------------------------------------------------

class TestScoreLargeTradeNet:
    def test_empty_returns_zero(self) -> None:
        assert score_large_trade_net([]) == 0.0

    def test_unknown_only_returns_zero(self) -> None:
        trades = [_lt(1_000_000, 100, "unknown")] * 5
        assert score_large_trade_net(trades) == 0.0

    def test_all_buy_returns_positive_one(self) -> None:
        trades = [_lt(1_000_000, 100, "buy")] * 5
        result = score_large_trade_net(trades)
        assert result == pytest.approx(1.0)

    def test_all_sell_returns_negative_one(self) -> None:
        trades = [_lt(1_000_000, 100, "sell")] * 5
        result = score_large_trade_net(trades)
        assert result == pytest.approx(-1.0)

    def test_net_sell_returns_negative(self) -> None:
        trades = [_lt(1_000_000, 300, "sell"), _lt(1_000_000, 100, "buy")]
        result = score_large_trade_net(trades)
        assert result < 0.0

    def test_net_buy_returns_positive(self) -> None:
        trades = [_lt(1_000_000, 300, "buy"), _lt(1_000_000, 100, "sell")]
        result = score_large_trade_net(trades)
        assert result > 0.0

    def test_equal_buy_sell_returns_zero(self) -> None:
        trades = [_lt(1_000_000, 100, "buy"), _lt(1_000_000, 100, "sell")]
        result = score_large_trade_net(trades)
        assert result == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# IF-04  find_large_trade_clusters
# ---------------------------------------------------------------------------

class TestFindLargeTradeClusters:
    def test_no_cluster_returns_empty(self) -> None:
        trades = [_lt(1_000_000, 100, "buy"), _lt(2_000_000, 100, "sell")]
        result = find_large_trade_clusters(trades)
        assert result == []

    def test_three_close_trades_form_cluster(self) -> None:
        trades = [
            _lt(1_000_000, 100, "buy"),
            _lt(1_010_000, 200, "buy"),
            _lt(1_020_000, 150, "buy"),
        ]
        result = find_large_trade_clusters(trades, price_tolerance=30_000)
        assert len(result) == 1
        price, total_vol = result[0]
        assert total_vol == 450

    def test_two_trades_not_enough_for_cluster(self) -> None:
        trades = [_lt(1_000_000, 100, "buy"), _lt(1_005_000, 200, "sell")]
        result = find_large_trade_clusters(trades, price_tolerance=30_000)
        assert result == []

    def test_cluster_price_is_within_tolerance(self) -> None:
        trades = [
            _lt(1_000_000, 50, "buy"),
            _lt(1_029_000, 60, "buy"),
            _lt(1_015_000, 70, "sell"),
        ]
        result = find_large_trade_clusters(trades, price_tolerance=30_000)
        assert len(result) == 1
        _, total_vol = result[0]
        assert total_vol == 180


# ---------------------------------------------------------------------------
# IF-05  score_end_of_session_drift
# ---------------------------------------------------------------------------

class TestScoreEndOfSessionDrift:
    def test_less_than_8_bars_returns_zero(self) -> None:
        bars = [_fb(1.0)] * 7
        result = score_end_of_session_drift(bars)
        assert result == 0.0

    def test_no_drift_returns_zero(self) -> None:
        # uniform session: last 6 same as full
        bars = [_fb(1.0)] * 10
        result = score_end_of_session_drift(bars)
        assert result == pytest.approx(0.0, abs=1e-6)

    def test_eod_bearish_drift_returns_negative(self) -> None:
        # first 6 bars bullish, last 6 bearish → drift negative
        early = [_fb(2.0)] * 6
        late = [_fb(0.3)] * 6
        bars = early + late
        result = score_end_of_session_drift(bars)
        assert result < 0.0

    def test_small_drift_returns_zero(self) -> None:
        # early and late both near 1.0 → abs(drift) < 0.2 → 0.0
        bars = [_fb(1.05)] * 6 + [_fb(0.98)] * 6
        result = score_end_of_session_drift(bars)
        assert result == 0.0

    def test_eod_bullish_drift_returns_positive(self) -> None:
        early = [_fb(0.3)] * 6
        late = [_fb(2.0)] * 6
        bars = early + late
        result = score_end_of_session_drift(bars)
        assert result > 0.0


# ---------------------------------------------------------------------------
# IF-06  score_volume_spike
# ---------------------------------------------------------------------------

class TestScoreVolumeSpike:
    def test_no_spike_returns_zero_empty_list(self) -> None:
        bars = [_fb(1.0, vol=100)] * 6
        score, spikes = score_volume_spike(bars)
        assert score == 0.0
        assert spikes == []

    def test_bearish_spike_returns_negative_score(self) -> None:
        normal = [_fb(1.0, net=0, vol=100)] * 5
        spike = FlowBar(
            ts="t", ticks=50, total_vol=300, uptick_vol=50,
            downtick_vol=250, flat_vol=0, ud_ratio=0.2, net_flow=-200,
        )
        bars = normal + [spike]
        score, spikes = score_volume_spike(bars)
        assert score < 0.0
        assert spike in spikes

    def test_bullish_spike_returns_positive_score(self) -> None:
        normal = [_fb(1.0, net=0, vol=100)] * 5
        spike = FlowBar(
            ts="t", ticks=50, total_vol=300, uptick_vol=250,
            downtick_vol=50, flat_vol=0, ud_ratio=5.0, net_flow=200,
        )
        bars = normal + [spike]
        score, spikes = score_volume_spike(bars)
        assert score > 0.0
        assert spike in spikes

    def test_empty_bars_returns_zero(self) -> None:
        score, spikes = score_volume_spike([])
        assert score == 0.0
        assert spikes == []
