"""Shared stand-ins for the fubon_neo 2.5.0 SDK in tests.

The SDK is an optional extra and is not installed in the unit-test environment,
so these stand-ins carry the *real* public surface of the 2.5.0 wheel
(introspected from it): the method names, positional signatures, enum members
and the ``Result`` envelope.  Mocks are ``create_autospec`` instances of the
spec classes below, so a method the real SDK does not have raises
``AttributeError`` and a wrong argument list raises ``TypeError`` inside the code
under test, which fails the test.  Nothing here ever connects to a broker.
"""

from __future__ import annotations

import enum
import inspect
import sys
import types
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, create_autospec

# --------------------------------------------------------------------------- #
# Enums (fubon_neo.constant)
# --------------------------------------------------------------------------- #


class BSAction(enum.Enum):
    Buy = "Buy"
    Sell = "Sell"


class TimeInForce(enum.Enum):
    ROD = "ROD"
    IOC = "IOC"
    FOK = "FOK"


class PriceType(enum.Enum):
    """Stock price types."""

    Limit = "Limit"
    Market = "Market"
    LimitUp = "LimitUp"
    LimitDown = "LimitDown"
    Reference = "Reference"


class OrderType(enum.Enum):
    """Stock order types."""

    Stock = "Stock"
    Margin = "Margin"
    Short = "Short"
    SBL = "SBL"
    DayTrade = "DayTrade"


class FutOptMarketType(enum.Enum):
    Future = "Future"
    FutureNight = "FutureNight"
    Option = "Option"
    OptionNight = "OptionNight"


class FutOptPriceType(enum.Enum):
    Limit = "Limit"
    Market = "Market"
    RangeMarket = "RangeMarket"
    Reference = "Reference"


class FutOptOrderType(enum.Enum):
    Auto = "Auto"
    New = "New"
    Close = "Close"
    FdayTrade = "FdayTrade"


class CallPut(enum.Enum):
    Call = "Call"
    Put = "Put"


_ENUMS: tuple[type[enum.Enum], ...] = (
    BSAction,
    TimeInForce,
    PriceType,
    OrderType,
    FutOptMarketType,
    FutOptPriceType,
    FutOptOrderType,
    CallPut,
)


# --------------------------------------------------------------------------- #
# Order / result objects
# --------------------------------------------------------------------------- #


class FutOptOrder:
    """Real ``fubon_neo.sdk.FutOptOrder`` constructor signature; keeps its fields."""

    def __init__(
        self,
        market_type: Any,
        price_type: Any,
        time_in_force: Any,
        order_type: Any,
        buy_sell: Any,
        symbol: Any,
        buy_sell2: Any = None,
        symbol2: Any = None,
        lot: Any = None,
        price: Any = None,
        user_def: Any = None,
    ) -> None:
        self.market_type = market_type
        self.price_type = price_type
        self.time_in_force = time_in_force
        self.order_type = order_type
        self.buy_sell = buy_sell
        self.symbol = symbol
        self.buy_sell2 = buy_sell2
        self.symbol2 = symbol2
        self.lot = lot
        self.price = price
        self.user_def = user_def


class FakeResult:
    """The SDK's ``Result`` envelope: ``is_success``, ``message``, ``data``."""

    def __init__(self, is_success: Any, data: Any = None, message: str | None = None) -> None:
        self.is_success = is_success
        self.data = data
        self.message = message


def ok(data: Any = None) -> FakeResult:
    """A successful ``Result`` carrying *data*."""
    return FakeResult(True, data=data)


def fail(message: str = "rejected") -> FakeResult:
    """A failed ``Result`` (``is_success=False``) with *message*."""
    return FakeResult(False, data=None, message=message)


