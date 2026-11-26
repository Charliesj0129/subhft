"""OrderAdapter's place_order kwargs must reach sdk.futopt.place_order through the Fubon client."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from tests.unit.fubon_mock_helper import (
    BSAction,
    FutOptMarketType,
    FutOptOrder,
    FutOptOrderType,
    FutOptPriceType,
    SdkFactory,
    TimeInForce,
    install_fubon_neo_mock,
    make_account,
    ok,
)

install_fubon_neo_mock()

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.core import timebase
from hft_platform.feed_adapter.fubon import facade as facade_module
from hft_platform.feed_adapter.fubon.client import FubonClient
from hft_platform.feed_adapter.fubon.facade import FubonClientFacade
from hft_platform.feed_adapter.fubon.order_codec import (
    FubonOrderCodec,
    is_taifex_night_session,
    resolve_futopt_market_type,
    translate_adapter_order_kwargs,
)

_TAIPEI = ZoneInfo("Asia/Taipei")


def _taipei_ns(hour: int, minute: int = 0, second: int = 0) -> int:
    """UTC epoch ns of a Taipei wall-clock time on a fixed date (the date itself is irrelevant)."""
    return int(datetime(2026, 11, 23, hour, minute, second, tzinfo=_TAIPEI).timestamp()) * 1_000_000_000


@pytest.fixture(autouse=True)
def _pin_taipei_midday(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the clock the translator reads to a day-session time so Future is deterministic."""
    monkeypatch.setattr(timebase, "now_ns", lambda: _taipei_ns(10, 30))


class _LoginFactory(SdkFactory):
    def __call__(self, *args: Any, **kwargs: Any) -> MagicMock:
        sdk = super().__call__(*args, **kwargs)
        sdk.login.return_value = ok([make_account()])
        sdk.apikey_login.return_value = ok([make_account()])
        return sdk


@pytest.fixture()
def factory(monkeypatch: pytest.MonkeyPatch) -> _LoginFactory:
    for name in ("HFT_FUBON_TEST_URL", "HFT_FUBON_API_KEY", "HFT_FUBON_CERT_PASS", "HFT_FUBON_ACCOUNT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HFT_ORDER_MODE", "live")
    monkeypatch.setenv("HFT_FUBON_PERSONAL_ID", "A123456789")
    monkeypatch.setenv("HFT_FUBON_PASSWORD", "pwd")
    monkeypatch.setenv("HFT_FUBON_CERT_PATH", "/c.pfx")
    factory = _LoginFactory()
    monkeypatch.setattr(facade_module, "FubonSDK", factory)
    return factory


def _order_adapter_kwargs(**overrides: Any) -> dict[str, Any]:
    """The exact kwargs OrderAdapter sends, encoded by the codec bootstrap wires for Fubon."""
    codec = FubonOrderCodec()
    tif = codec.encode_tif(TIF.LIMIT)
    kwargs: dict[str, Any] = {
        "contract_code": "TMFK6",
        "exchange": "TAIFEX",
        "action": codec.encode_side(Side.BUY),
        "price": 22000.5,
        "qty": 2,
        "order_type": tif,
        "tif": tif,
        "custom_field": "a1b2c3",
        "product_type": "Future",
        "price_type": codec.encode_price_type("LMT"),
        "intent": SimpleNamespace(symbol="TMFK6"),
    }
    kwargs.update(overrides)
    return kwargs


def _logged_in_facade(factory: _LoginFactory) -> tuple[FubonClientFacade, MagicMock]:
    facade = FubonClientFacade(symbols_path=None, broker_config={"broker": "fubon"})
    facade.login()
    sdk = factory.instances[0]
    sdk.futopt.place_order.return_value = ok(SimpleNamespace(order_no="o1", seq_no="1"))
    return facade, sdk


def test_place_order_adapter_kwargs_reach_futopt_order(factory: _LoginFactory) -> None:
    facade, sdk = _logged_in_facade(factory)

    result = facade.place_order(**_order_adapter_kwargs())

    order = sdk.futopt.place_order.call_args.args[1]
    assert isinstance(order, FutOptOrder)
    assert order.symbol == "TMFK6"
    assert order.buy_sell == BSAction.Buy
    assert order.lot == 2
    assert order.price == "22000.5"
    assert order.price_type == FutOptPriceType.Limit
    assert order.time_in_force == TimeInForce.ROD
    assert order.order_type == FutOptOrderType.Auto
    assert order.market_type == FutOptMarketType.Future
    assert order.user_def == "a1b2c3"
    assert result.order_no == "o1"


def test_place_order_adapter_kwargs_ioc_sell_force_flat_shape(factory: _LoginFactory) -> None:
    facade, sdk = _logged_in_facade(factory)
    codec = FubonOrderCodec()
    tif = codec.encode_tif(TIF.IOC)

    facade.place_order(
        **_order_adapter_kwargs(action=codec.encode_side(Side.SELL), order_type=tif, tif=tif, price=21999.0)
    )

    order = sdk.futopt.place_order.call_args.args[1]
    assert order.buy_sell == BSAction.Sell
    assert order.time_in_force == TimeInForce.IOC
    assert order.price == "21999"


def test_place_order_adapter_kwargs_unknown_key_fails_closed(factory: _LoginFactory) -> None:
    facade, sdk = _logged_in_facade(factory)

    with pytest.raises(TypeError, match="order_cond"):
        facade.place_order(**_order_adapter_kwargs(order_cond="Cash"))

    sdk.futopt.place_order.assert_not_called()


