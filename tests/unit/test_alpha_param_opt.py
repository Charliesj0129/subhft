"""Unit tests for alpha._param_opt — parameter optimization grid search."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha._param_opt import (
    _build_grid_row,
    _evaluate_parameter_robustness,
    _evaluate_stress_backtest,
    _optimization_objective,
    _optimize_parameters,
    _run_single_grid_point,
)
from hft_platform.alpha._validation_types import ValidationConfig

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeResult:
    """Minimal backtest result stub."""

    sharpe_is: float = 1.5
    sharpe_oos: float = 1.2
    max_drawdown: float = -0.08
    turnover: float = 0.5
    run_id: str = "run-001"
    config_hash: str = "hash-abc"
    equity_curve: np.ndarray | None = None

    def __post_init__(self) -> None:
        if self.equity_curve is None:
            object.__setattr__(self, "equity_curve", np.linspace(100, 120, 100))


@dataclass
class FakeBacktestConfig:
    """Minimal backtest config stub with fields used by _param_opt."""

    signal_threshold: float = 0.3
    maker_fee_bps: float = -0.2
    taker_fee_bps: float = 0.2
    submit_ack_latency_ms: float = 36.0
    modify_ack_latency_ms: float = 43.0
    cancel_ack_latency_ms: float = 47.0
    live_uplift_factor: float = 1.5
    latency_profile_id: str = "sim_p95_v2026-02-26"


def _make_runner_cls(result: FakeResult | None = None) -> type:
    """Return a fake runner class whose .run() returns *result*."""

    class _FakeRunner:
        def __init__(self, alpha: Any, cfg: Any) -> None:
            self.alpha = alpha
            self.cfg = cfg

        def run(self) -> FakeResult:
            return result if result is not None else FakeResult()

    return _FakeRunner


def _default_vcfg(**overrides: Any) -> ValidationConfig:
    """Return a ValidationConfig with sensible defaults for testing."""
    defaults: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "data_paths": ["/data/test.npz"],
        "enable_param_optimization": True,
        "opt_signal_threshold_min": 0.1,
        "opt_signal_threshold_max": 0.5,
        "opt_signal_threshold_steps": 3,
        "opt_objective": "risk_adjusted",
        "opt_max_is_oos_gap": 1.0,
        "opt_min_neighbor_objective_ratio": 0.6,
        "opt_min_deflated_sharpe": -0.1,
    }
    defaults.update(overrides)
    return ValidationConfig(**defaults)


# ---------------------------------------------------------------------------
# _optimization_objective
# ---------------------------------------------------------------------------


class TestOptimizationObjective:
    def test_sharpe_oos_mode(self) -> None:
        val = _optimization_objective(1.5, -0.08, 0.5, "sharpe_oos")
        assert val == pytest.approx(1.5)

    def test_risk_adjusted_mode_no_penalties(self) -> None:
        # drawdown < 10% and turnover < 1.0 → no penalties
        val = _optimization_objective(1.5, -0.08, 0.5, "risk_adjusted")
        assert val == pytest.approx(1.5)

    def test_risk_adjusted_mode_with_drawdown_penalty(self) -> None:
        # drawdown = -0.20 → penalty = (0.20 - 0.10) * 2.0 = 0.2
        val = _optimization_objective(1.5, -0.20, 0.5, "risk_adjusted")
        assert val == pytest.approx(1.3)

    def test_risk_adjusted_mode_with_turnover_penalty(self) -> None:
        # turnover = 2.0 → penalty = (2.0 - 1.0) * 0.25 = 0.25
        val = _optimization_objective(1.5, -0.05, 2.0, "risk_adjusted")
        assert val == pytest.approx(1.25)

    def test_ic_first_mode(self) -> None:
        val = _optimization_objective(1.5, -0.05, 2.0, "ic_first")
        # 1.5 - 0.1 * 2.0 = 1.3
        assert val == pytest.approx(1.3)


# ---------------------------------------------------------------------------
# _build_grid_row
# ---------------------------------------------------------------------------


class TestBuildGridRow:
    def test_basic_row_structure(self) -> None:
        result = FakeResult(sharpe_is=1.5, sharpe_oos=1.2, max_drawdown=-0.08, turnover=0.5)
        row = _build_grid_row(result, threshold=0.3, objective_mode="risk_adjusted")
        assert row["signal_threshold"] == pytest.approx(0.3)
        assert row["sharpe_is"] == pytest.approx(1.5)
        assert row["sharpe_oos"] == pytest.approx(1.2)
        assert row["max_drawdown"] == pytest.approx(-0.08)
        assert row["turnover"] == pytest.approx(0.5)
        assert row["run_id"] == "run-001"
        assert row["config_hash"] == "hash-abc"
        assert "objective" in row

    def test_objective_uses_correct_mode(self) -> None:
        result = FakeResult(sharpe_oos=2.0, max_drawdown=-0.05, turnover=0.3)
        row_ra = _build_grid_row(result, 0.3, "risk_adjusted")
        row_so = _build_grid_row(result, 0.3, "sharpe_oos")
        # sharpe_oos mode returns raw sharpe
        assert row_so["objective"] == pytest.approx(2.0)
        # risk_adjusted with small dd/turnover also returns raw sharpe (no penalties)
        assert row_ra["objective"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# _run_single_grid_point
# ---------------------------------------------------------------------------


class TestRunSingleGridPoint:
    def test_calls_runner_with_replaced_threshold(self) -> None:
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        result = FakeResult()
        runner_cls = _make_runner_cls(result)
        row = _run_single_grid_point("alpha_obj", runner_cls, base_cfg, 0.45, "risk_adjusted")
        assert row["signal_threshold"] == pytest.approx(0.45)
        assert row["sharpe_oos"] == pytest.approx(result.sharpe_oos)

    def test_runner_receives_modified_config(self) -> None:
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        captured: list[Any] = []

        class _CapturingRunner:
            def __init__(self, alpha: Any, cfg: Any) -> None:
                captured.append(cfg)

            def run(self) -> FakeResult:
                return FakeResult()

        _run_single_grid_point("alpha_obj", _CapturingRunner, base_cfg, 0.55, "risk_adjusted")
        assert len(captured) == 1
        assert captured[0].signal_threshold == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# _optimize_parameters
# ---------------------------------------------------------------------------


class TestOptimizeParameters:
    def test_disabled_returns_base(self) -> None:
        cfg = _default_vcfg(enable_param_optimization=False)
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult()
        out = _optimize_parameters(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=cfg,
            runner_cls=_make_runner_cls(),
        )
        assert out["enabled"] is False
        assert out["passed"] is True
        assert out["selected_signal_threshold"] == pytest.approx(0.3)

    def test_grid_search_selects_best_objective(self) -> None:
        """When one grid point has higher sharpe, it should be selected."""
        call_count = 0

        class _VaryingRunner:
            def __init__(self, alpha: Any, cfg: Any) -> None:
                self.cfg = cfg

            def run(self) -> FakeResult:
                nonlocal call_count
                call_count += 1
                # Vary sharpe_oos by threshold to create a clear winner
                t = self.cfg.signal_threshold
                sharpe = 2.0 if abs(t - 0.3) < 0.01 else 1.0
                return FakeResult(sharpe_oos=sharpe, sharpe_is=sharpe + 0.1)

        vcfg = _default_vcfg(
            opt_signal_threshold_min=0.1,
            opt_signal_threshold_max=0.5,
            opt_signal_threshold_steps=3,
        )
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult(sharpe_oos=2.0, sharpe_is=2.1)
        out = _optimize_parameters(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=vcfg,
            runner_cls=_VaryingRunner,
        )
        assert out["enabled"] is True
        assert out["selected_row"] is not None
        assert out["selected_row"]["sharpe_oos"] == pytest.approx(2.0)

    def test_risk_flags_overfit_gap(self) -> None:
        """Large IS-OOS gap triggers overfit_gap_risk."""
        vcfg = _default_vcfg(opt_max_is_oos_gap=0.5)
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        # sharpe_is=3.0, sharpe_oos=1.0 → gap=2.0 > 0.5
        big_gap_result = FakeResult(sharpe_is=3.0, sharpe_oos=1.0)

        class _Runner:
            def __init__(self, alpha: Any, cfg: Any) -> None:
                pass

            def run(self) -> FakeResult:
                return big_gap_result

        out = _optimize_parameters(
            alpha="a",
            base_cfg=base_cfg,
            base_result=big_gap_result,
            config=vcfg,
            runner_cls=_Runner,
        )
        assert out["risks"]["overfit_gap_risk"] is True
        assert out["passed"] is False

    def test_sharpe_only_objective_mode(self) -> None:
        vcfg = _default_vcfg(opt_objective="sharpe_oos")
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult()
        out = _optimize_parameters(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=vcfg,
            runner_cls=_make_runner_cls(),
        )
        assert out["objective"] == "sharpe_oos"


# ---------------------------------------------------------------------------
# _evaluate_parameter_robustness
# ---------------------------------------------------------------------------


class TestEvaluateParameterRobustness:
    def test_stable_neighbors_pass(self) -> None:
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult(sharpe_oos=1.2, max_drawdown=-0.08, turnover=0.5)
        out = _evaluate_parameter_robustness(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            runner_cls=_make_runner_cls(FakeResult(sharpe_oos=1.1, max_drawdown=-0.09, turnover=0.6)),
        )
        assert out["passed"] is True
        assert len(out["sweep"]) == 3
        assert out["ratios"] == [0.8, 1.0, 1.2]

    def test_cliff_risk_detected(self) -> None:
        """Neighbor sharpe drops sharply → cliff_risk."""
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult(sharpe_oos=2.0, max_drawdown=-0.05, turnover=0.5)
        # Neighbor sharpe much lower
        out = _evaluate_parameter_robustness(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            runner_cls=_make_runner_cls(FakeResult(sharpe_oos=0.1, max_drawdown=-0.05, turnover=0.5)),
        )
        assert out["risks"]["cliff_risk"] is True
        assert out["passed"] is False

    def test_sign_flip_risk_detected(self) -> None:
        """Neighbor sharpe goes negative → sign_flip_risk."""
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult(sharpe_oos=1.0, max_drawdown=-0.05, turnover=0.5)
        out = _evaluate_parameter_robustness(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            runner_cls=_make_runner_cls(FakeResult(sharpe_oos=-0.5, max_drawdown=-0.05, turnover=0.5)),
        )
        assert out["risks"]["sign_flip_risk"] is True

    def test_deflated_sharpe_in_optimize(self) -> None:
        """Verify deflated_sharpe and selection_penalty appear in output."""
        vcfg = _default_vcfg()
        base_cfg = FakeBacktestConfig(signal_threshold=0.3)
        base_result = FakeResult()
        out = _optimize_parameters(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=vcfg,
            runner_cls=_make_runner_cls(),
        )
        assert "deflated_sharpe" in out
        assert "selection_penalty" in out
        assert out["selection_penalty"] > 0.0


# ---------------------------------------------------------------------------
# _evaluate_stress_backtest
# ---------------------------------------------------------------------------


class TestEvaluateStressBacktest:
    def test_stress_pass_with_resilient_sharpe(self) -> None:
        base_cfg = FakeBacktestConfig()
        base_result = FakeResult(sharpe_oos=1.5, max_drawdown=-0.08)
        stress_result = FakeResult(sharpe_oos=1.0, max_drawdown=-0.10)
        vcfg = _default_vcfg(
            min_stress_sharpe_ratio=0.5,
            stress_latency_multiplier=1.5,
            stress_fee_multiplier=1.5,
        )
        out = _evaluate_stress_backtest(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=vcfg,
            runner_cls=_make_runner_cls(stress_result),
        )
        assert out["passed"] is True
        assert out["multipliers"]["latency"] == pytest.approx(1.5)
        assert out["multipliers"]["fees"] == pytest.approx(1.5)
        assert out["checks"]["sharpe_resilience"] is True
        assert out["checks"]["drawdown_limit"] is True

    def test_stress_fail_sharpe_drops_too_much(self) -> None:
        base_cfg = FakeBacktestConfig()
        base_result = FakeResult(sharpe_oos=2.0)
        # stress sharpe < base * min_ratio (2.0 * 0.5 = 1.0)
        stress_result = FakeResult(sharpe_oos=0.5)
        vcfg = _default_vcfg(min_stress_sharpe_ratio=0.5)
        out = _evaluate_stress_backtest(
            alpha="a",
            base_cfg=base_cfg,
            base_result=base_result,
            config=vcfg,
            runner_cls=_make_runner_cls(stress_result),
        )
        assert out["passed"] is False
        assert out["checks"]["sharpe_resilience"] is False

    def test_stress_config_applies_multipliers(self) -> None:
        """Verify the stress config applies fee and latency multipliers."""
        captured: list[Any] = []

        class _CapturingRunner:
            def __init__(self, alpha: Any, cfg: Any) -> None:
                captured.append(cfg)

            def run(self) -> FakeResult:
                return FakeResult(sharpe_oos=1.0, max_drawdown=-0.05)

        base_cfg = FakeBacktestConfig(
            maker_fee_bps=-0.2,
            taker_fee_bps=0.2,
            submit_ack_latency_ms=36.0,
        )
        vcfg = _default_vcfg(stress_latency_multiplier=2.0, stress_fee_multiplier=3.0)
        _evaluate_stress_backtest(
            alpha="a",
            base_cfg=base_cfg,
            base_result=FakeResult(),
            config=vcfg,
            runner_cls=_CapturingRunner,
        )
        assert len(captured) == 1
        stress_cfg = captured[0]
        assert stress_cfg.maker_fee_bps == pytest.approx(-0.6)
        assert stress_cfg.taker_fee_bps == pytest.approx(0.6)
        assert stress_cfg.submit_ack_latency_ms == pytest.approx(72.0)
