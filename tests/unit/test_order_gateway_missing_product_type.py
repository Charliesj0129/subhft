"""A futures order with no metadata product type must still be futures-typed.

2026-11-09: a poisoned metadata cache handed ``product_type=None`` to
``OrderGateway.place_order`` for TMFK6. The untyped branch built
``sj.Order(price_type=StockPriceType.LMT)`` and shioaji 1.7.9 rejected every
order with "FuturesPriceType must be FuturesPriceType enum or string". The
gateway now takes the product type from the broker contract's ``security_type``.

The real-SDK tests never log in: shioaji runs its order-type conversion before
its auth check, so a correctly typed order fails with ``AuthError`` and the
incident's mistyped order fails with ``ShioajiTypeError``.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest

from hft_platform.feed_adapter.shioaji.order_gateway import OrderGateway, _contract_product_type

sj = pytest.importorskip("shioaji")


class _StubClient:
    contracts_ready = True

    def __init__(self, api: Any, security_type: str) -> None:
        self.api = api
        self._security_type = security_type

    def _get_contract(self, exchange: str, code: str, product_type: str | None = None, allow_synthetic: bool = False):
        exch = "TAIFEX" if self._security_type in {"FUT", "OPT"} else "TSE"
        return sj.BaseContract(security_type=self._security_type, exchange=exch, code=code)

    def _record_api_latency(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture()
def offline_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.chdir(tmp_path)  # shioaji writes shioaji.log into the cwd
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return sj.Shioaji(simulation=True)


def _place(gw: OrderGateway, code: str, exchange: str, product_type: str | None) -> BaseException:
    with pytest.raises(Exception) as excinfo:
        gw.place_order(
            contract_code=code,
            exchange=exchange,
            action="Sell",
            price=27000.0,
            qty=1,
            order_type="ROD",
            tif="ROD",
            product_type=product_type,
            price_type="LMT",
            timeout=0,
        )
    return excinfo.value


@pytest.mark.parametrize("product_type", [None, ""])
@pytest.mark.parametrize("security_type", ["FUT", "OPT"])
def test_place_order_without_product_type_sends_futures_typed_order(
    offline_api: Any, product_type: str | None, security_type: str
) -> None:
    gw = OrderGateway(_StubClient(offline_api, security_type))

    exc = _place(gw, "TMFK6", "FUT", product_type)

    assert "PriceType must be" not in str(exc), f"SDK rejected the order type: {exc!r}"
    assert type(exc).__name__ == "AuthError"


def test_place_order_without_product_type_keeps_stock_path_for_stock_contract(offline_api: Any) -> None:
    gw = OrderGateway(_StubClient(offline_api, "STK"))

    exc = _place(gw, "2330", "TSE", None)

    assert "PriceType must be" not in str(exc), f"SDK rejected the order type: {exc!r}"
    assert type(exc).__name__ == "AuthError"


@pytest.mark.parametrize(
    ("security_type", "expected"),
    [
        (sj.SecurityType.Futures, "future"),
        (sj.SecurityType.Option, "option"),
        (sj.SecurityType.Stock, None),
        (sj.SecurityType.Index, None),
        ("fut", "future"),
        (None, None),
        (7, None),
    ],
)
def test_contract_product_type_maps_security_type(security_type: Any, expected: str | None) -> None:
    class _Contract:
        pass

    contract = _Contract()
    if security_type is not None:
        setattr(contract, "security_type", security_type)

    assert _contract_product_type(contract) == expected
