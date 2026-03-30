import json
import types
from pathlib import Path

import numpy as np
import pytest

from hft_platform.alpha.validation import (
    ValidationConfig,
    run_gate_a,
    run_gate_b,
)


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
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

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
    path.with_suffix(path.suffix + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")

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


class TestBatchValidate:
    def test_batch_validate_empty_list(self):
        from hft_platform.alpha.validation import batch_validate

        result = batch_validate(alpha_ids=[], data_paths=[], gate="a")
        assert result["total"] == 0
        assert result["results"] == []

    def test_batch_validate_unknown_alpha(self, tmp_path: Path):
        from hft_platform.alpha.validation import batch_validate

        result = batch_validate(
            alpha_ids=["nonexistent_alpha"],
            data_paths=[],
            gate="a",
            project_root=str(tmp_path),
            experiments_dir=str(tmp_path / "experiments"),
        )
        assert result["total"] == 1
        assert result["failed"] == 1
        assert "error" in result["results"][0]


# ---------------------------------------------------------------------------
# Pure helper: _bh_correction
# ---------------------------------------------------------------------------


class TestBhCorrection:
    def test_empty_pvalues_returns_empty_lists(self):
        from hft_platform.alpha.validation import _bh_correction

        reject, adjusted = _bh_correction([], 0.05)
        assert reject == []
        assert adjusted == []

    def test_single_pvalue_below_threshold_rejects(self):
        from hft_platform.alpha.validation import _bh_correction

        reject, adjusted = _bh_correction([0.01], 0.05)
        assert len(reject) == 1
        assert reject[0] is True
        assert len(adjusted) == 1

    def test_all_high_pvalues_no_rejection(self):
        from hft_platform.alpha.validation import _bh_correction

        pvals = [0.9, 0.8, 0.7, 0.6]
        reject, adjusted = _bh_correction(pvals, 0.05)
        assert len(reject) == 4
        assert not any(reject)

    def test_mixed_pvalues_partial_rejection(self):
        from hft_platform.alpha.validation import _bh_correction

        # Low p-values should be rejected
        pvals = [0.001, 0.002, 0.5, 0.9]
        reject, adjusted = _bh_correction(pvals, 0.05)
        assert len(reject) == 4
        # The very small p-values must be rejected
        assert reject[0] is True
        assert reject[1] is True


# ---------------------------------------------------------------------------
# Pure helper: _compute_oos_returns
# ---------------------------------------------------------------------------


class TestComputeOosReturns:
    def test_too_short_returns_empty(self):
        from hft_platform.alpha.validation import _compute_oos_returns

        result = _compute_oos_returns(np.array([1.0, 2.0]), 0.5)
        assert result.size == 0

    def test_normal_equity_curve_produces_returns(self):
        from hft_platform.alpha.validation import _compute_oos_returns

        # Rising equity curve
        curve = np.linspace(1.0, 2.0, 100)
        result = _compute_oos_returns(curve, 0.5)
        assert result.size > 0
        # All returns should be finite and positive for a monotonically rising curve
        assert np.all(np.isfinite(result))
        assert np.all(result >= 0.0)

    def test_zero_base_handled_gracefully(self):
        from hft_platform.alpha.validation import _compute_oos_returns

        # Curve with zero in the denominator
        curve = np.array([0.0, 0.0, 1.0, 2.0, 3.0])
        result = _compute_oos_returns(curve, 0.3)
        # Must not raise; output contains only finite values
        assert np.all(np.isfinite(result))


# ---------------------------------------------------------------------------
# Pure helper: _evaluate_oos_statistical_tests
# ---------------------------------------------------------------------------


class TestEvaluateOosStatisticalTests:
    def test_insufficient_samples_returns_failed(self):
        from hft_platform.alpha.validation import _evaluate_oos_statistical_tests

        few = np.array([0.01] * 10, dtype=np.float64)
        result = _evaluate_oos_statistical_tests(
            few, pvalue_threshold=0.05, min_tests_pass=2, bootstrap_samples=100
        )
        assert result["passed"] is False
        assert result["reason"] == "insufficient_oos_returns"
        assert result["sample_count"] == 10

    def test_positive_returns_likely_passes(self):
        from hft_platform.alpha.validation import _evaluate_oos_statistical_tests

        rng = np.random.default_rng(42)
        positive = rng.normal(0.01, 0.05, size=100)
        result = _evaluate_oos_statistical_tests(
            positive, pvalue_threshold=0.10, min_tests_pass=2, bootstrap_samples=200
        )
        assert "passed" in result
        assert "tests" in result
        assert "sample_count" in result
        assert result["sample_count"] == 100

    def test_result_contains_expected_test_keys(self):
        from hft_platform.alpha.validation import _evaluate_oos_statistical_tests

        rng = np.random.default_rng(99)
        arr = rng.normal(0.0, 0.05, size=60)
        result = _evaluate_oos_statistical_tests(
            arr, pvalue_threshold=0.05, min_tests_pass=2, bootstrap_samples=100
        )
        tests = result["tests"]
        for key in ("ttest_mean_gt_zero", "wilcoxon_gt_zero", "sign_test_gt_half", "bootstrap_ci_mean"):
            assert key in tests, f"Missing test key: {key}"


# ---------------------------------------------------------------------------
# Pure helper: _extract_stat_test_pvalues / _extract_bds_pvalue
# ---------------------------------------------------------------------------


class TestExtractStatTestPvalues:
    def test_empty_dict_returns_empty_list(self):
        from hft_platform.alpha.validation import _extract_stat_test_pvalues

        result = _extract_stat_test_pvalues({})
        assert result == []

    def test_missing_tests_key_returns_empty_list(self):
        from hft_platform.alpha.validation import _extract_stat_test_pvalues

        result = _extract_stat_test_pvalues({"other_key": 42})
        assert result == []

    def test_extracts_pvalue_from_tests(self):
        from hft_platform.alpha.validation import _extract_stat_test_pvalues

        stat_tests = {
            "tests": {
                "ttest_mean_gt_zero": {"pvalue": 0.01, "pass": True},
                "wilcoxon_gt_zero": {"pvalue": 0.02, "pass": True},
                "sign_test_gt_half": {"pvalue": 0.03, "pass": True},
                "bootstrap_ci_mean": {"pvalue": 0.04, "ci_low": 0.001, "ci_high": 0.05, "pass": True},
            }
        }
        result = _extract_stat_test_pvalues(stat_tests)
        assert len(result) == 4
        assert abs(result[0] - 0.01) < 1e-9
        assert abs(result[1] - 0.02) < 1e-9


class TestExtractBdsPvalue:
    def test_missing_tests_returns_none(self):
        from hft_platform.alpha.validation import _extract_bds_pvalue

        assert _extract_bds_pvalue({}) is None

    def test_missing_bds_key_returns_none(self):
        from hft_platform.alpha.validation import _extract_bds_pvalue

        assert _extract_bds_pvalue({"tests": {}}) is None

    def test_valid_bds_returns_pvalue(self):
        from hft_platform.alpha.validation import _extract_bds_pvalue

        stat_tests = {"tests": {"bds_independence": {"pvalue": 0.15, "pass": True}}}
        result = _extract_bds_pvalue(stat_tests)
        assert result is not None
        assert abs(result - 0.15) < 1e-9


# ---------------------------------------------------------------------------
# Pure helper: _optimization_objective
# ---------------------------------------------------------------------------


class TestOptimizationObjective:
    def test_sharpe_oos_mode_returns_sharpe(self):
        from hft_platform.alpha._param_opt import _optimization_objective

        val = _optimization_objective(1.5, 0.05, 2.0, "sharpe_oos")
        assert val == pytest.approx(1.5)

    def test_ic_first_mode_penalizes_turnover(self):
        from hft_platform.alpha._param_opt import _optimization_objective

        val_low_turnover = _optimization_objective(1.0, 0.05, 0.5, "ic_first")
        val_high_turnover = _optimization_objective(1.0, 0.05, 5.0, "ic_first")
        # Higher turnover should give a lower objective
        assert val_low_turnover > val_high_turnover

    def test_default_mode_penalizes_drawdown_and_turnover(self):
        from hft_platform.alpha._param_opt import _optimization_objective

        # Same Sharpe, higher drawdown → lower objective
        val_low_dd = _optimization_objective(1.0, 0.05, 0.5, "risk_adjusted")
        val_high_dd = _optimization_objective(1.0, 0.50, 0.5, "risk_adjusted")
        assert val_low_dd > val_high_dd

    def test_default_mode_applies_turnover_penalty(self):
        from hft_platform.alpha._param_opt import _optimization_objective

        val_low_to = _optimization_objective(1.0, 0.05, 0.5, "default")
        val_high_to = _optimization_objective(1.0, 0.05, 5.0, "default")
        assert val_low_to > val_high_to


# ---------------------------------------------------------------------------
# Pure helper: _run_bds_independence_test
# ---------------------------------------------------------------------------


class TestRunBdsIndependenceTest:
    def test_too_few_samples_returns_unavailable(self):
        from hft_platform.alpha.validation import _run_bds_independence_test

        arr = np.linspace(0, 1, 20)
        result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
        assert result["available"] is False
        assert result["reason"] == "insufficient_samples"
        # Diagnostic-only: treated as pass
        assert result["pass"] is True

    def test_constant_series_returns_unavailable(self):
        from hft_platform.alpha.validation import _run_bds_independence_test

        arr = np.ones(100)
        result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
        assert result["available"] is False
        assert result["reason"] == "constant_series"

    def test_normal_data_returns_result_dict(self):
        from hft_platform.alpha.validation import _run_bds_independence_test

        rng = np.random.default_rng(7)
        arr = rng.normal(0, 1, size=100)
        result = _run_bds_independence_test(arr=arr, pvalue_threshold=0.05)
        assert result["available"] is True
        assert "pvalue" in result
        assert "pass" in result


# ---------------------------------------------------------------------------
# Pure helper: _bds_correlation_delta
# ---------------------------------------------------------------------------


class TestBdsCorrelationDelta:
    def test_too_short_returns_zero(self):
        from hft_platform.alpha.validation import _bds_correlation_delta

        result = _bds_correlation_delta(np.array([1.0, 2.0]), 0.5)
        assert result == 0.0

    def test_iid_series_delta_near_zero(self):
        from hft_platform.alpha.validation import _bds_correlation_delta

        rng = np.random.default_rng(0)
        arr = rng.normal(0, 1, size=200)
        result = _bds_correlation_delta(arr, epsilon=0.5)
        assert np.isfinite(result)


# ---------------------------------------------------------------------------
# Pure helper: _update_manifest_status
# ---------------------------------------------------------------------------


class TestUpdateManifestStatus:
    def test_missing_impl_file_returns_false(self, tmp_path: Path):
        from hft_platform.alpha.validation import _update_manifest_status

        result = _update_manifest_status("nonexistent_alpha", "GATE_A", tmp_path)
        assert result is False

    def test_updates_status_in_impl_file(self, tmp_path: Path):
        from hft_platform.alpha.validation import _update_manifest_status

        alpha_id = "test_alpha"
        impl_dir = tmp_path / "research" / "alphas" / alpha_id
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "impl.py"
        impl_file.write_text(
            "manifest = AlphaManifest(status=AlphaStatus.RESEARCH)\n"
        )

        result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
        assert result is True
        updated_content = impl_file.read_text()
        assert "AlphaStatus.GATE_A" in updated_content

    def test_already_at_target_status_returns_false(self, tmp_path: Path):
        from hft_platform.alpha.validation import _update_manifest_status

        alpha_id = "test_alpha_noop"
        impl_dir = tmp_path / "research" / "alphas" / alpha_id
        impl_dir.mkdir(parents=True)
        impl_file = impl_dir / "impl.py"
        impl_file.write_text(
            "manifest = AlphaManifest(status=AlphaStatus.GATE_A)\n"
        )

        result = _update_manifest_status(alpha_id, "GATE_A", tmp_path)
        assert result is False


# ---------------------------------------------------------------------------
# Pure helper: _check_hftbacktest_v2_data_format
# ---------------------------------------------------------------------------


class TestCheckHftbacktestV2DataFormat:
    def test_non_npz_file_reports_error(self, tmp_path: Path):
        from hft_platform.alpha.validation import _check_hftbacktest_v2_data_format

        path = tmp_path / "data.npy"
        arr = np.array([1, 2, 3], dtype=np.int64)
        np.save(str(path), arr)

        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("not a .npz" in e.lower() or ".npz" in e for e in errors)

    def test_valid_npz_with_required_fields_no_errors(self, tmp_path: Path):
        from hft_platform.alpha.validation import _check_hftbacktest_v2_data_format

        path = tmp_path / "data.npz"
        dt = np.dtype([("exch_ts", "<i8"), ("local_ts", "<i8"), ("ev", "<i8")])
        arr = np.zeros(5, dtype=dt)
        np.savez(str(path), data=arr)

        errors = _check_hftbacktest_v2_data_format(str(path))
        # May have DEPTH_SNAPSHOT_EVENT warning if hftbacktest not installed, but no structural errors
        structural = [e for e in errors if "Missing required field" in e or "not a .npz" in e.lower()]
        assert structural == []

    def test_npz_missing_data_key_reports_error(self, tmp_path: Path):
        from hft_platform.alpha.validation import _check_hftbacktest_v2_data_format

        path = tmp_path / "missing_data.npz"
        np.savez(str(path), other_key=np.array([1, 2, 3]))

        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("Missing 'data'" in e for e in errors)


# ---------------------------------------------------------------------------
# Pure helper: _field_available
# ---------------------------------------------------------------------------


class TestFieldAvailable:
    def test_direct_field_match(self):
        from hft_platform.alpha.validation import _field_available

        assert _field_available("best_bid", {"best_bid", "best_ask"}) is True

    def test_current_mid_via_best_bid_ask(self):
        from hft_platform.alpha.validation import _field_available

        assert _field_available("current_mid", {"best_bid", "best_ask"}) is True

    def test_current_mid_via_bid_px_ask_px(self):
        from hft_platform.alpha.validation import _field_available

        assert _field_available("current_mid", {"bid_px", "ask_px"}) is True

    def test_current_mid_missing_fields_returns_false(self):
        from hft_platform.alpha.validation import _field_available

        assert _field_available("current_mid", {"volume"}) is False

    def test_missing_field_not_in_available(self):
        from hft_platform.alpha.validation import _field_available

        assert _field_available("some_unknown_field", {"price", "volume"}) is False


# ---------------------------------------------------------------------------
# Pure helper: _dataset_row_count
# ---------------------------------------------------------------------------


class TestDatasetRowCount:
    def test_npy_file_returns_correct_count(self, tmp_path: Path):
        from hft_platform.alpha.validation import _dataset_row_count

        path = tmp_path / "data.npy"
        arr = np.zeros((50, 3), dtype=np.float64)
        np.save(str(path), arr)

        count = _dataset_row_count(path)
        assert count == 50

    def test_npz_file_with_data_key_returns_count(self, tmp_path: Path):
        from hft_platform.alpha.validation import _dataset_row_count

        path = tmp_path / "data.npz"
        arr = np.zeros(30, dtype=np.float64)
        np.savez(str(path), data=arr)

        count = _dataset_row_count(path)
        assert count == 30

    def test_npz_file_fallback_to_first_key(self, tmp_path: Path):
        from hft_platform.alpha.validation import _dataset_row_count

        path = tmp_path / "data.npz"
        arr = np.zeros(15, dtype=np.float64)
        np.savez(str(path), other=arr)

        count = _dataset_row_count(path)
        assert count == 15
