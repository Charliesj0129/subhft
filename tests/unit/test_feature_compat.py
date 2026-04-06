"""Unit tests for src/hft_platform/feature/compat.py.

Covers:
- FeatureCompatibilityIssue dataclass
- check_feature_profile_compat (unknown feature set, schema too new, ema_window validation)
- check_runtime_feature_engine_compat (None engine, missing callables, valid engine)
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from hft_platform.feature.compat import (
    FeatureCompatibilityIssue,
    check_feature_profile_compat,
    check_runtime_feature_engine_compat,
)
from hft_platform.feature.profile import FeatureProfile
from hft_platform.feature.registry import FeatureRegistry, build_default_lob_feature_set_v3, default_feature_registry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry() -> FeatureRegistry:
    return default_feature_registry()


def _profile(
    *,
    feature_set_id: str = "lob_shared_v3",
    schema_version: int | None = None,
    params: dict | None = None,
) -> FeatureProfile:
    return FeatureProfile(
        profile_id="test_profile",
        feature_set_id=feature_set_id,
        schema_version=schema_version,
        params=params or {},
    )


# ---------------------------------------------------------------------------
# FeatureCompatibilityIssue
# ---------------------------------------------------------------------------


class TestFeatureCompatibilityIssue:
    def test_is_frozen_dataclass(self):
        issue = FeatureCompatibilityIssue("error", "some_code", "msg")
        with pytest.raises((AttributeError, TypeError)):
            issue.level = "warning"  # type: ignore[misc]

    def test_fields_stored_correctly(self):
        issue = FeatureCompatibilityIssue("warning", "large_ema_window", "too big")
        assert issue.level == "warning"
        assert issue.code == "large_ema_window"
        assert issue.message == "too big"

    def test_equality_by_value(self):
        a = FeatureCompatibilityIssue("error", "x", "msg")
        b = FeatureCompatibilityIssue("error", "x", "msg")
        assert a == b

    def test_inequality_on_different_code(self):
        a = FeatureCompatibilityIssue("error", "code_a", "msg")
        b = FeatureCompatibilityIssue("error", "code_b", "msg")
        assert a != b


# ---------------------------------------------------------------------------
# check_feature_profile_compat — unknown feature set
# ---------------------------------------------------------------------------


class TestCheckFeatureProfileCompatUnknownSet:
    def test_unknown_feature_set_returns_single_error(self):
        reg = _make_registry()
        profile = _profile(feature_set_id="nonexistent_set")
        issues = check_feature_profile_compat(profile, reg)
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].code == "unknown_feature_set"

    def test_unknown_feature_set_message_contains_id(self):
        reg = _make_registry()
        profile = _profile(feature_set_id="mystery_v99")
        issues = check_feature_profile_compat(profile, reg)
        assert "mystery_v99" in issues[0].message

    def test_empty_registry_returns_error_for_any_profile(self):
        empty_reg = FeatureRegistry()
        profile = _profile(feature_set_id="lob_shared_v3")
        issues = check_feature_profile_compat(profile, empty_reg)
        assert any(i.code == "unknown_feature_set" for i in issues)


# ---------------------------------------------------------------------------
# check_feature_profile_compat — schema version
# ---------------------------------------------------------------------------


class TestCheckFeatureProfileCompatSchemaVersion:
    def test_no_schema_version_produces_no_issues(self):
        reg = _make_registry()
        profile = _profile(schema_version=None)
        issues = check_feature_profile_compat(profile, reg)
        assert issues == []

    def test_schema_version_equal_to_runtime_is_ok(self):
        reg = _make_registry()
        # lob_shared_v3 has schema_version=3
        profile = _profile(schema_version=3)
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code != "schema_too_new" for i in issues)

    def test_schema_version_less_than_runtime_is_ok(self):
        reg = _make_registry()
        profile = _profile(schema_version=1)
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code != "schema_too_new" for i in issues)

    def test_schema_version_greater_than_runtime_produces_error(self):
        reg = _make_registry()
        profile = _profile(schema_version=999)
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "schema_too_new" for i in issues)

    def test_schema_too_new_error_message_contains_versions(self):
        reg = _make_registry()
        profile = _profile(schema_version=99)
        issues = check_feature_profile_compat(profile, reg)
        err = next(i for i in issues if i.code == "schema_too_new")
        assert "99" in err.message

    def test_schema_version_bump_by_one_produces_error(self):
        reg = _make_registry()
        # v3 → profile claims v4
        profile = _profile(schema_version=4)
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "schema_too_new" for i in issues)


# ---------------------------------------------------------------------------
# check_feature_profile_compat — ema_window param
# ---------------------------------------------------------------------------


class TestCheckFeatureProfileCompatEmaWindow:
    def test_valid_ema_window_no_issues(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 64})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code != "invalid_ema_window" for i in issues)
        assert all(i.code != "large_ema_window" for i in issues)

    def test_ema_window_zero_is_error(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 0})
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "invalid_ema_window" for i in issues)
        err = next(i for i in issues if i.code == "invalid_ema_window")
        assert err.level == "error"

    def test_ema_window_negative_is_error(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": -10})
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "invalid_ema_window" for i in issues)

    def test_ema_window_above_512_is_warning(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 1024})
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "large_ema_window" for i in issues)
        warn = next(i for i in issues if i.code == "large_ema_window")
        assert warn.level == "warning"

    def test_ema_window_exactly_512_is_ok(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 512})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_ema_window_513_is_warning(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 513})
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "large_ema_window" for i in issues)

    def test_ema_window_1_is_ok(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 1})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_ema_window_as_string_int_is_ok(self):
        reg = _make_registry()
        # params may come from YAML as strings
        profile = _profile(params={"ema_window": "64"})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_ema_window_non_numeric_string_is_error(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": "not_a_number"})
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "invalid_ema_window" for i in issues)

    def test_no_ema_window_param_produces_no_ema_issues(self):
        reg = _make_registry()
        profile = _profile(params={"other_param": 42})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_empty_params_produces_no_ema_issues(self):
        reg = _make_registry()
        profile = _profile(params={})
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_none_params_produces_no_ema_issues(self):
        reg = _make_registry()
        profile = FeatureProfile(
            profile_id="test",
            feature_set_id="lob_shared_v3",
            params=None,  # type: ignore[arg-type]
        )
        issues = check_feature_profile_compat(profile, reg)
        assert all(i.code not in {"invalid_ema_window", "large_ema_window"} for i in issues)

    def test_large_ema_window_message_contains_value(self):
        reg = _make_registry()
        profile = _profile(params={"ema_window": 2048})
        issues = check_feature_profile_compat(profile, reg)
        warn = next(i for i in issues if i.code == "large_ema_window")
        assert "2048" in warn.message


# ---------------------------------------------------------------------------
# check_feature_profile_compat — combined scenarios
# ---------------------------------------------------------------------------


class TestCheckFeatureProfileCompatCombined:
    def test_valid_profile_no_issues(self):
        reg = _make_registry()
        profile = _profile(schema_version=2, params={"ema_window": 8})
        issues = check_feature_profile_compat(profile, reg)
        assert issues == []

    def test_multiple_issues_returned_together(self):
        reg = _make_registry()
        # schema_too_new + large_ema_window
        profile = _profile(schema_version=999, params={"ema_window": 9999})
        issues = check_feature_profile_compat(profile, reg)
        codes = {i.code for i in issues}
        assert "schema_too_new" in codes
        assert "large_ema_window" in codes

    def test_uses_v1_feature_set(self):
        reg = _make_registry()
        profile = _profile(feature_set_id="lob_shared_v1", schema_version=1)
        issues = check_feature_profile_compat(profile, reg)
        assert issues == []

    def test_profile_schema_newer_than_v1_runtime(self):
        # Registry only has v1
        reg = FeatureRegistry()
        from hft_platform.feature.registry import build_default_lob_feature_set_v1
        reg.register(build_default_lob_feature_set_v1())
        profile = _profile(feature_set_id="lob_shared_v1", schema_version=99)
        issues = check_feature_profile_compat(profile, reg)
        assert any(i.code == "schema_too_new" for i in issues)


# ---------------------------------------------------------------------------
# check_runtime_feature_engine_compat
# ---------------------------------------------------------------------------


class TestCheckRuntimeFeatureEngineCompatNone:
    def test_none_engine_returns_single_error(self):
        issues = check_runtime_feature_engine_compat(None)
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].code == "feature_engine_missing"

    def test_none_engine_message_describes_problem(self):
        issues = check_runtime_feature_engine_compat(None)
        assert "not enabled" in issues[0].message.lower() or "missing" in issues[0].message.lower()


class TestCheckRuntimeFeatureEngineCompatMissingApi:
    """Objects that are missing required callable attributes produce missing_api errors."""

    ALL_REQUIRED = ("feature_set_id", "schema_version", "get_feature", "get_feature_view")

    def test_empty_object_has_all_missing_api_errors(self):
        issues = check_runtime_feature_engine_compat(object())
        codes = [i.code for i in issues]
        assert codes.count("missing_api") == len(self.ALL_REQUIRED)

    def test_missing_single_attribute_produces_one_error(self):
        engine = MagicMock()
        del engine.get_feature_view  # remove one attribute
        issues = check_runtime_feature_engine_compat(engine)
        missing = [i for i in issues if i.code == "missing_api"]
        assert len(missing) == 1
        assert "get_feature_view" in missing[0].message

    def test_non_callable_attribute_is_flagged(self):
        engine = MagicMock()
        engine.feature_set_id = "lob_shared_v3"  # not callable
        issues = check_runtime_feature_engine_compat(engine)
        missing = [i for i in issues if i.code == "missing_api"]
        assert any("feature_set_id" in i.message for i in missing)

    def test_all_missing_api_errors_are_error_level(self):
        issues = check_runtime_feature_engine_compat(object())
        for issue in issues:
            assert issue.level == "error"

    def test_missing_api_message_contains_attribute_name(self):
        engine = MagicMock()
        del engine.schema_version
        issues = check_runtime_feature_engine_compat(engine)
        missing = next(i for i in issues if i.code == "missing_api")
        assert "schema_version" in missing.message


class TestCheckRuntimeFeatureEngineCompatValid:
    def test_fully_compliant_engine_has_no_issues(self):
        engine = MagicMock()
        # All required attributes present and callable
        engine.feature_set_id = MagicMock()
        engine.schema_version = MagicMock()
        engine.get_feature = MagicMock()
        engine.get_feature_view = MagicMock()
        issues = check_runtime_feature_engine_compat(engine)
        assert issues == []

    def test_extra_attributes_do_not_cause_issues(self):
        engine = MagicMock()
        engine.feature_set_id = MagicMock()
        engine.schema_version = MagicMock()
        engine.get_feature = MagicMock()
        engine.get_feature_view = MagicMock()
        engine.some_extra_method = MagicMock()
        issues = check_runtime_feature_engine_compat(engine)
        assert issues == []

    def test_returns_empty_list_for_valid_engine(self):
        engine = MagicMock()
        issues = check_runtime_feature_engine_compat(engine)
        # MagicMock provides callables for everything by default
        assert isinstance(issues, list)
