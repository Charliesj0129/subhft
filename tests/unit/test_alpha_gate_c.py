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
        # Gate C reports the trend check as ERROR without mid prices.
        "mid_prices": np.linspace(100.0, 101.0, 4, dtype=np.float64),
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


class _FakeFillResult:
    """A fill-priced taker result: ``net`` points per trading day from 2026-04-01."""

    method = "taker_ioc_touch_v1"

    def __init__(self, net: list[float]):
        self.net = list(net)

    def daily_rows(self) -> list[dict[str, Any]]:
        return [
            {"date": f"2026-04-{i + 1:02d}", "net_pts": v, "pnl_pts": v, "n_contracts": 2, "fills": 2}
            for i, v in enumerate(self.net)
        ]

    def n_is_days(self, split: float) -> int:
        n = len(self.net)
        return 0 if n < 2 else max(1, min(n - 1, int(n * split)))

    def sharpe_split(self, split: float) -> tuple[float, float]:
        k = self.n_is_days(split)

        def sharpe(xs: list[float]) -> float:
            a = np.asarray(xs, dtype=np.float64)
            return float(a.mean() / a.std() * np.sqrt(252)) if a.size >= 2 and a.std() > 0 else 0.0

        return sharpe(self.net[:k]), sharpe(self.net[k:])


_PROFITABLE_DAYS = [3.0, 1.0, 4.0, 2.0, 5.0, 1.0, 3.0, 2.0, 4.0, 3.0]


def _fill_sim(net: list[float]) -> Any:
    calls: list[tuple] = []

    def run(alpha: Any, cfg: Any, hbt_paths: list[str], cost_model: Any) -> _FakeFillResult:
        calls.append((cfg, list(hbt_paths), cost_model))
        return _FakeFillResult(net)

    run.calls = calls  # type: ignore[attr-defined]  # test-only call log on the fake
    return run


_FAKE_COST = types.SimpleNamespace(cost_per_side_pts=0.75, label="TEST(comm=0.25,tax=0.5)")


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
    monkeypatch.setattr("hft_platform.alpha._gate_c._evaluate_trend_contamination", lambda **kw: {"passed": True})
    monkeypatch.setattr(
        "research.registry.scorecard.compute_scorecard",
        lambda *a, **kw: _FakeScorecard(),
    )
    monkeypatch.setattr("research.backtest.gate_c_runtime._run_taker_sim", _fill_sim(_PROFITABLE_DAYS))
    monkeypatch.setattr("research.backtest.gate_c_runtime._load_taker_cost_profile", lambda instrument: _FAKE_COST)
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

    def test_day_axis_sub_gates_measure_the_fill_priced_days(self, gate_c_mocks):
        # Day-axis sub-gates read the dated fill-priced rows, never the per-tick
        # mid-to-mid equity curve.
        report, *_ = _run(gate_c_mocks)
        outcomes = {e["name"]: e["outcome"] for e in report.details["sub_gates_advisory"]}
        assert outcomes["sharpe_threshold"] == "pass"
        assert outcomes["winning_day_pct"] == "pass"
        assert report.details["verdict"]["verdict"] == "PASS"  # no profile: sub-gates are advisory

    def test_missing_mid_prices_makes_trend_check_error(self, gate_c_mocks, monkeypatch):
        class NoMidRunner(_FakeRunner):
            def run(self):
                return _make_backtest_result(mid_prices=None)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", NoMidRunner)
        report, *_ = _run(gate_c_mocks)
        assert report.passed is False
        assert report.details["trend_gate_passed"] is False
        assert report.details["verdict"]["verdict"] == "INVALID"

    def test_optimizer_result_without_passed_is_error_not_pass(self, gate_c_mocks, monkeypatch):
        monkeypatch.setattr(
            "hft_platform.alpha._gate_c._optimize_parameters", lambda **kw: {"selected_signal_threshold": 0.3}
        )
        report, *_ = _run(gate_c_mocks, {"enable_param_optimization": True})
        assert report.passed is False
        assert report.details["verdict"]["reasons"] == ["gate_c.parameter_optimization:error"]

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


