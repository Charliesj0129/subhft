"""Coverage tests for feature/rollout.py — missing lines 74-78, 94/105/110-118, 143-145, 177-190."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.feature.rollout import FeatureRolloutAssignment, FeatureRolloutController

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctrl(tmp_path: Path, name: str = "rollout.json") -> FeatureRolloutController:
    return FeatureRolloutController(tmp_path / name)


# ---------------------------------------------------------------------------
# resolve_profile_id — lines 74-78: disabled/shadow states
# ---------------------------------------------------------------------------


class TestResolveProfileId:
    def test_returns_none_when_no_assignment(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        assert ctrl.resolve_profile_id("lob_shared_v3") is None

    def test_returns_none_for_disabled_state(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        # Now disable it
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="disabled",
        )
        assert ctrl.resolve_profile_id("lob_shared_v3") is None

    def test_returns_shadow_profile_for_shadow_state(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p_active",
        )
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            profile_id="p_shadow",
        )
        result = ctrl.resolve_profile_id("lob_shared_v3")
        assert result == "p_shadow"

    def test_returns_active_profile_when_shadow_profile_is_none(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p_active",
        )
        # Enter shadow state via shadow_profile_id arg
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            shadow_profile_id="p_shad",
        )
        # Now manually set shadow_profile_id to None to test fallback to active_profile_id
        cur = ctrl.get("lob_shared_v3")
        ctrl._sets["lob_shared_v3"] = FeatureRolloutAssignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            active_profile_id="p_active",
            shadow_profile_id=None,  # explicitly None
            version=cur.version + 1,
        )
        result = ctrl.resolve_profile_id("lob_shared_v3")
        assert result == "p_active"

    def test_returns_active_profile_id_for_active_state(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        assert ctrl.resolve_profile_id("lob_shared_v3") == "p1"


# ---------------------------------------------------------------------------
# set_assignment — lines 94/105/110-118: invalid state, shadow edge cases
# ---------------------------------------------------------------------------


class TestSetAssignment:
    def test_invalid_state_raises(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        with pytest.raises(ValueError, match="Invalid rollout state"):
            ctrl.set_assignment(
                feature_set_id="lob_shared_v3",
                state="broken",
                profile_id="p1",
            )

    def test_active_without_profile_id_raises(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        with pytest.raises(ValueError, match="profile_id is required"):
            ctrl.set_assignment(
                feature_set_id="lob_shared_v3",
                state="active",
            )

    def test_shadow_via_profile_id_arg(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p_active",
        )
        a = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            profile_id="p_shad",
        )
        assert a.state == "shadow"
        assert a.shadow_profile_id == "p_shad"

    def test_shadow_via_shadow_profile_id_arg(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p_active",
        )
        a = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            shadow_profile_id="p_shadow2",
        )
        assert a.shadow_profile_id == "p_shadow2"

    def test_shadow_without_any_profile_and_no_existing_raises(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        # No prior assignment, no profile_id/shadow_profile_id provided
        with pytest.raises(ValueError, match="profile_id.*required for first shadow rollout"):
            ctrl.set_assignment(
                feature_set_id="lob_shared_v3",
                state="shadow",
            )

    def test_shadow_reuses_existing_shadow_when_no_new_one_given(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p_active",
        )
        # Set an initial shadow profile
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
            profile_id="p_shad_v1",
        )
        # Shadow again without specifying new profile — should reuse existing
        a = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="shadow",
        )
        assert a.shadow_profile_id == "p_shad_v1"

    def test_disabled_preserves_ids(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        a = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="disabled",
        )
        assert a.state == "disabled"
        assert a.active_profile_id == "p1"  # preserved

    def test_version_increments_on_each_assignment(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        a1 = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        a2 = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p2",
        )
        assert a2.version > a1.version

    def test_active_sets_prev_active_when_profile_changes(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        a2 = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p2",
        )
        assert a2.prev_active_profile_id == "p1"

    def test_active_with_shadow_profile_id_also_updates_shadow(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        a = ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
            shadow_profile_id="p_shad",
        )
        assert a.active_profile_id == "p1"
        assert a.shadow_profile_id == "p_shad"


# ---------------------------------------------------------------------------
# rollback — lines 143-145
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_restores_previous_active(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p2",
        )
        rb = ctrl.rollback(feature_set_id="lob_shared_v3")
        assert rb.active_profile_id == "p1"

    def test_rollback_nonexistent_feature_set_raises(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        with pytest.raises(KeyError, match="No rollout assignment"):
            ctrl.rollback(feature_set_id="nonexistent_set")

    def test_rollback_no_prev_active_raises(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        # No second assignment, so prev_active_profile_id is None
        with pytest.raises(ValueError, match="No previous active profile"):
            ctrl.rollback(feature_set_id="lob_shared_v3")


# ---------------------------------------------------------------------------
# from_file — lines 177-190: JSON parsing
# ---------------------------------------------------------------------------


class TestFromFile:
    def test_from_file_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        ctrl = FeatureRolloutController.from_file(tmp_path / "missing.json")
        assert ctrl.assignments() == ()
        assert ctrl.version == 0

    def test_from_file_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        path.write_text("NOT VALID JSON {{{", encoding="utf-8")
        ctrl = FeatureRolloutController.from_file(path)
        assert ctrl.assignments() == ()

    def test_from_file_non_dict_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        ctrl = FeatureRolloutController.from_file(path)
        assert ctrl.assignments() == ()

    def test_from_file_roundtrip_preserves_all_fields(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        ctrl = FeatureRolloutController(path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
            actor="tester",
            notes="initial",
        )
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p2",
            actor="tester2",
        )
        ctrl2 = FeatureRolloutController.from_file(path)
        a = ctrl2.get("lob_shared_v3")
        assert a is not None
        assert a.active_profile_id == "p2"
        assert a.prev_active_profile_id == "p1"
        assert a.actor == "tester2"
        assert ctrl2.version == ctrl.version

    def test_from_file_skips_non_dict_entries_in_sets(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        data = {
            "version": 1,
            "sets": [
                "not_a_dict",
                {
                    "feature_set_id": "lob_shared_v3",
                    "state": "active",
                    "active_profile_id": "p1",
                    "shadow_profile_id": None,
                    "prev_active_profile_id": None,
                    "version": 1,
                    "updated_at": "",
                    "actor": "",
                    "notes": "",
                },
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        ctrl = FeatureRolloutController.from_file(path)
        assert ctrl.get("lob_shared_v3") is not None
        assert len(ctrl.assignments()) == 1

    def test_from_file_skips_entries_without_feature_set_id(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        data = {
            "version": 0,
            "sets": [
                {"state": "active"},  # no feature_set_id
                {
                    "feature_set_id": "lob_shared_v3",
                    "state": "active",
                    "active_profile_id": "p1",
                },
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        ctrl = FeatureRolloutController.from_file(path)
        assert len(ctrl.assignments()) == 1

    def test_from_file_null_profile_ids_become_none(self, tmp_path: Path) -> None:
        path = tmp_path / "rollout.json"
        data = {
            "version": 1,
            "sets": [
                {
                    "feature_set_id": "lob_shared_v3",
                    "state": "disabled",
                    "active_profile_id": None,
                    "shadow_profile_id": "",
                    "prev_active_profile_id": None,
                    "version": 1,
                    "updated_at": "",
                    "actor": "",
                    "notes": "",
                }
            ],
        }
        path.write_text(json.dumps(data), encoding="utf-8")
        ctrl = FeatureRolloutController.from_file(path)
        a = ctrl.get("lob_shared_v3")
        assert a is not None
        assert a.active_profile_id is None
        assert a.shadow_profile_id is None

    def test_save_creates_parent_dirs_if_needed(self, tmp_path: Path) -> None:
        deep_path = tmp_path / "a" / "b" / "c" / "rollout.json"
        ctrl = FeatureRolloutController(deep_path)
        ctrl.set_assignment(
            feature_set_id="lob_shared_v3",
            state="active",
            profile_id="p1",
        )
        assert deep_path.exists()

    def test_assignments_returns_sorted_tuple(self, tmp_path: Path) -> None:
        ctrl = _make_ctrl(tmp_path)
        ctrl.set_assignment(feature_set_id="z_set", state="active", profile_id="p1")
        ctrl.set_assignment(feature_set_id="a_set", state="active", profile_id="p2")
        assignments = ctrl.assignments()
        assert assignments[0].feature_set_id == "a_set"
        assert assignments[1].feature_set_id == "z_set"
