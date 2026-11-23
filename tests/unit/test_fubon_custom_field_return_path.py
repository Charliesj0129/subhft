"""A Fubon report must hand the order's custom_field token back to ExecutionNormalizer.

OrderAdapter sends a 6-hex token as ``custom_field`` and registers it in ``order_id_map``: the broker
ids do not exist yet when the first ACK arrives, so the token is the only key that can attribute it.  The
Fubon client translates it to ``user_def``, the reports echo it as ``user_def``, and the callback
adapter must forward it under the canonical ``custom_field`` key that ``ExecutionNormalizer`` (and
``_on_exec``) read.  These tests run the whole return path: place kwargs -> SDK report ->
FubonExecutionCallbackAdapter -> ExecutionNormalizer.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from tests.unit.fubon_mock_helper import BSAction, install_fubon_neo_mock, make_fubon_sdk_mock

install_fubon_neo_mock()

from hft_platform.contracts.strategy import TIF, Side
from hft_platform.execution.field_map import get_field_map
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.feed_adapter.fubon.execution_callbacks import (
    FubonExecutionCallbackAdapter,
    bridge_order_callback,
)
from hft_platform.feed_adapter.fubon.order_codec import FubonOrderCodec, translate_adapter_order_kwargs

_TOKEN = "a1b2c3"
_ORDER_KEY = "r47:42"
_INGEST_NS = 1_700_000_000_000_000_000
_DAY_SESSION_NS = int(datetime(2026, 11, 23, 10, 30, tzinfo=ZoneInfo("Asia/Taipei")).timestamp()) * 1_000_000_000
_ORDER_SETTERS = ("set_on_futopt_order", "set_on_order_futopt_changed")


def _sent_user_def(token: str = _TOKEN) -> str:
    """The user_def the Fubon client sends for the custom_field token OrderAdapter places."""
    codec = FubonOrderCodec()
    tif = codec.encode_tif(TIF.LIMIT)
    sent = translate_adapter_order_kwargs(
        {
            "contract_code": "TXFL6",
            "exchange": "TAIFEX",
            "action": codec.encode_side(Side.BUY),
            "price": 22000.0,
            "qty": 2,
            "order_type": tif,
            "tif": tif,
            "custom_field": token,
            "product_type": "Future",
            "price_type": codec.encode_price_type("LMT"),
            "intent": SimpleNamespace(symbol="TXFL6"),
        },
        now_ns=lambda: _DAY_SESSION_NS,
    )
    user_def = sent["user_def"]
    assert isinstance(user_def, str)
    return user_def


def _order_report(user_def: str | None, **overrides: Any) -> SimpleNamespace:
    """A FutOptOrderResult-shaped report echoing *user_def*; broker ids are not in order_id_map."""
    fields: dict[str, Any] = {
        "order_no": "l0001",
        "seq_no": "00000000001",
        "symbol": "FITX",
        "expiry_date": "202612",
        "strike_price": None,
        "call_put": None,
        "symbol_leg2": None,
        "buy_sell": BSAction.Buy,
        "price": 22000.0,
        "lot": 2,
        "status": 10,
        "function_type": 0,
        "user_def": user_def,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _fill_report(user_def: str | None, **overrides: Any) -> SimpleNamespace:
    """A FutOptFilledData-shaped report echoing *user_def*; broker ids are not in order_id_map."""
    fields: dict[str, Any] = {
        "order_no": "l0001",
        "seq_no": "00000000001",
        "symbol": "FITX",
        "expiry_date": "202612",
        "strike_price": None,
        "call_put": None,
        "symbol_leg2": None,
        "buy_sell": BSAction.Buy,
        "filled_no": "00000000007",
        "filled_lots": 2,
        "filled_price": 22000.0,
        "filled_time": "10:31:00.931",
        "user_def": user_def,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _Wired:
    """The SDK callbacks wired into the adapter, plus what the platform handlers received."""

    def __init__(self) -> None:
        self.sdk: MagicMock = make_fubon_sdk_mock()
        self.orders: list[dict[str, Any]] = []
        self.deals: list[dict[str, Any]] = []
        self.adapter = FubonExecutionCallbackAdapter(self.sdk)
        # The same {state, payload} envelope services.system hands to _on_exec for an order.
        self.adapter.register(
            on_order=bridge_order_callback(
                lambda state, payload: self.orders.append({"state": state, "payload": payload})
            ),
            on_deal=self.deals.append,
        )

    def deliver_order(self, report: Any, setter: str = "set_on_futopt_order") -> None:
        getattr(self.sdk, setter).call_args.args[0]("0", report)

    def deliver_deal(self, report: Any) -> None:
        self.sdk.set_on_futopt_filled.call_args.args[0](None, report)


@pytest.fixture()
def wired() -> _Wired:
    return _Wired()


@pytest.fixture()
def normalizer(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> ExecutionNormalizer:
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'TXFL6'\n    exchange: 'TAIFEX'\n    price_scale: 10000\n")
    monkeypatch.setenv("SYMBOLS_CONFIG", str(cfg))
    # Only the token is registered: that is all OrderAdapter knows when the first ACK arrives.
    return ExecutionNormalizer(
        order_id_map={_TOKEN: _ORDER_KEY},
        field_map=get_field_map("fubon"),
        default_account_id="ACCT-1",
    )


def _normalize_order(normalizer: ExecutionNormalizer, envelope: dict[str, Any]) -> Any:
    return normalizer.normalize_order(RawExecEvent("order", envelope, _INGEST_NS))


def test_order_report_resolves_the_placed_token_to_the_registered_order_key(
    wired: _Wired, normalizer: ExecutionNormalizer
) -> None:
    wired.deliver_order(_order_report(_sent_user_def()))

    event = _normalize_order(normalizer, wired.orders[0])

    assert event is not None
    assert event.client_order_id == _ORDER_KEY
    assert event.strategy_id == "r47"


def test_deal_report_resolves_the_placed_token_to_the_registered_order_key(
    wired: _Wired, normalizer: ExecutionNormalizer
) -> None:
    wired.deliver_deal(_fill_report(_sent_user_def()))

    fill = normalizer.normalize_fill(RawExecEvent("deal", wired.deals[0], _INGEST_NS))

    assert fill is not None
    assert fill.client_order_id == _ORDER_KEY
    assert fill.strategy_id == "r47"


@pytest.mark.parametrize("setter", _ORDER_SETTERS)
def test_status_39_order_report_resolves_the_placed_token_on_both_order_channels(
    wired: _Wired, normalizer: ExecutionNormalizer, setter: str
) -> None:
    wired.deliver_order(_order_report(_sent_user_def(), status=39), setter)

    event = _normalize_order(normalizer, wired.orders[0])

    assert wired.orders[0]["payload"]["status"] == "39"
    assert event is not None
    assert event.client_order_id == _ORDER_KEY
    assert event.strategy_id == "r47"


def test_report_without_user_def_stays_unattributed(wired: _Wired, normalizer: ExecutionNormalizer) -> None:
    wired.deliver_order(_order_report(None))
    wired.deliver_deal(_fill_report(None))

    event = _normalize_order(normalizer, wired.orders[0])
    fill = normalizer.normalize_fill(RawExecEvent("deal", wired.deals[0], _INGEST_NS))

    assert event is not None and fill is not None
    assert (event.client_order_id, event.strategy_id) == ("", "UNKNOWN")
    assert (fill.client_order_id, fill.strategy_id) == ("", "UNKNOWN")


def test_custom_field_does_not_leak_into_the_next_report_through_the_reused_buffers(wired: _Wired) -> None:
    wired.deliver_order(_order_report(_sent_user_def()))
    wired.deliver_order(_order_report(None, order_no="l0002"))
    wired.deliver_deal(_fill_report(_sent_user_def()))
    wired.deliver_deal(_fill_report(None, order_no="l0002", filled_no="00000000008"))

    assert [env["payload"]["custom_field"] for env in wired.orders] == [_TOKEN, ""]
    assert [deal["custom_field"] for deal in wired.deals] == [_TOKEN, ""]


def test_strategy_id_keeps_carrying_user_def_next_to_custom_field(wired: _Wired) -> None:
    wired.deliver_order(_order_report(_sent_user_def()))
    wired.deliver_deal(_fill_report(_sent_user_def()))

    assert wired.orders[0]["payload"]["strategy_id"] == _TOKEN
    assert wired.deals[0]["strategy_id"] == _TOKEN
