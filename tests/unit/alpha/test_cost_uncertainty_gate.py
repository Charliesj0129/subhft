"""Unit tests for CostUncertaintyGate (Slice B task 10).

The gate fails when the one-sided 95% confidence-interval lower bound on
mean daily PnL falls at or below ``cost_uncertainty_p95_lower_bound_min_pts``.
This catches alphas whose headline PnL is statistically indistinguishable
from cost noise once day-to-day variance is accounted for.

Design pivot (2026-05-05): the original Slice B plan referenced
``Scorecard.cost_sensitivity_ratio`` as the trigger, but that field is not
reliably populated on ``BacktestResult`` (Task 9 follow-up finding).
We instead operate directly on ``daily_pnl`` — the same contract used by
``InventoryMtMGate`` — making the two gates true siblings.
"""

from __future__ import annotations

from dataclasses import dataclass

from hft_platform.alpha._sub_gates.cost_uncertainty import CostUncertaintyGate
from hft_platform.alpha._sub_gates.registry import SubGateResult


@dataclass
class _FakeResult:
    daily_pnl: list[dict] | None = None


class TestCostUncertaintyGate:
    def test_r47_shape_high_variance_single_day_dominated_fails(self) -> None:
        """R47-like single-day-dominated PnL: P95 lower bound is negative.

        5 days, one outlier carrying ~97% of the total PnL.
        mean ≈ 47.96, sample_std ≈ 103.2, sem ≈ 46.16
        P95 lower bound ≈ 47.96 - 1.645 * 46.16 ≈ -27.97
        -27.97 <= 0.0 → FAIL
        """
        r47_daily = [
            {"date": "2026-04-01", "pnl_pts": 2.0, "fills": 1},
            {"date": "2026-04-02", "pnl_pts": 232.4, "fills": 35},
            {"date": "2026-04-03", "pnl_pts": 1.5, "fills": 1},
            {"date": "2026-04-04", "pnl_pts": 1.9, "fills": 1},
            {"date": "2026-04-05", "pnl_pts": 2.0, "fills": 1},
        ]
        result = _FakeResult(daily_pnl=r47_daily)
        thresholds = {"cost_uncertainty_p95_lower_bound_min_pts": 0.0}
        gate = CostUncertaintyGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert isinstance(out, SubGateResult)
        assert out.name == "cost_uncertainty"
        assert out.passed is False
        assert out.metrics["n_days"] == 5
        # P95 lower bound must be clearly negative
        assert out.metrics["p95_lower_bound_pts"] is not None
        assert out.metrics["p95_lower_bound_pts"] < 0.0
        assert out.metrics["threshold_pts"] == 0.0
        assert "P95 lower bound" in out.details

    def test_robust_alpha_low_variance_passes(self) -> None:
        """Steady alpha: low variance, mean comfortably above floor.

        10 days, pnl ≈ 30 + i*0.5 (range 30.5..35.0), 5 fills/day.
        mean = 32.75, sample_std ≈ 1.51, sem ≈ 0.48
        P95 lower bound ≈ 32.75 - 1.645 * 0.48 ≈ 31.96
        31.96 > 0.0 → PASS
        """
        robust_daily = [
            {
                "date": f"2026-04-{i:02d}",
                "pnl_pts": 30.0 + i * 0.5,
                "fills": 5,
            }
            for i in range(1, 11)
        ]
        result = _FakeResult(daily_pnl=robust_daily)
        thresholds = {"cost_uncertainty_p95_lower_bound_min_pts": 0.0}
        gate = CostUncertaintyGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert out.metrics["n_days"] == 10
        assert out.metrics["p95_lower_bound_pts"] is not None
        assert out.metrics["p95_lower_bound_pts"] > 0.0
        assert out.details == "OK"

    def test_missing_threshold_returns_advisory_pass(self) -> None:
        """Loose-profile semantics: no threshold key → advisory PASS."""
        daily = [
            {"date": "2026-04-01", "pnl_pts": -10.0, "fills": 5},
            {"date": "2026-04-02", "pnl_pts": -8.0, "fills": 4},
            {"date": "2026-04-03", "pnl_pts": -12.0, "fills": 6},
        ]
        result = _FakeResult(daily_pnl=daily)
        thresholds: dict = {}
        gate = CostUncertaintyGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert "advisory" in out.details
        assert out.metrics["threshold_pts"] is None
        # Still computes the lower bound for observability
        assert out.metrics["p95_lower_bound_pts"] is not None

    def test_n_days_below_two_returns_advisory_pass(self) -> None:
        """Variance undefined with n<2 → advisory PASS."""
        daily = [{"date": "2026-04-01", "pnl_pts": 100.0, "fills": 5}]
        result = _FakeResult(daily_pnl=daily)
        thresholds = {"cost_uncertainty_p95_lower_bound_min_pts": 0.0}
        gate = CostUncertaintyGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert out.metrics["n_days"] == 1
        assert out.metrics["p95_lower_bound_pts"] is None
        assert "n_days=1" in out.details

    def test_zero_fill_days_excluded_from_series(self) -> None:
        """Days with no fills (weekends/holidays) MUST NOT enter the PnL series."""
        mixed = [
            {"date": "2026-04-01", "pnl_pts": 30.0, "fills": 5},
            {"date": "2026-04-02", "pnl_pts": 0.0, "fills": 0},  # excluded
            {"date": "2026-04-03", "pnl_pts": 28.0, "fills": 4},
            {"date": "2026-04-04", "pnl_pts": 32.0, "fills": 6},
        ]
        result = _FakeResult(daily_pnl=mixed)
        thresholds = {"cost_uncertainty_p95_lower_bound_min_pts": 0.0}
        gate = CostUncertaintyGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.metrics["n_days"] == 3
        # Mean over [30, 28, 32] = 30.0 — NOT diluted by the zero-fill day
        assert out.metrics["mean_daily_pnl_pts"] == 30.0

    def test_applies_to_includes_maker_and_taker(self) -> None:
        gate = CostUncertaintyGate()
        assert "maker" in gate.applies_to
        assert "taker" in gate.applies_to


