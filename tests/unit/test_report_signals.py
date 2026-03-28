"""Unit tests for SignalEngine (reports/signals.py).

Tests cover:
- Bearish / bullish / neutral session bias
- Direction assignment for "unknown" large trades
- rule_scores dict contains required keys
- Empty session handling
"""

from __future__ import annotations

import pytest

from hft_platform.reports.models import FlowBar, LargeTrade, SessionData, SignalReport
from hft_platform.reports.signals import SignalEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fb(ud: float = 0.8, net: int = -20, vol: int = 200) -> FlowBar:
    up = int(vol * ud / (1 + ud)) if ud > 0 else 0
    dn = vol - up
    return FlowBar(
        ts="t",
        ticks=50,
        total_vol=vol,
        uptick_vol=up,
        downtick_vol=dn,
        flat_vol=0,
        ud_ratio=ud,
        net_flow=net,
    )


def _make_sd(
    flow: list[FlowBar] | None = None,
    trades: list[LargeTrade] | None = None,
) -> SessionData:
    return SessionData(
        session="day",
        symbol="TXFD6",
        date="2026-03-27",
        open=330_490_000,
        high=330_490_000,
        low=323_750_000,
        close=324_380_000,
        volume=58_107,
        tick_count=38_153,
        bars_5m=[],
        flow_5m=flow or [],
        large_trades=trades or [],
        spread_dist={},
        depth_imbalance=[],
    )


# ---------------------------------------------------------------------------
# Bearish / bullish / neutral bias
# ---------------------------------------------------------------------------


class TestBias:
    def test_bearish_session(self) -> None:
        """All bars with ud_ratio=0.6 → strongly bearish → bias='bearish', confidence>0.3.

        ud=0.6 triggers both IF-01 (session U/D score -1.0) and IF-02 (sustained
        pressure: 10 consecutive bars with ud < 0.7 → score -1.0), giving a
        weighted sum of -(0.25 + 0.15) = -0.40 < -0.3 threshold.
        """
        bars = [_fb(ud=0.6, net=-30) for _ in range(10)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.bias == "bearish"
        assert report.bias_confidence > 0.3

    def test_bullish_session(self) -> None:
        """All bars with ud_ratio=1.4 → strongly bullish → bias='bullish'."""
        bars = [_fb(ud=1.4, net=30) for _ in range(10)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.bias == "bullish"

    def test_neutral_session(self) -> None:
        """All bars with ud_ratio=1.0 → perfectly balanced → bias='neutral'."""
        bars = [_fb(ud=1.0, net=0) for _ in range(10)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.bias == "neutral"


# ---------------------------------------------------------------------------
# Direction assignment
# ---------------------------------------------------------------------------


class TestDirectionAssignment:
    def test_trade_at_session_low_becomes_sell(self) -> None:
        """Trade priced at session low (≤ midpoint) should be classified as 'sell'."""
        sd = _make_sd(trades=[LargeTrade(ts="t", price=323_750_000, volume=50, direction="unknown")])
        engine = SignalEngine()
        report = engine.analyze(sd)

        # The key_large_trades list is populated from large_trades input;
        # direction should have been resolved
        classified = [t for t in sd.large_trades]  # original unchanged
        assert classified[0].direction == "unknown"  # immutability check

        # Check the assigned trades in the returned report indirectly:
        # If session low ≤ midpoint, it should score as sell in IF-03
        # large_sell_volume should be > 0
        assert report.large_sell_volume > 0

    def test_trade_at_session_high_becomes_buy(self) -> None:
        """Trade priced at session high (> midpoint) should be classified as 'buy'."""
        sd = _make_sd(trades=[LargeTrade(ts="t", price=330_490_000, volume=50, direction="unknown")])
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.large_buy_volume > 0

    def test_original_trades_not_mutated(self) -> None:
        """_assign_directions must not mutate the original trade list."""
        trade = LargeTrade(ts="t", price=323_750_000, volume=50, direction="unknown")
        sd = _make_sd(trades=[trade])
        engine = SignalEngine()
        engine.analyze(sd)

        # Original object must not be modified
        assert trade.direction == "unknown"


# ---------------------------------------------------------------------------
# rule_scores
# ---------------------------------------------------------------------------


class TestRuleScores:
    def test_rule_scores_has_if01_key(self) -> None:
        bars = [_fb() for _ in range(5)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert "IF-01_session_ud" in report.rule_scores

    def test_all_weight_keys_present(self) -> None:
        bars = [_fb() for _ in range(5)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        expected_keys = {
            "IF-01_session_ud",
            "IF-02_sustained",
            "IF-03_large_net",
            "IF-04_cluster",
            "IF-05_eod_drift",
            "IF-06_vol_spike",
            "SR-02_double_pattern",
            "SR-06_failed_breakout",
        }
        assert expected_keys == set(report.rule_scores.keys())


# ---------------------------------------------------------------------------
# Empty session
# ---------------------------------------------------------------------------


class TestEmptySession:
    def test_empty_session_is_neutral(self) -> None:
        """Session with no bars and no trades → neutral bias, net_flow=0."""
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.bias == "neutral"
        assert report.total_net_flow == 0

    def test_empty_session_returns_signal_report(self) -> None:
        """analyze() must return a SignalReport instance even for empty input."""
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert isinstance(report, SignalReport)


# ---------------------------------------------------------------------------
# Flow aggregation
# ---------------------------------------------------------------------------


class TestFlowAggregation:
    def test_total_net_flow_sums_bars(self) -> None:
        bars = [_fb(net=-20) for _ in range(5)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.total_net_flow == -100

    def test_strongest_sell_is_min_ud(self) -> None:
        bars = [_fb(ud=0.5, net=-50), _fb(ud=1.2, net=20), _fb(ud=0.8, net=-10)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        # Strongest sell = bar with lowest ud_ratio
        assert report.strongest_sell.ud_ratio == pytest.approx(0.5)

    def test_strongest_buy_is_max_ud(self) -> None:
        bars = [_fb(ud=0.5, net=-50), _fb(ud=1.4, net=30), _fb(ud=0.8, net=-10)]
        sd = _make_sd(flow=bars)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.strongest_buy.ud_ratio == pytest.approx(1.4)


# ---------------------------------------------------------------------------
# S/R levels
# ---------------------------------------------------------------------------


class TestSRLevels:
    def test_supports_at_or_below_close(self) -> None:
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        for level in report.supports:
            assert level.price <= sd.close, f"Support at {level.price} > close {sd.close}"

    def test_resistances_above_close(self) -> None:
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        for level in report.resistances:
            assert level.price > sd.close, f"Resistance at {level.price} <= close {sd.close}"

    def test_at_most_three_supports(self) -> None:
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert len(report.supports) <= 3

    def test_at_most_three_resistances(self) -> None:
        sd = _make_sd()
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert len(report.resistances) <= 3


# ---------------------------------------------------------------------------
# Large trade aggregation
# ---------------------------------------------------------------------------


class TestLargeTradeAggregation:
    def test_large_buy_sell_volumes(self) -> None:
        trades = [
            LargeTrade(ts="t", price=327_000_000, volume=30, direction="buy"),
            LargeTrade(ts="t", price=326_000_000, volume=20, direction="sell"),
        ]
        sd = _make_sd(trades=trades)
        engine = SignalEngine()
        report = engine.analyze(sd)

        assert report.large_buy_volume == 30
        assert report.large_sell_volume == 20
        assert report.large_net == 10
