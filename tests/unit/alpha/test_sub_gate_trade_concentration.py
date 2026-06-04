"""Tests for TradeConcentrationGate (Round 22 / goal §5)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.trade_concentration import TradeConcentrationGate


@dataclass
class _FakeResult:
    trade_pnl: list[Any] = field(default_factory=list)
    daily_pnl: list[Any] = field(default_factory=list)


def _gate() -> TradeConcentrationGate:
    return TradeConcentrationGate()


class TestTradeConcentrationGate:
    def test_skip_when_no_trade_data(self) -> None:
        out = _gate().evaluate(_FakeResult(), config=None, thresholds={})
        assert out.passed is True
        assert out.metrics["n_trades"] == 0.0

    def test_passes_when_distribution_is_diffuse(self) -> None:
        # 10 trades summing to +100; biggest win = 20 (20%); no losses.
        trades = [10.0, 15.0, 12.0, 8.0, 11.0, 20.0, 9.0, 5.0, 6.0, 4.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={
                "top_trade_share_max_pct": 40.0,
                "worst_loss_share_max_pct": 50.0,
            },
        )
        assert out.passed is True
        assert out.metrics["top_trade_share_pct"] == 20.0  # 20/100

    def test_fails_when_single_trade_dominates_pnl(self) -> None:
        # 5 trades; one win=80 dominates total=100 (80%).
        trades = [80.0, 8.0, 5.0, 4.0, 3.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={
                "top_trade_share_max_pct": 40.0,
                "worst_loss_share_max_pct": 50.0,
            },
        )
        assert out.passed is False
        assert out.metrics["top_trade_share_pct"] == 80.0

    def test_fails_when_single_loss_dominates(self) -> None:
        # 10 small wins + 1 catastrophic loss.  Total=10*5 - 30 = 20.
        # worst loss = 30, share = 30/20 = 150% — well over 50%.
        trades = [5.0] * 10 + [-30.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={
                "top_trade_share_max_pct": 40.0,
                "worst_loss_share_max_pct": 50.0,
            },
        )
        assert out.passed is False
        assert out.metrics["worst_loss_share_pct"] > 50.0

    def test_zero_total_with_winning_trade_reads_100pct_top(self) -> None:
        # Wins and losses cancel out; biggest_win > 0 so top share clamps
        # to 100% (the strategy is entirely held together by that trade).
        trades = [10.0, -10.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert out.metrics["top_trade_share_pct"] == 100.0
        assert out.passed is False

    def test_default_thresholds_used_when_absent(self) -> None:
        trades = [50.0, 30.0, 20.0]  # top = 50/100 = 50%, > default 40%
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert out.passed is False  # default 40% top trips

    def test_falls_back_to_daily_pnl_when_trade_pnl_absent(self) -> None:
        # No trade_pnl; gate should read daily_pnl (canonical dict shape).
        daily = [{"pnl_pts": v} for v in [10.0, 12.0, 8.0, 5.0]]
        out = _gate().evaluate(
            _FakeResult(daily_pnl=daily),
            config=None,
            thresholds={"top_trade_share_max_pct": 40.0},
        )
        assert out.metrics["n_trades"] == 4.0
        # max=12, total=35, share≈34.3% < 40% -> pass
        assert out.passed is True

    def test_details_string_includes_both_metrics(self) -> None:
        trades = [10.0, 15.0, -5.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert "top" in out.details
        assert "worst-loss" in out.details

    def test_only_top_trip_fails_not_worst(self) -> None:
        # Single big win, no losses — top trips, worst is 0%.
        trades = [80.0, 5.0, 5.0, 5.0, 5.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={
                "top_trade_share_max_pct": 40.0,
                "worst_loss_share_max_pct": 50.0,
            },
        )
        assert out.passed is False
        assert out.metrics["worst_loss_share_pct"] == 0.0
        assert out.metrics["top_trade_share_pct"] == 80.0

    def test_applies_to_both_strategy_types(self) -> None:
        gate = _gate()
        assert "maker" in gate.applies_to
        assert "taker" in gate.applies_to


class TestLossDistributionMetrics:
    """§5 虧損分布: distribution ACROSS losers (advisory, additive)."""

    def test_loss_distribution_metrics_for_fat_tailed_losses(self) -> None:
        # 3 wins + 4 losers; one loser (-30) dwarfs the rest (-2,-3,-4).
        trades = [20.0, 15.0, 10.0, -2.0, -3.0, -4.0, -30.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        m = out.metrics
        assert m["loss_count"] == 4.0
        assert round(m["loss_rate_pct"], 1) == 57.1  # 4 / 7
        # gross loss 39; worst 3 = 30+4+3 = 37 -> 94.9%
        assert round(m["loss_top3_share_pct"], 1) == 94.9
        # mean |loss| 9.75 / median 3.5 -> ~2.79 (fat tail)
        assert m["loss_fat_tail_ratio"] > 2.0

    def test_no_losers_reports_neutral_loss_distribution(self) -> None:
        out = _gate().evaluate(
            _FakeResult(trade_pnl=[10.0, 5.0, 3.0]), config=None, thresholds={}
        )
        m = out.metrics
        assert m["loss_count"] == 0.0
        assert m["loss_rate_pct"] == 0.0
        assert m["loss_top3_share_pct"] == 0.0
        assert m["loss_fat_tail_ratio"] == 1.0

    def test_loss_metrics_are_advisory_and_do_not_change_pass_fail(self) -> None:
        # Diffuse wins, small evenly-spread losers -> gate passes, and the
        # loss-distribution metrics are present without affecting the verdict.
        trades = [10.0, 11.0, 12.0, 9.0, 8.0, -2.0, -3.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={"top_trade_share_max_pct": 40.0, "worst_loss_share_max_pct": 50.0},
        )
        assert out.passed is True
        assert out.metrics["loss_count"] == 2.0
        assert out.metrics["loss_fat_tail_ratio"] == 1.0  # 2.5 mean / 2.5 median
