"""Integration tests for feature schema and backend parity."""

from __future__ import annotations

from typing import Any

from hft_platform.feature.parity import (
    check_backend_parity,
    check_schema_parity,
)
from hft_platform.feature.registry import (
    FEATURE_SET_VERSION,
    build_default_lob_feature_set_v1,
    default_feature_registry,
)

# ---------------------------------------------------------------------------
# check_backend_parity
# ---------------------------------------------------------------------------


class TestCheckBackendParity:
    def test_identical_values_pass(self) -> None:
        py = {"feat_a": 100, "feat_b": 200}
        rs = {"feat_a": 100, "feat_b": 200}
        report = check_backend_parity(py, rs)
        assert report.passed is True
        assert report.mismatch_count == 0
        assert report.checked == 2

    def test_value_mismatch_detected(self) -> None:
        py = {"feat_a": 100.0, "feat_b": 200.0}
        rs = {"feat_a": 999.0, "feat_b": 200.0}
        report = check_backend_parity(py, rs, abs_tolerance=1.0)
        assert report.passed is False
        assert report.mismatch_count == 1
        assert report.mismatches[0].feature_id == "feat_a"

    def test_within_tolerance_passes(self) -> None:
        py = {"feat_a": 100.0}
        rs = {"feat_a": 100.0000001}
        report = check_backend_parity(py, rs, abs_tolerance=1e-4, rel_tolerance=1e-4)
        assert report.passed is True

    def test_missing_in_rust(self) -> None:
        py = {"feat_a": 1, "feat_b": 2}
        rs = {"feat_a": 1}
        report = check_backend_parity(py, rs)
        assert report.passed is False
        mismatch_ids = [m.feature_id for m in report.mismatches]
        assert "feat_b" in mismatch_ids
        assert "MISSING in rust" in report.mismatches[0].detail

    def test_missing_in_python(self) -> None:
        py = {"feat_a": 1}
        rs = {"feat_a": 1, "feat_b": 2}
        report = check_backend_parity(py, rs)
        assert report.passed is False

    def test_both_none_skipped(self) -> None:
        py = {"feat_a": None}
        rs = {"feat_a": None}
        report = check_backend_parity(py, rs)
        assert report.passed is True
        assert "feat_a" in report.skipped

    def test_non_numeric_mismatch(self) -> None:
        py = {"feat_a": "foo"}
        rs = {"feat_a": "bar"}
        report = check_backend_parity(py, rs)
        assert report.passed is False
        assert "non-numeric" in report.mismatches[0].detail

    def test_non_numeric_match(self) -> None:
        py = {"feat_a": "foo"}
        rs = {"feat_a": "foo"}
        report = check_backend_parity(py, rs)
        assert report.passed is True

    def test_to_dict_structure(self) -> None:
        py = {"feat_a": 1}
        rs = {"feat_a": 1}
        report = check_backend_parity(py, rs)
        d = report.to_dict()
        assert "passed" in d
        assert "mismatch_count" in d
        assert "mismatches" in d
        assert "skipped" in d


# ---------------------------------------------------------------------------
# check_schema_parity
# ---------------------------------------------------------------------------


class TestCheckSchemaParity:
    def _make_schema(
        self,
        fsid: str = "lob_shared_v1",
        schema_version: int = 1,
        features: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if features is None:
            features = [
                {"feature_id": "best_bid", "dtype": "i64", "scale": 10000, "source_kind": "book"},
                {"feature_id": "best_ask", "dtype": "i64", "scale": 10000, "source_kind": "book"},
            ]
        return {
            "default": fsid,
            "feature_sets": {
                fsid: {
                    "schema_version": schema_version,
                    "features": features,
                }
            },
        }

    def test_identical_schemas_pass(self) -> None:
        schema = self._make_schema()
        report = check_schema_parity(schema, schema)
        assert report.passed is True

    def test_schema_version_mismatch(self) -> None:
        reg = self._make_schema(schema_version=1)
        live = self._make_schema(schema_version=2)
        report = check_schema_parity(reg, live)
        assert report.passed is False
        detail_texts = " ".join(m.detail for m in report.mismatches)
        assert "schema_version" in detail_texts

    def test_missing_feature_in_live(self) -> None:
        features_full = [
            {"feature_id": "best_bid", "dtype": "i64", "scale": 10000, "source_kind": "book"},
            {"feature_id": "best_ask", "dtype": "i64", "scale": 10000, "source_kind": "book"},
        ]
        features_partial = [
            {"feature_id": "best_bid", "dtype": "i64", "scale": 10000, "source_kind": "book"},
        ]
        reg = self._make_schema(features=features_full)
        live = self._make_schema(features=features_partial)
        report = check_schema_parity(reg, live)
        assert report.passed is False
        fids = [m.feature_id for m in report.mismatches]
        assert any("best_ask" in fid for fid in fids)

    def test_dtype_mismatch(self) -> None:
        features_reg = [{"feature_id": "best_bid", "dtype": "i64", "scale": 10000, "source_kind": "book"}]
        features_live = [{"feature_id": "best_bid", "dtype": "f64", "scale": 10000, "source_kind": "book"}]
        reg = self._make_schema(features=features_reg)
        live = self._make_schema(features=features_live)
        report = check_schema_parity(reg, live)
        assert report.passed is False
        assert any("dtype" in m.detail for m in report.mismatches)

    def test_missing_feature_set_in_live(self) -> None:
        reg = self._make_schema(fsid="lob_v1")
        live: dict[str, Any] = {"default": None, "feature_sets": {}}
        report = check_schema_parity(reg, live)
        assert report.passed is False


# ---------------------------------------------------------------------------
# Integration: default feature registry parity with itself
# ---------------------------------------------------------------------------


class TestDefaultRegistryParity:
    def test_default_registry_self_parity(self) -> None:
        """The default registry should be parity-identical with itself."""
        reg = default_feature_registry()
        schema = reg.to_dict()
        report = check_schema_parity(schema, schema)
        assert report.passed is True
        assert report.mismatch_count == 0

    def test_feature_set_version_constant(self) -> None:
        """FEATURE_SET_VERSION constant must match the default feature_set_id."""
        fs = build_default_lob_feature_set_v1()
        assert fs.feature_set_id == FEATURE_SET_VERSION

    def test_backend_parity_all_zeros(self) -> None:
        """Python and Rust backends returning identical zeros should pass."""
        reg = default_feature_registry()
        fs = reg.get_default()
        # Simulate both backends returning zero for all features
        py_vals = {spec.feature_id: 0 for spec in fs.features}
        rs_vals = {spec.feature_id: 0 for spec in fs.features}
        report = check_backend_parity(py_vals, rs_vals)
        assert report.passed is True
        assert report.checked == len(fs.features)
