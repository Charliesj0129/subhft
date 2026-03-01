import json
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
    run_gate_a,
    run_gate_b,
    run_gate_c,
)
from research.backtest.hbt_runner import BacktestConfig
from research.registry.schemas import Scorecard


def test_run_gate_a_passes_with_alias_fields(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(
        8,
        dtype=[
            ("best_bid", "i8"),
            ("best_ask", "i8"),
            ("bid_depth", "f8"),
            ("ask_depth", "f8"),
            ("qty", "f8"),
        ],
    )
    np.save(path, arr)

    manifest = types.SimpleNamespace(
        data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"),
        complexity="O(1)",
    )
    report = run_gate_a(manifest, [str(path)])
    assert report.passed
    assert report.details["missing_fields"] == []


def test_run_gate_a_fails_when_required_fields_missing(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(8, dtype=[("px", "i8"), ("qty", "f8")])
    np.save(path, arr)

    manifest = types.SimpleNamespace(
        data_fields=("bid_px", "ask_px"),
        complexity="O(N)",
    )
    report = run_gate_a(manifest, [str(path)])
    assert not report.passed
    assert "bid_px" in report.details["missing_fields"]
    assert "ask_px" in report.details["missing_fields"]


def test_run_gate_b_skip(tmp_path: Path):
    report = run_gate_b(alpha_id="ofi_mc", project_root=tmp_path, skip_tests=True, timeout_s=1)
    assert report.passed
    assert report.details["skipped"] is True


def test_run_gate_b_failure(monkeypatch, tmp_path: Path):
    class _Proc:
        returncode = 1
        stdout = "failed tests"
        stderr = "trace"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    report = run_gate_b(alpha_id="ofi_mc", project_root=tmp_path, skip_tests=False, timeout_s=1)
    assert not report.passed
    assert report.details["returncode"] == 1


def test_run_gate_a_requires_fields_in_all_data_paths(tmp_path: Path):
    good_path = tmp_path / "feed_good.npy"
    bad_path = tmp_path / "feed_bad.npy"

    good = np.zeros(
        4,
        dtype=[
            ("best_bid", "i8"),
            ("best_ask", "i8"),
            ("bid_depth", "f8"),
            ("ask_depth", "f8"),
            ("qty", "f8"),
        ],
    )
    bad = np.zeros(4, dtype=[("px", "i8"), ("qty", "f8")])
    np.save(good_path, good)
    np.save(bad_path, bad)

    manifest = types.SimpleNamespace(
        data_fields=("bid_px", "ask_px", "bid_qty", "ask_qty", "trade_vol", "current_mid"),
        complexity="O(1)",
    )
    report = run_gate_a(manifest, [str(good_path), str(bad_path)])
    assert not report.passed
    assert str(bad_path) in report.details["missing_fields_by_path"]
    assert "bid_px" in report.details["missing_fields"]


def test_run_gate_a_requires_paper_refs_when_enforced(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)
    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=(),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        require_paper_refs=True,
        require_paper_index_link=False,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert not report.passed
    assert report.details["paper_governance"]["paper_ref_missing"] is True


def test_run_gate_a_requires_paper_index_link(tmp_path: Path):
    path = tmp_path / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)

    paper_index = tmp_path / "research" / "knowledge" / "paper_index.json"
    paper_index.parent.mkdir(parents=True, exist_ok=True)
    paper_index.write_text(
        '{"120":{"ref":"120","arxiv_id":"2408.03594","title":"OFI","alphas":["ofi_mc"]}}',
        encoding="utf-8",
    )
    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        require_paper_refs=True,
        require_paper_index_link=True,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert report.passed
    assert report.details["paper_governance"]["passed"] is True


def test_run_gate_a_requires_data_meta_when_enforced(tmp_path: Path):
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)

    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert not report.passed
    assert str(path) in report.details["data_governance"]["missing_data_metadata"]


