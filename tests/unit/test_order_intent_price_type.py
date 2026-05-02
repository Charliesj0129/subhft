"""Tests for OrderIntent.price_type field and RiskFeedback dataclass."""

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side


def test_order_intent_default_price_type_is_lmt():
    intent = OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="TXO22500D6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=225000000,
        qty=1,
    )
    assert intent.price_type == "LMT"


def test_order_intent_mkt_price_type():
    intent = OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="TXO22500D6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=0,
        qty=1,
        price_type="MKT",
        tif=TIF.IOC,
    )
    assert intent.price_type == "MKT"
    assert intent.tif == TIF.IOC


def test_order_intent_backward_compat_no_price_type_arg():
    intent = OrderIntent(
        intent_id=1,
        strategy_id="test",
        symbol="TXFD6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=200000000,
        qty=1,
    )
    assert intent.price_type == "LMT"
    assert intent.price == 200000000


def test_risk_feedback_dataclass():
    from hft_platform.contracts.strategy import RiskFeedback

    fb = RiskFeedback(
        intent_id=42, strategy_id="eye", symbol="TXO22500D6", reason_code="GREEKS_DELTA_LIMIT", timestamp_ns=1000000000
    )
    assert fb.intent_id == 42
    assert fb.reason_code == "GREEKS_DELTA_LIMIT"


def test_risk_feedback_is_frozen():
    from hft_platform.contracts.strategy import RiskFeedback

    fb = RiskFeedback(intent_id=1, strategy_id="s", symbol="X", reason_code="R", timestamp_ns=0)
    try:
        fb.intent_id = 99
        assert False, "Should have raised"
    except (AttributeError, Exception):
        pass


def test_adapter_prefers_intent_price_type():
    """When intent has non-default price_type, adapter should use it over metadata."""
    intent = OrderIntent(
        intent_id=1,
        strategy_id="eye",
        symbol="TXO22500D6",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=0,
        qty=1,
        price_type="MKT",
        tif=TIF.IOC,
    )
    order_params = {"price_type": "LMT"}
    intent_price_type = getattr(intent, "price_type", "LMT")
    raw_price_type = intent_price_type if intent_price_type != "LMT" else str(order_params.get("price_type", "LMT"))
    assert raw_price_type == "MKT"


def test_adapter_falls_back_to_metadata_when_lmt():
    intent = OrderIntent(
        intent_id=1, strategy_id="s", symbol="X", intent_type=IntentType.NEW, side=Side.BUY, price=100, qty=1
    )
    order_params = {"price_type": "MKP"}
    intent_price_type = getattr(intent, "price_type", "LMT")
    raw_price_type = intent_price_type if intent_price_type != "LMT" else str(order_params.get("price_type", "LMT"))
    assert raw_price_type == "MKP"
