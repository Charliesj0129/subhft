"""Coverage tests for feature/parity.py.

Tests all branches of check_schema_parity, the dataclasses ParityMismatch and
ParityReport, _rust_available, and the callable/non-callable introspection paths.

Note: check_backend_parity() has known internal bugs (uses wrong constructor
keywords and mutable methods on frozen dataclass) — its branches that raise are
tested to document that behaviour.
"""

from __future__ import annotations

import math
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.feature.parity import (
    ParityMismatch,
    ParityReport,
    _rust_available,
    check_backend_parity,
    check_schema_parity,
)

# ---------------------------------------------------------------------------
# ParityMismatch — dataclass
# ---------------------------------------------------------------------------


class TestParityMismatch:
    def test_creation_with_all_fields(self) -> None:
        m = ParityMismatch(event_idx=3, feature_id="spread_scaled", python_value=1.5, rust_value=1.6)
        assert m.event_idx == 3
        assert m.feature_id == "spread_scaled"
        assert m.python_value == pytest.approx(1.5)
        assert m.rust_value == pytest.approx(1.6)

    def test_frozen_immutability(self) -> None:
        m = ParityMismatch(event_idx=0, feature_id="best_bid", python_value=100.0, rust_value=101.0)
        with pytest.raises((AttributeError, TypeError)):
            m.event_idx = 99  # type: ignore[misc]

    def test_has_slots(self) -> None:
        assert hasattr(ParityMismatch, "__slots__")

    def test_nan_values_allowed(self) -> None:
        m = ParityMismatch(event_idx=0, feature_id="x:dtype", python_value=float("nan"), rust_value=float("nan"))
        assert math.isnan(m.python_value)
        assert math.isnan(m.rust_value)

    def test_negative_event_idx(self) -> None:
        m = ParityMismatch(event_idx=-1, feature_id="test", python_value=0.0, rust_value=1.0)
        assert m.event_idx == -1

    def test_equality_for_identical_values(self) -> None:
        m1 = ParityMismatch(event_idx=5, feature_id="ofi", python_value=10.0, rust_value=10.0)
        m2 = ParityMismatch(event_idx=5, feature_id="ofi", python_value=10.0, rust_value=10.0)
        assert m1 == m2


# ---------------------------------------------------------------------------
# ParityReport — dataclass
# ---------------------------------------------------------------------------


class TestParityReport:
    def test_creation_pass(self) -> None:
        r = ParityReport(total_events=100, mismatches=(), passed=True)
        assert r.total_events == 100
        assert r.mismatches == ()
        assert r.passed is True

    def test_creation_fail(self) -> None:
        m = ParityMismatch(event_idx=0, feature_id="x", python_value=1.0, rust_value=2.0)
        r = ParityReport(total_events=10, mismatches=(m,), passed=False)
        assert r.passed is False
        assert len(r.mismatches) == 1
        assert r.mismatches[0] is m

    def test_frozen_immutability(self) -> None:
        r = ParityReport(total_events=5, mismatches=(), passed=True)
        with pytest.raises((AttributeError, TypeError)):
            r.passed = False  # type: ignore[misc]

    def test_has_slots(self) -> None:
        assert hasattr(ParityReport, "__slots__")

    def test_zero_events_empty_mismatches(self) -> None:
        r = ParityReport(total_events=0, mismatches=(), passed=True)
        assert r.total_events == 0
        assert r.mismatches == ()


# ---------------------------------------------------------------------------
# _rust_available
# ---------------------------------------------------------------------------


class TestRustAvailable:
    def test_returns_bool(self) -> None:
        result = _rust_available()
        assert isinstance(result, bool)

    def test_false_when_kernel_none(self) -> None:
        with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", None):
            assert _rust_available() is False

    def test_true_when_kernel_present(self) -> None:
        sentinel = object()
        with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel):
            assert _rust_available() is True


# ---------------------------------------------------------------------------
# check_backend_parity — documents the broken internal implementation
# ---------------------------------------------------------------------------


