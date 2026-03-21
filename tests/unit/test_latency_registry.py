"""Unit tests for latency profile registry."""

from __future__ import annotations

from hft_platform.alpha._latency_registry import (
    load_latency_profiles,
    validate_latency_profile_id,
)


class TestValidateLatencyProfileId:
    def test_known_profile_returns_true(self):
        profiles = {"sim_p95_v2026-02-26": {"submit_ms": 36.0}}
        valid, detail = validate_latency_profile_id("sim_p95_v2026-02-26", profiles)
        assert valid is True
        assert "found" in detail

    def test_unknown_profile_returns_false(self):
        profiles = {"sim_p95_v2026-02-26": {"submit_ms": 36.0}}
        valid, detail = validate_latency_profile_id("nonexistent_profile", profiles)
        assert valid is False
        assert "not in registry" in detail

    def test_empty_profiles_returns_true_with_skip(self):
        valid, detail = validate_latency_profile_id("any_id", {})
        assert valid is True
        assert "SKIPPED" in detail

    def test_typo_in_profile_id_detected(self):
        profiles = {"sim_p95_v2026-02-26": {"submit_ms": 36.0}}
        valid, detail = validate_latency_profile_id("sim_p95_v2026-02-27", profiles)
        assert valid is False


class TestLoadLatencyProfiles:
    def test_returns_dict(self):
        result = load_latency_profiles("/nonexistent/path")
        assert isinstance(result, dict)

    def test_passes_path_arg(self):
        result = load_latency_profiles(path="/nonexistent/file.yaml")
        assert isinstance(result, dict)
        assert result == {}