def make_convert_symbol(extra: dict[tuple[Any, ...], str] | None = None) -> Any:
    """A ``futopt.convert_symbol`` stand-in with the contract mappings seen on the test environment.

    Keys are ``(product, expiry_date)`` for futures and ``(product, expiry_date, strike_price,
    call_put)`` for options.  An unknown product raises like the live SDK does for a code it
    does not know (``Symbol Name error``); ``TMF`` is deliberately not mapped, the test
    environment rejects it.
    """
    table: dict[tuple[Any, ...], str] = {
        ("FITX", "202612"): "TXFL6",
        ("FITX", "202701"): "TXFA7",
        ("FIMTX", "202612"): "MXFL6",
        ("TXO", "202612", 20000.0, CallPut.Call): "TXO20000L6",
        ("TXO", "202612", 20000.0, CallPut.Put): "TXO20000X6",
        ("TXO", "202612", 20100.0, CallPut.Call): "TXO20100L6",
    }
    table.update(extra or {})

    def convert_symbol(symbol: Any, expiry_date: Any, strike_price: Any = None, call_put: Any = None) -> str:
        key: tuple[Any, ...] = (symbol, expiry_date)
        if strike_price is not None or call_put is not None:
            key = (symbol, expiry_date, strike_price, call_put)
        if key not in table:
            raise ValueError(f"Symbol Name error: {key!r}")
        return table[key]

    return convert_symbol


def make_account(
    account: str = "1234567", branch_no: str = "6460", name: str = "tester", account_type: Any = "futopt"
) -> SimpleNamespace:
    """A trading ``Account`` as login returns it.

    A real login returns one account per product the customer holds, so ``account_type`` is
    ``"futopt"`` for the futures/options account and ``"stock"`` for the securities one.
    """
    return SimpleNamespace(name=name, branch_no=branch_no, account=account, account_type=account_type)


def make_order_result(order_no: str = "l0001", seq_no: str = "00000000001", **extra: Any) -> SimpleNamespace:
    """A ``FutOptOrderResult`` stand-in (the object cancel/modify act on)."""
    return SimpleNamespace(order_no=order_no, seq_no=seq_no, **extra)


# --------------------------------------------------------------------------- #
# Real SDK surface (names and positional signatures of fubon-neo 2.5.0)
# --------------------------------------------------------------------------- #


class FutOptSpec:
    """``sdk.futopt`` of fubon-neo 2.5.0."""

    def place_order(self, account: Any, py_order: Any, unblock: Any = None) -> Any: ...
    def cancel_order(self, account: Any, order_res: Any, unblock: Any = None) -> Any: ...
    def make_modify_price_obj(self, order: Any, price: Any = None, price_type: Any = None) -> Any: ...
    def modify_price(self, account: Any, order_res: Any, unblock: Any = None) -> Any: ...
    def make_modify_lot_obj(self, order: Any, lot: Any) -> Any: ...
    def modify_lot(self, account: Any, order_res: Any, unblock: Any = None) -> Any: ...
    def get_order_results(self, account: Any, market_type: Any = None) -> Any: ...
    def get_order_results_detail(self, account: Any, market_type: Any = None) -> Any: ...
    def order_history(self, account: Any, start_date: Any, end_date: Any, market_type: Any = None) -> Any: ...
    def filled_history(self, account: Any, market_type: Any, start_date: Any, end_date: Any = None) -> Any: ...
    def batch_place_order(self, account: Any, orders: Any) -> Any: ...
    def batch_cancel_order(self, account: Any, orders: Any) -> Any: ...
    def batch_modify_price(self, account: Any, orders: Any) -> Any: ...
    def batch_modify_lot(self, account: Any, orders: Any) -> Any: ...
    def batch_order_lists(self, account: Any) -> Any: ...
    def batch_order_detail(self, *args: Any, **kwargs: Any) -> Any: ...
    def query_estimate_margin(self, account: Any, order: Any) -> Any: ...
    def convert_symbol(self, symbol: Any, expiry_date: Any, strike_price: Any = None, call_put: Any = None) -> Any: ...


class FutOptAccountingSpec:
    """``sdk.futopt_accounting`` of fubon-neo 2.5.0."""

    def query_margin_equity(self, account: Any) -> Any: ...
    def query_hybrid_position(self, account: Any) -> Any: ...
    def query_single_position(self, account: Any) -> Any: ...
    def close_position_record(self, account: Any, start_date: Any, end_date: Any = None) -> Any: ...