def test_run_gate_a_data_meta_pass(tmp_path: Path):
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)
    meta = {
        "dataset_id": "feed",
        "source_type": "real",
        "owner": "charlie",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert report.passed
    assert report.details["data_governance"]["passed"] is True


def test_run_gate_a_data_meta_requires_provenance_keys(tmp_path: Path):
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)
    meta = {
        "dataset_id": "feed",
        "source_type": "real",
        "owner": "charlie",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        required_data_provenance_fields=("source", "generator", "seed"),
        data_ul=1,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert not report.passed
    invalid = report.details["data_governance"]["invalid_data_metadata"][str(path)]
    assert "missing_provenance:source" in invalid
    assert "missing_provenance:generator" in invalid
    assert "missing_provenance:seed" in invalid


def test_run_gate_a_data_meta_with_provenance_keys_passes(tmp_path: Path):
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)
    meta = {
        "dataset_id": "feed",
        "source_type": "synthetic",
        "owner": "charlie",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
        "source": "unit_test",
        "generator": "tests",
        "seed": 42,
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        required_data_provenance_fields=("source", "generator", "seed"),
        data_ul=1,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert report.passed
    assert report.details["data_governance"]["invalid_data_metadata"] == {}


def test_run_gate_a_data_ul_reports_achieved_and_missing_fields_warn_only(tmp_path: Path):
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)
    meta = {
        "dataset_id": "feed",
        "source_type": "synthetic",
        "owner": "charlie",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

    manifest = types.SimpleNamespace(
        alpha_id="ofi_mc",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=("120",),
    )
    cfg = ValidationConfig(
        alpha_id="ofi_mc",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        data_ul=3,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
    assert report.passed
    data_gov = report.details["data_governance"]
    assert data_gov["data_ul_target"] == 3
    assert data_gov["data_ul_achieved"] == 2
    assert str(path) in data_gov["data_ul_missing_fields"]
    assert "rng_seed" in data_gov["data_ul_missing_fields"][str(path)]


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

    monkeypatch.setattr("research.backtest.hbt_runner.ResearchBacktestRunner", _FakeRunner)
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

    monkeypatch.setattr("research.backtest.hbt_runner.ResearchBacktestRunner", _FakeRunner)
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
# P0: skills/roles governance in Gate A
# ---------------------------------------------------------------------------


def test_gate_a_skills_governance_warning_when_no_skills(tmp_path):
    """Manifest with empty skills_used → Gate A details include a warning."""
    manifest = types.SimpleNamespace(
        data_fields=(),
        complexity="O(1)",
        skills_used=(),
        roles_used=(),
    )
    report = run_gate_a(manifest, [])
    sg = report.details["skills_governance"]
    assert len(sg["warnings"]) >= 1
    assert any("skills_used" in w for w in sg["warnings"])


def test_gate_a_skills_governance_no_warning_when_skills_set(tmp_path):
    """Manifest with skills_used populated → no skills warning."""
    manifest = types.SimpleNamespace(
        data_fields=(),
        complexity="O(1)",
        skills_used=("iterative-retrieval", "hft-backtester"),
        roles_used=("planner",),
    )
    report = run_gate_a(manifest, [])
    sg = report.details["skills_governance"]
    assert not any("skills_used" in w for w in sg["warnings"])
    assert not any("roles_used" in w for w in sg["warnings"])


def test_gate_a_skills_governance_always_in_details(tmp_path):
    """skills_governance key is always present in Gate A details."""
    manifest = types.SimpleNamespace(
        data_fields=(),
        complexity="O(1)",
    )
    report = run_gate_a(manifest, [])
    assert "skills_governance" in report.details
    sg = report.details["skills_governance"]
    assert "roles_used" in sg
    assert "skills_used" in sg
    assert "warnings" in sg


# ---------------------------------------------------------------------------
# C7: data_ul ValidationConfig + Gate A data_ul_achieved in details
# ---------------------------------------------------------------------------


def test_gate_a_data_ul_achieved_in_details_when_meta_present(tmp_path: Path):
    """Gate A data_governance.data_ul_achieved reflects achieved UL tier from meta.json."""
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)

    # UL4-compliant meta (has regimes_covered but not UL5 fingerprint/lineage)
    meta = {
        "dataset_id": "feed",
        "source_type": "synthetic",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
        "rng_seed": 42,
        "generator_script": "research/tools/synth_lob_gen.py",
        "generator_version": "v1",
        "parameters": {"n_rows": 8},
        "regimes_covered": ["trending", "mean_reverting"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    manifest = types.SimpleNamespace(
        alpha_id="test_alpha",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=(),
    )
    # Request UL3 — should be satisfied because meta has UL4 fields
    cfg = ValidationConfig(
        alpha_id="test_alpha",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        data_ul=3,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)

    dg = report.details["data_governance"]
    assert "data_ul_achieved" in dg
    assert "data_ul_target" in dg
    assert dg["data_ul_target"] == 3
    # Achieved should be at least UL3 (meta has all required UL3 fields)
    assert dg["data_ul_achieved"] >= 3


def test_gate_a_data_ul_warns_when_target_not_met(tmp_path: Path):
    """Gate A emits a warning but does not block when meta does not meet data_ul target."""
    data_root = tmp_path / "research" / "data" / "raw"
    data_root.mkdir(parents=True, exist_ok=True)
    path = data_root / "feed.npy"
    arr = np.zeros(8, dtype=[("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")])
    np.save(path, arr)

    # UL2-only meta — does NOT have UL3 fields
    meta = {
        "dataset_id": "feed",
        "source_type": "real",
        "schema_version": 1,
        "rows": 8,
        "fields": ["best_bid", "best_ask", "qty"],
    }
    path.with_suffix(path.suffix + ".meta.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )

    manifest = types.SimpleNamespace(
        alpha_id="test_alpha",
        data_fields=("bid_px", "ask_px", "trade_vol"),
        complexity="O(1)",
        paper_refs=(),
    )
    # Request UL5 — meta only meets UL2 → should warn (not block)
    cfg = ValidationConfig(
        alpha_id="test_alpha",
        data_paths=[str(path)],
        enforce_data_governance=True,
        require_data_meta=True,
        allowed_data_roots=(str(data_root),),
        data_ul=5,
    )
    report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)

    dg = report.details["data_governance"]
    assert len(dg["warnings"]) >= 1  # warn-only, not blocking
    assert dg["data_ul_target"] == 5
    assert dg["data_ul_achieved"] is not None
    assert dg["data_ul_achieved"] < 5
