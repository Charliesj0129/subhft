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

# --------------------------------------------------------------------------- #
# Enum members the adapter references by literal name. A removal/rename of any
# of these is a silent runtime break: ``getattr(Enum, key, DEFAULT)`` fallbacks
# in order_gateway.py mask the absence (wrong order encoding), and the
# no-fallback QuoteType lookups in subscription_manager.py raise outright.
# --------------------------------------------------------------------------- #
ENUM_MEMBERS: dict[str, set[str]] = {
    "Action": {"Buy", "Sell"},                       # order_gateway.py:104
    "StockPriceType": {"LMT"},                        # order_gateway.py:126,269 (default)
    "OrderType": {"ROD", "IOC", "FOK"},               # order_gateway.py:127-131,276,326
    "StockOrderCond": {"Cash"},                       # order_gateway.py:283,293 (default)
    "StockOrderLot": {"Common"},                      # order_gateway.py:300,309 (default)
    "FuturesPriceType": {"LMT"},                      # order_gateway.py:316 (default)
    "FuturesOCType": {"Auto"},                        # order_gateway.py:333,337 (default)
    "QuoteType": {"Tick", "BidAsk"},                  # subscription_manager.py:245-284 (no fallback)
    "QuoteVersion": {"v0", "v1"},                     # reconnect_orchestrator.py:296-298
    "OrderState": {"StockDeal", "FuturesDeal"},       # subscription_manager.py:437 (+downstream)
    "Exchange": {"TSE", "TAIFEX"},                    # contracts_runtime.py:438
    "SecurityType": {"Stock", "Future"},              # contracts_runtime.py:441-443
}

# Enums resolved wholesale via ``getattr(sdk.constant, "<Name>", None)`` — the
# CLASS existing is what matters (a rename of the enum type breaks the lookup).
DYNAMIC_ENUM_CLASSES: set[str] = {
    "OrderState",        # subscription_manager.py:437
    "TicksQueryType",    # historical_gateway.py:41
    "ScannerType",       # scanner_gateway.py:126
    "FuturesOrderType",  # order_gateway.py:323 (optional, falls back to OrderType)
    "StockPriceType", "OrderType", "StockOrderCond", "StockOrderLot",
    "FuturesPriceType", "FuturesOCType",  # order_gateway.py getattr() key lookups
}

# --------------------------------------------------------------------------- #
# Methods the adapter calls WITHOUT a hasattr guard — removal is a hard break.
# (update_price/update_qty are intentionally absent: order_gateway.py guards
# them with hasattr and falls back to update_order, so their removal is benign.)
# --------------------------------------------------------------------------- #
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

# Keyword arguments the adapter passes by name (a removed/renamed param we pass
# raises TypeError). Positional-only callers are covered by method existence.
METHOD_PARAMS: dict[str, set[str]] = {
    "place_order": {"timeout", "cb"},                 # order_gateway.py:142,240
    "cancel_order": {"timeout", "cb"},                # order_gateway.py:352
    "update_order": {"trade", "price", "qty", "timeout", "cb"},  # order_gateway.py:376-398
    "subscribe": {"quote_type", "version"},           # subscription_manager.py:245-249
    "unsubscribe": {"quote_type", "version"},         # subscription_manager.py:280-284
    "login": {"api_key", "secret_key", "contracts_timeout", "fetch_contract",
              "subscribe_trade"},                      # session_runtime.py:140-147
}

# --------------------------------------------------------------------------- #
# Order constructor fields the adapter passes (order_gateway.py:132-236).
# --------------------------------------------------------------------------- #
CTOR_FIELDS: dict[str, set[str]] = {
    "Order": {"price", "quantity", "action", "price_type", "order_type", "custom_field"},
    "StockOrder": {"price", "quantity", "action", "price_type", "order_type",
                   "order_cond", "order_lot", "account", "custom_field"},
    "FuturesOrder": {"price", "quantity", "action", "price_type", "order_type",
                     "octype", "account", "custom_field"},
}

# --------------------------------------------------------------------------- #
# The six Solace callback-wrap symbols the arity shim monkeypatches
# (client.py:73-144). Removal/rename silently no-ops the shim -> the documented
# ~hourly SIGABRT crash-loop returns. Values are the expected param arity (incl.
# self) that the shim forwards; an arity change means the shim may be removable.
# --------------------------------------------------------------------------- #
SOL_WRAP_SYMBOLS: dict[str, int] = {
    "onreply_callback_wrap": 4,
    "reply_callback_wrap": 4,
    "event_callback_wrap": 5,
    "msg_callback_wrap": 3,
    "p2p_callback_wrap": 3,
    "session_down_callback_wrap": 1,
}

# SOL_* config keys: a default change is BEHAVIORAL (ops-relevant; ties to the
# 451 reconnect incident), never auto-blocking.
SOL_CONFIG_KEYS: set[str] = {
    "SOL_CONNECT_TIMEOUT_MS", "SOL_RECONNECT_RETRIES", "SOL_KEEP_ALIVE_MS",
    "SOL_RECONNECT_RETRY_WAIT", "SOL_KEEP_ALIVE_LIMIT",
}

# Stream data-classes consumed field-by-name on the hot path (tick_dispatcher.py,
# quote_runtime.py normalize callbacks by attribute). A removed/type-changed
# field on these is BREAKING wholesale; an added field is additive.
READ_MODEL_QUALS: set[str] = {
    "TickSTKv1", "TickFOPv1", "BidAskSTKv1", "BidAskFOPv1", "QuoteSTKv1", "QuoteFOPv1",
}

# The adapter catches builtin/own exceptions (KeyError, ImportError, OSError,
# StaleInstrumentError) and bare ``Exception`` — it does NOT depend on any
# ``shioaji.error`` class by name (``_infra.py`` raises the *builtin*
# TimeoutError). So exception-class removals are benign for the platform.
EXCEPTIONS_CAUGHT: set[str] = set()

# Remediation pointers surfaced on BREAKING findings (kind -> file:symbol).
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


# --------------------------------------------------------------------------- #
# Lookup helpers used by classify.py.
# --------------------------------------------------------------------------- #
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
