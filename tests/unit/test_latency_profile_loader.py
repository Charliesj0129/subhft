from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hft_platform.alpha.latency_profiles import (
    load_profiles,
    resolve_profile,
)


@pytest.fixture()
def profiles_yaml(tmp_path: Path) -> Path:
    data = {
        "profiles": {
            "shioaji_sim_p95_v2026-03-04": {
                "description": "Shioaji sim P95",
                "submit_ack_latency_ms": 36.0,
                "modify_ack_latency_ms": 43.0,
                "cancel_ack_latency_ms": 47.0,
                "local_decision_pipeline_latency_us": 250,
                "measurement_date": "2026-03-04",
            },
            "shioaji_sim_p95_v2026-02-26": {
                "description": "Shioaji sim P95 compat",
                "submit_ack_latency_ms": 36.0,
                "modify_ack_latency_ms": 43.0,
                "cancel_ack_latency_ms": 47.0,
                "local_decision_pipeline_latency_us": 250,
                "measurement_date": "2026-02-26",
            },
        },
    }
    p = tmp_path / "latency_profiles.yaml"
    p.write_text(yaml.dump(data))
    return p


class TestLoadProfiles:
    def test_load_from_yaml(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        assert "shioaji_sim_p95_v2026-03-04" in profiles
        assert profiles["shioaji_sim_p95_v2026-03-04"]["submit_ack_latency_ms"] == 36.0

    def test_load_missing_file(self, tmp_path: Path) -> None:
        profiles = load_profiles(tmp_path / "missing.yaml")
        assert profiles == {}

    def test_load_empty_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("")
        profiles = load_profiles(p)
        assert profiles == {}


class TestResolveProfile:
    def test_direct_lookup(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        result = resolve_profile("shioaji_sim_p95_v2026-03-04", profiles)
        assert result["submit_ack_latency_ms"] == 36.0

    def test_alias_resolution(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        result = resolve_profile("sim_p95_v2026-03-04", profiles)
        assert result["submit_ack_latency_ms"] == 36.0

    def test_missing_profile_raises(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        with pytest.raises(KeyError, match="not found"):
            resolve_profile("nonexistent_profile", profiles)

    def test_missing_required_fields_raises(self, tmp_path: Path) -> None:
        data = {
            "profiles": {
                "incomplete": {
                    "submit_ack_latency_ms": 36.0,
                    # missing other required fields
                },
            },
        }
        p = tmp_path / "bad.yaml"
        p.write_text(yaml.dump(data))
        profiles = load_profiles(p)
        with pytest.raises(ValueError, match="missing required fields"):
            resolve_profile("incomplete", profiles)

    def test_auto_load_from_yaml_path(self, profiles_yaml: Path) -> None:
        result = resolve_profile(
            "shioaji_sim_p95_v2026-03-04",
            yaml_path=profiles_yaml,
        )
        assert result["submit_ack_latency_ms"] == 36.0

    def test_prefix_match(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        # "shioaji_sim_p95_v2026-03" should match "shioaji_sim_p95_v2026-03-04"
        # but only if there's exactly one match
        result = resolve_profile("shioaji_sim_p95_v2026-03", profiles)
        assert result["submit_ack_latency_ms"] == 36.0

    def test_ambiguous_prefix_raises(self, profiles_yaml: Path) -> None:
        profiles = load_profiles(profiles_yaml)
        with pytest.raises(KeyError, match="Ambiguous"):
            resolve_profile("shioaji_sim_p95", profiles)