def test_place_order_adapter_kwargs_client_translates_custom_field() -> None:
    client = FubonClient()
    client._order_gateway = MagicMock()

    client.place_order(**_order_adapter_kwargs())

    sent = client._order_gateway.place_order.call_args.kwargs
    assert sent["symbol"] == "TMFK6"
    assert sent["user_def"] == "a1b2c3"
    assert sent["price"] == 220005000
    assert sent["price_type"] == "LMT"
    assert "contract_code" not in sent and "custom_field" not in sent


def _pin_clock(monkeypatch: pytest.MonkeyPatch, ns: int) -> None:
    monkeypatch.setattr(timebase, "now_ns", lambda: ns)


@pytest.mark.parametrize(
    ("product_type", "taipei_time", "expected"),
    [
        ("future", (10, 30, 0), "Future"),
        ("future", (20, 0, 0), "FutureNight"),
        ("future", (2, 0, 0), "FutureNight"),
        ("option", (10, 30, 0), "Option"),
        ("option", (20, 0, 0), "OptionNight"),
        ("option", (2, 0, 0), "OptionNight"),
        ("Future", (10, 30, 0), "Future"),
        ("OPTION", (20, 0, 0), "OptionNight"),
        ("futures", (10, 30, 0), "Future"),
        ("options", (20, 0, 0), "OptionNight"),
    ],
)
def test_translate_market_type_follows_product_type_and_session(
    product_type: str, taipei_time: tuple[int, int, int], expected: str
) -> None:
    out = translate_adapter_order_kwargs(
        _order_adapter_kwargs(product_type=product_type), now_ns=lambda: _taipei_ns(*taipei_time)
    )

    assert out["market_type"] == expected


@pytest.mark.parametrize(
    ("taipei_time", "expected"),
    [
        ((4, 59, 59), "FutureNight"),
        ((5, 0, 0), "Future"),
        ((8, 44, 59), "Future"),
        ((8, 45, 0), "Future"),
        ((14, 59, 59), "Future"),
        ((15, 0, 0), "FutureNight"),
        ((0, 0, 0), "FutureNight"),
        ((23, 59, 59), "FutureNight"),
    ],
)
def test_translate_market_type_session_boundaries_are_half_open_at_taipei_time(
    taipei_time: tuple[int, int, int], expected: str
) -> None:
    out = translate_adapter_order_kwargs(_order_adapter_kwargs(), now_ns=lambda: _taipei_ns(*taipei_time))

    assert out["market_type"] == expected


def test_night_session_is_told_in_taipei_time_not_utc() -> None:
    # 07:00 UTC is 15:00 in Taipei (night opens); 06:59:59 UTC is still day.
    utc_seven = 7 * 3600 * 1_000_000_000

    assert is_taifex_night_session(utc_seven) is True
    assert is_taifex_night_session(utc_seven - 1_000_000_000) is False


def test_translate_market_type_default_clock_is_timebase_now_ns(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin_clock(monkeypatch, _taipei_ns(21, 0))

    out = translate_adapter_order_kwargs(_order_adapter_kwargs(product_type="option"))

    assert out["market_type"] == "OptionNight"


@pytest.mark.parametrize("product_type", [None, "", "  ", "stock", "index", "etf", 7])
def test_translate_market_type_unknown_product_type_raises(product_type: Any) -> None:
    kwargs = _order_adapter_kwargs(product_type=product_type)

    with pytest.raises(ValueError, match="product_type"):
        translate_adapter_order_kwargs(kwargs, now_ns=lambda: _taipei_ns(10, 30))


def test_translate_market_type_missing_product_type_raises() -> None:
    kwargs = _order_adapter_kwargs()
    del kwargs["product_type"]

    with pytest.raises(ValueError, match="product_type"):
        translate_adapter_order_kwargs(kwargs, now_ns=lambda: _taipei_ns(10, 30))


def test_resolve_futopt_market_type_unknown_product_type_raises_before_session_is_used() -> None:
    with pytest.raises(ValueError, match="product_type"):
        resolve_futopt_market_type("stock", _taipei_ns(20, 0))


def test_place_order_adapter_kwargs_night_future_reaches_futopt_order(
    factory: _LoginFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    facade, sdk = _logged_in_facade(factory)
    _pin_clock(monkeypatch, _taipei_ns(16, 0))

    facade.place_order(**_order_adapter_kwargs())

    assert sdk.futopt.place_order.call_args.args[1].market_type == FutOptMarketType.FutureNight


@pytest.mark.parametrize(
    ("taipei_time", "expected"),
    [((10, 30, 0), FutOptMarketType.Option), ((20, 0, 0), FutOptMarketType.OptionNight)],
)
def test_place_order_adapter_kwargs_option_reaches_futopt_order(
    factory: _LoginFactory,
    monkeypatch: pytest.MonkeyPatch,
    taipei_time: tuple[int, int, int],
    expected: FutOptMarketType,
) -> None:
    facade, sdk = _logged_in_facade(factory)
    _pin_clock(monkeypatch, _taipei_ns(*taipei_time))

    facade.place_order(**_order_adapter_kwargs(contract_code="TXO23000K6", product_type="option"))

    assert sdk.futopt.place_order.call_args.args[1].market_type == expected


def test_place_order_adapter_kwargs_unknown_product_type_fails_closed_before_sdk(factory: _LoginFactory) -> None:
    facade, sdk = _logged_in_facade(factory)

    with pytest.raises(ValueError, match="product_type"):
        facade.place_order(**_order_adapter_kwargs(product_type=None))

    sdk.futopt.place_order.assert_not_called()


def test_place_order_adapter_kwargs_client_sends_market_type(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FubonClient()
    client._order_gateway = MagicMock()
    _pin_clock(monkeypatch, _taipei_ns(3, 0))

    client.place_order(**_order_adapter_kwargs(product_type="option"))

    assert client._order_gateway.place_order.call_args.kwargs["market_type"] == "OptionNight"
