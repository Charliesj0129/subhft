"""Coverage tests for feature/profile.py — missing lines 23, 58/66-67/71/78-87/92-103."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hft_platform.feature.profile import FeatureProfile, FeatureProfileRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_profile(
    profile_id: str = "p1",
    feature_set_id: str = "lob_shared_v3",
    schema_version: int | None = 3,
    enabled: bool = True,
    state: str = "active",
    params: dict | None = None,
    owner: str = "",
    notes: str = "",
) -> FeatureProfile:
    return FeatureProfile(
        profile_id=profile_id,
        feature_set_id=feature_set_id,
        schema_version=schema_version,
        params=params or {},
        enabled=enabled,
        state=state,
        owner=owner,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# FeatureProfile.to_dict — line 23
# ---------------------------------------------------------------------------


class TestFeatureProfileToDict:
    def test_to_dict_all_fields_present(self) -> None:
        prof = _make_profile(
            profile_id="abc",
            feature_set_id="lob_shared_v1",
            schema_version=1,
            enabled=True,
            state="active",
            params={"ema_window": 8},
            owner="team",
            notes="test note",
        )
        d = prof.to_dict()
        assert d["profile_id"] == "abc"
        assert d["feature_set_id"] == "lob_shared_v1"
        assert d["schema_version"] == 1
        assert d["enabled"] is True
        assert d["state"] == "active"
        assert d["params"] == {"ema_window": 8}
        assert d["owner"] == "team"
        assert d["notes"] == "test note"

    def test_to_dict_none_schema_version(self) -> None:
        prof = _make_profile(schema_version=None)
        d = prof.to_dict()
        assert d["schema_version"] is None

    def test_to_dict_empty_params(self) -> None:
        prof = _make_profile(params={})
        d = prof.to_dict()
        assert d["params"] == {}

    def test_to_dict_disabled_state(self) -> None:
        prof = _make_profile(enabled=False, state="disabled")
        d = prof.to_dict()
        assert d["enabled"] is False
        assert d["state"] == "disabled"


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.register — line 58: make_default=True when already has default
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryRegister:
    def test_first_registered_becomes_default(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1"))
        assert reg.get_default().profile_id == "p1"

    def test_second_registration_keeps_original_default(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1"))
        reg.register(_make_profile("p2"))
        # default should still be p1 since make_default=False by default
        assert reg.get_default().profile_id == "p1"

    def test_make_default_true_overrides_existing_default(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1"))
        reg.register(_make_profile("p2"), make_default=True)
        assert reg.get_default().profile_id == "p2"

    def test_ids_returns_sorted_tuple(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p3"))
        reg.register(_make_profile("p1"))
        reg.register(_make_profile("p2"))
        assert reg.ids() == ("p1", "p2", "p3")


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.get — lines 66-67: KeyError wrapping
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryGet:
    def test_get_existing_profile(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1"))
        assert reg.get("p1").profile_id == "p1"

    def test_get_unknown_raises_key_error_with_message(self) -> None:
        reg = FeatureProfileRegistry()
        with pytest.raises(KeyError, match="Unknown feature profile"):
            reg.get("nonexistent")


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.get_default — line 71
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryGetDefault:
    def test_get_default_none_when_empty(self) -> None:
        reg = FeatureProfileRegistry()
        assert reg.get_default() is None

    def test_get_default_returns_registered_profile(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("px"))
        default = reg.get_default()
        assert default is not None
        assert default.profile_id == "px"


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.get_active_for_set — lines 78-87: env var path
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryGetActiveForSet:
    def test_returns_none_when_empty(self) -> None:
        reg = FeatureProfileRegistry()
        assert reg.get_active_for_set("lob_shared_v3") is None

    def test_returns_active_profile_for_set(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3"))
        result = reg.get_active_for_set("lob_shared_v3")
        assert result is not None
        assert result.profile_id == "p1"

    def test_returns_none_for_wrong_feature_set(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v1"))
        result = reg.get_active_for_set("lob_shared_v3")
        assert result is None

    def test_env_var_overrides_default_profile(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3"))
        reg.register(_make_profile("p2", feature_set_id="lob_shared_v3"))
        with patch.dict(os.environ, {"HFT_FEATURE_PROFILE_ID": "p2"}):
            result = reg.get_active_for_set("lob_shared_v3")
        assert result is not None
        assert result.profile_id == "p2"

    def test_env_var_ignored_if_profile_not_found(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3"))
        with patch.dict(os.environ, {"HFT_FEATURE_PROFILE_ID": "nonexistent"}):
            result = reg.get_active_for_set("lob_shared_v3")
        # falls through to default
        assert result is not None
        assert result.profile_id == "p1"

    def test_env_var_ignored_if_wrong_feature_set(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3"))
        reg.register(_make_profile("p_wrong", feature_set_id="lob_shared_v1"))
        with patch.dict(os.environ, {"HFT_FEATURE_PROFILE_ID": "p_wrong"}):
            result = reg.get_active_for_set("lob_shared_v3")
        # p_wrong is for v1, not v3, so fallback to default p1
        assert result is not None
        assert result.profile_id == "p1"

    def test_disabled_profile_skipped(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3", enabled=False))
        result = reg.get_active_for_set("lob_shared_v3")
        assert result is None

    def test_shadow_state_profile_returned(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3", state="shadow"))
        result = reg.get_active_for_set("lob_shared_v3")
        assert result is not None
        assert result.profile_id == "p1"

    def test_disabled_state_profile_skipped(self) -> None:
        # state="disabled" AND enabled=True: the fallback loop filters on state in {"active","shadow"},
        # so a "disabled" state profile is not returned via the loop.
        # But it IS returned as the default since it's the first registered.
        # To test the loop path, register it as non-default (first register a different set):
        reg = FeatureProfileRegistry()
        # Register a default profile for a different feature_set_id
        reg.register(_make_profile("p0", feature_set_id="lob_shared_v1", state="active"))
        # Register a disabled-state profile for v3 — the loop skips it
        reg.register(_make_profile("p1", feature_set_id="lob_shared_v3", state="disabled"))
        result = reg.get_active_for_set("lob_shared_v3")
        assert result is None


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.validate — lines 92-103: edge cases
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryValidate:
    def test_valid_registry_returns_no_errors(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1", state="active"))
        assert reg.validate() == []

    def test_empty_feature_set_id_reported(self) -> None:
        reg = FeatureProfileRegistry()
        # directly inject a bad profile
        prof = FeatureProfile(profile_id="p1", feature_set_id="")
        reg._profiles["p1"] = prof
        reg._default_id = "p1"
        errors = reg.validate()
        assert any("missing feature_set_id" in e for e in errors)

    def test_invalid_state_reported(self) -> None:
        reg = FeatureProfileRegistry()
        prof = FeatureProfile(profile_id="p1", feature_set_id="lob_shared_v3", state="unknown_state")
        reg._profiles["p1"] = prof
        reg._default_id = "p1"
        errors = reg.validate()
        assert any("invalid state" in e for e in errors)

    def test_default_not_in_registry_reported(self) -> None:
        reg = FeatureProfileRegistry()
        reg.register(_make_profile("p1"))
        # Manually point default to a non-existent ID
        reg._default_id = "ghost"
        errors = reg.validate()
        assert any("default profile" in e and "ghost" in e for e in errors)

    def test_all_valid_states_accepted(self) -> None:
        for state in ("active", "shadow", "disabled"):
            reg = FeatureProfileRegistry()
            reg.register(_make_profile("p1", state=state))
            assert reg.validate() == [], f"state={state} should not produce errors"


# ---------------------------------------------------------------------------
# FeatureProfileRegistry.from_file — lines 78-87: YAML parsing
# ---------------------------------------------------------------------------


class TestFeatureProfileRegistryFromFile:
    def test_from_file_nonexistent_path_returns_empty_registry(self, tmp_path: Path) -> None:
        reg = FeatureProfileRegistry.from_file(tmp_path / "missing.yaml")
        assert reg.ids() == ()
        assert reg.get_default() is None

    def test_from_file_parses_single_profile(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "default_profile_id: p1\n"
            "profiles:\n"
            "  - profile_id: p1\n"
            "    feature_set_id: lob_shared_v3\n"
            "    schema_version: 3\n"
            "    enabled: true\n"
            "    state: active\n"
            "    params:\n"
            "      ema_window: 5\n"
            "    owner: team_a\n"
            "    notes: from file\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        assert "p1" in reg.ids()
        prof = reg.get("p1")
        assert prof.feature_set_id == "lob_shared_v3"
        assert prof.schema_version == 3
        assert prof.params == {"ema_window": 5}
        assert prof.owner == "team_a"
        assert prof.notes == "from file"

    def test_from_file_parses_multiple_profiles(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "profiles:\n"
            "  - profile_id: a\n"
            "    feature_set_id: lob_shared_v1\n"
            "  - profile_id: b\n"
            "    feature_set_id: lob_shared_v2\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        assert set(reg.ids()) == {"a", "b"}

    def test_from_file_skips_entries_missing_profile_id(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "profiles:\n"
            "  - feature_set_id: lob_shared_v3\n"
            "  - profile_id: good\n"
            "    feature_set_id: lob_shared_v3\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        assert reg.ids() == ("good",)

    def test_from_file_skips_entries_missing_feature_set_id(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "profiles:\n"
            "  - profile_id: bad\n"
            "  - profile_id: good\n"
            "    feature_set_id: lob_shared_v3\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        assert reg.ids() == ("good",)

    def test_from_file_invalid_format_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text("- this\n- is\n- a\n- list\n", encoding="utf-8")
        with pytest.raises(ValueError, match="expected mapping"):
            FeatureProfileRegistry.from_file(path)

    def test_from_file_path_stored(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text("profiles: []\n", encoding="utf-8")
        reg = FeatureProfileRegistry.from_file(path)
        assert reg.path == str(path)

    def test_from_file_default_profile_id_set(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "default_profile_id: p2\n"
            "profiles:\n"
            "  - profile_id: p1\n"
            "    feature_set_id: lob_shared_v3\n"
            "  - profile_id: p2\n"
            "    feature_set_id: lob_shared_v3\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        default = reg.get_default()
        assert default is not None
        assert default.profile_id == "p2"

    def test_from_file_skips_non_dict_entries(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text(
            "profiles:\n"
            "  - not_a_dict_value\n"
            "  - profile_id: p1\n"
            "    feature_set_id: lob_shared_v3\n",
            encoding="utf-8",
        )
        reg = FeatureProfileRegistry.from_file(path)
        assert reg.ids() == ("p1",)

    def test_to_dict_includes_errors_and_path(self, tmp_path: Path) -> None:
        path = tmp_path / "profiles.yaml"
        path.write_text("profiles: []\n", encoding="utf-8")
        reg = FeatureProfileRegistry.from_file(path)
        d = reg.to_dict()
        assert "path" in d
        assert "default_profile_id" in d
        assert "profiles" in d
        assert "errors" in d
