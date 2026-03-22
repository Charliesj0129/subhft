"""Gate C/D/E validation tests and supporting statistical helper tests.

This module covers:
- _compute_oos_returns, _evaluate_oos_statistical_tests, _bh_correction helpers
- run_gate_c (walk-forward, parameter optimisation)
- ValidationConfig defaults
- _optimize_parameters boundary detection
- Scorecard BDS p-value propagation
"""

import types
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.validation import (
    ValidationConfig,
    _bh_correction,
    _compute_oos_returns,
    _evaluate_oos_statistical_tests,
    _optimize_parameters,
    run_gate_c,
)
from research.backtest.types import BacktestConfig
from research.registry.schemas import Scorecard

# ---------------------------------------------------------------------------
# Statistical helper tests
# ---------------------------------------------------------------------------


def test_compute_oos_returns_extracts_tail_returns():
    equity = np.array([100.0, 101.0, 102.0, 101.5, 103.0, 104.0], dtype=np.float64)
    returns = _compute_oos_returns(equity, is_oos_split=0.6)
    assert returns.size >= 2
    assert np.isfinite(returns).all()


def test_statistical_tests_fail_when_insufficient_samples():
    arr = np.array([0.001, -0.001, 0.0], dtype=np.float64)
    report = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.1,
        min_tests_pass=2,
        bootstrap_samples=100,
    )
    assert report["passed"] is False
    assert report["reason"] == "insufficient_oos_returns"


def test_statistical_tests_pass_for_consistent_positive_oos_returns():
    rng = np.random.default_rng(42)
    arr = rng.normal(loc=0.002, scale=0.001, size=256).astype(np.float64)
    report = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.1,
        min_tests_pass=2,
        bootstrap_samples=300,
    )
    assert report["passed"] is True
    assert report["tests_passed"] >= 2


def test_statistical_tests_include_bds_diagnostic():
    rng = np.random.default_rng(7)
    arr = rng.normal(loc=0.0, scale=0.001, size=256).astype(np.float64)
    report = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.1,
        min_tests_pass=1,
        bootstrap_samples=200,
    )
    assert "diagnostic_gate_passed" in report
    bds = report["tests"]["bds_independence"]
    assert "pvalue" in bds
    assert isinstance(bool(bds["pass"]), bool)


def test_statistical_tests_bds_detects_dependence():
    arr = np.tile(np.asarray([0.002, -0.002], dtype=np.float64), 200)
    report = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.1,
        min_tests_pass=1,
        bootstrap_samples=200,
    )
    assert report["tests"]["bds_independence"]["reject_iid"] is True
    assert report["diagnostic_gate_passed"] is False


def test_bh_correction_all_significant():
    rejected, adjusted = _bh_correction([0.01, 0.01, 0.01, 0.01], alpha=0.1)
    assert rejected == [True, True, True, True]
    assert all(0.0 <= p <= 1.0 for p in adjusted)


def test_bh_correction_none_significant():
    rejected, adjusted = _bh_correction([0.5, 0.6, 0.7, 0.8], alpha=0.1)
    assert rejected == [False, False, False, False]
    assert all(0.0 <= p <= 1.0 for p in adjusted)


def test_bh_correction_partial():
    rejected, _ = _bh_correction([0.01, 0.5, 0.6, 0.7], alpha=0.1)
    assert rejected == [True, False, False, False]


# ---------------------------------------------------------------------------
# Gate C tests
# ---------------------------------------------------------------------------


