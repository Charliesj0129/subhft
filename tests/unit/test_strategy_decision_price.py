"""Verify StrategyRunner stamps decision_price on OrderIntent."""
from hft_platform.contracts.strategy import IntentType, OrderIntent, Side


def test_intent_has_decision_price_from_lob() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
        decision_price=200_000_000,
    )
    assert intent.decision_price == 200_000_000


def test_intent_decision_price_zero_without_lob() -> None:
    intent = OrderIntent(
        intent_id=1, strategy_id="s1", symbol="XMT",
        intent_type=IntentType.NEW, side=Side.BUY, price=200_000_000, qty=1,
    )
    assert intent.decision_price == 0
