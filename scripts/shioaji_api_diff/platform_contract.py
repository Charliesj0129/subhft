"""The platform's Shioaji "used-by-us" contract, encoded as data.

This module answers one question for the classifier: *does the platform depend
on this SDK symbol?* A change the platform depends on is BREAKING; a change to
something we never touch is BENIGN. The data here is traced directly from
``src/hft_platform/feed_adapter/shioaji/`` (the only place SDK imports are
allowed) and is kept honest by a meta-test that greps the adapter and asserts
every referenced enum member appears below (see ``tests/.../test_classify.py``).

Each entry carries a ``remediation`` pointer (adapter file:symbol) so a BREAKING
finding self-describes what to fix.

Stdlib-only (no ``hft_platform`` import) so it can be imported anywhere.
"""

from __future__ import annotations

ENUM_MEMBERS: dict[str, set[str]] = {
    "Action": {"Buy", "Sell"},
    "StockPriceType": {"LMT"},
    "OrderType": {"ROD", "IOC", "FOK"},
    "StockOrderCond": {"Cash"},
    "StockOrderLot": {"Common"},
    "FuturesPriceType": {"LMT"},
    "FuturesOCType": {"Auto"},
    "QuoteType": {"Tick", "BidAsk"},
    "QuoteVersion": {"v0", "v1"},
    "OrderState": {"StockDeal", "FuturesDeal"},
    "Exchange": {"TSE", "TAIFEX"},
    "SecurityType": {"Stock", "Futures"},
}

DYNAMIC_ENUM_CLASSES: set[str] = {
    "OrderState",
    "TicksQueryType",
    "ScannerType",
    "FuturesOrderType",
    "StockPriceType", "OrderType", "StockOrderCond", "StockOrderLot",
    "FuturesPriceType", "FuturesOCType",
}

EXISTENCE_CRITICAL_METHODS: dict[str, set[str]] = {
    "Shioaji": {
        "login", "logout", "place_order", "cancel_order", "update_order",
        "fetch_contracts", "activate_ca", "list_positions", "list_position_detail",
        "account_balance", "margin", "snapshots", "ticks", "kbars", "scanners",
        "usage", "set_order_callback", "trading_limits",
        "list_profit_loss", "short_stock_sources", "credit_enquires",
        "subscribe", "unsubscribe",
    },
    "Quote": {"subscribe", "unsubscribe"},
}

METHOD_PARAMS: dict[str, set[str]] = {
    "place_order": {"timeout", "cb"},
    "cancel_order": {"timeout", "cb"},
    "update_order": {"trade", "price", "qty", "timeout", "cb"},
    "subscribe": {"quote_type", "version"},
    "unsubscribe": {"quote_type", "version"},
    "login": {"api_key", "secret_key", "subscribe_trade"},
    "fetch_contracts": {"contract_download", "contracts_timeout", "contracts_cb"},
}

CTOR_FIELDS: dict[str, set[str]] = {
    "Order": {"price", "quantity", "action", "price_type", "order_type", "custom_field"},
    "StockOrder": {"price", "quantity", "action", "price_type", "order_type",
                   "order_cond", "order_lot", "account", "custom_field"},
    "FuturesOrder": {"price", "quantity", "action", "price_type", "order_type",
                     "octype", "account", "custom_field"},
}

SOL_WRAP_SYMBOLS: dict[str, int] = {
    "onreply_callback_wrap": 4,
    "reply_callback_wrap": 4,
    "event_callback_wrap": 5,
    "msg_callback_wrap": 3,
    "p2p_callback_wrap": 3,
    "session_down_callback_wrap": 1,
}

SOL_CONFIG_KEYS: set[str] = {
    "SOL_CONNECT_TIMEOUT_MS", "SOL_RECONNECT_RETRIES", "SOL_KEEP_ALIVE_MS",
    "SOL_RECONNECT_RETRY_WAIT", "SOL_KEEP_ALIVE_LIMIT",
}

READ_MODEL_QUALS: set[str] = {
    "TickSTKv1", "TickFOPv1", "BidAskSTKv1", "BidAskFOPv1", "QuoteSTKv1", "QuoteFOPv1",
}

EXCEPTIONS_CAUGHT: set[str] = set()

REMEDIATION: dict[str, str] = {
    "enum:Action": "order_gateway.py:_place_order",
    "enum:StockPriceType": "order_gateway.py:_map_stock_price_type",
    "enum:OrderType": "order_codec.py:_TIF_MAP / order_gateway.py:_map_stock_order_type",
    "enum:StockOrderCond": "order_gateway.py:_map_stock_order_cond",
    "enum:StockOrderLot": "order_gateway.py:_map_stock_order_lot",
    "enum:FuturesPriceType": "order_gateway.py:_map_futures_price_type",
    "enum:FuturesOCType": "order_gateway.py:_map_futures_oc_type",
    "enum:QuoteType": "subscription_manager.py:subscribe/unsubscribe (no fallback)",
    "enum:QuoteVersion": "reconnect_orchestrator.py:_resubscribe",
    "enum:OrderState": "subscription_manager.py:_dispatch_order_callback",
    "enum:Exchange": "contracts_runtime.py",
    "enum:SecurityType": "contracts_runtime.py",
    "method:place_order": "order_gateway.py:place_order",
    "method:cancel_order": "order_gateway.py:cancel_order",
    "method:update_order": "order_gateway.py:update_order",
    "method:subscribe": "subscription_manager.py:subscribe",
    "method:unsubscribe": "subscription_manager.py:unsubscribe",
    "method:login": "session_runtime.py:login",
    "ctor:Order": "order_gateway.py:_place_order",
    "ctor:StockOrder": "order_gateway.py:_place_order_typed",
    "ctor:FuturesOrder": "order_gateway.py:_place_order_typed",
    "sol_wrap": "client.py:_apply_solace_arity_shim",
    "sol_config": "ops: SOL_* reconnect knobs (_solace_env.py / compose env)",
    "layout": "feed_adapter/shioaji/ — SDK import paths + arity shim + SOL_* config",
    "class_removed": "client.py:_quote_api — VERIFIED on 1.5.3: api.quote survives as a "
                     "property returning _QuoteProxy (subscribe/unsubscribe/v1 callbacks/"
                     "set_event_callback intact); class folded into Shioaji. Re-verify if a "
                     "future release drops the proxy.",
}


def enum_member_used(enum_name: str, member: str) -> bool:
    return member in ENUM_MEMBERS.get(enum_name, set())


def enum_class_used(enum_name: str) -> bool:
    return enum_name in ENUM_MEMBERS or enum_name in DYNAMIC_ENUM_CLASSES


def method_existence_critical(class_qual: str, method: str) -> bool:
    return method in EXISTENCE_CRITICAL_METHODS.get(class_qual, set())


def method_param_used(method: str, param: str) -> bool:
    return param in METHOD_PARAMS.get(method, set())


def ctor_field_used(model_qual: str, field: str) -> bool:
    return field in CTOR_FIELDS.get(model_qual, set())


def model_field_used(model_qual: str, field: str) -> bool:
    """True if the platform reads/writes this model field (ctor or hot-path read)."""
    return ctor_field_used(model_qual, field) or model_qual in READ_MODEL_QUALS


def sol_wrap_expected_arity(symbol: str) -> int | None:
    return SOL_WRAP_SYMBOLS.get(symbol)


def remediation(key: str) -> str:
    return REMEDIATION.get(key, "(no specific remediation pointer)")
