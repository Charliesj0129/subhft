"""Tests for SessionGovernor: SessionPhase ordering, TrackGate, config loading."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from hft_platform.ops.session_governor import SessionGovernor, SessionPhase, TrackGate


def _write_config(tmp_path: Path) -> Path:
    config = {
        "tracks": {
            "futures_day": {
                "symbols": ["TMFD6"],
                "schedule": [
                    {"phase": "open", "time": "08:45"},
                    {"phase": "close_only", "time": "13:40"},
                    {"phase": "force_flat", "time": "13:44"},
                    {"phase": "closed", "time": "13:45"},
                ],
            },
            "futures_night": {
                "symbols": ["TMFD6"],
                "schedule": [
                    {"phase": "open", "time": "15:00"},
                    {"phase": "close_only", "time": "04:55"},
                    {"phase": "force_flat", "time": "04:59"},
                    {"phase": "closed", "time": "05:00"},
                ],
            },
        }
    }
    cfg_path = tmp_path / "session_governor.yaml"
    cfg_path.write_text(yaml.dump(config), encoding="utf-8")
    return cfg_path


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
    def test_unknown_symbol_defaults_to_closed(self) -> None:
        gate = TrackGate()
        assert gate.get_phase("UNKNOWN") == SessionPhase.CLOSED

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
        assert gate.get_phase("2330") == SessionPhase.CLOSED  # unknown symbol, default CLOSED

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
        assert gov.track_gate.get_phase("ANY") == SessionPhase.CLOSED

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

    def test_phase_for_dt_uses_day_schedule(self, tmp_path: Path) -> None:
        gov = SessionGovernor(config_path=_write_config(tmp_path))

        close_only = gov._phase_for_dt("futures_day", datetime(2026, 3, 30, 13, 40, tzinfo=gov._tz))
        forced = gov._phase_for_dt("futures_day", datetime(2026, 3, 30, 13, 44, tzinfo=gov._tz))

        assert close_only == SessionPhase.CLOSE_ONLY
        assert forced == SessionPhase.FORCE_FLAT

    def test_phase_for_dt_handles_overnight_wraparound(self, tmp_path: Path) -> None:
        gov = SessionGovernor(config_path=_write_config(tmp_path))

        late_evening = gov._phase_for_dt("futures_night", datetime(2026, 3, 30, 23, 0, tzinfo=gov._tz))
        pre_close = gov._phase_for_dt("futures_night", datetime(2026, 3, 31, 4, 56, tzinfo=gov._tz))
        closed = gov._phase_for_dt("futures_night", datetime(2026, 3, 31, 5, 0, tzinfo=gov._tz))

        assert late_evening == SessionPhase.OPEN
        assert pre_close == SessionPhase.CLOSE_ONLY
        assert closed == SessionPhase.CLOSED

    @pytest.mark.asyncio
    async def test_force_flat_phase_invokes_position_flattener(self, tmp_path: Path) -> None:
        flattener = AsyncMock()
        gov = SessionGovernor(config_path=_write_config(tmp_path), position_flattener=flattener)

        gov.transition_track("futures_day", SessionPhase.FORCE_FLAT)
        await asyncio.sleep(0)

        flattener.flatten_track.assert_awaited_once_with("futures_day", ["TMFD6"])

    @pytest.mark.asyncio
    async def test_start_and_stop_manage_background_task(self, tmp_path: Path) -> None:
        gov = SessionGovernor(config_path=_write_config(tmp_path))
        gov._poll_interval_s = 0.01

        await gov.start()
        assert gov._task is not None
        assert gov._running is True

        await gov.stop()
        assert gov._task is None
        assert gov._running is False

    def test_tmfd6_is_consistent_across_strategy_and_session_config(self) -> None:
        strategies = yaml.safe_load(Path("config/base/strategies.yaml").read_text(encoding="utf-8"))
        sessions = yaml.safe_load(Path("config/base/session_governor.yaml").read_text(encoding="utf-8"))

        cbs = next(item for item in strategies["strategies"] if item["id"] == "CBS_TMFD6")
        assert cbs["symbols"] == ["TMFD6"]
        assert sessions["tracks"]["futures_day"]["symbols"] == ["TMFD6"]
        assert sessions["tracks"]["futures_night"]["symbols"] == ["TMFD6"]
