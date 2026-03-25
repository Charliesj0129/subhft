"""Tests for SessionGovernor: SessionPhase ordering, TrackGate, config loading."""

from __future__ import annotations

from pathlib import Path

import yaml

from hft_platform.ops.session_governor import SessionGovernor, SessionPhase, TrackGate


class TestSessionPhaseOrdering:
    def test_init_is_lowest(self) -> None:
        assert SessionPhase.INIT < SessionPhase.PRE_OPEN

    def test_open_after_pre_open(self) -> None:
        assert SessionPhase.PRE_OPEN < SessionPhase.OPEN

    def test_closed_is_highest(self) -> None:
        assert SessionPhase.CLOSED > SessionPhase.FORCE_FLAT

    def test_full_ordering(self) -> None:
        phases = [
            SessionPhase.INIT,
            SessionPhase.PRE_OPEN,
            SessionPhase.OPEN,
            SessionPhase.CLOSE_ONLY,
            SessionPhase.FORCE_FLAT,
            SessionPhase.CLOSED,
        ]
        for i in range(len(phases) - 1):
            assert phases[i] < phases[i + 1]


class TestTrackGate:
    def test_unknown_symbol_defaults_to_open(self) -> None:
        gate = TrackGate()
        assert gate.get_phase("UNKNOWN") == SessionPhase.OPEN

    def test_register_and_query(self) -> None:
        gate = TrackGate()
        gate.register_symbol("2330", "stock")
        gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
        assert gate.get_phase("2330") == SessionPhase.CLOSE_ONLY

    def test_track_phases_snapshot_is_independent(self) -> None:
        gate = TrackGate()
        gate.set_track_phase("stock", SessionPhase.OPEN)
        snapshot = gate.track_phases
        gate.set_track_phase("stock", SessionPhase.CLOSED)
        assert snapshot["stock"] == SessionPhase.OPEN
        assert gate.get_phase("2330") == SessionPhase.OPEN  # unknown symbol, default

    def test_symbol_to_track_snapshot(self) -> None:
        gate = TrackGate()
        gate.register_symbol("2330", "stock")
        snap = gate.symbol_to_track
        assert snap["2330"] == "stock"


class TestSessionGovernorConfigLoading:
    def test_loads_tracks_from_yaml(self, tmp_path: Path) -> None:
        config = {
            "tracks": {
                "stock": {
                    "symbols": ["2330", "2317"],
                    "schedule": [{"phase": "open", "time": "09:00"}],
                },
            }
        }
        cfg_path = tmp_path / "session_governor.yaml"
        cfg_path.write_text(yaml.dump(config), encoding="utf-8")

        gov = SessionGovernor(config_path=cfg_path)
        gate = gov.track_gate
        assert gate.get_phase("2330") == SessionPhase.INIT
        assert gate.get_phase("2317") == SessionPhase.INIT

    def test_missing_config_does_not_raise(self, tmp_path: Path) -> None:
        gov = SessionGovernor(config_path=tmp_path / "nonexistent.yaml")
        assert gov.track_gate.get_phase("ANY") == SessionPhase.OPEN

    def test_transition_track_fires_callback(self, tmp_path: Path) -> None:
        config = {"tracks": {"stock": {"symbols": ["2330"], "schedule": []}}}
        cfg_path = tmp_path / "session_governor.yaml"
        cfg_path.write_text(yaml.dump(config), encoding="utf-8")

        gov = SessionGovernor(config_path=cfg_path)
        captured: list[tuple] = []
        gov.register_phase_callback(lambda t, o, n: captured.append((t, o, n)))
        gov.transition_track("stock", SessionPhase.OPEN)
        assert len(captured) == 1
        assert captured[0] == ("stock", SessionPhase.INIT, SessionPhase.OPEN)
