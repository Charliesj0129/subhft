from hft_platform.core.order_ids import OrderIdResolver


def test_normalize_order_key_variants():
    resolver = OrderIdResolver(order_id_map={})

    assert resolver.normalize_order_key(None) is None
    assert resolver.normalize_order_key({"strategy_id": "s", "intent_id": 1}) == "s:1"
    assert resolver.normalize_order_key({"strategy_id": "s"}) == "s"
    assert resolver.normalize_order_key({"intent_id": 1}) is None
    assert resolver.normalize_order_key(["s", 1]) == "s:1"
    assert resolver.normalize_order_key(["s"]) == "s"
    assert resolver.normalize_order_key([]) is None
    assert resolver.normalize_order_key("plain") == "plain"


def test_resolve_order_key_uses_live_orders_and_mapping():
    resolver = OrderIdResolver(order_id_map={"ext": "s:2", "alt": {"strategy_id": "s", "intent_id": 3}})

    live_orders = {"s:9": {}}
    assert resolver.resolve_order_key("s", "9", live_orders) == "s:9"
    assert resolver.resolve_order_key("s", "ext") == "s:2"
    assert resolver.resolve_order_key("s", "alt") == "s:3"
    assert resolver.resolve_order_key("s", "unknown") == "s:unknown"
    assert resolver.resolve_order_key("s", None) == "s:"


def test_resolve_strategy_id_paths():
    resolver = OrderIdResolver(order_id_map={"o1": "strat:5", "o2": {"strategy_id": "s"}})

    assert resolver.resolve_strategy_id("o1") == "strat"
    assert resolver.resolve_strategy_id("o2") == "s"
    assert resolver.resolve_strategy_id("missing") == "UNKNOWN"
    resolver.order_id_map["o3"] = "solo"
    assert resolver.resolve_strategy_id("o3") == "solo"
    assert resolver.resolve_strategy_id_from_candidates(["", "missing", "o1"]) == "strat"