class FubonSDKSpec:
    """``fubon_neo.sdk.FubonSDK`` of fubon-neo 2.5.0.

    The real constructor connects the trading websocket, defaulting to the
    production endpoint when ``url`` is omitted.
    """

    futopt: Any = None
    futopt_accounting: Any = None
    accounting: Any = None
    stock: Any = None

    def __init__(self, pong_inteval: Any = None, missed_count: Any = None, url: Any = None) -> None: ...
    def login(self, personal_id: Any, pass_: Any, cert_path: Any, cert_pass: Any = None) -> Any: ...
    def apikey_login(self, personal_id: Any, api_key: Any, cert_path: Any, cert_pass: Any = None) -> Any: ...
    def apikey_dma_login(self, personal_id: Any, api_key: Any) -> Any: ...
    def dma_login(self, *args: Any, **kwargs: Any) -> Any: ...
    def logout(self) -> Any: ...
    def shutdown(self) -> Any: ...
    def init_realtime(self, mode: Any = None, version: Any = None, base_url: Any = None) -> Any: ...
    def realtime_ws_url(self, mode: Any) -> Any: ...
    def exchange_realtime_token(self, *args: Any, **kwargs: Any) -> Any: ...
    def recover_event_data(self, *args: Any, **kwargs: Any) -> Any: ...
    def set_on_event(self, callback: Any) -> Any: ...
    def set_on_order(self, callback: Any) -> Any: ...
    def set_on_order_changed(self, callback: Any) -> Any: ...
    def set_on_filled(self, callback: Any) -> Any: ...
    def set_on_futopt_order(self, callback: Any) -> Any: ...
    def set_on_order_futopt_changed(self, callback: Any) -> Any: ...
    def set_on_futopt_filled(self, callback: Any) -> Any: ...


def make_fubon_sdk_mock() -> MagicMock:
    """An autospec'd ``FubonSDK`` instance whose ``futopt``/``futopt_accounting`` are autospec'd too."""
    sdk = create_autospec(FubonSDKSpec, instance=True)
    sdk.futopt = create_autospec(FutOptSpec, instance=True)
    sdk.futopt.convert_symbol.side_effect = make_convert_symbol()
    sdk.futopt_accounting = create_autospec(FutOptAccountingSpec, instance=True)
    return sdk


class SdkFactory:
    """Stand-in for the ``FubonSDK`` class: records constructor calls, returns autospec'd SDKs.

    Constructor arguments are checked against the real signature, so a wrong
    keyword (for example ``ws_url=``) raises ``TypeError``.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.instances: list[MagicMock] = []

    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        inspect.signature(FubonSDKSpec).bind(*args, **kwargs)
        self.calls.append((args, kwargs))
        sdk = make_fubon_sdk_mock()
        self.instances.append(sdk)
        return sdk


# --------------------------------------------------------------------------- #
# fubon_neo module install
# --------------------------------------------------------------------------- #


def install_fubon_neo_mock() -> types.ModuleType:
    """Install fake ``fubon_neo.constant`` and ``fubon_neo.sdk`` modules into sys.modules.

    ``fubon_neo.sdk`` carries only ``FutOptOrder``; it deliberately has no
    ``FubonSDK``, so ``from fubon_neo.sdk import FubonSDK`` still fails like an
    uninstalled SDK and no test can construct a connecting SDK by accident.

    Returns:
        The fake constant module for direct attribute access in tests.
    """
    if "fubon_neo.constant" in sys.modules:
        return sys.modules["fubon_neo.constant"]

    fubon_neo = types.ModuleType("fubon_neo")
    constant = types.ModuleType("fubon_neo.constant")
    sdk_module = types.ModuleType("fubon_neo.sdk")

    for enum_cls in _ENUMS:
        setattr(constant, enum_cls.__name__, enum_cls)
    sdk_module.FutOptOrder = FutOptOrder

    fubon_neo.constant = constant
    fubon_neo.sdk = sdk_module
    sys.modules["fubon_neo"] = fubon_neo
    sys.modules["fubon_neo.constant"] = constant
    sys.modules["fubon_neo.sdk"] = sdk_module
    return constant
