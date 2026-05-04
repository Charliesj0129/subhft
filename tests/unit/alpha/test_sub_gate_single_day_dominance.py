"""Tests for SingleDayDominanceGate."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.single_day_dominance import SingleDayDominanceGate


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


class TestSingleDayDominanceGate:
    def test_passes_when_distribution_is_balanced(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[10.0] * 10)
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is True

    def test_fails_when_one_day_dominates(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[100.0] + [1.0] * 30)
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is False
        assert "top_day_contribution_pct" in out.metrics
        assert out.metrics["top_day_contribution_pct"] > 25.0

    def test_uses_signed_contribution_in_aggregate(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[100.0, -1.0, -1.0, -1.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 50.0})
        assert out.passed is False  # 100 / (100+1+1+1)*100 ~= 97%

    def test_handles_negative_total_pnl(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[0.0, 0.0, 0.0])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 25.0})
        assert out.passed is True
        assert "no measurable PnL" in out.details

    def test_dict_entries_are_supported(self) -> None:
        gate = SingleDayDominanceGate()
        r = _FakeResult(daily_pnl=[{"pnl_pts": 100.0}, {"pnl_pts": 1.0}])
        out = gate.evaluate(r, config=None, thresholds={"outlier_day_contribution_max_pct": 50.0})
        assert out.passed is False  # 100/101 ~= 99%