class TestCostUncertaintyGateStrictMode:
    """Punch-list (2026-05-29): strict profile fails on low N / missing threshold."""

    def _days(self, n: int, pnl: float = 1.0) -> list[dict]:
        return [{"pnl_pts": pnl, "fills": 1} for _ in range(n)]

    def test_strict_fails_when_n_days_below_min(self) -> None:
        # Strict floor is 5 days; supplying 3 must FAIL.
        result = _FakeResult(daily_pnl=self._days(3))
        out = CostUncertaintyGate().evaluate(
            result,
            config=None,
            thresholds={
                "cost_uncertainty_p95_lower_bound_min_pts": 0.0,
                "_is_strict_profile": True,
            },
        )
        assert out.passed is False
        assert "STRICT FAIL" in out.details
        assert "n_days=3" in out.details

    def test_loose_advisory_passes_when_n_days_short(self) -> None:
        # Same input, loose profile (no _is_strict_profile) → advisory PASS
        # for n>=2 but legitimate evaluation; n<2 is still advisory.
        result = _FakeResult(daily_pnl=self._days(1))
        out = CostUncertaintyGate().evaluate(
            result,
            config=None,
            thresholds={"cost_uncertainty_p95_lower_bound_min_pts": 0.0},
        )
        assert out.passed is True
        assert "advisory" in out.details

    def test_strict_fails_when_threshold_absent(self) -> None:
        result = _FakeResult(daily_pnl=self._days(10, pnl=5.0))
        out = CostUncertaintyGate().evaluate(
            result,
            config=None,
            thresholds={"_is_strict_profile": True},  # no min_pts threshold
        )
        assert out.passed is False
        assert "STRICT FAIL" in out.details
        assert "threshold absent" in out.details

    def test_strict_passes_on_legitimate_high_n_positive_lower_bound(self) -> None:
        # 10 days, all +5, std=0 → lower bound = 5 > 0 → pass
        result = _FakeResult(daily_pnl=self._days(10, pnl=5.0))
        out = CostUncertaintyGate().evaluate(
            result,
            config=None,
            thresholds={
                "cost_uncertainty_p95_lower_bound_min_pts": 0.0,
                "_is_strict_profile": True,
            },
        )
        assert out.passed is True
        assert out.metrics["n_days"] == 10
