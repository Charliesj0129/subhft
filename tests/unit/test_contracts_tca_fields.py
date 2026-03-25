"""Verify TCA fields exist on contracts with correct defaults."""
from hft_platform.contracts.execution import FillEvent, PositionDelta
from hft_platform.contracts.strategy import IntentType, OrderCommand, OrderIntent, Side, StormGuardState


def test_fill_event_has_tca_prices() -> None:
    fill = FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=Side.BUY, qty=1, price=200_000_000,
        fee=130_000, tax=0, ingest_ts_ns=0, match_ts_ns=0,
    )
    assert fill.decision_price == 0
    assert fill.arrival_price == 0


def test_fill_event_with_tca_prices() -> None:
    fill = FillEvent(
        fill_id="f1", account_id="a1", order_id="o1", strategy_id="s1",
        symbol="XMT", side=Side.BUY, qty=1, price=200_000_000,
        fee=130_000, tax=0, ingest_ts_ns=0, match_ts_ns=0,
        decision_price=199_500_000, arrival_price=199_800_000,
    )
    assert fill.decision_price == 199_500_000
    assert fill.arrival_price == 199_800_000


def test_position_delta_has_gross_pnl_and_fees() -> None:
    delta = PositionDelta(
        account_id="a1", strategy_id="s1", symbol="XMT",
        net_qty=0, avg_price=0, realized_pnl=100_000,
        unrealized_pnl=0, delta_source="FILL",
    )
    assert delta.gross_pnl == 0
    assert delta.fees == 0


def test_order_intent_has_decision_price() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    assert intent.decision_price == 0


def test_order_command_has_tca_prices() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    cmd = OrderCommand(cmd_id=1, intent=intent, deadline_ns=0, storm_guard_state=StormGuardState.NORMAL)
    assert cmd.decision_price == 0
    assert cmd.arrival_price == 0