class TestScorecardCarriesGateDAudits:
    def test_persisted_scorecard_carries_audit_sub_gates_for_gate_d(self, gate_c_mocks, monkeypatch):
        """Gate D reads Gate C's audit results from the scorecard, not a missing payload."""
        from hft_platform.alpha.promotion import PromotionConfig, _evaluate_gate_d_verdict

        captured: dict[str, Any] = {}

        class _CapturingTracker(_FakeTracker):
            def log_run(self, **kwargs: Any) -> Path:
                captured.update(kwargs)
                return self.base_dir / "meta.json"

        monkeypatch.setattr("hft_platform.alpha.experiments.ExperimentTracker", _CapturingTracker)
        report, *_ = _run(gate_c_mocks)

        scorecard = captured["scorecard_payload"]
        sub_gate = next(e for e in report.details["sub_gates_advisory"] if e["name"] == "cost_uncertainty")
        assert scorecard["strategy_type"] == "taker"
        assert scorecard["cost_uncertainty"]["outcome"] == sub_gate["outcome"]
        assert "inventory_mtm" not in scorecard  # maker-only sub-gate

        _, checks = _evaluate_gate_d_verdict(
            {**scorecard, "sharpe_oos": 1.5, "max_drawdown": -0.05, "turnover": 0.5, "correlation_pool_max": 0.2},
            PromotionConfig(alpha_id="test_alpha", owner="o"),
        )
        assert checks["cost_uncertainty_audit"]["detail"] != (
            "MISSING — scorecard.cost_uncertainty absent; the audit cannot be evaluated"
        )
        assert checks["cost_uncertainty_audit"]["outcome"] == sub_gate["outcome"]
        assert checks["inventory_mtm_audit"]["outcome"] == "not_applicable"


# ---------------------------------------------------------------------------
# RunStamp (research v2 F4)
# ---------------------------------------------------------------------------


def test_taker_run_stamp_hashes_the_hftbt_npz_the_runner_reads(gate_c_mocks, monkeypatch):
    tmp_path = gate_c_mocks
    source = tmp_path / "day.npy"
    source.write_bytes(b"research")
    converted = tmp_path / "day_hftbt.npz"
    converted.write_bytes(b"hftbt")
    logged: dict[str, Any] = {}

    class _CapturingTracker(_FakeTracker):
        def log_run(self, **kwargs: Any) -> Path:
            logged.update(kwargs)
            return self.base_dir / "meta.json"

    monkeypatch.setattr("research.backtest.hft_native_runner.ensure_hftbt_npz", lambda p: str(converted))
    monkeypatch.setattr("hft_platform.alpha.experiments.ExperimentTracker", _CapturingTracker)
    alpha = types.SimpleNamespace(manifest=types.SimpleNamespace(alpha_id="test_alpha"))
    cfg = ValidationConfig(
        alpha_id="test_alpha",
        data_paths=[str(source)],
        enable_walk_forward=False,
        enable_param_optimization=False,
        stat_correction_method="none",
    )

    run_gate_c(alpha, cfg, tmp_path, [str(source)], tmp_path / "experiments")

    datasets = logged["run_stamp"]["datasets"]
    assert list(datasets) == [str(source), str(converted)]
    assert all(datasets.values())
    assert logged["run_stamp"]["mode"] == "exploratory"
    assert logged["run_stamp"]["calibration"] == "uncalibrated"


# ---------------------------------------------------------------------------
# Fill-priced taker PnL (research v2 C2c)
# ---------------------------------------------------------------------------


def _check(report: GateReport, name: str) -> dict[str, Any]:
    return next(e for e in report.details["gate_c_checks"] if e["name"] == name)


