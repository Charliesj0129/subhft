"""Tests for MonthlyDistributionGate.

Encodes goal §6:
- ``max_drawdown_pts <= drawdown_to_avg_monthly_max_ratio * avg_monthly_net_pnl_pts``
- ``top_month_contribution_pct <= top_month_contribution_max_pct``
- Records ``median_monthly_net_pnl_pts`` and ``worst_monthly_pnl_pts`` for audit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from hft_platform.alpha._sub_gates.monthly_distribution import (
    MonthlyDistributionGate,
)


@dataclass
class _FakeResult:
    daily_pnl: list[Any] = field(default_factory=list)


def _entry(date: str, pnl: float) -> dict:
    return {"date": date, "pnl_pts": pnl}


_THRESHOLDS = {
    "top_month_contribution_max_pct": 50.0,
    "drawdown_to_avg_monthly_max_ratio": 2.0,
    "monthly_distribution_min_months": 2,
}


class TestMonthlyDistributionGate:
    def test_passes_when_months_are_balanced_and_drawdown_modest(self) -> None:
        gate = MonthlyDistributionGate()
        daily = (
            [_entry(f"2026-01-{d:02d}", 10.0) for d in range(1, 21)]
            + [_entry(f"2026-02-{d:02d}", 10.0) for d in range(1, 21)]
            + [_entry(f"2026-03-{d:02d}", 10.0) for d in range(1, 21)]
        )
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is True
        assert out.metrics["n_months"] == 3.0
        assert out.metrics["top_month_contribution_pct"] < 50.0
        assert out.metrics["avg_monthly_net_pnl_pts"] == 200.0

    def test_fails_when_single_month_dominates(self) -> None:
        gate = MonthlyDistributionGate()
        daily = [_entry("2026-01-15", 1000.0)] + [_entry(f"2026-0{m}-15", 5.0) for m in range(2, 7)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["top_month_contribution_pct"] > 50.0

    def test_fails_when_drawdown_exceeds_two_times_avg_monthly(self) -> None:
        gate = MonthlyDistributionGate()
        # 4 months: +50, +50, -300 (big DD), +50 — avg = -37.5 (negative, ratio undefined → fail).
        # Use: +100, +100, -150, +100 — avg = +37.5, mdd-from-equity peak.
        # Equity: 100, 200, 50, 150 → peak=200, trough-after=50, mdd=150.
        # ratio = 150 / 37.5 = 4.0 > 2.0 → FAIL.
        daily = [
            _entry("2026-01-15", 100.0),
            _entry("2026-02-15", 100.0),
            _entry("2026-03-15", -150.0),
            _entry("2026-04-15", 100.0),
        ]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["max_drawdown_pts"] >= 150.0
        assert out.metrics["drawdown_to_avg_monthly_ratio"] > 2.0

    def test_fails_when_avg_monthly_is_not_positive(self) -> None:
        gate = MonthlyDistributionGate()
        daily = [
            _entry("2026-01-15", -50.0),
            _entry("2026-02-15", -30.0),
            _entry("2026-03-15", 20.0),
        ]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["avg_monthly_net_pnl_pts"] < 0.0

    def test_records_median_and_worst_month(self) -> None:
        gate = MonthlyDistributionGate()
        daily = [
            _entry("2026-01-15", 100.0),
            _entry("2026-02-15", 200.0),
            _entry("2026-03-15", -50.0),
            _entry("2026-04-15", 300.0),
        ]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.metrics["worst_monthly_pnl_pts"] == -50.0
        # median of [100, 200, -50, 300] = (100+200)/2 = 150
        assert out.metrics["median_monthly_net_pnl_pts"] == 150.0

    def test_fails_when_too_few_months_to_evaluate(self) -> None:
        gate = MonthlyDistributionGate()
        daily = [_entry("2026-01-15", 100.0), _entry("2026-01-20", 50.0)]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["n_months"] == 1.0
        assert "insufficient" in out.details.lower()

    def test_handles_empty_daily_pnl(self) -> None:
        gate = MonthlyDistributionGate()
        r = _FakeResult(daily_pnl=[])
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        assert out.passed is False
        assert out.metrics["n_months"] == 0.0

    def test_skips_entries_missing_date(self) -> None:
        gate = MonthlyDistributionGate()
        daily = [
            {"pnl_pts": 100.0},  # no date — must be ignored, not crash
            _entry("2026-02-15", 50.0),
            _entry("2026-03-15", 50.0),
        ]
        r = _FakeResult(daily_pnl=daily)
        out = gate.evaluate(r, config=None, thresholds=_THRESHOLDS)
        # 2 valid months, balanced
        assert out.metrics["n_months"] == 2.0

    def test_attribute_name(self) -> None:
        assert MonthlyDistributionGate.name == "monthly_distribution"
        assert MonthlyDistributionGate.applies_to == {"maker", "taker"}
