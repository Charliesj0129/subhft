"""Tests for SessionGovernor, SessionPhase, and TrackGate."""

from __future__ import annotations

from hft_platform.ops.session_governor import SessionGovernor, SessionPhase, TrackGate


class TestSessionPhase:
    def test_phase_values(self) -> None:
        assert SessionPhase.PRE_OPEN == "PRE_OPEN"
        assert SessionPhase.OPEN == "OPEN"
        assert SessionPhase.CLOSE_ONLY == "CLOSE_ONLY"
        assert SessionPhase.CLOSED == "CLOSED"


class TestTrackGate:
    def test_gate_ordering(self) -> None:
        assert TrackGate.OPEN < TrackGate.REDUCE_ONLY < TrackGate.CLOSE_ONLY < TrackGate.LOCKED


class TestSessionGovernor:
    def test_initial_state_is_pre_open_locked(self) -> None:
        gov = SessionGovernor()
        assert gov.phase == SessionPhase.PRE_OPEN
        assert gov.effective_gate == TrackGate.LOCKED
        assert gov.is_locked()

    def test_advance_to_open_unlocks_gate(self) -> None:
        gov = SessionGovernor()
        gov.advance_phase(SessionPhase.OPEN)
        assert gov.phase == SessionPhase.OPEN
        assert gov.effective_gate == TrackGate.OPEN
        assert gov.allows_new_orders()
        assert gov.allows_close_orders()

    def test_advance_to_close_only_blocks_new(self) -> None:
        gov = SessionGovernor()
        gov.advance_phase(SessionPhase.OPEN)
        gov.advance_phase(SessionPhase.CLOSE_ONLY)
        assert gov.effective_gate == TrackGate.CLOSE_ONLY
        assert not gov.allows_new_orders()
        assert gov.allows_close_orders()

    def test_override_gate_takes_precedence(self) -> None:
        gov = SessionGovernor()
        gov.advance_phase(SessionPhase.OPEN)
        assert gov.allows_new_orders()
        gov.set_override_gate(TrackGate.REDUCE_ONLY)
        assert not gov.allows_new_orders()
        assert gov.effective_gate == TrackGate.REDUCE_ONLY

    def test_clear_override_restores_phase_gate(self) -> None:
        gov = SessionGovernor()
        gov.advance_phase(SessionPhase.OPEN)
        gov.set_override_gate(TrackGate.LOCKED)
        assert gov.is_locked()
        gov.clear_override_gate()
        assert gov.allows_new_orders()

    def test_on_phase_change_callback(self) -> None:
        phases: list[SessionPhase] = []
        gov = SessionGovernor(on_phase_change=lambda p: phases.append(p))
        gov.advance_phase(SessionPhase.OPEN)
        gov.advance_phase(SessionPhase.CLOSED)
        assert phases == [SessionPhase.OPEN, SessionPhase.CLOSED]

    def test_snapshot_returns_expected_keys(self) -> None:
        gov = SessionGovernor()
        snap = gov.snapshot()
        assert "phase" in snap
        assert "effective_gate" in snap
        assert "override_gate" in snap
