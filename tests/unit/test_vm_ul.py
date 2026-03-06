from __future__ import annotations

from research.tools.vm_ul import UL_REQUIRED_FIELDS, DataUL, infer_data_ul, validate_meta_ul


def test_data_ul_enum_values() -> None:
    assert DataUL.UL1.value == 1
    assert DataUL.UL6.value == 6


def test_validate_meta_ul_happy_path() -> None:
    meta = {
        "dataset_id": "x",
        "source_type": "synthetic",
        "schema_version": 1,
        "rows": 100,
        "fields": ["mid"],
    }
    ok, missing = validate_meta_ul(meta, DataUL.UL2)
    assert ok is True
    assert missing == []


def test_validate_meta_ul_missing_fields_for_ul3() -> None:
    meta = {
        "dataset_id": "x",
        "source_type": "synthetic",
        "schema_version": 1,
        "rows": 100,
        "fields": ["mid"],
    }
    ok, missing = validate_meta_ul(meta, DataUL.UL3)
    assert ok is False
    assert "rng_seed" in missing
    assert "generator_script" in missing
    assert "generator_version" in missing
    assert "parameters" in missing


def test_validate_meta_ul_cumulative_required_fields() -> None:
    required = UL_REQUIRED_FIELDS[DataUL.UL5]
    assert "dataset_id" in required
    assert "rng_seed" in required
    assert "regimes_covered" in required
    assert "data_fingerprint" in required
    assert "lineage" in required


def test_infer_data_ul_reports_highest_satisfied_level() -> None:
    meta = {
        "dataset_id": "x",
        "source_type": "synthetic",
        "schema_version": 1,
        "rows": 100,
        "fields": ["mid"],
        "rng_seed": 42,
        "generator_script": "gen.py",
        "generator_version": "v1",
        "parameters": {"n_rows": 100},
        "regimes_covered": ["volatile"],
    }
    assert infer_data_ul(meta) == DataUL.UL4