def test_gate_c_walk_forward_fail(monkeypatch, tmp_path: Path):
    class _FakeRunner:
        def __init__(self, alpha, cfg):
            self.alpha = alpha
            self.cfg = cfg

        def run(self):
            return types.SimpleNamespace(
                signals=np.asarray([0.1, 0.2, 0.1, 0.2], dtype=np.float64),
                equity_curve=np.asarray([100.0, 100.2, 100.4, 100.6], dtype=np.float64),
                positions=np.asarray([0.0, 1.0, 1.0, 1.0], dtype=np.float64),
                sharpe_is=1.2,
                sharpe_oos=1.1,
                ic_mean=0.05,
                ic_std=0.01,
                turnover=0.2,
                max_drawdown=-0.05,
                regime_metrics={},
                capacity_estimate=10.0,
                run_id="run-1",
                config_hash="cfg-1",
                latency_profile={"model_applied": True},
            )

        def run_walk_forward(self, alpha, cfg):
            del alpha, cfg
            return types.SimpleNamespace(
                config=types.SimpleNamespace(n_splits=5),
                folds=[object(), object(), object()],
                fold_sharpe_mean=0.2,
                fold_sharpe_std=0.1,
                fold_sharpe_min=0.0,
                fold_sharpe_max=0.3,
                fold_consistency_pct=0.2,
                fold_ic_mean=0.01,
            )

    class _FakeTracker:
        def __init__(self, base_dir):
            self.base_dir = base_dir

        def log_run(self, **kwargs):
            del kwargs
            return self.base_dir / "meta.json"

    monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", _FakeRunner)
    monkeypatch.setattr("research.backtest.hft_native_runner.ensure_hftbt_npz", lambda p: p)
    monkeypatch.setattr("hft_platform.alpha.experiments.ExperimentTracker", _FakeTracker)
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_oos_statistical_tests",
        lambda *a, **k: {
            "passed": True,
            "tests_passed": 4,
            "tests": {
                "ttest_mean_gt_zero": {"pvalue": 0.01, "pass": True},
                "wilcoxon_gt_zero": {"pvalue": 0.01, "pass": True},
                "sign_test_gt_half": {"pvalue": 0.01, "pass": True},
                "bootstrap_ci_mean": {"pvalue": 0.01, "ci_low": 0.001, "ci_high": 0.01, "pass": True},
            },
        },
    )
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_stress_backtest",
        lambda **kwargs: {"passed": True},
    )
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_parameter_robustness",
        lambda **kwargs: {"passed": True},
    )

    alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="ofi_mc"))
    cfg = ValidationConfig(alpha_id="ofi_mc", data_paths=["dummy.npy"])
    report, *_ = run_gate_c(
        alpha=alpha,
        config=cfg,
        root=tmp_path,
        resolved_data_paths=[],
        experiments_base=tmp_path / "experiments",
    )
    assert report.passed is False
    assert report.details["walk_forward_gate_passed"] is False


def test_gate_c_skip_walk_forward(monkeypatch, tmp_path: Path):
    class _FakeRunner:
        def __init__(self, alpha, cfg):
            self.alpha = alpha
            self.cfg = cfg

        def run(self):
            return types.SimpleNamespace(
                signals=np.asarray([0.1, 0.2, 0.1, 0.2], dtype=np.float64),
                equity_curve=np.asarray([100.0, 100.2, 100.4, 100.6], dtype=np.float64),
                positions=np.asarray([0.0, 1.0, 1.0, 1.0], dtype=np.float64),
                sharpe_is=1.2,
                sharpe_oos=1.1,
                ic_mean=0.05,
                ic_std=0.01,
                turnover=0.2,
                max_drawdown=-0.05,
                regime_metrics={},
                capacity_estimate=10.0,
                run_id="run-2",
                config_hash="cfg-2",
                latency_profile={"model_applied": True},
            )

        def run_walk_forward(self, alpha, cfg):
            del alpha, cfg
            raise AssertionError("run_walk_forward must be skipped")

    class _FakeTracker:
        def __init__(self, base_dir):
            self.base_dir = base_dir

        def log_run(self, **kwargs):
            del kwargs
            return self.base_dir / "meta.json"

    monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", _FakeRunner)
    monkeypatch.setattr("research.backtest.hft_native_runner.ensure_hftbt_npz", lambda p: p)
    monkeypatch.setattr("hft_platform.alpha.experiments.ExperimentTracker", _FakeTracker)
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_oos_statistical_tests",
        lambda *a, **k: {
            "passed": True,
            "tests_passed": 2,
            "tests": {
                "ttest_mean_gt_zero": {"pvalue": 0.01, "pass": True},
                "wilcoxon_gt_zero": {"pvalue": 0.02, "pass": True},
                "sign_test_gt_half": {"pvalue": 0.6, "pass": False},
                "bootstrap_ci_mean": {"pvalue": 0.7, "ci_low": -0.1, "ci_high": 0.2, "pass": False},
            },
        },
    )
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_stress_backtest",
        lambda **kwargs: {"passed": True},
    )
    monkeypatch.setattr(
        "hft_platform.alpha.validation._evaluate_parameter_robustness",
        lambda **kwargs: {"passed": True},
    )

    alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="ofi_mc"))
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=["dummy.npy"],
        enable_walk_forward=False,
        enable_param_optimization=False,
        stat_correction_method="none",
        min_stat_tests_pass=2,
        min_stat_tests_bh_pass=1,
    )
    report, *_ = run_gate_c(
        alpha=alpha,
        config=cfg,
        root=tmp_path,
        resolved_data_paths=[],
        experiments_base=tmp_path / "experiments",
    )
    assert report.passed is True
    assert report.details["walk_forward"]["skipped"] is True


# ---------------------------------------------------------------------------
# ValidationConfig defaults
# ---------------------------------------------------------------------------


def test_validation_config_defaults():
    cfg = ValidationConfig(alpha_id="x", data_paths=["y.npy"])
    assert cfg.enable_walk_forward is True
    assert cfg.wf_n_splits == 5
    assert cfg.wf_min_fold_consistency == 0.6
    assert cfg.wf_min_fold_sharpe_min == -0.5
    assert cfg.enable_param_optimization is True
    assert cfg.opt_signal_threshold_steps == 8
    assert cfg.opt_objective == "risk_adjusted"
    assert cfg.require_paper_refs is False
    assert cfg.require_paper_index_link is False
    assert cfg.enforce_data_governance is False
    assert cfg.require_data_meta is False
    assert cfg.stat_correction_method == "bh"
    assert cfg.min_stat_tests_bh_pass == 1


