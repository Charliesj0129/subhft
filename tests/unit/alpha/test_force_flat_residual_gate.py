"""Round 43: ForceFlatResidualGate (goal 驗證標準 §3 + §5)."""

from __future__ import annotations

import pytest

from hft_platform.alpha._sub_gates import (
    ensure_builtin_sub_gates_registered,
    get_registered_sub_gates,
)
from hft_platform.alpha._sub_gates.force_flat_residual import (
    ForceFlatResidualGate,
)


class _Result:
    """Minimal stand-in mirroring the BacktestResult fields the gate reads."""

    def __init__(
        self,
        *,
        trade_pnl: list[float] | None = None,
        abs_residual_qty: int = 0,
        mark_method: str = "",
    ) -> None:
        self.trade_pnl = trade_pnl
        self.abs_residual_qty = abs_residual_qty
        self.mark_method = mark_method


class TestForceFlatResidualGate:
    def setup_method(self) -> None:
        self.gate = ForceFlatResidualGate()

    def test_applies_to_both_strategy_types(self) -> None:
        assert self.gate.applies_to == {"maker", "taker"}

    def test_no_trades_returns_advisory_pass(self) -> None:
        r = _Result(trade_pnl=[], abs_residual_qty=3, mark_method="force_flat_last_mid")
        out = self.gate.evaluate(r, config=None, thresholds={})
        assert out.passed is True
        assert out.metrics["n_trips"] == 0.0
        assert "advisory" in out.details

    def test_no_residual_passes(self) -> None:
        r = _Result(
            trade_pnl=[1.0, 2.0, 3.0],
            abs_residual_qty=0,
            mark_method="no_residual",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert out.passed is True
        assert out.metrics["force_flat_trip_share_pct"] == 0.0

    def test_residual_below_threshold_passes(self) -> None:
        # 1 force-flat unit / 10 trips = 10% < 30%.
        r = _Result(
            trade_pnl=[1.0] * 10,
            abs_residual_qty=1,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert out.passed is True
        assert out.metrics["force_flat_trip_share_pct"] == pytest.approx(10.0)

    def test_residual_at_threshold_passes(self) -> None:
        # 3 / 10 = 30% exactly — boundary inclusive.
        r = _Result(
            trade_pnl=[1.0] * 10,
            abs_residual_qty=3,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert out.passed is True

    def test_residual_above_threshold_fails(self) -> None:
        # 5 / 10 = 50% > 30% threshold.
        r = _Result(
            trade_pnl=[1.0] * 10,
            abs_residual_qty=5,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert out.passed is False
        assert out.metrics["force_flat_trip_share_pct"] == pytest.approx(50.0)
        assert "ff_share=50.0%" in out.details

    def test_residual_exceeding_n_trips_clamps_to_100pct(self) -> None:
        # Defensive: abs_residual_qty > n_trips can happen for
        # taker series where many of the entry deltas were absorbed
        # by intermediate exits.  Upper-bound clamp keeps the metric
        # bounded by n_trips.
        r = _Result(
            trade_pnl=[1.0, 2.0],
            abs_residual_qty=10,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert out.passed is False
        assert out.metrics["force_flat_trip_share_pct"] == pytest.approx(100.0)

    def test_missing_threshold_returns_advisory_metrics(self) -> None:
        r = _Result(
            trade_pnl=[1.0, 2.0, 3.0],
            abs_residual_qty=2,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(r, config=None, thresholds={})
        # Threshold absent → loose-profile advisory PASS with metrics.
        assert out.passed is True
        assert out.metrics["force_flat_trip_share_max_pct"] is None
        assert out.metrics["force_flat_trip_share_pct"] == pytest.approx(100.0 * 2 / 3)
        assert "advisory" in out.details

    def test_missing_attributes_default_to_safe_zero(self) -> None:
        # Object without the new fields (legacy fixture) must not crash.
        class _Bare:
            pass

        out = self.gate.evaluate(_Bare(), config=None, thresholds={})
        assert out.passed is True
        assert out.metrics["abs_residual_qty"] == 0.0

    def test_mark_method_surfaces_in_details(self) -> None:
        r = _Result(
            trade_pnl=[1.0, 2.0, 3.0],
            abs_residual_qty=1,
            mark_method="force_flat_last_mid",
        )
        out = self.gate.evaluate(
            r,
            config=None,
            thresholds={"force_flat_trip_share_max_pct": 30.0},
        )
        assert "force_flat_last_mid" in out.details


class TestRegistryRegistration:
    def test_force_flat_residual_is_registered_after_ensure(self) -> None:
        ensure_builtin_sub_gates_registered()
        names = {g.name for g in get_registered_sub_gates()}
        assert "force_flat_residual" in names

    def test_registered_gate_applies_to_both_strategy_types(self) -> None:
        ensure_builtin_sub_gates_registered()
        for gate in get_registered_sub_gates():
            if gate.name == "force_flat_residual":
                assert gate.applies_to == {"maker", "taker"}
                return
        raise AssertionError("force_flat_residual not in registry")