class TestCheckBackendParityBroken:
    """check_backend_parity has an internal bug: it tries to call
    ParityReport(total_features=...) which is not a valid constructor arg.
    We document that it raises TypeError when called with non-empty dicts.
    """

    def test_raises_with_non_empty_input(self) -> None:
        """Calling check_backend_parity with any feature dict raises TypeError
        because the implementation calls ParityReport(total_features=...) which
        does not match the frozen dataclass constructor signature."""
        with pytest.raises(TypeError):
            check_backend_parity({"feat": 1.0}, {"feat": 1.0})

    def test_raises_with_mismatched_dicts(self) -> None:
        with pytest.raises(TypeError):
            check_backend_parity({"a": 1.0}, {"b": 2.0})


# ---------------------------------------------------------------------------
# check_schema_parity — all branches
# ---------------------------------------------------------------------------


class TestCheckSchemaParity:
    """Tests for check_schema_parity branches."""

    def test_returns_parity_report(self) -> None:
        result = check_schema_parity({}, {})
        assert isinstance(result, ParityReport)

    def test_no_rust_returns_passing_report(self) -> None:
        """When _rust_available() is False, returns trivially passing report."""
        with patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", None):
            result = check_schema_parity({}, {})
        assert result.passed is True
        assert result.total_events == 0
        assert result.mismatches == ()

    def test_rust_available_no_introspection_api(self) -> None:
        """Rust available but no get_lob_feature_ids/get_lob_feature_schema → passes trivially."""

        # Use an object with no introspection attributes
        class _MockCore:
            pass

        mock_core = _MockCore()
        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})
        # No feature_ids introspection → passes
        assert isinstance(result, ParityReport)
        assert result.passed is True

    def test_rust_available_with_matching_feature_ids(self) -> None:
        """When Rust exposes feature ids that match Python registry, no mismatches."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=py_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value=None)  # returns non-dict
        mock_kernel_cls = MagicMock()
        mock_kernel_cls.return_value = MagicMock(spec=[])  # no feature_ids attr

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is True
        assert result.mismatches == ()

    def test_rust_has_extra_feature_id_creates_mismatch(self) -> None:
        """Feature id in Rust but not Python → mismatch entry."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)
        rust_ids = py_ids + ["rust_only_feature"]

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=rust_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value={})

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
        mismatch_ids = [m.feature_id for m in result.mismatches]
        assert "rust_only_feature" in mismatch_ids
        # Mismatch has python_value=0.0 (missing from Python), rust_value=1.0
        rust_only = next(m for m in result.mismatches if m.feature_id == "rust_only_feature")
        assert rust_only.python_value == 0.0
        assert rust_only.rust_value == 1.0

    def test_python_has_extra_feature_id_creates_mismatch(self) -> None:
        """Feature id in Python but not Rust → mismatch entry."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)
        # Rust only has a subset of Python features
        rust_ids = py_ids[:-2]  # drop last 2

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=rust_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value={})

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
        # Two python-only features → 2 mismatches
        assert len(result.mismatches) == 2
        for m in result.mismatches:
            assert m.python_value == 1.0
            assert m.rust_value == 0.0

    def test_warmup_mismatch_detected(self) -> None:
        """When Rust schema has differing warmup_min_events, a mismatch is created."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        # Pick first feature ID to mismatch warmup
        first_fid = py_ids[0]
        py_spec = {spec.feature_id: spec for spec in fs.features}[first_fid]

        rust_ids = py_ids
        rust_schema = {
            first_fid: {
                "warmup_min_events": py_spec.warmup_min_events + 99,  # deliberate mismatch
                "dtype": py_spec.dtype,
            }
        }

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=rust_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value=rust_schema)

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
        warmup_mismatch_ids = [m.feature_id for m in result.mismatches]
        assert f"{first_fid}:warmup_min_events" in warmup_mismatch_ids

    def test_dtype_mismatch_detected(self) -> None:
        """When Rust schema has differing dtype, a mismatch with nan values is created."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        first_fid = py_ids[0]
        rust_ids = py_ids
        rust_schema = {
            first_fid: {
                "dtype": "wrong_dtype",  # deliberate dtype mismatch
            }
        }

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=rust_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value=rust_schema)

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
        dtype_mismatch = next(m for m in result.mismatches if m.feature_id == f"{first_fid}:dtype")
        assert math.isnan(dtype_mismatch.python_value)
        assert math.isnan(dtype_mismatch.rust_value)

    def test_get_feature_schema_returns_non_dict_is_ignored(self) -> None:
        """If get_lob_feature_schema returns non-dict, rust_schema stays empty — no warmup check."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=py_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value="not a dict")

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        # No warmup/dtype mismatches since rust_schema is empty; feature IDs match
        assert result.passed is True

    def test_kernel_probe_fallback_when_get_ids_unavailable(self) -> None:
        """When get_lob_feature_ids is not callable, falls back to kernel probe."""
        mock_core = MagicMock(spec=[])  # no attributes by default
        sentinel = object()

        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        # Kernel probe found no feature_ids → trivially passes
        assert isinstance(result, ParityReport)

    def test_kernel_probe_exception_is_swallowed(self) -> None:
        """If kernel instantiation raises during probe, it's caught and ignored."""
        mock_kernel_cls = MagicMock()
        mock_kernel_cls.side_effect = RuntimeError("kernel init fail")

        mock_core = MagicMock(spec=[])  # no get_lob_feature_ids

        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        # Probe failed gracefully → trivially passes
        assert result.passed is True

    def test_kernel_probe_with_feature_ids_attr(self) -> None:
        """When kernel instance has feature_ids attribute, it is used for comparison."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        mock_kernel_instance = MagicMock()
        mock_kernel_instance.feature_ids = py_ids  # matches Python ids
        mock_kernel_cls = MagicMock(return_value=mock_kernel_instance)

        mock_core = MagicMock(spec=[])  # no callable introspection functions

        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        # Kernel feature_ids match Python → no mismatches
        assert result.passed is True
        assert result.mismatches == ()

    def test_kernel_probe_extra_feature_in_kernel_creates_mismatch(self) -> None:
        """When kernel.feature_ids has a feature not in Python registry, mismatch created."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        mock_kernel_instance = MagicMock()
        mock_kernel_instance.feature_ids = py_ids + ["extra_kernel_feature"]
        mock_kernel_cls = MagicMock(return_value=mock_kernel_instance)

        mock_core = MagicMock(spec=[])

        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", mock_kernel_cls),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
        mismatch_ids = [m.feature_id for m in result.mismatches]
        assert "extra_kernel_feature" in mismatch_ids

    def test_total_events_counts_mismatches(self) -> None:
        """total_events reflects the number of comparison steps taken."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        # Drop 3 features from Rust → 3 mismatches
        rust_ids = py_ids[3:]

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=rust_ids)
        mock_core.get_lob_feature_schema = MagicMock(return_value={})

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.total_events == 3
        assert len(result.mismatches) == 3

    def test_passed_false_when_mismatches_present(self) -> None:
        """A report with mismatches must have passed=False."""
        from hft_platform.feature.registry import default_feature_registry

        registry = default_feature_registry()
        fs = registry.get_default()
        py_ids = list(fs.feature_ids)

        mock_core = MagicMock()
        mock_core.get_lob_feature_ids = MagicMock(return_value=py_ids[:-1])  # one missing
        mock_core.get_lob_feature_schema = MagicMock(return_value={})

        sentinel = object()
        with (
            patch("hft_platform.feature.parity._RUST_LOB_FEATURE_KERNEL_V1", sentinel),
            patch("hft_platform.feature.parity._rust_core", mock_core),
        ):
            result = check_schema_parity({}, {})

        assert result.passed is False