# ---------------------------------------------------------------------------
# _optimize_parameters tests
# ---------------------------------------------------------------------------


def test_optimize_parameters_selects_interior_threshold():
    class _Runner:
        def __init__(self, alpha, cfg):
            self.cfg = cfg

        def run(self):
            threshold = float(self.cfg.signal_threshold)
            sharpe_oos = 2.0 - abs(threshold - 0.3) * 8.0
            return types.SimpleNamespace(
                sharpe_is=sharpe_oos + 0.2,
                sharpe_oos=sharpe_oos,
                max_drawdown=-0.1,
                turnover=0.4,
                run_id=f"r-{threshold:.2f}",
                config_hash=f"c-{threshold:.2f}",
            )

    cfg = ValidationConfig(
        alpha_id="x",
        data_paths=["d.npy"],
        opt_signal_threshold_min=0.1,
        opt_signal_threshold_max=0.5,
        opt_signal_threshold_steps=5,
        opt_objective="sharpe_oos",
    )
    base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
    base_result = types.SimpleNamespace(
        sharpe_is=2.2,
        sharpe_oos=2.0,
        max_drawdown=-0.1,
        turnover=0.4,
        run_id="base",
        config_hash="base",
        equity_curve=np.ones(100, dtype=np.float64),
    )
    out = _optimize_parameters(
        alpha=object(),
        base_cfg=base_cfg,
        base_result=base_result,
        config=cfg,
        runner_cls=_Runner,
    )
    assert out["passed"] is True
    assert abs(float(out["selected_signal_threshold"]) - 0.3) < 1e-9


def test_optimize_parameters_flags_boundary_risk():
    class _Runner:
        def __init__(self, alpha, cfg):
            self.cfg = cfg

        def run(self):
            threshold = float(self.cfg.signal_threshold)
            sharpe_oos = threshold * 10.0
            return types.SimpleNamespace(
                sharpe_is=sharpe_oos + 0.1,
                sharpe_oos=sharpe_oos,
                max_drawdown=-0.05,
                turnover=0.2,
                run_id=f"r-{threshold:.2f}",
                config_hash=f"c-{threshold:.2f}",
            )

    cfg = ValidationConfig(
        alpha_id="x",
        data_paths=["d.npy"],
        opt_signal_threshold_min=0.1,
        opt_signal_threshold_max=0.5,
        opt_signal_threshold_steps=5,
        opt_objective="sharpe_oos",
    )
    base_cfg = BacktestConfig(data_paths=[], signal_threshold=0.3)
    base_result = types.SimpleNamespace(
        sharpe_is=3.1,
        sharpe_oos=3.0,
        max_drawdown=-0.05,
        turnover=0.2,
        run_id="base",
        config_hash="base",
        equity_curve=np.ones(100, dtype=np.float64),
    )
    out = _optimize_parameters(
        alpha=object(),
        base_cfg=base_cfg,
        base_result=base_result,
        config=cfg,
        runner_cls=_Runner,
    )
    assert out["passed"] is False


# ---------------------------------------------------------------------------
# P0-B: BDS p-value → Scorecard propagation
# ---------------------------------------------------------------------------


def test_statistical_tests_bds_pvalue_key_present():
    """_evaluate_oos_statistical_tests always returns bds_independence.pvalue as a float."""
    rng = np.random.default_rng(99)
    arr = rng.normal(loc=0.001, scale=0.001, size=256).astype(np.float64)
    report = _evaluate_oos_statistical_tests(
        arr,
        pvalue_threshold=0.1,
        min_tests_pass=1,
        bootstrap_samples=200,
    )
    bds = report["tests"]["bds_independence"]
    assert "pvalue" in bds
    pvalue = bds["pvalue"]
    assert isinstance(pvalue, float)
    assert 0.0 <= pvalue <= 1.0


def test_scorecard_stat_bds_pvalue_roundtrip():
    """Scorecard.stat_bds_pvalue survives to_dict() / from_dict() round-trip."""
    sc = Scorecard(stat_bds_pvalue=0.034)
    d = sc.to_dict()
    assert d["stat_bds_pvalue"] == pytest.approx(0.034)
    sc2 = Scorecard.from_dict(d)
    assert sc2.stat_bds_pvalue == pytest.approx(0.034)


def test_scorecard_stat_bds_pvalue_defaults_to_none():
    """Scorecard.from_dict() on a legacy dict without stat_bds_pvalue yields None."""
    legacy = {"sharpe_oos": 1.5, "max_drawdown": -0.1}
    sc = Scorecard.from_dict(legacy)
    assert sc.stat_bds_pvalue is None
