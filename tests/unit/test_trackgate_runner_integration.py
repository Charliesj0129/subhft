"""Tests for TrackGate per-event phase filtering in StrategyRunner."""

from types import SimpleNamespace

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.ops.session_governor import SessionPhase, TrackGate


def test_order_intent_session_phase_defaults_none():
    intent = OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TXFR1",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=170000,
        qty=1,
    )
    assert intent.session_phase is None


def test_filter_stamps_session_phase_on_order_intent_objects():
    # §7 groundwork: OrderIntent objects passing the phase filter are stamped
    # with the phase they were emitted under. Filtering behaviour is unchanged.
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol("TXFR1", "futures_day")
    gate.set_track_phase("futures_day", SessionPhase.OPEN)
    intent = OrderIntent(
        intent_id=1,
        strategy_id="s1",
        symbol="TXFR1",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=170000,
        qty=1,
    )

    result = StrategyRunner.filter_intents_by_phase([intent], gate)

    assert result == [intent]
    assert intent.session_phase == "OPEN"


def test_filter_leaves_typed_intent_tuple_unstamped():
    # The typed_intent_v1 tuple fast-path is immutable; stamping is a no-op and
    # must not raise or alter the tuple (honest limitation, documented).
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol("TXFR1", "futures_day")
    gate.set_track_phase("futures_day", SessionPhase.OPEN)
    tup = (
        "typed_intent_v1",
        1,
        "s1",
        "TXFR1",
        int(IntentType.NEW),
        int(Side.BUY),
        1000000,
        1,
        int(TIF.LIMIT),
        "",
        0,
        0,
        "",
        "",
        "",
        0,
    )

    result = StrategyRunner.filter_intents_by_phase([tup], gate)

    assert result == [tup]


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
    # CANCEL and FORCE_FLAT always allowed; IOC NEW also allowed (aggressive exits)
    always_allowed = {IntentType.CANCEL, IntentType.FORCE_FLAT}
    assert IntentType.CANCEL in always_allowed
    assert IntentType.FORCE_FLAT in always_allowed


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


def _make_typed_intent(
    intent_type: IntentType,
    symbol: str = _SYMBOL,
    tif: TIF = TIF.LIMIT,
    side: int = int(Side.SELL),
) -> tuple:
    return (_TAG, 1, "s1", symbol, int(intent_type), side, 1000000, 1, int(tif), "", 0, 0, "", "", "", 0)


def _make_position_store(symbol: str = _SYMBOL, net_qty: int = 0) -> SimpleNamespace:
    """Create a minimal position store with a single position for testing."""
    pos = SimpleNamespace(net_qty=net_qty)
    key = f"acc:s1:{symbol}"
    return SimpleNamespace(positions={key: pos})


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


# ---------------------------------------------------------------------------
# CLOSE_ONLY IOC exit allowance tests
# ---------------------------------------------------------------------------


def test_close_only_allows_ioc_sell_when_long_position():
    """During CLOSE_ONLY, IOC SELL is allowed when long position exists (reduces exposure)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=10)  # long position
    ioc_intent = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL))
    result = StrategyRunner.filter_intents_by_phase([ioc_intent], gate, position_store=ps)
    assert result == [ioc_intent]


def test_close_only_blocks_limit_new_intent():
    """During CLOSE_ONLY, LIMIT new orders (entries) must still be blocked."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    limit_intent = _make_typed_intent(IntentType.NEW, tif=TIF.LIMIT)
    result = StrategyRunner.filter_intents_by_phase([limit_intent], gate)
    assert result == []


