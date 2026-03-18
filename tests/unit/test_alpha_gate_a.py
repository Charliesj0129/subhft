"""Unit tests for hft_platform.alpha._gate_a — Gate A validation logic.

Tests cover: field resolution with aliases, complexity validation, precision
warnings, paper governance, data governance, skills governance, data format
checks (hftbacktest V2), and edge cases.
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import numpy as np

from hft_platform.alpha._gate_a import (
    _check_hftbacktest_v2_data_format,
    _field_available,
    _load_data_fields,
    _load_dataset_metadata,
    _load_paper_index,
    _resolve_paper_ref,
    _validate_dataset_metadata,
    run_gate_a,
)
from hft_platform.alpha._validation_types import GateReport, ValidationConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_structured_npy(path: Path, fields: list[tuple[str, str]], n: int = 8) -> Path:
    """Create a structured .npy file with given dtype fields."""
    dtype = np.dtype(fields)
    arr = np.zeros(n, dtype=dtype)
    np.save(path, arr)
    return path


def _make_structured_npz(path: Path, fields: list[tuple[str, str]], n: int = 8) -> Path:
    """Create a structured .npz file with a 'data' key."""
    dtype = np.dtype(fields)
    arr = np.zeros(n, dtype=dtype)
    np.savez(path, data=arr)
    return path


def _simple_manifest(**kwargs):
    defaults = {
        "data_fields": (),
        "complexity": "O(1)",
    }
    defaults.update(kwargs)
    return types.SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# _field_available
# ---------------------------------------------------------------------------


class TestFieldAvailable:
    def test_exact_match(self):
        assert _field_available("best_bid", {"best_bid", "best_ask"})

    def test_alias_bid_px(self):
        assert _field_available("bid_px", {"best_bid"})

    def test_alias_ask_px(self):
        assert _field_available("ask_px", {"ask_price"})

    def test_alias_bid_qty(self):
        assert _field_available("bid_qty", {"bid_depth"})

    def test_alias_ask_qty(self):
        assert _field_available("ask_qty", {"ask_size"})

    def test_alias_trade_vol(self):
        assert _field_available("trade_vol", {"qty"})
        assert _field_available("trade_vol", {"volume"})

    def test_current_mid_from_bid_ask(self):
        assert _field_available("current_mid", {"best_bid", "best_ask"})

    def test_current_mid_from_bid_px_ask_px(self):
        assert _field_available("current_mid", {"bid_px", "ask_px"})

    def test_current_mid_direct(self):
        assert _field_available("current_mid", {"current_mid"})

    def test_current_mid_alias(self):
        assert _field_available("current_mid", {"mid"})

    def test_missing_field(self):
        assert not _field_available("nonexistent", {"best_bid"})

    def test_empty_available(self):
        assert not _field_available("bid_px", set())


# ---------------------------------------------------------------------------
# _load_data_fields
# ---------------------------------------------------------------------------


class TestLoadDataFields:
    def test_loads_npy_structured(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "test.npy",
            [("price", "i8"), ("volume", "f8")],
        )
        fields = _load_data_fields(str(path))
        assert fields == {"price", "volume"}

    def test_loads_npz_structured(self, tmp_path: Path):
        path = _make_structured_npz(
            tmp_path / "test.npz",
            [("price", "i8"), ("qty", "f8")],
        )
        fields = _load_data_fields(str(path))
        assert fields == {"price", "qty"}

    def test_unstructured_npy_returns_empty(self, tmp_path: Path):
        path = tmp_path / "plain.npy"
        np.save(path, np.zeros(10))
        fields = _load_data_fields(str(path))
        assert fields == set()

    def test_npz_without_data_key_returns_empty(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        np.savez(path, other=np.zeros(5))
        fields = _load_data_fields(str(path))
        assert fields == set()


# ---------------------------------------------------------------------------
# _check_hftbacktest_v2_data_format
# ---------------------------------------------------------------------------


class TestCheckHftbacktestV2DataFormat:
    def test_non_npz_file_reports_error(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "test.npy",
            [("exch_ts", "i8"), ("local_ts", "i8"), ("ev", "i4")],
        )
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("not a .npz" in e for e in errors)

    def test_npz_missing_data_key(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        np.savez(path, other=np.zeros(5))
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("Missing 'data'" in e for e in errors)

    def test_unstructured_npz(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        np.savez(path, data=np.zeros(5))
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("structured array" in e for e in errors)

    def test_wrong_timestamp_dtype(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        arr = np.zeros(5, dtype=[("exch_ts", "f8"), ("local_ts", "i8"), ("ev", "i4")])
        np.savez(path, data=arr)
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("exch_ts" in e and "int64" in e for e in errors)

    def test_missing_required_fields(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        arr = np.zeros(5, dtype=[("price", "i8")])
        np.savez(path, data=arr)
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("exch_ts" in e for e in errors)
        assert any("local_ts" in e for e in errors)
        assert any("ev" in e for e in errors)

    def test_ev_non_integer_type(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        arr = np.zeros(5, dtype=[("exch_ts", "i8"), ("local_ts", "i8"), ("ev", "f8")])
        np.savez(path, data=arr)
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("ev" in e and "integer" in e for e in errors)

    def test_valid_format_no_errors(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        arr = np.zeros(5, dtype=[("exch_ts", "i8"), ("local_ts", "i8"), ("ev", "i4"), ("px", "i8")])
        np.savez(path, data=arr)
        errors = _check_hftbacktest_v2_data_format(str(path))
        # May still have errors about first event not being DEPTH_SNAPSHOT_EVENT,
        # but no structural errors should be present.
        structural = [e for e in errors if "int64" in e or "structured" in e or "Missing" in e]
        assert structural == []

    def test_corrupt_file(self, tmp_path: Path):
        path = tmp_path / "test.npz"
        path.write_bytes(b"not a numpy file")
        errors = _check_hftbacktest_v2_data_format(str(path))
        assert any("Failed to load" in e for e in errors)


# ---------------------------------------------------------------------------
# _load_paper_index / _resolve_paper_ref
# ---------------------------------------------------------------------------


class TestPaperIndex:
    def test_load_paper_index_missing_dir(self, tmp_path: Path):
        result = _load_paper_index(tmp_path)
        assert result == {}

    def test_load_paper_index_none_root(self):
        result = _load_paper_index(None)
        assert result == {}

    def test_load_paper_index_valid(self, tmp_path: Path):
        index_path = tmp_path / "research" / "knowledge" / "paper_index.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text('{"120": {"title": "OFI", "alphas": ["ofi_mc"]}}')
        result = _load_paper_index(tmp_path)
        assert "120" in result

    def test_load_paper_index_invalid_json(self, tmp_path: Path):
        index_path = tmp_path / "research" / "knowledge" / "paper_index.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text("not json")
        result = _load_paper_index(tmp_path)
        assert result == {}

    def test_load_paper_index_non_dict(self, tmp_path: Path):
        index_path = tmp_path / "research" / "knowledge" / "paper_index.json"
        index_path.parent.mkdir(parents=True)
        index_path.write_text("[1, 2, 3]")
        result = _load_paper_index(tmp_path)
        assert result == {}

    def test_resolve_paper_ref_direct_key(self):
        index = {"120": {"title": "OFI", "alphas": ["ofi_mc"]}}
        key, row = _resolve_paper_ref("120", index)
        assert key == "120"
        assert row is not None

    def test_resolve_paper_ref_by_arxiv_id(self):
        index = {"120": {"arxiv_id": "2408.03594", "alphas": ["ofi_mc"]}}
        key, row = _resolve_paper_ref("2408.03594", index)
        assert key == "120"

    def test_resolve_paper_ref_not_found(self):
        index = {"120": {"arxiv_id": "2408.03594", "alphas": ["ofi_mc"]}}
        key, row = _resolve_paper_ref("9999.99999", index)
        assert key is None
        assert row is None

    def test_resolve_paper_ref_skips_non_dict_rows(self):
        index = {"120": "not_a_dict", "121": {"arxiv_id": "123", "alphas": []}}
        key, row = _resolve_paper_ref("123", index)
        assert key == "121"


# ---------------------------------------------------------------------------
# _load_dataset_metadata / _validate_dataset_metadata
# ---------------------------------------------------------------------------


class TestDatasetMetadata:
    def test_load_missing_metadata(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        payload, meta_path, error = _load_dataset_metadata(data_path)
        assert payload is None
        assert error == "missing_meta_file"

    def test_load_valid_metadata(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {"dataset_id": "test", "source_type": "synthetic"}
        (data_path.with_suffix(data_path.suffix + ".meta.json")).write_text(json.dumps(meta))
        payload, meta_path, error = _load_dataset_metadata(data_path)
        assert payload == meta
        assert error is None

    def test_load_invalid_json_metadata(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        (data_path.with_suffix(data_path.suffix + ".meta.json")).write_text("not json")
        payload, meta_path, error = _load_dataset_metadata(data_path)
        assert payload is None
        assert "invalid_json" in error

    def test_load_non_dict_metadata(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        (data_path.with_suffix(data_path.suffix + ".meta.json")).write_text("[1]")
        payload, meta_path, error = _load_dataset_metadata(data_path)
        assert payload is None
        assert error == "invalid_format"

    def test_validate_metadata_all_present(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        arr = np.zeros(10, dtype=[("px", "i8")])
        np.save(data_path, arr)
        meta = {
            "dataset_id": "test",
            "source_type": "synthetic",
            "owner": "alice",
            "schema_version": 1,
            "rows": 10,
            "fields": ["px"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert problems == []

    def test_validate_metadata_missing_keys(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        problems = _validate_dataset_metadata({}, data_path)
        assert any("missing:dataset_id" in p for p in problems)
        assert any("missing:source_type" in p for p in problems)
        assert any("missing:owner" in p for p in problems)

    def test_validate_metadata_invalid_source_type(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {
            "dataset_id": "t",
            "source_type": "invalid",
            "owner": "x",
            "schema_version": 1,
            "rows": 5,
            "fields": ["a"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert "source_type_must_be_synthetic_or_real" in problems

    def test_validate_metadata_schema_version_zero(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {
            "dataset_id": "t",
            "source_type": "real",
            "owner": "x",
            "schema_version": 0,
            "rows": 5,
            "fields": ["a"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert "schema_version_must_be>=1" in problems

    def test_validate_metadata_rows_mismatch(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        arr = np.zeros(10, dtype=[("px", "i8")])
        np.save(data_path, arr)
        meta = {
            "dataset_id": "t",
            "source_type": "real",
            "owner": "x",
            "schema_version": 1,
            "rows": 99,
            "fields": ["px"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert any("rows_mismatch" in p for p in problems)

    def test_validate_metadata_empty_fields(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {
            "dataset_id": "t",
            "source_type": "real",
            "owner": "x",
            "schema_version": 1,
            "rows": 5,
            "fields": [],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert "fields_must_be_nonempty_list" in problems

    def test_validate_metadata_rows_not_int(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {
            "dataset_id": "t",
            "source_type": "real",
            "owner": "x",
            "schema_version": 1,
            "rows": "many",
            "fields": ["a"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert "rows_not_int" in problems

    def test_validate_metadata_schema_version_not_int(self, tmp_path: Path):
        data_path = tmp_path / "test.npy"
        np.save(data_path, np.zeros(5))
        meta = {
            "dataset_id": "t",
            "source_type": "real",
            "owner": "x",
            "schema_version": "abc",
            "rows": 5,
            "fields": ["a"],
        }
        problems = _validate_dataset_metadata(meta, data_path)
        assert "schema_version_not_int" in problems


# ---------------------------------------------------------------------------
# run_gate_a — integration-style tests
# ---------------------------------------------------------------------------


class TestRunGateA:
    def test_passes_basic(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "feed.npy",
            [("best_bid", "i8"), ("best_ask", "i8"), ("qty", "f8")],
        )
        manifest = _simple_manifest(
            data_fields=("bid_px", "ask_px", "trade_vol"),
            complexity="O(1)",
        )
        report = run_gate_a(manifest, [str(path)])
        assert isinstance(report, GateReport)
        assert report.passed
        assert report.gate == "Gate A"

    def test_fails_bad_complexity(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "feed.npy",
            [("best_bid", "i8"), ("best_ask", "i8")],
        )
        manifest = _simple_manifest(
            data_fields=("bid_px",),
            complexity="O(N^2)",
        )
        report = run_gate_a(manifest, [str(path)])
        assert not report.passed
        assert report.details["complexity_ok"] is False

    def test_complexity_on_accepted(self, tmp_path: Path):
        for cplx in ("O(1)", "O(N)", "O1", "ON", "o(1)", "o(n)"):
            path = _make_structured_npy(tmp_path / f"feed_{cplx}.npy", [("px", "i8")])
            manifest = _simple_manifest(data_fields=(), complexity=cplx)
            report = run_gate_a(manifest, [str(path)])
            assert report.details["complexity_ok"], f"Expected complexity_ok for {cplx}"

    def test_precision_warnings_for_price_field(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("price", "i8")])
        manifest = _simple_manifest(data_fields=("price",), complexity="O(1)")
        report = run_gate_a(manifest, [str(path)])
        assert len(report.details["precision_warnings"]) >= 1
        assert "price" in report.details["precision_warnings"][0]

    def test_no_precision_warning_for_price_diff(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("price_diff", "i8")])
        manifest = _simple_manifest(data_fields=("price_diff",), complexity="O(1)")
        report = run_gate_a(manifest, [str(path)])
        assert report.details["precision_warnings"] == []

    def test_no_data_paths_with_required_fields(self):
        manifest = _simple_manifest(data_fields=("bid_px",), complexity="O(1)")
        report = run_gate_a(manifest, [])
        assert not report.passed
        assert "<no_data_paths>" in report.details["missing_fields_by_path"]

    def test_no_data_paths_no_required_fields(self):
        manifest = _simple_manifest(data_fields=(), complexity="O(1)")
        report = run_gate_a(manifest, [])
        assert report.passed

    def test_paper_refs_enforcement_blocks(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("px", "i8")])
        manifest = _simple_manifest(
            alpha_id="test_alpha",
            data_fields=(),
            complexity="O(1)",
            paper_refs=(),
        )
        cfg = ValidationConfig(
            alpha_id="test_alpha",
            data_paths=[str(path)],
            require_paper_refs=True,
        )
        report = run_gate_a(manifest, [str(path)], config=cfg)
        assert not report.passed
        assert report.details["paper_governance"]["paper_ref_missing"]

    def test_paper_ref_unresolved_blocks(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("px", "i8")])
        # Create empty paper index
        idx_path = tmp_path / "research" / "knowledge" / "paper_index.json"
        idx_path.parent.mkdir(parents=True)
        idx_path.write_text("{}")
        manifest = _simple_manifest(
            alpha_id="test_alpha",
            data_fields=(),
            complexity="O(1)",
            paper_refs=("999",),
        )
        cfg = ValidationConfig(
            alpha_id="test_alpha",
            data_paths=[str(path)],
            require_paper_refs=True,
            require_paper_index_link=True,
        )
        report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
        assert not report.passed
        assert "999" in report.details["paper_governance"]["unresolved_paper_refs"]

    def test_paper_ref_unmapped_alpha_blocks(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("px", "i8")])
        idx_path = tmp_path / "research" / "knowledge" / "paper_index.json"
        idx_path.parent.mkdir(parents=True)
        idx_path.write_text('{"120": {"title": "OFI", "alphas": ["other_alpha"]}}')
        manifest = _simple_manifest(
            alpha_id="my_alpha",
            data_fields=(),
            complexity="O(1)",
            paper_refs=("120",),
        )
        cfg = ValidationConfig(
            alpha_id="my_alpha",
            data_paths=[str(path)],
            require_paper_refs=True,
            require_paper_index_link=True,
        )
        report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
        assert not report.passed
        assert "120" in report.details["paper_governance"]["unmapped_paper_refs"]

    def test_data_governance_invalid_root(self, tmp_path: Path):
        outside = tmp_path / "outside"
        outside.mkdir()
        path = _make_structured_npy(outside / "feed.npy", [("px", "i8")])
        manifest = _simple_manifest(data_fields=(), complexity="O(1)")
        cfg = ValidationConfig(
            alpha_id="test",
            data_paths=[str(path)],
            enforce_data_governance=True,
            allowed_data_roots=("research/data/raw",),
        )
        report = run_gate_a(manifest, [str(path)], config=cfg, root=tmp_path)
        assert not report.passed
        assert len(report.details["data_governance"]["invalid_data_roots"]) > 0

    def test_skills_governance_empty_warns(self):
        manifest = _simple_manifest(
            data_fields=(),
            complexity="O(1)",
            skills_used=(),
            roles_used=(),
        )
        report = run_gate_a(manifest, [])
        sg = report.details["skills_governance"]
        assert len(sg["warnings"]) >= 2

    def test_skills_governance_populated_no_warns(self):
        manifest = _simple_manifest(
            data_fields=(),
            complexity="O(1)",
            skills_used=("iterative-retrieval",),
            roles_used=("planner",),
        )
        report = run_gate_a(manifest, [])
        sg = report.details["skills_governance"]
        skills_role_warns = [w for w in sg["warnings"] if "skills_used" in w or "roles_used" in w]
        assert skills_role_warns == []

    def test_hftbacktest_v2_format_check_with_enforce(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "feed.npy",
            [("px", "i8")],
        )
        manifest = _simple_manifest(data_fields=(), complexity="O(1)")
        cfg = ValidationConfig(
            alpha_id="test",
            data_paths=[str(path)],
            enforce_data_governance=True,
        )
        report = run_gate_a(manifest, [str(path)], config=cfg)
        # V2 format errors are advisory (non-blocking in Gate A)
        assert "invalid_data_formats" in report.details["data_governance"]

    def test_complexity_with_spaces_accepted(self, tmp_path: Path):
        path = _make_structured_npy(tmp_path / "feed.npy", [("px", "i8")])
        manifest = _simple_manifest(data_fields=(), complexity="O (N)")
        report = run_gate_a(manifest, [str(path)])
        assert report.details["complexity_ok"]

    def test_multiple_price_fields_emit_warnings(self, tmp_path: Path):
        path = _make_structured_npy(
            tmp_path / "feed.npy",
            [("entry_price", "i8"), ("exit_price", "i8")],
        )
        manifest = _simple_manifest(
            data_fields=("entry_price", "exit_price"),
            complexity="O(1)",
        )
        report = run_gate_a(manifest, [str(path)])
        assert len(report.details["precision_warnings"]) == 2

    def test_available_fields_union_across_paths(self, tmp_path: Path):
        path1 = _make_structured_npy(tmp_path / "a.npy", [("field_a", "i8")])
        path2 = _make_structured_npy(tmp_path / "b.npy", [("field_b", "i8")])
        manifest = _simple_manifest(data_fields=(), complexity="O(1)")
        report = run_gate_a(manifest, [str(path1), str(path2)])
        assert "field_a" in report.details["available_fields"]
        assert "field_b" in report.details["available_fields"]
