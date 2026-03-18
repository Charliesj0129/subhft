"""Unit tests for hft_platform.alpha._gate_c — Gate C backtest validation gate.

Tests cover: core metric checks, statistical testing correction methods,
walk-forward gating, parameter optimization integration, stress/robustness
evaluation, scorecard propagation, and edge cases.

Heavy external dependencies (HftNativeRunner, ExperimentTracker, research.*)
are mocked to isolate Gate C logic.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hft_platform.alpha._gate_c import run_gate_c
from hft_platform.alpha._validation_types import GateReport, ValidationConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_backtest_result(**overrides: Any) -> types.SimpleNamespace:
    defaults = {
        "signals": np.asarray([0.1, 0.2, 0.1, 0.2], dtype=np.float64),
        "equity_curve": np.linspace(100.0, 110.0, 100, dtype=np.float64),
        "positions": np.asarray([0.0, 1.0, 1.0, 1.0], dtype=np.float64),
        "sharpe_is": 1.5,
        "sharpe_oos": 1.2,
        "ic_mean": 0.05,
        "ic_std": 0.01,
        "turnover": 0.2,
        "max_drawdown": -0.05,
        "regime_metrics": {},
        "capacity_estimate": 10.0,
        "run_id": "run-test",
        "config_hash": "cfg-test",
        "latency_profile": {"model_applied": True},
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _make_wf_result(**overrides: Any) -> types.SimpleNamespace:
    defaults = {
        "config": types.SimpleNamespace(n_splits=5),
        "folds": [object(), object(), object()],
        "fold_sharpe_mean": 1.0,
        "fold_sharpe_std": 0.2,
        "fold_sharpe_min": 0.5,
        "fold_sharpe_max": 1.5,
        "fold_consistency_pct": 0.8,
        "fold_ic_mean": 0.03,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


class _FakeRunner:
    """Minimal runner that returns canned results."""

    def __init__(self, alpha: Any, cfg: Any):
        self.alpha = alpha
        self.cfg = cfg

    def run(self) -> types.SimpleNamespace:
        return _make_backtest_result()

    def run_walk_forward(self, alpha: Any, cfg: Any) -> types.SimpleNamespace:
        return _make_wf_result()


class _FakeTracker:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir

    def log_run(self, **kwargs: Any) -> Path:
        return self.base_dir / "meta.json"


class _FakeScorecard:
    data_ul = 3

    def to_dict(self) -> dict[str, Any]:
        return {"data_ul": self.data_ul}


def _all_stat_tests_pass(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return {
        "passed": True,
        "tests_passed": 4,
        "tests": {
            "ttest_mean_gt_zero": {"pvalue": 0.01, "pass": True},
            "wilcoxon_gt_zero": {"pvalue": 0.01, "pass": True},
            "sign_test_gt_half": {"pvalue": 0.01, "pass": True},
            "bootstrap_ci_mean": {"pvalue": 0.01, "ci_low": 0.001, "ci_high": 0.01, "pass": True},
        },
    }


def _stress_pass(**kwargs: Any) -> dict[str, Any]:
    return {"passed": True}


def _robustness_pass(**kwargs: Any) -> dict[str, Any]:
    return {"passed": True}


def _opt_pass(**kwargs: Any) -> dict[str, Any]:
    return {"passed": True, "selected_signal_threshold": 0.3}


@pytest.fixture()
def gate_c_mocks(monkeypatch, tmp_path):
    """Patch all external dependencies for Gate C."""
    monkeypatch.setattr("hft_platform.alpha._gate_c._ensure_project_root_on_path", lambda *a: None)
    monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", _FakeRunner)
    monkeypatch.setattr("research.backtest.hft_native_runner.ensure_hftbt_npz", lambda p: p)
    monkeypatch.setattr("hft_platform.alpha.experiments.ExperimentTracker", _FakeTracker)
    monkeypatch.setattr(
        "hft_platform.alpha._gate_c._evaluate_oos_statistical_tests",
        _all_stat_tests_pass,
    )
    monkeypatch.setattr("hft_platform.alpha._gate_c._evaluate_stress_backtest", _stress_pass)
    monkeypatch.setattr("hft_platform.alpha._gate_c._evaluate_parameter_robustness", _robustness_pass)
    monkeypatch.setattr("hft_platform.alpha._gate_c._optimize_parameters", _opt_pass)
    monkeypatch.setattr(
        "research.registry.scorecard.compute_scorecard",
        lambda *a, **kw: _FakeScorecard(),
    )
    return tmp_path


def _run(tmp_path: Path, cfg_overrides: dict | None = None) -> tuple[GateReport, str, str, str, str]:
    alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="test_alpha"))
    cfg_kwargs: dict[str, Any] = {
        "alpha_id": "test_alpha",
        "data_paths": [],
        "enable_walk_forward": False,
        "enable_param_optimization": False,
        "stat_correction_method": "none",
        "min_stat_tests_pass": 2,
        "min_stat_tests_bh_pass": 1,
    }
    if cfg_overrides:
        cfg_kwargs.update(cfg_overrides)
    cfg = ValidationConfig(**cfg_kwargs)
    return run_gate_c(
        alpha=alpha,
        config=cfg,
        root=tmp_path,
        resolved_data_paths=[],
        experiments_base=tmp_path / "experiments",
    )


# ---------------------------------------------------------------------------
# Core metric checks
# ---------------------------------------------------------------------------


class TestGateCCoreMetrics:
    def test_passes_when_all_metrics_ok(self, gate_c_mocks):
        report, run_id, config_hash, scorecard_path, meta_path = _run(gate_c_mocks)
        assert report.passed is True
        assert report.gate == "Gate C"
        assert report.details["core_metrics_passed"] is True

    def test_fails_when_sharpe_below_threshold(self, gate_c_mocks, monkeypatch):
        class LowSharpeRunner(_FakeRunner):
            def run(self):
                return _make_backtest_result(sharpe_oos=-0.5)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", LowSharpeRunner)
        report, *_ = _run(gate_c_mocks, {"min_sharpe_oos": 0.5})
        assert report.passed is False
        assert report.details["core_metrics_passed"] is False

    def test_fails_when_drawdown_exceeds_limit(self, gate_c_mocks, monkeypatch):
        class BigDrawdownRunner(_FakeRunner):
            def run(self):
                return _make_backtest_result(max_drawdown=-0.5)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", BigDrawdownRunner)
        report, *_ = _run(gate_c_mocks, {"max_abs_drawdown": 0.3})
        assert report.passed is False

    def test_fails_when_turnover_too_low(self, gate_c_mocks, monkeypatch):
        class LowTurnoverRunner(_FakeRunner):
            def run(self):
                return _make_backtest_result(turnover=0.0)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", LowTurnoverRunner)
        report, *_ = _run(gate_c_mocks, {"min_turnover": 0.1})
        assert report.passed is False


# ---------------------------------------------------------------------------
# Statistical correction methods
# ---------------------------------------------------------------------------


class TestGateCStatCorrection:
    def test_bh_correction_method(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"stat_correction_method": "bh"})
        mt = report.details["multiple_testing"]
        assert mt["method"] == "bh"
        assert len(mt["raw_pvalues"]) == 4

    def test_bonferroni_correction_method(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"stat_correction_method": "bonferroni"})
        mt = report.details["multiple_testing"]
        assert mt["method"] == "bonferroni"
        assert len(mt["adjusted_pvalues"]) == 4

    def test_none_correction_method(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"stat_correction_method": "none"})
        mt = report.details["multiple_testing"]
        assert mt["method"] == "none"

    def test_unknown_correction_falls_back_to_none(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"stat_correction_method": "foobar"})
        mt = report.details["multiple_testing"]
        assert mt["method"] == "none"

    def test_stat_gate_fails_when_insufficient_bh_pass(self, gate_c_mocks, monkeypatch):
        def only_one_pass(*a, **kw):
            return {
                "passed": True,
                "tests_passed": 1,
                "tests": {
                    "ttest_mean_gt_zero": {"pvalue": 0.01, "pass": True},
                    "wilcoxon_gt_zero": {"pvalue": 0.5, "pass": False},
                    "sign_test_gt_half": {"pvalue": 0.5, "pass": False},
                    "bootstrap_ci_mean": {"pvalue": 0.5, "ci_low": -0.1, "ci_high": 0.1, "pass": False},
                },
            }

        monkeypatch.setattr("hft_platform.alpha._gate_c._evaluate_oos_statistical_tests", only_one_pass)
        report, *_ = _run(
            gate_c_mocks,
            {
                "stat_correction_method": "bh",
                "min_stat_tests_bh_pass": 4,
            },
        )
        assert report.details["stat_gate_passed"] is False
        assert report.passed is False


# ---------------------------------------------------------------------------
# Walk-forward gating
# ---------------------------------------------------------------------------


class TestGateCWalkForward:
    def test_walk_forward_skipped_when_disabled(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"enable_walk_forward": False})
        wf = report.details["walk_forward"]
        assert wf["skipped"] is True
        assert report.details["walk_forward_gate_passed"] is True

    def test_walk_forward_fails_low_consistency(self, gate_c_mocks, monkeypatch):
        class WFRunner(_FakeRunner):
            def run_walk_forward(self, alpha, cfg):
                return _make_wf_result(fold_consistency_pct=0.1)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", WFRunner)
        report, *_ = _run(
            gate_c_mocks,
            {
                "enable_walk_forward": True,
                "wf_min_fold_consistency": 0.6,
            },
        )
        assert report.details["walk_forward_gate_passed"] is False
        assert report.passed is False

    def test_walk_forward_fails_low_sharpe_min(self, gate_c_mocks, monkeypatch):
        class WFRunner(_FakeRunner):
            def run_walk_forward(self, alpha, cfg):
                return _make_wf_result(fold_sharpe_min=-2.0)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", WFRunner)
        report, *_ = _run(
            gate_c_mocks,
            {
                "enable_walk_forward": True,
                "wf_min_fold_sharpe_min": -0.5,
            },
        )
        assert report.details["walk_forward_gate_passed"] is False

    def test_walk_forward_passes(self, gate_c_mocks, monkeypatch):
        class WFRunner(_FakeRunner):
            def run_walk_forward(self, alpha, cfg):
                return _make_wf_result(
                    fold_consistency_pct=0.9,
                    fold_sharpe_min=0.3,
                )

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", WFRunner)
        report, *_ = _run(gate_c_mocks, {"enable_walk_forward": True})
        assert report.details["walk_forward_gate_passed"] is True
        wf = report.details["walk_forward"]
        assert "n_splits" in wf
        assert "fold_consistency_pct" in wf


# ---------------------------------------------------------------------------
# Parameter optimization
# ---------------------------------------------------------------------------


class TestGateCOptimization:
    def test_optimization_disabled(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"enable_param_optimization": False})
        assert report.details["optimization_gate_passed"] is True

    def test_optimization_fails_blocks_gate(self, gate_c_mocks, monkeypatch):
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._optimize_parameters",
            lambda **kw: {"passed": False, "selected_signal_threshold": 0.3},
        )
        report, *_ = _run(gate_c_mocks, {"enable_param_optimization": True})
        assert report.details["optimization_gate_passed"] is False
        assert report.passed is False

    def test_selected_threshold_changes_rerun(self, gate_c_mocks, monkeypatch):
        """When optimization selects a different threshold, runner should be re-run."""
        run_count = {"n": 0}

        class CountingRunner(_FakeRunner):
            def run(self):
                run_count["n"] += 1
                return _make_backtest_result()

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", CountingRunner)
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._optimize_parameters",
            lambda **kw: {"passed": True, "selected_signal_threshold": 0.999},
        )
        _run(gate_c_mocks, {"enable_param_optimization": True})
        # Initial run + re-run with new threshold = 2
        assert run_count["n"] == 2

    def test_selected_threshold_same_no_rerun(self, gate_c_mocks, monkeypatch):
        """When optimization selects same threshold, no re-run needed."""
        run_count = {"n": 0}

        class CountingRunner(_FakeRunner):
            def run(self):
                run_count["n"] += 1
                return _make_backtest_result()

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", CountingRunner)
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._optimize_parameters",
            lambda **kw: {"passed": True, "selected_signal_threshold": 0.3},
        )
        _run(gate_c_mocks, {"enable_param_optimization": True})
        assert run_count["n"] == 1


# ---------------------------------------------------------------------------
# Stress / robustness evaluation
# ---------------------------------------------------------------------------


class TestGateCStressAndRobustness:
    def test_stress_fail_blocks_gate(self, gate_c_mocks, monkeypatch):
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._evaluate_stress_backtest",
            lambda **kw: {"passed": False},
        )
        report, *_ = _run(gate_c_mocks)
        assert report.passed is False

    def test_robustness_fail_blocks_gate(self, gate_c_mocks, monkeypatch):
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._evaluate_parameter_robustness",
            lambda **kw: {"passed": False},
        )
        report, *_ = _run(gate_c_mocks)
        assert report.passed is False


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------


class TestGateCOutputStructure:
    def test_returns_five_tuple(self, gate_c_mocks):
        result = _run(gate_c_mocks)
        assert len(result) == 5
        report, run_id, config_hash, scorecard_path, meta_path = result
        assert isinstance(report, GateReport)
        assert isinstance(run_id, str)
        assert isinstance(config_hash, str)
        assert isinstance(scorecard_path, str)
        assert isinstance(meta_path, str)

    def test_details_contain_expected_keys(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks)
        expected_keys = {
            "run_id",
            "config_hash",
            "sharpe_is",
            "sharpe_oos",
            "ic_mean",
            "ic_std",
            "turnover",
            "max_drawdown",
            "criteria",
            "core_metrics_passed",
            "stat_gate_passed",
            "walk_forward_gate_passed",
            "optimization_gate_passed",
            "statistical_tests",
            "multiple_testing",
            "walk_forward",
            "parameter_optimization",
            "stress_backtest",
            "parameter_robustness",
            "latency_profile",
            "scorecard_path",
            "data_ul_advisory",
            "selected_signal_threshold",
            "base_signal_threshold",
        }
        assert expected_keys.issubset(set(report.details.keys()))

    def test_data_ul_advisory_structure(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks)
        advisory = report.details["data_ul_advisory"]
        assert "value" in advisory
        assert "recommended_min" in advisory
        assert "warn" in advisory
        assert "blocking" in advisory
        assert advisory["blocking"] is False

    def test_criteria_contains_config_values(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks, {"min_sharpe_oos": 0.5})
        criteria = report.details["criteria"]
        assert criteria["min_sharpe_oos"] == 0.5


# ---------------------------------------------------------------------------
# Research engine error
# ---------------------------------------------------------------------------


class TestGateCResearchEngineRejected:
    def test_research_engine_raises_error(self, gate_c_mocks):
        with pytest.raises(ValueError, match="research"):
            alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="test_alpha"))
            cfg = ValidationConfig(
                alpha_id="test_alpha",
                data_paths=[],
                backtest_engine="research",
            )
            run_gate_c(
                alpha=alpha,
                config=cfg,
                root=gate_c_mocks,
                resolved_data_paths=[],
                experiments_base=gate_c_mocks / "experiments",
            )
