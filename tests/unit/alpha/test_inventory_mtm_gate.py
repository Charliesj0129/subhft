"""Unit tests for InventoryMtMGate (Slice B task 9).

The gate fails when realized PnL plus residual mark-to-market is below
the cost floor (cost_floor_per_fill_pts * n_fills). Maker-only.
"""

from __future__ import annotations

from dataclasses import dataclass

from hft_platform.alpha._sub_gates.inventory_mtm import InventoryMtMGate
from hft_platform.alpha._sub_gates.registry import SubGateResult


@dataclass
class _FakeResult:
    daily_pnl: list[dict] | None = None


class TestInventoryMtMGate:
    def test_r47_fixture_realized_plus_residual_below_cost_floor_fails(self) -> None:
        """R47-shape: realized=+239.8 pts, residual_mtm=-300 pts → net negative.

        cost_floor_total = 0.5 * 39 = 19.5 pts
        net = 239.8 + (-300.0) = -60.2 pts
        -60.2 < 19.5 → FAIL
        """
        r47_daily = [
            {
                "date": "2026-04-02",
                "pnl_pts": 239.8,
                "fills": 39,
                "residual_mtm_pts": -300.0,
            },
        ]
        result = _FakeResult(daily_pnl=r47_daily)
        thresholds = {"cost_floor_per_fill_pts": 0.5}
        gate = InventoryMtMGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert isinstance(out, SubGateResult)
        assert out.name == "inventory_mtm"
        assert out.passed is False
        assert out.metrics["net_pts"] == -60.2
        assert out.metrics["cost_floor_total_pts"] == 19.5
        assert "below cost_floor_total" in out.details

    def test_robust_fixture_realized_plus_residual_above_cost_floor_passes(self) -> None:
        """Robust alpha: realized substantially > cost_floor × n_fills, residual ≈ 0.

        cost_floor_total = 0.5 * 18 = 9.0 pts
        net = 180.0 + (-2.0) = 178.0 pts
        178.0 >= 9.0 → PASS
        """
        robust_daily = [
            {"date": "2026-04-01", "pnl_pts": 100.0, "fills": 10, "residual_mtm_pts": 0.0},
            {"date": "2026-04-02", "pnl_pts": 80.0, "fills": 8, "residual_mtm_pts": -2.0},
        ]
        result = _FakeResult(daily_pnl=robust_daily)
        thresholds = {"cost_floor_per_fill_pts": 0.5}
        gate = InventoryMtMGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert out.metrics["net_pts"] == 178.0
        assert out.metrics["n_fills"] == 18

    def test_missing_cost_floor_threshold_returns_advisory_pass(self) -> None:
        """Loose-profile semantics: no cost_floor key → advisory PASS (not fail-closed)."""
        daily = [
            {
                "date": "2026-04-01",
                "pnl_pts": -10.0,
                "fills": 5,
                "residual_mtm_pts": -5.0,
            }
        ]
        result = _FakeResult(daily_pnl=daily)
        thresholds: dict = {}
        gate = InventoryMtMGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert "advisory" in out.details
        assert out.metrics["cost_floor_total_pts"] is None

    def test_empty_daily_pnl_returns_pass(self) -> None:
        """No fills → no claim → gate cannot block. Boundary case: 0 >= 0."""
        result = _FakeResult(daily_pnl=[])
        thresholds = {"cost_floor_per_fill_pts": 0.5}
        gate = InventoryMtMGate()

        out = gate.evaluate(result, config=None, thresholds=thresholds)

        assert out.passed is True
        assert out.metrics["n_fills"] == 0

    def test_applies_to_includes_only_maker(self) -> None:
        gate = InventoryMtMGate()
        assert "maker" in gate.applies_to
        assert "taker" not in gate.applies_to

    def test_name_is_inventory_mtm(self) -> None:
        gate = InventoryMtMGate()
        assert gate.name == "inventory_mtm"


class TestInventoryMtMGateStrictMode:
    """Punch-list (2026-05-29): strict profile must fail-closed on missing evidence."""

    def test_strict_fails_on_legacy_float_shape(self) -> None:
        # Float rows (pre-Slice-B payload) lack ``fills``/``residual_mtm_pts``.
        result = _FakeResult(daily_pnl=[1.2, -0.4, 2.0])  # type: ignore[arg-type]
        out = InventoryMtMGate().evaluate(
            result,
            config=None,
            thresholds={"cost_floor_per_fill_pts": 0.5, "_is_strict_profile": True},
        )
        assert out.passed is False
        assert "STRICT FAIL" in out.details
        assert "legacy float-shape" in out.details

    def test_loose_advisory_passes_on_legacy_float_shape(self) -> None:
        # Same input under loose profile → advisory PASS (back-compat).
        result = _FakeResult(daily_pnl=[1.2, -0.4, 2.0])  # type: ignore[arg-type]
        out = InventoryMtMGate().evaluate(
            result,
            config=None,
            thresholds={"cost_floor_per_fill_pts": 0.5},  # no _is_strict_profile
        )
        assert out.passed is True
        assert "advisory" in out.details

    def test_strict_fails_when_cost_floor_threshold_absent(self) -> None:
        result = _FakeResult(daily_pnl=[{"pnl_pts": 5.0, "fills": 3, "residual_mtm_pts": 0.0}])
        out = InventoryMtMGate().evaluate(
            result,
            config=None,
            thresholds={"_is_strict_profile": True},  # no cost_floor_per_fill_pts
        )
        assert out.passed is False
        assert "cost_floor_per_fill_pts" in out.details
        assert "STRICT FAIL" in out.details

    def test_strict_passes_when_legitimate_pass(self) -> None:
        result = _FakeResult(daily_pnl=[{"pnl_pts": 100.0, "fills": 10, "residual_mtm_pts": 5.0}])
        out = InventoryMtMGate().evaluate(
            result,
            config=None,
            thresholds={"cost_floor_per_fill_pts": 0.5, "_is_strict_profile": True},
        )
        # net = 105, threshold = 5 → pass
        assert out.passed is True