class TestGateCFillPricedPnl:
    def test_mid_to_mid_alone_cannot_pass_gate_c(self, gate_c_mocks, monkeypatch):
        # The screening runner reports sharpe_oos 1.2; the fills lose money every day.
        losing = [-3.0, -1.0, -4.0, -2.0, -5.0, -1.0, -3.0, -2.0, -4.0, -3.0]
        monkeypatch.setattr("research.backtest.gate_c_runtime._run_taker_sim", _fill_sim(losing))
        report, *_ = _run(gate_c_mocks)

        assert _check(report, "gate_c.screening_sharpe_oos")["outcome"] == "pass"
        assert _check(report, "gate_c.sharpe_oos")["outcome"] == "fail"
        assert report.passed is False
        assert report.details["verdict"]["verdict"] == "KILL"  # a measured fail, not INVALID

    def test_screening_sharpe_still_blocks(self, gate_c_mocks, monkeypatch):
        class LosingScreen(_FakeRunner):
            def run(self):
                return _make_backtest_result(sharpe_oos=-1.0)

        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", LosingScreen)
        report, *_ = _run(gate_c_mocks)

        assert _check(report, "gate_c.sharpe_oos")["outcome"] == "pass"
        assert _check(report, "gate_c.screening_sharpe_oos")["metrics"]["method"] == "mid_to_mid_screening_v1"
        assert report.passed is False

    def test_reports_fill_priced_sharpe_and_labels_screening(self, gate_c_mocks):
        report, *_ = _run(gate_c_mocks)
        fill = report.details["fill_backtest"]

        assert report.details["pnl_method"] == "taker_ioc_touch_v1"
        assert fill["n_is_days"] == 7 and fill["n_oos_days"] == 3
        assert report.details["sharpe_oos"] == pytest.approx(fill["sharpe_oos"])
        assert report.details["sharpe_oos"] != pytest.approx(1.2)  # not the screening value
        assert report.details["screening"] == {
            "method": "mid_to_mid_screening_v1",
            "sharpe_is": 1.5,
            "sharpe_oos": 1.2,
            "max_drawdown": -0.05,
        }
        assert fill["total_net_pts"] == pytest.approx(sum(_PROFITABLE_DAYS))

    def test_scorecard_carries_the_fill_priced_sharpe_for_gate_d(self, gate_c_mocks, monkeypatch):
        seen: list[dict] = []

        def capture(payload, **kw):
            seen.append(payload)
            return _FakeScorecard()

        monkeypatch.setattr("research.registry.scorecard.compute_scorecard", capture)
        report, *_ = _run(gate_c_mocks)

        assert seen[0]["sharpe_oos"] == pytest.approx(report.details["fill_backtest"]["sharpe_oos"])
        assert seen[0]["sharpe_is"] == pytest.approx(report.details["fill_backtest"]["sharpe_is"])

    def test_both_runs_are_costed_by_the_cost_profile(self, gate_c_mocks, monkeypatch):
        sim = _fill_sim(_PROFITABLE_DAYS)
        configs: list[Any] = []

        class Recording(_FakeRunner):
            def __init__(self, alpha, cfg):
                super().__init__(alpha, cfg)
                configs.append(cfg)

        monkeypatch.setattr("research.backtest.gate_c_runtime._run_taker_sim", sim)
        monkeypatch.setattr("research.backtest.hft_native_runner.HftNativeRunner", Recording)
        _run(gate_c_mocks)

        assert configs[0].cost_per_side_pts == pytest.approx(0.75)
        cfg, _paths, cost = sim.calls[0]
        assert cost is _FAKE_COST
        assert cfg.cost_per_side_pts == pytest.approx(0.75)

    def test_missing_cost_profile_is_invalid(self, gate_c_mocks, monkeypatch):
        def no_profile(instrument):
            raise KeyError(instrument)

        monkeypatch.setattr("research.backtest.gate_c_runtime._load_taker_cost_profile", no_profile)
        report, *_ = _run(gate_c_mocks)

        assert _check(report, "gate_c.sharpe_oos")["outcome"] == "error"
        assert report.passed is False
        assert report.details["verdict"]["verdict"] == "INVALID"

    def test_failed_fill_run_is_invalid(self, gate_c_mocks, monkeypatch):
        def broken(*args):
            raise RuntimeError("exchange model started with position 1; a day must start flat")

        monkeypatch.setattr("research.backtest.gate_c_runtime._run_taker_sim", broken)
        report, *_ = _run(gate_c_mocks)

        entry = _check(report, "gate_c.sharpe_oos")
        assert entry["outcome"] == "error"
        assert "start flat" in entry["details"]
        assert report.details["verdict"]["verdict"] == "INVALID"

    def test_fewer_than_two_oos_days_is_insufficient(self, gate_c_mocks, monkeypatch):
        monkeypatch.setattr("research.backtest.gate_c_runtime._run_taker_sim", _fill_sim([2.0, 3.0, 1.0]))
        report, *_ = _run(gate_c_mocks)

        entry = _check(report, "gate_c.sharpe_oos")
        assert entry["outcome"] == "insufficient_sample"
        assert report.passed is False
