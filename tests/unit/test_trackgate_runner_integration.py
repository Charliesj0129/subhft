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


def test_unknown_symbol_defaults_to_closed():
    """D6: Unknown symbols now default to CLOSED (fail-safe)."""
    gate = TrackGate()
    assert gate.get_phase("UNKNOWN_SYM") == SessionPhase.CLOSED


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


# ---------------------------------------------------------------------------
# Runner filtering tests for FORCE_FLAT phase
# ---------------------------------------------------------------------------

# typed_intent_v1 tuple layout: (tag, version, strategy_id, symbol, intent_type, ...)
# IntentType: NEW=0, AMEND=1, CANCEL=2, FORCE_FLAT=3
_SYMBOL = "TSMC"
_TAG = "typed_intent_v1"


def _make_typed_intent(intent_type: IntentType, symbol: str = _SYMBOL) -> tuple:
    return (_TAG, 1, "s1", symbol, int(intent_type), 1, 1000000, 1, 0, "", 0, 0, "", "", "", 0)


def _build_runner_with_gate(phase: SessionPhase):
    from unittest.mock import MagicMock

    from hft_platform.strategy.runner import StrategyRunner

    runner = StrategyRunner(bus=MagicMock(), risk_queue=MagicMock())
    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", phase)
    runner.track_gate = gate
    return runner


def test_force_flat_allows_cancel_intent():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    cancel_intent = _make_typed_intent(IntentType.CANCEL)
    result = StrategyRunner.filter_intents_by_phase([cancel_intent], gate)
    assert result == [cancel_intent]


def test_force_flat_allows_force_flat_intent():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    ff_intent = _make_typed_intent(IntentType.FORCE_FLAT)
    result = StrategyRunner.filter_intents_by_phase([ff_intent], gate)
    assert result == [ff_intent]


def test_force_flat_blocks_new_intent():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    new_intent = _make_typed_intent(IntentType.NEW)
    result = StrategyRunner.filter_intents_by_phase([new_intent], gate)
    assert result == []


def test_force_flat_blocks_amend_intent():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    amend_intent = _make_typed_intent(IntentType.AMEND)
    result = StrategyRunner.filter_intents_by_phase([amend_intent], gate)
    assert result == []


def test_force_flat_mixed_intents_only_allows_cancel_and_force_flat():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    intents = [
        _make_typed_intent(IntentType.NEW),
        _make_typed_intent(IntentType.CANCEL),
        _make_typed_intent(IntentType.FORCE_FLAT),
        _make_typed_intent(IntentType.AMEND),
    ]
    result = StrategyRunner.filter_intents_by_phase(intents, gate)
    assert len(result) == 2
    assert _make_typed_intent(IntentType.CANCEL) in result
    assert _make_typed_intent(IntentType.FORCE_FLAT) in result


def test_closed_phase_blocks_all_intents():
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSED)
    intents = [
        _make_typed_intent(IntentType.NEW),
        _make_typed_intent(IntentType.CANCEL),
        _make_typed_intent(IntentType.FORCE_FLAT),
        _make_typed_intent(IntentType.AMEND),
    ]
    result = StrategyRunner.filter_intents_by_phase(intents, gate)
    assert result == []
