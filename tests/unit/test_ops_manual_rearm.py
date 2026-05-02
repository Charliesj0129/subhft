"""Unit tests for hft_platform.ops.manual_rearm.ManualRearmGate.

Covers: construction, rearm_strategy, rearm_platform, requires_manual_rearm,
snapshot, _load_state, _write_state, _default_state, _platform_section,
_strategies_section — targeting ≥80% line coverage.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hft_platform.ops.manual_rearm import ManualRearmGate

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestManualRearmGateConstruction:
    def test_default_state_path_is_set(self) -> None:
        gate = ManualRearmGate()
        assert gate.state_path is not None
        assert str(gate.state_path).endswith("runtime_state.json")

    def test_custom_state_path_string(self, tmp_path: Path) -> None:
        p = str(tmp_path / "state.json")
        gate = ManualRearmGate(state_path=p)
        assert gate.state_path == Path(p)

    def test_custom_state_path_pathlib(self, tmp_path: Path) -> None:
        p = tmp_path / "sub" / "state.json"
        gate = ManualRearmGate(state_path=p)
        assert gate.state_path == p


# ---------------------------------------------------------------------------
# _load_state: missing file returns default state
# ---------------------------------------------------------------------------


class TestLoadState:
    def test_missing_file_returns_default(self, tmp_path: Path) -> None:
        gate = ManualRearmGate(state_path=tmp_path / "nonexistent.json")
        state = gate._load_state()
        assert state == {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {},
        }

    def test_valid_file_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": True, "reason": "test"},
                "strategies": {"s1": {"manual_rearm_required": True, "reason": "x"}},
            },
        )
        gate = ManualRearmGate(state_path=path)
        state = gate._load_state()
        assert state["platform"]["manual_rearm_required"] is True
        assert state["strategies"]["s1"]["manual_rearm_required"] is True

    def test_non_dict_json_returns_default(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        gate = ManualRearmGate(state_path=path)
        state = gate._load_state()
        assert state["platform"]["manual_rearm_required"] is False
        assert state["strategies"] == {}

    def test_file_missing_platform_key_is_normalised(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(path, {"strategies": {}})
        gate = ManualRearmGate(state_path=path)
        state = gate._load_state()
        assert "platform" in state
        assert "manual_rearm_required" in state["platform"]

    def test_file_missing_strategies_key_is_normalised(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(path, {"platform": {"manual_rearm_required": False, "reason": None}})
        gate = ManualRearmGate(state_path=path)
        state = gate._load_state()
        assert "strategies" in state
        assert isinstance(state["strategies"], dict)


# ---------------------------------------------------------------------------
# _write_state: atomic write via .tmp rename
# ---------------------------------------------------------------------------


class TestWriteState:
    def test_write_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "state.json"
        gate = ManualRearmGate(state_path=path)
        state = gate._default_state()
        gate._write_state(state)
        assert path.exists()

    def test_write_round_trips(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        original = {
            "platform": {"manual_rearm_required": True, "reason": "test"},
            "strategies": {"s1": {"manual_rearm_required": False, "reason": None}},
        }
        gate._write_state(original)
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded == original

    def test_no_tmp_file_left_after_write(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        gate._write_state(gate._default_state())
        tmp = path.with_suffix(".json.tmp")
        assert not tmp.exists()


# ---------------------------------------------------------------------------
# rearm_strategy
# ---------------------------------------------------------------------------


class TestRearmStrategy:
    def test_rearm_strategy_clears_flag(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"alpha": {"manual_rearm_required": True, "reason": "halt"}},
            },
        )
        gate = ManualRearmGate(state_path=path)
        gate.rearm_strategy("alpha")
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["strategies"]["alpha"]["manual_rearm_required"] is False
        assert reloaded["strategies"]["alpha"]["reason"] is None

    def test_rearm_strategy_raises_when_not_required(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"alpha": {"manual_rearm_required": False, "reason": None}},
            },
        )
        gate = ManualRearmGate(state_path=path)
        with pytest.raises(ValueError, match="does not require manual re-arm"):
            gate.rearm_strategy("alpha")

    def test_rearm_strategy_raises_for_unknown_strategy(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {},
            },
        )
        gate = ManualRearmGate(state_path=path)
        with pytest.raises(ValueError, match="does not require manual re-arm"):
            gate.rearm_strategy("nonexistent")

    def test_rearm_strategy_raises_when_value_not_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"alpha": "bad_value"},
            },
        )
        gate = ManualRearmGate(state_path=path)
        with pytest.raises(ValueError):
            gate.rearm_strategy("alpha")

    def test_rearm_strategy_works_without_preexisting_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        # Create a state with rearm required, write it, then test rearm raises (no file initially)
        gate = ManualRearmGate(state_path=path)
        # No file exists -> strategy not present -> should raise
        with pytest.raises(ValueError):
            gate.rearm_strategy("alpha")


# ---------------------------------------------------------------------------
# rearm_platform
# ---------------------------------------------------------------------------


class TestRearmPlatform:
    def test_rearm_platform_clears_flag(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": True, "reason": "storm"},
                "strategies": {},
            },
        )
        gate = ManualRearmGate(state_path=path)
        gate.rearm_platform()
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["platform"]["manual_rearm_required"] is False
        assert reloaded["platform"]["reason"] is None

    def test_rearm_platform_when_already_false(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {},
            },
        )
        gate = ManualRearmGate(state_path=path)
        # Should not raise — idempotent
        gate.rearm_platform()
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert reloaded["platform"]["manual_rearm_required"] is False

    def test_rearm_platform_creates_file_when_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        gate.rearm_platform()
        assert path.exists()


# ---------------------------------------------------------------------------
# requires_manual_rearm
# ---------------------------------------------------------------------------


class TestRequiresManualRearm:
    def test_platform_scope_true(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": True, "reason": "x"},
                "strategies": {},
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("platform") is True

    def test_platform_scope_false(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("platform") is False

    def test_platform_scope_case_insensitive(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("PLATFORM") is False
        assert gate.requires_manual_rearm("  Platform  ") is False

    def test_strategy_scope_specific_id_true(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"s1": {"manual_rearm_required": True, "reason": "x"}},
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy", strategy_id="s1") is True

    def test_strategy_scope_specific_id_false(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"s1": {"manual_rearm_required": False, "reason": None}},
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy", strategy_id="s1") is False

    def test_strategy_scope_missing_id_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy", strategy_id="nope") is False

    def test_strategy_scope_any_returns_true_when_any_required(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {
                    "s1": {"manual_rearm_required": False, "reason": None},
                    "s2": {"manual_rearm_required": True, "reason": "halt"},
                },
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy") is True

    def test_strategy_scope_any_returns_false_when_none_required(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {
                    "s1": {"manual_rearm_required": False, "reason": None},
                    "s2": {"manual_rearm_required": False, "reason": None},
                },
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy") is False

    def test_strategy_scope_any_with_non_dict_entry_is_skipped(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"s1": "bad"},
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy") is False

    def test_unsupported_scope_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        gate = ManualRearmGate(state_path=path)
        with pytest.raises(ValueError, match="unsupported scope"):
            gate.requires_manual_rearm("global")

    def test_strategy_scope_specific_id_non_dict_returns_false(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": False, "reason": None},
                "strategies": {"s1": "bad_value"},
            },
        )
        gate = ManualRearmGate(state_path=path)
        assert gate.requires_manual_rearm("strategy", strategy_id="s1") is False


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_returns_loaded_state(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        _write_state(
            path,
            {
                "platform": {"manual_rearm_required": True, "reason": "test"},
                "strategies": {},
            },
        )
        gate = ManualRearmGate(state_path=path)
        snap = gate.snapshot()
        assert snap["platform"]["manual_rearm_required"] is True

    def test_snapshot_returns_default_when_no_file(self, tmp_path: Path) -> None:
        gate = ManualRearmGate(state_path=tmp_path / "missing.json")
        snap = gate.snapshot()
        assert snap == {
            "platform": {"manual_rearm_required": False, "reason": None},
            "strategies": {},
        }


# ---------------------------------------------------------------------------
# Static helpers
# ---------------------------------------------------------------------------


class TestStaticHelpers:
    def test_default_state_shape(self) -> None:
        state = ManualRearmGate._default_state()
        assert state["platform"]["manual_rearm_required"] is False
        assert state["platform"]["reason"] is None
        assert state["strategies"] == {}

    def test_platform_section_creates_if_missing(self) -> None:
        state: dict = {}
        section = ManualRearmGate._platform_section(state)
        assert "platform" in state
        assert section["manual_rearm_required"] is False

    def test_platform_section_replaces_non_dict(self) -> None:
        state: dict = {"platform": "bad"}
        ManualRearmGate._platform_section(state)
        assert isinstance(state["platform"], dict)

    def test_platform_section_fills_defaults(self) -> None:
        state: dict = {"platform": {}}
        section = ManualRearmGate._platform_section(state)
        assert "manual_rearm_required" in section
        assert "reason" in section

    def test_strategies_section_creates_if_missing(self) -> None:
        state: dict = {}
        section = ManualRearmGate._strategies_section(state)
        assert "strategies" in state
        assert isinstance(section, dict)

    def test_strategies_section_replaces_non_dict(self) -> None:
        state: dict = {"strategies": "bad"}
        ManualRearmGate._strategies_section(state)
        assert isinstance(state["strategies"], dict)
