"""Tests for TriggerExecutor strategy-side helper."""
import pytest


def _make_intent(price=200000000, symbol="TXFD6"):
    from hft_platform.contracts.strategy import IntentType, OrderIntent, Side
    return OrderIntent(intent_id=1, strategy_id="test", symbol=symbol,
        intent_type=IntentType.NEW, side=Side.SELL, price=price, qty=1)


def test_register_returns_trigger_id():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    tid = te.register("TXFD6", TriggerCondition.LE(195000000), _make_intent())
    assert isinstance(tid, str) and len(tid) > 0


def test_on_tick_fires_when_condition_met():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    intent = _make_intent()
    te.register("TXFD6", TriggerCondition.LE(195000000), intent)
    fired = te.on_tick("TXFD6", 194000000)
    assert len(fired) == 1 and fired[0] is intent


def test_on_tick_does_not_fire_when_condition_not_met():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    te.register("TXFD6", TriggerCondition.LE(195000000), _make_intent())
    assert len(te.on_tick("TXFD6", 196000000)) == 0


def test_trigger_is_one_shot():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    te.register("TXFD6", TriggerCondition.LE(195000000), _make_intent())
    fired1 = te.on_tick("TXFD6", 194000000)
    fired2 = te.on_tick("TXFD6", 194000000)
    assert len(fired1) == 1 and len(fired2) == 0


def test_ge_condition():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    te.register("TXFD6", TriggerCondition.GE(205000000), _make_intent())
    assert len(te.on_tick("TXFD6", 206000000)) == 1


def test_cancel_trigger():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    tid = te.register("TXFD6", TriggerCondition.LE(195000000), _make_intent())
    assert te.cancel(tid) is True
    assert len(te.on_tick("TXFD6", 194000000)) == 0


def test_cancel_nonexistent_returns_false():
    from hft_platform.execution.trigger_executor import TriggerExecutor
    assert TriggerExecutor().cancel("nonexistent") is False


def test_max_triggers_bounded():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor(max_triggers=3)
    for i in range(3):
        te.register("TXFD6", TriggerCondition.LE(i), _make_intent())
    with pytest.raises(ValueError, match="max triggers"):
        te.register("TXFD6", TriggerCondition.LE(999), _make_intent())


def test_wrong_symbol_no_fire():
    from hft_platform.execution.trigger_executor import TriggerCondition, TriggerExecutor
    te = TriggerExecutor()
    te.register("TXFD6", TriggerCondition.LE(195000000), _make_intent())
    assert len(te.on_tick("MXFD6", 194000000)) == 0
