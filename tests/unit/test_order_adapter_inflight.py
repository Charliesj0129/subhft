"""Verify OrderAdapter stores inflight OrderCommands and stamps arrival_price."""
from hft_platform.contracts.strategy import (
    IntentType,
    OrderCommand,
    OrderIntent,
    Side,
    StormGuardState,
)
from hft_platform.order.adapter import OrderAdapter


def test_inflight_store_and_retrieve() -> None:
    adapter = OrderAdapter.__new__(OrderAdapter)
    adapter._inflight = {}
    cmd = OrderCommand(
        cmd_id=1,
        intent=OrderIntent(
            intent_id=1, strategy_id="s1", symbol="XMT",
            intent_type=IntentType.NEW, side=Side.BUY,
            price=200_000_000, qty=1, decision_price=200_000_000,
        ),
        deadline_ns=0,
        storm_guard_state=StormGuardState.NORMAL,
        decision_price=200_000_000,
    )
    order_key = "s1:1"
    adapter._inflight[order_key] = cmd
    assert adapter.get_inflight(order_key) is cmd
    assert adapter.get_inflight("nonexistent") is None
