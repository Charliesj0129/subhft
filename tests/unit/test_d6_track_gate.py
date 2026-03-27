"""D6: Unknown symbols must default to CLOSED; gating must be per-intent, not per-event."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hft_platform.ops.session_governor import SessionPhase, TrackGate


class TestTrackGateDefaultClosed:
    def test_unknown_symbol_returns_closed(self):
        gate = TrackGate()
        assert gate.get_phase("UNKNOWN_SYMBOL") == SessionPhase.CLOSED

    def test_registered_symbol_returns_correct_phase(self):
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)
        assert gate.get_phase("TXFD6") == SessionPhase.OPEN

    def test_registered_track_no_phase_returns_closed(self):
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        assert gate.get_phase("TXFD6") == SessionPhase.CLOSED

    def test_warning_logged_once_per_unknown_symbol(self, capsys):
        gate = TrackGate()
        gate.get_phase("NEW_SYM")
        gate.get_phase("NEW_SYM")
        gate.get_phase("NEW_SYM")
        # structlog uses PrintLoggerFactory in this project — check stdout
        captured = capsys.readouterr()
        count = captured.out.count("track_gate_unknown_symbol_blocked")
        assert count == 1, f"Expected 1 warning, got {count}"

    def test_env_override_restores_open_default(self):
        with patch.dict(os.environ, {"HFT_TRACK_GATE_DEFAULT_OPEN": "1"}):
            gate = TrackGate()
            assert gate.get_phase("UNKNOWN") == SessionPhase.OPEN


class TestPerIntentGating:
    def test_intent_for_unregistered_symbol_is_filtered(self):
        """Strategy triggered by registered symbol emits intent for unregistered symbol."""
        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.OPEN)

        intent_registered = MagicMock()
        intent_registered.symbol = "TXFD6"
        intent_registered.intent_type = 1  # NEW

        intent_unregistered = MagicMock()
        intent_unregistered.symbol = "NEWHEDGE"
        intent_unregistered.intent_type = 1  # NEW

        intents = [intent_registered, intent_unregistered]

        from hft_platform.contracts.strategy import IntentType

        _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
        filtered = []
        for intent in intents:
            phase = gate.get_phase(intent.symbol)
            if phase == SessionPhase.OPEN:
                filtered.append(intent)
            elif phase == SessionPhase.CLOSE_ONLY:
                if intent.intent_type in _CLOSE_ONLY_TYPES:
                    filtered.append(intent)

        assert len(filtered) == 1
        assert filtered[0].symbol == "TXFD6"

    def test_close_only_allows_cancel_intent(self):
        """During CLOSE_ONLY, CANCEL intents pass through."""
        from hft_platform.contracts.strategy import IntentType

        gate = TrackGate()
        gate.register_symbol("TXFD6", "futures_day")
        gate.set_track_phase("futures_day", SessionPhase.CLOSE_ONLY)

        cancel_intent = MagicMock()
        cancel_intent.symbol = "TXFD6"
        cancel_intent.intent_type = IntentType.CANCEL

        new_intent = MagicMock()
        new_intent.symbol = "TXFD6"
        new_intent.intent_type = IntentType.NEW

        filtered = []
        _CLOSE_ONLY_TYPES = (IntentType.CANCEL, IntentType.FORCE_FLAT)
        for intent in [cancel_intent, new_intent]:
            phase = gate.get_phase(intent.symbol)
            if phase == SessionPhase.OPEN:
                filtered.append(intent)
            elif phase == SessionPhase.CLOSE_ONLY:
                if intent.intent_type in _CLOSE_ONLY_TYPES:
                    filtered.append(intent)

        assert len(filtered) == 1
        assert filtered[0] is cancel_intent
