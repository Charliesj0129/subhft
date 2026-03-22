"""Coverage tests for feature/parity.py — targeting 80%+ line coverage.

The module was at 0% coverage.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# ParityMismatch / ParityReport data classes
# ---------------------------------------------------------------------------


def test_parity_mismatch_fields():
    from hft_platform.feature.parity import ParityMismatch

    m = ParityMismatch(event_idx=0, feature_id="ofi_l1", python_value=1.0, rust_value=1.5)
    assert m.event_idx == 0
    assert m.feature_id == "ofi_l1"
    assert m.python_value == 1.0
    assert m.rust_value == 1.5


def test_parity_mismatch_frozen():
    from hft_platform.feature.parity import ParityMismatch

    m = ParityMismatch(event_idx=1, feature_id="spread", python_value=2.0, rust_value=2.0)
    with pytest.raises((AttributeError, TypeError)):
        m.event_idx = 99  # type: ignore[misc]


def test_parity_report_passed():
    from hft_platform.feature.parity import ParityReport

    report = ParityReport(total_events=5, mismatches=(), passed=True)
    assert report.passed is True
    assert report.total_events == 5
    assert len(report.mismatches) == 0


def test_parity_report_failed():
    from hft_platform.feature.parity import ParityMismatch, ParityReport

    m = ParityMismatch(0, "f1", 1.0, 2.0)
    report = ParityReport(total_events=1, mismatches=(m,), passed=False)
    assert report.passed is False
    assert len(report.mismatches) == 1


# ---------------------------------------------------------------------------
# _rust_available
# ---------------------------------------------------------------------------


def test_rust_available_when_none():
    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", None):
        from hft_platform.feature.parity import _rust_available

        assert _rust_available() is False


def test_rust_available_when_present():
    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        from hft_platform.feature.parity import _rust_available

        assert _rust_available() is True


# ---------------------------------------------------------------------------
# check_schema_parity — rust unavailable branch
# ---------------------------------------------------------------------------


def test_check_schema_parity_rust_unavailable():
    """When Rust is unavailable, should trivially pass."""
    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", None):
        with patch("hft_platform.feature.parity._rust_core", None):
            from hft_platform.feature.parity import check_schema_parity

            report = check_schema_parity({}, {})
            assert report.passed is True
            assert report.total_events == 0


# ---------------------------------------------------------------------------
# check_schema_parity — rust available, no introspection API
# ---------------------------------------------------------------------------


def test_check_schema_parity_rust_no_introspection():
    """Rust available but no get_lob_feature_ids → trivially passing."""
    mock_rust_core = MagicMock()
    # Remove introspection functions
    del mock_rust_core.get_lob_feature_ids
    del mock_rust_core.get_lob_feature_schema

    mock_kernel_cls = MagicMock()
    mock_kernel_instance = MagicMock()
    del mock_kernel_instance.feature_ids
    mock_kernel_cls.return_value = mock_kernel_instance

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            from hft_platform.feature.parity import check_schema_parity

            report = check_schema_parity({}, {})
            # Should pass trivially since no introspection available
            assert report.passed is True


# ---------------------------------------------------------------------------
# check_schema_parity — rust with feature_ids, matching sets
# ---------------------------------------------------------------------------


def _make_feature_spec(feature_id, warmup_min_events=8, dtype="int64"):
    spec = SimpleNamespace(
        feature_id=feature_id,
        warmup_min_events=warmup_min_events,
        dtype=dtype,
    )
    return spec


def _make_feature_set(feature_ids, warmup=8, dtype="int64"):
    specs = [_make_feature_spec(fid, warmup, dtype) for fid in feature_ids]
    fs = SimpleNamespace(
        features=specs,
        feature_ids=feature_ids,
    )
    return fs


def _make_registry(feature_ids, warmup=8, dtype="int64"):
    fs = _make_feature_set(feature_ids, warmup, dtype)
    reg = MagicMock()
    reg.get_default.return_value = fs
    return reg


def test_check_schema_parity_matching_ids():
    """Matching Python and Rust feature IDs → passed."""
    mock_rust_core = MagicMock()
    rust_ids = ("bb", "ba", "mid", "spread")
    mock_rust_core.get_lob_feature_ids.return_value = rust_ids
    mock_rust_core.get_lob_feature_schema.return_value = {
        "bb": {"warmup_min_events": 8, "dtype": "int64"},
        "ba": {"warmup_min_events": 8, "dtype": "int64"},
        "mid": {"warmup_min_events": 8, "dtype": "int64"},
        "spread": {"warmup_min_events": 8, "dtype": "int64"},
    }

    mock_registry = _make_registry(list(rust_ids))

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is True


def test_check_schema_parity_extra_python_feature():
    """Feature in Python but not Rust → mismatch."""
    mock_rust_core = MagicMock()
    rust_ids = ("bb", "ba")
    mock_rust_core.get_lob_feature_ids.return_value = rust_ids
    mock_rust_core.get_lob_feature_schema.return_value = {}

    py_ids = ["bb", "ba", "extra_python_only"]
    mock_registry = _make_registry(py_ids)

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is False
    fids = [m.feature_id for m in report.mismatches]
    assert "extra_python_only" in fids


def test_check_schema_parity_extra_rust_feature():
    """Feature in Rust but not Python → mismatch."""
    mock_rust_core = MagicMock()
    rust_ids = ("bb", "ba", "extra_rust_only")
    mock_rust_core.get_lob_feature_ids.return_value = rust_ids
    mock_rust_core.get_lob_feature_schema.return_value = {}

    py_ids = ["bb", "ba"]
    mock_registry = _make_registry(py_ids)

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is False
    fids = [m.feature_id for m in report.mismatches]
    assert "extra_rust_only" in fids


def test_check_schema_parity_warmup_mismatch():
    """Warmup mismatch between Python and Rust → mismatch entry."""
    mock_rust_core = MagicMock()
    rust_ids = ("bb",)
    mock_rust_core.get_lob_feature_ids.return_value = rust_ids
    mock_rust_core.get_lob_feature_schema.return_value = {
        "bb": {"warmup_min_events": 16, "dtype": "int64"},  # Rust says 16
    }

    py_ids = ["bb"]
    mock_registry = _make_registry(py_ids, warmup=8)  # Python says 8

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is False
    fids = [m.feature_id for m in report.mismatches]
    assert any("warmup_min_events" in fid for fid in fids)


def test_check_schema_parity_dtype_mismatch():
    """dtype mismatch → mismatch entry with nan values."""
    mock_rust_core = MagicMock()
    rust_ids = ("bb",)
    mock_rust_core.get_lob_feature_ids.return_value = rust_ids
    mock_rust_core.get_lob_feature_schema.return_value = {
        "bb": {"warmup_min_events": 8, "dtype": "float32"},  # Rust: float32
    }

    py_ids = ["bb"]
    mock_registry = _make_registry(py_ids, warmup=8, dtype="int64")  # Python: int64

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", MagicMock()):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is False
    dtype_mismatches = [m for m in report.mismatches if "dtype" in m.feature_id]
    assert len(dtype_mismatches) == 1
    assert math.isnan(dtype_mismatches[0].python_value)
    assert math.isnan(dtype_mismatches[0].rust_value)


def test_check_schema_parity_kernel_probe_fallback():
    """When get_lob_feature_ids not available, fall back to kernel instance probe."""
    mock_rust_core = MagicMock()
    del mock_rust_core.get_lob_feature_ids
    del mock_rust_core.get_lob_feature_schema

    mock_kernel_cls = MagicMock()
    mock_kernel_instance = MagicMock()
    mock_kernel_instance.feature_ids = ("bb", "ba")
    mock_kernel_cls.return_value = mock_kernel_instance

    py_ids = ["bb", "ba"]
    mock_registry = _make_registry(py_ids)

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    assert report.passed is True


def test_check_schema_parity_kernel_probe_exception():
    """kernel probe fails → fall back to no-introspection (trivially pass)."""
    mock_rust_core = MagicMock()
    del mock_rust_core.get_lob_feature_ids
    del mock_rust_core.get_lob_feature_schema

    mock_kernel_cls = MagicMock()
    mock_kernel_cls.side_effect = RuntimeError("kernel init failed")

    py_ids = ["bb", "ba"]
    mock_registry = _make_registry(py_ids)

    with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls):
        with patch("hft_platform.feature.parity._rust_core", mock_rust_core):
            with patch("hft_platform.feature.parity.default_feature_registry", return_value=mock_registry):
                from hft_platform.feature.parity import check_schema_parity

                report = check_schema_parity({}, {})
    # Kernel probe failed → rust_feature_ids is None → trivially pass
    assert report.passed is True
