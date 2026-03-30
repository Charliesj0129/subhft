"""Unit tests for hft_platform.alpha._validation_helpers.

Covers all public helper functions targeting untested branches.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from hft_platform.alpha._validation_helpers import (
    _dataset_metadata_candidates,
    _dataset_row_count,
    _ensure_project_root_on_path,
    _has_hftbt_data,
    _load_dataset_metadata,
    _make_validation_artifact_dir,
    _missing_or_blank_metadata_keys,
    _path_under_any,
    _pushd,
    _resolve_allowed_data_roots,
    _resolve_data_path,
    _resolve_first_data_meta_path,
    _validate_alpha_id,
    _write_json,
)


# ---------------------------------------------------------------------------
# _validate_alpha_id
# ---------------------------------------------------------------------------


def test_validate_alpha_id_valid():
    # Should not raise — valid lowercase alpha_id with underscore
    result = _validate_alpha_id("ofi_mc")
    assert result is None


def test_validate_alpha_id_valid_with_numbers():
    result = _validate_alpha_id("a1b2c3")
    assert result is None


def test_validate_alpha_id_starts_with_digit_raises():
    with pytest.raises(ValueError, match="Invalid alpha_id"):
        _validate_alpha_id("1ofi")


def test_validate_alpha_id_uppercase_raises():
    with pytest.raises(ValueError, match="Invalid alpha_id"):
        _validate_alpha_id("OFI")


def test_validate_alpha_id_empty_raises():
    with pytest.raises(ValueError, match="Invalid alpha_id"):
        _validate_alpha_id("")


def test_validate_alpha_id_too_long_raises():
    # max 64 chars: 1 start + 63 rest = 64 total
    long_id = "a" * 65
    with pytest.raises(ValueError, match="Invalid alpha_id"):
        _validate_alpha_id(long_id)


def test_validate_alpha_id_max_length_allowed():
    # Exactly 64 chars is valid — should not raise
    max_id = "a" + "b" * 63
    result = _validate_alpha_id(max_id)
    assert result is None


def test_validate_alpha_id_non_string_raises():
    with pytest.raises(ValueError, match="Invalid alpha_id"):
        _validate_alpha_id(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _resolve_data_path
# ---------------------------------------------------------------------------


def test_resolve_data_path_absolute_unchanged(tmp_path):
    p = str(tmp_path / "data.npy")
    result = _resolve_data_path(tmp_path, p)
    assert result == p


def test_resolve_data_path_relative_joined_to_root(tmp_path):
    result = _resolve_data_path(tmp_path, "data/file.npy")
    assert result == str((tmp_path / "data" / "file.npy").resolve())


# ---------------------------------------------------------------------------
# _resolve_allowed_data_roots
# ---------------------------------------------------------------------------


def _make_validation_config(allowed_data_roots=()) -> "ValidationConfig":
    from hft_platform.alpha._validation_types import ValidationConfig
    return ValidationConfig(
        alpha_id="test_alpha",
        data_paths=[],
        allowed_data_roots=tuple(allowed_data_roots),
    )


def test_resolve_allowed_data_roots_none_inputs():
    result = _resolve_allowed_data_roots(None, None)
    assert result == []


def test_resolve_allowed_data_roots_none_root():
    cfg = _make_validation_config()
    result = _resolve_allowed_data_roots(None, cfg)
    assert result == []


def test_resolve_allowed_data_roots_absolute_path(tmp_path):
    cfg = _make_validation_config(allowed_data_roots=[str(tmp_path)])
    result = _resolve_allowed_data_roots(tmp_path, cfg)
    assert str(tmp_path) in result


def test_resolve_allowed_data_roots_relative_path(tmp_path):
    cfg = _make_validation_config(allowed_data_roots=["research/data"])
    result = _resolve_allowed_data_roots(tmp_path, cfg)
    assert str((tmp_path / "research" / "data").resolve()) in result


def test_resolve_allowed_data_roots_skips_blank_entries(tmp_path):
    cfg = _make_validation_config(allowed_data_roots=["", "  "])
    result = _resolve_allowed_data_roots(tmp_path, cfg)
    assert result == []


# ---------------------------------------------------------------------------
# _path_under_any
# ---------------------------------------------------------------------------


def test_path_under_any_exact_match(tmp_path):
    assert _path_under_any(tmp_path, [tmp_path]) is True


def test_path_under_any_child_path(tmp_path):
    child = tmp_path / "subdir" / "file.npy"
    assert _path_under_any(child, [tmp_path]) is True


def test_path_under_any_outside_all_roots(tmp_path):
    other = Path(tempfile.mkdtemp())
    assert _path_under_any(other, [tmp_path]) is False


def test_path_under_any_empty_roots(tmp_path):
    assert _path_under_any(tmp_path, []) is False


# ---------------------------------------------------------------------------
# _missing_or_blank_metadata_keys
# ---------------------------------------------------------------------------


def test_missing_metadata_key_absent():
    result = _missing_or_blank_metadata_keys({}, ("symbol",))
    assert "symbol" in result


def test_missing_metadata_key_none_value():
    result = _missing_or_blank_metadata_keys({"symbol": None}, ("symbol",))
    assert "symbol" in result


def test_missing_metadata_key_blank_string():
    result = _missing_or_blank_metadata_keys({"symbol": "  "}, ("symbol",))
    assert "symbol" in result


def test_missing_metadata_key_empty_list():
    result = _missing_or_blank_metadata_keys({"fields": []}, ("fields",))
    assert "fields" in result


def test_missing_metadata_key_empty_dict():
    result = _missing_or_blank_metadata_keys({"meta": {}}, ("meta",))
    assert "meta" in result


def test_missing_metadata_key_present_and_valid():
    result = _missing_or_blank_metadata_keys({"symbol": "2330"}, ("symbol",))
    assert "symbol" not in result


def test_missing_metadata_key_non_empty_list_is_ok():
    result = _missing_or_blank_metadata_keys({"fields": ["ofi"]}, ("fields",))
    assert "fields" not in result


# ---------------------------------------------------------------------------
# _dataset_row_count
# ---------------------------------------------------------------------------


def test_dataset_row_count_1d_npy(tmp_path):
    arr = np.arange(50, dtype=np.float64)
    p = tmp_path / "data.npy"
    np.save(str(p), arr)
    assert _dataset_row_count(p) == 50


def test_dataset_row_count_2d_npy(tmp_path):
    arr = np.ones((10, 5), dtype=np.float64)
    p = tmp_path / "data.npy"
    np.save(str(p), arr)
    assert _dataset_row_count(p) == 10


def test_dataset_row_count_npz_with_data_key(tmp_path):
    arr = np.arange(20, dtype=np.float64)
    p = tmp_path / "data.npz"
    np.savez(str(p), data=arr)
    assert _dataset_row_count(p) == 20


def test_dataset_row_count_npz_without_data_key(tmp_path):
    arr = np.arange(15, dtype=np.float64)
    p = tmp_path / "data.npz"
    np.savez(str(p), features=arr)
    assert _dataset_row_count(p) == 15


def test_dataset_row_count_scalar_npy(tmp_path):
    arr = np.array(42.0)
    p = tmp_path / "scalar.npy"
    np.save(str(p), arr)
    count = _dataset_row_count(p)
    # Scalar arrays: ndim==0, size==1
    assert count == 1


def test_dataset_row_count_returns_none_when_arr_operations_fail(tmp_path, monkeypatch):
    """Exception inside the try block (after np.load) returns None."""
    arr = np.arange(10, dtype=np.float64)
    p = tmp_path / "data.npy"
    np.save(str(p), arr)
    # Patch np.asarray to raise inside the try block
    import hft_platform.alpha._validation_helpers as vh
    original_asarray = np.asarray

    def _bad_asarray(x):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(vh.np, "asarray", _bad_asarray)
    result = _dataset_row_count(p)
    assert result is None


# ---------------------------------------------------------------------------
# _has_hftbt_data
# ---------------------------------------------------------------------------


def test_has_hftbt_data_named_hftbt_npz(tmp_path):
    f = tmp_path / "hftbt.npz"
    f.write_bytes(b"")
    assert _has_hftbt_data([str(f)]) is True


def test_has_hftbt_data_sibling_hftbt_npz(tmp_path):
    sibling = tmp_path / "hftbt.npz"
    sibling.write_bytes(b"")
    other = tmp_path / "data.npy"
    assert _has_hftbt_data([str(other)]) is True


def test_has_hftbt_data_no_match(tmp_path):
    other = tmp_path / "data.npy"
    assert _has_hftbt_data([str(other)]) is False


def test_has_hftbt_data_empty_list():
    assert _has_hftbt_data([]) is False


# ---------------------------------------------------------------------------
# _load_dataset_metadata
# ---------------------------------------------------------------------------


def test_load_dataset_metadata_finds_meta_json(tmp_path):
    data_path = tmp_path / "data.npy"
    meta_path = tmp_path / "data.npy.meta.json"
    payload = {"symbol": "2330", "rows": 1000}
    meta_path.write_text(json.dumps(payload))
    result, found_path, err = _load_dataset_metadata(data_path)
    assert result == payload
    assert err is None
    assert found_path == meta_path


def test_load_dataset_metadata_invalid_json(tmp_path):
    data_path = tmp_path / "data.npy"
    meta_path = tmp_path / "data.npy.meta.json"
    meta_path.write_text("{invalid json}")
    result, found_path, err = _load_dataset_metadata(data_path)
    assert result is None
    assert err is not None
    assert "invalid_json" in err


def test_load_dataset_metadata_non_dict_payload(tmp_path):
    data_path = tmp_path / "data.npy"
    meta_path = tmp_path / "data.npy.meta.json"
    meta_path.write_text(json.dumps([1, 2, 3]))  # list, not dict
    result, found_path, err = _load_dataset_metadata(data_path)
    assert result is None
    assert err == "invalid_format"


def test_load_dataset_metadata_missing_file(tmp_path):
    data_path = tmp_path / "data.npy"
    result, found_path, err = _load_dataset_metadata(data_path)
    assert result is None
    assert found_path is None
    assert err == "missing_meta_file"


# ---------------------------------------------------------------------------
# _dataset_metadata_candidates
# ---------------------------------------------------------------------------


def test_dataset_metadata_candidates_returns_four_paths(tmp_path):
    data_path = tmp_path / "data.npy"
    candidates = _dataset_metadata_candidates(data_path)
    assert len(candidates) == 4
    assert all(isinstance(p, Path) for p in candidates)


# ---------------------------------------------------------------------------
# _resolve_first_data_meta_path
# ---------------------------------------------------------------------------


def test_resolve_first_data_meta_path_finds_existing(tmp_path):
    data_path = tmp_path / "data.npy"
    meta_path = tmp_path / "data.npy.meta.json"
    meta_path.write_text(json.dumps({"symbol": "X"}))
    result = _resolve_first_data_meta_path([str(data_path)])
    assert result == str(meta_path)


def test_resolve_first_data_meta_path_returns_none_when_missing(tmp_path):
    data_path = tmp_path / "data.npy"
    result = _resolve_first_data_meta_path([str(data_path)])
    assert result is None


def test_resolve_first_data_meta_path_empty_list():
    result = _resolve_first_data_meta_path([])
    assert result is None


# ---------------------------------------------------------------------------
# _write_json
# ---------------------------------------------------------------------------


def test_write_json_creates_file_with_content(tmp_path):
    path = tmp_path / "out" / "report.json"
    payload = {"key": "value", "num": 42}
    _write_json(path, payload)
    assert path.exists()
    loaded = json.loads(path.read_text())
    assert loaded == payload


def test_write_json_creates_parent_dirs(tmp_path):
    path = tmp_path / "a" / "b" / "c" / "report.json"
    _write_json(path, {"x": 1})
    assert path.exists()


# ---------------------------------------------------------------------------
# _pushd
# ---------------------------------------------------------------------------


def test_pushd_changes_and_restores_cwd(tmp_path):
    original = Path.cwd()
    with _pushd(tmp_path):
        assert Path.cwd() == tmp_path.resolve()
    assert Path.cwd() == original


def test_pushd_restores_cwd_on_exception(tmp_path):
    original = Path.cwd()
    try:
        with _pushd(tmp_path):
            raise RuntimeError("test error")
    except RuntimeError:
        pass
    assert Path.cwd() == original


# ---------------------------------------------------------------------------
# _make_validation_artifact_dir
# ---------------------------------------------------------------------------


def test_make_validation_artifact_dir_creates_dir(tmp_path):
    experiments_base = tmp_path / "research" / "experiments"
    result = _make_validation_artifact_dir(experiments_base, "ofi_mc")
    assert result.exists()
    assert result.is_dir()
    assert "ofi_mc" in str(result)


# ---------------------------------------------------------------------------
# _ensure_project_root_on_path
# ---------------------------------------------------------------------------


def test_ensure_project_root_on_path_adds_root_to_sys_path(tmp_path):
    import sys
    (tmp_path / "research").mkdir()
    _ensure_project_root_on_path(tmp_path)
    assert str(tmp_path) in sys.path