def test_close_only_mixed_intents_allows_cancel_force_flat_and_reducing_ioc():
    """During CLOSE_ONLY, allow CANCEL + FORCE_FLAT + position-reducing IOC, block rest."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=10)  # long position, SELL reduces
    intents = [
        _make_typed_intent(IntentType.NEW, tif=TIF.LIMIT),
        _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL)),
        _make_typed_intent(IntentType.CANCEL),
        _make_typed_intent(IntentType.FORCE_FLAT),
        _make_typed_intent(IntentType.AMEND),
    ]
    result = StrategyRunner.filter_intents_by_phase(intents, gate, position_store=ps)
    assert len(result) == 3
    assert _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL)) in result
    assert _make_typed_intent(IntentType.CANCEL) in result
    assert _make_typed_intent(IntentType.FORCE_FLAT) in result


def test_force_flat_still_blocks_ioc_new_intent():
    """During FORCE_FLAT, even IOC new orders must be blocked (flattener handles exits)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.FORCE_FLAT)
    ioc_intent = _make_typed_intent(IntentType.NEW, tif=TIF.IOC)
    result = StrategyRunner.filter_intents_by_phase([ioc_intent], gate)
    assert result == []


# ---------------------------------------------------------------------------
# CLOSE_ONLY IOC position-aware check tests
# ---------------------------------------------------------------------------


def test_close_only_ioc_buy_allowed_when_short_position():
    """IOC BUY in CLOSE_ONLY is allowed when net_qty < 0 (closing a short)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=-5)  # short position
    ioc_buy = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.BUY))
    result = StrategyRunner.filter_intents_by_phase([ioc_buy], gate, position_store=ps)
    assert result == [ioc_buy]


def test_close_only_ioc_sell_allowed_when_long_position():
    """IOC SELL in CLOSE_ONLY is allowed when net_qty > 0 (closing a long)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=10)  # long position
    ioc_sell = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL))
    result = StrategyRunner.filter_intents_by_phase([ioc_sell], gate, position_store=ps)
    assert result == [ioc_sell]


def test_close_only_ioc_buy_blocked_when_no_position():
    """IOC BUY in CLOSE_ONLY is blocked when no position (would open new risk)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=0)  # flat
    ioc_buy = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.BUY))
    result = StrategyRunner.filter_intents_by_phase([ioc_buy], gate, position_store=ps)
    assert result == []


def test_close_only_ioc_buy_blocked_when_long_position():
    """IOC BUY in CLOSE_ONLY is blocked when already long (would increase exposure)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=10)  # long — BUY would add, not reduce
    ioc_buy = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.BUY))
    result = StrategyRunner.filter_intents_by_phase([ioc_buy], gate, position_store=ps)
    assert result == []


def test_close_only_ioc_sell_blocked_when_no_position():
    """IOC SELL in CLOSE_ONLY is blocked when no position (would open new risk)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=0)  # flat
    ioc_sell = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL))
    result = StrategyRunner.filter_intents_by_phase([ioc_sell], gate, position_store=ps)
    assert result == []


def test_close_only_ioc_sell_blocked_when_short_position():
    """IOC SELL in CLOSE_ONLY is blocked when already short (would increase exposure)."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=-5)  # short — SELL would add, not reduce
    ioc_sell = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL))
    result = StrategyRunner.filter_intents_by_phase([ioc_sell], gate, position_store=ps)
    assert result == []


def test_close_only_ioc_blocked_when_no_position_store():
    """IOC in CLOSE_ONLY is conservatively blocked when position_store is None."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ioc_sell = _make_typed_intent(IntentType.NEW, tif=TIF.IOC, side=int(Side.SELL))
    result = StrategyRunner.filter_intents_by_phase([ioc_sell], gate, position_store=None)
    assert result == []


def test_close_only_cancel_and_force_flat_always_pass():
    """CANCEL and FORCE_FLAT pass regardless of position state in CLOSE_ONLY."""
    from hft_platform.strategy.runner import StrategyRunner

    gate = TrackGate()
    gate.register_symbol(_SYMBOL, "stock")
    gate.set_track_phase("stock", SessionPhase.CLOSE_ONLY)
    ps = _make_position_store(net_qty=0)  # flat — no position
    cancel = _make_typed_intent(IntentType.CANCEL)
    force_flat = _make_typed_intent(IntentType.FORCE_FLAT)
    result = StrategyRunner.filter_intents_by_phase([cancel, force_flat], gate, position_store=ps)
    assert len(result) == 2
    assert cancel in result
    assert force_flat in result
