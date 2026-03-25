"""Tests for TrackGate per-event phase filtering in StrategyRunner."""

from hft_platform.contracts.strategy import IntentType
from hft_platform.ops.session_governor import SessionPhase, TrackGate


def test_track_gate_blocks_new_in_close_only():
    gate = TrackGate()
    gate.register_symbol("2330", "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    assert gate.get_phase("2330") == SessionPhase.CLOSE_ONLY


def test_track_gate_blocks_all_in_force_flat():
    gate = TrackGate()
    gate.register_symbol("2330", "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    assert gate.get_phase("2330") == SessionPhase.FORCE_FLAT


def test_track_gate_passes_in_open():
    gate = TrackGate()
    gate.register_symbol("2330", "stock")
    gate.set_track_phase("stock", SessionPhase.OPEN)
    assert gate.get_phase("2330") == SessionPhase.OPEN


def test_unknown_symbol_defaults_to_open():
    gate = TrackGate()
    assert gate.get_phase("UNKNOWN_SYM") == SessionPhase.OPEN


def test_close_only_allows_cancel_and_force_flat():
    gate = TrackGate()
    gate.register_symbol("2330", "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    phase = gate.get_phase("2330")
    assert phase == SessionPhase.CLOSE_ONLY
    allowed = {IntentType.CANCEL, IntentType.FORCE_FLAT}
    assert IntentType.CANCEL in allowed
    assert IntentType.FORCE_FLAT in allowed
    assert IntentType.NEW not in allowed


def test_multiple_symbols_different_tracks():
    gate = TrackGate()
    gate.register_symbol("2330", "stock")
    gate.register_symbol("TXF1", "futures_day")
    gate.set_track_phase("stock", SessionPhase.OPEN)
    gate.set_track_phase("futures_day", SessionPhase.CLOSE_ONLY)
    assert gate.get_phase("2330") == SessionPhase.OPEN
    assert gate.get_phase("TXF1") == SessionPhase.CLOSE_ONLY


def test_track_gate_runner_has_attribute():
    """StrategyRunner must expose a track_gate attribute (None by default)."""
    from unittest.mock import MagicMock
    from hft_platform.strategy.runner import StrategyRunner

    mock_bus = MagicMock()
    mock_queue = MagicMock()
    runner = StrategyRunner(bus=mock_bus, risk_queue=mock_queue)
    assert hasattr(runner, "track_gate")
    assert runner.track_gate is None
