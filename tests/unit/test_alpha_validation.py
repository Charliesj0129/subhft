import json
import types
from pathlib import Path

import numpy as np

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
