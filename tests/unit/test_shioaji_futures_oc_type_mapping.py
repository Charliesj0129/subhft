from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway

sj = pytest.importorskip("shioaji")


class _StubClient:
    contracts_ready = True

    def __init__(self, api: Any) -> None:
        self.api = api

    def _get_contract(self, exchange: str, code: str, product_type: str | None = None, allow_synthetic: bool = False):
        return MagicMock(code=code)

    def _record_api_latency(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture()
def sdk(monkeypatch: pytest.MonkeyPatch) -> Any:
    stub = MagicMock()
    stub.constant.FuturesOCType = sj.FuturesOCType
    monkeypatch.setattr(OrderGateway, "_sdk", staticmethod(lambda: stub))
    return stub


@pytest.fixture()
def gateway(sdk: Any) -> OrderGateway:
    return OrderGateway(_StubClient(MagicMock()))


def test_sdk_futures_oc_type_members_are_exactly_the_ones_the_gateway_maps_to() -> None:
    members = {name for name in dir(sj.FuturesOCType) if not name.startswith("_") and name != "value"}

    assert members == {"Auto", "Cover", "DayTrade", "New"}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("close", "Cover"),
        ("Close", "Cover"),
        ("cover", "Cover"),
        ("Cover", "Cover"),
        ("daytrade", "DayTrade"),
        ("DayTrade", "DayTrade"),
        ("day_trade", "DayTrade"),
        ("day-trade", "DayTrade"),
        ("new", "New"),
        ("New", "New"),
        ("open", "New"),
        ("Open", "New"),
        ("auto", "Auto"),
        ("Auto", "Auto"),
        ("", "Auto"),
        (None, "Auto"),
    ],
)
def test_futures_oc_type_maps_to_the_sdk_member_when_the_alias_is_known(
    gateway: OrderGateway, raw: str | None, expected: str
) -> None:
    assert gateway._map_futures_oc_type(raw).value == expected


@pytest.mark.parametrize("raw", ["bogus", "opne", "closee", "Close Today"])
def test_futures_oc_type_raises_when_the_value_is_unknown_and_not_empty(gateway: OrderGateway, raw: str) -> None:
    with pytest.raises(ValueError, match="oc_type"):
        gateway._map_futures_oc_type(raw)


def test_place_order_sends_a_cover_order_when_oc_type_is_close(gateway: OrderGateway, sdk: Any) -> None:
    gateway.place_order(
        contract_code="TMFK6",
        exchange="FUT",
        action="Sell",
        price=22000.0,
        qty=1,
        order_type="ROD",
        tif="ROD",
        product_type="future",
        oc_type="close",
        timeout=5000,
    )

    assert sdk.order.FuturesOrder.call_args.kwargs["octype"].value == "Cover"
    gateway._client.api.place_order.assert_called_once()


def test_place_order_never_reaches_the_broker_when_oc_type_is_unknown(gateway: OrderGateway, sdk: Any) -> None:
    with pytest.raises(ValueError, match="oc_type"):
        gateway.place_order(
            contract_code="TMFK6",
            exchange="FUT",
            action="Sell",
            price=22000.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            product_type="future",
            oc_type="bogus",
            timeout=5000,
        )

    gateway._client.api.place_order.assert_not_called()
