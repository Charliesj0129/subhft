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
        assert out.metrics["top_trade_share_pct"] == 20.0

    def test_fails_when_single_trade_dominates_pnl(self) -> None:
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
        trades = [10.0, -10.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert out.metrics["top_trade_share_pct"] == 100.0
        assert out.passed is False

    def test_default_thresholds_used_when_absent(self) -> None:
        trades = [50.0, 30.0, 20.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert out.passed is False

    def test_falls_back_to_daily_pnl_when_trade_pnl_absent(self) -> None:
        daily = [{"pnl_pts": v} for v in [10.0, 12.0, 8.0, 5.0]]
        out = _gate().evaluate(
            _FakeResult(daily_pnl=daily),
            config=None,
            thresholds={"top_trade_share_max_pct": 40.0},
        )
        assert out.metrics["n_trades"] == 4.0
        assert out.passed is True

    def test_details_string_includes_both_metrics(self) -> None:
        trades = [10.0, 15.0, -5.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        assert "top" in out.details
        assert "worst-loss" in out.details

    def test_only_top_trip_fails_not_worst(self) -> None:
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
        trades = [20.0, 15.0, 10.0, -2.0, -3.0, -4.0, -30.0]
        out = _gate().evaluate(_FakeResult(trade_pnl=trades), config=None, thresholds={})
        m = out.metrics
        assert m["loss_count"] == 4.0
        assert round(m["loss_rate_pct"], 1) == 57.1
        assert round(m["loss_top3_share_pct"], 1) == 94.9
        assert m["loss_fat_tail_ratio"] > 2.0

    def test_no_losers_reports_neutral_loss_distribution(self) -> None:
        out = _gate().evaluate(_FakeResult(trade_pnl=[10.0, 5.0, 3.0]), config=None, thresholds={})
        m = out.metrics
        assert m["loss_count"] == 0.0
        assert m["loss_rate_pct"] == 0.0
        assert m["loss_top3_share_pct"] == 0.0
        assert m["loss_fat_tail_ratio"] == 1.0

    def test_loss_metrics_are_advisory_and_do_not_change_pass_fail(self) -> None:
        trades = [10.0, 11.0, 12.0, 9.0, 8.0, -2.0, -3.0]
        out = _gate().evaluate(
            _FakeResult(trade_pnl=trades),
            config=None,
            thresholds={"top_trade_share_max_pct": 40.0, "worst_loss_share_max_pct": 50.0},
        )
        assert out.passed is True
        assert out.metrics["loss_count"] == 2.0
        assert out.metrics["loss_fat_tail_ratio"] == 1.0
