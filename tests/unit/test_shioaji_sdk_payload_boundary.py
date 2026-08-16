"""The shioaji 1.5.x Rust payload types must become plain dicts at the adapter
boundary.

Measured on THESHOW 2026-08-10: the first two orders in two months produced

    normalize_fill_zero_qty  raw_keys="<class 'builtins.OrderEventDict'>"
    WAL Write Failed!  error="Type is not JSON serializable: builtins.OrderEventDict"

and then the strategy went silent for two days across 31.4M events.

Introspected from the installed shioaji 1.5.6, `shioaji._core.OrderEventDict`
carries the *complete* mapping protocol — ``keys`` ``items`` ``values`` ``get``
``__getitem__`` ``__iter__`` ``__len__`` ``__contains__`` — while being neither
a ``dict`` subclass nor registered with ``collections.abc.Mapping``. It is
structurally a mapping and nominally nothing, so every ``isinstance(x, dict)``
gate in the platform rejects it:

* ``normalizer.normalize_order:155`` returns ``None`` outright — and logs
  nothing, which is why the order path failed silently;
* ``normalizer.normalize_order:178`` returns ``None`` again for a nested
  non-dict ``order``, so a shallow conversion is not enough;
* ``normalizer._payload_get:84`` falls through to ``getattr``, which a Rust
  mapping does not answer, so ``qty`` reads 0 and the fill is dropped;
* ``orjson`` serializes ``dict``, not "things shaped like a dict", so the WAL
  write fails on the same object.

The real type cannot be constructed from Python (``TypeError: cannot create
'builtins.OrderEventDict' instances``), so the double below reproduces the
measured surface exactly. ``test_the_double_is_faithful_to_the_sdk_type`` pins
that premise — if it ever drifts, the rest of this file is testing nothing.

Fixing this in ``execution/normalizer.py`` would leak SDK type knowledge into
platform code and violate ``.agent/rules/26-multi-broker-governance.md``
("Broker SDK imports ONLY inside feed_adapter/<broker>/"), so the conversion
belongs in ``feed_adapter/shioaji/_compat.py`` alongside the other 1.3.3-vs-1.5
resolvers.
"""

from __future__ import annotations

import collections.abc as abc
from typing import Any

import orjson
import pytest

from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent
from hft_platform.feed_adapter.shioaji import subscription_manager as sub_mod
from hft_platform.feed_adapter.shioaji._compat import to_plain_payload


class SdkMapping:
    """Reproduces the measured surface of ``shioaji._core.OrderEventDict``.

    Full mapping protocol, no ``dict`` inheritance, no ``Mapping`` registration,
    and deliberately no attribute access for its keys — that last part is what
    makes ``getattr(payload, "quantity", 0)`` return the default.
    """

    __slots__ = ("_d",)

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def keys(self):  # noqa: ANN201
        return self._d.keys()

    def values(self):  # noqa: ANN201
        return self._d.values()

    def items(self):  # noqa: ANN201
        return self._d.items()

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def __iter__(self):  # noqa: ANN204
        return iter(self._d)

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, key: object) -> bool:
        return key in self._d


def _sdk_order_payload(op_type: str = "New", op_code: str = "00") -> SdkMapping:
    """A ``FuturesOrder`` callback with the exact keys shioaji 1.5.6 documents.

    Taken from the installed ``shioaji/_core.pyi`` (``FuturesOrderEvent``,
    ``FuturesOrderDetailDict``, ``EventOrderStatusDict``, ``OperationDict``,
    ``FuturesContractDict``, ``AccountDict``). Every nested value is itself an
    SDK mapping, which is what makes a shallow conversion insufficient.

    Note two things the stub settles that guesswork would have got wrong:
    ``FuturesOrderDetailDict`` has **no** ``custom_field`` (only the stock
    variant does), and ``EventOrderStatusDict`` has **no** ``status`` key — the
    order's disposition lives in ``operation.op_type``.
    """
    return SdkMapping(
        {
            "operation": SdkMapping({"op_type": op_type, "op_code": op_code, "op_msg": ""}),
            "order": SdkMapping(
                {
                    "id": "0f1a2b3c",
                    "seqno": "000123",
                    "ordno": "AB123",
                    "account": SdkMapping(
                        {
                            "account_type": "F",
                            "person_id": "",
                            "broker_id": "F002000",
                            "account_id": "1234567",
                            "signed": True,
                            "username": "",
                        }
                    ),
                    "action": "Buy",
                    "price": 22000.0,
                    "quantity": 1,
                    "order_type": "ROD",
                    "price_type": "LMT",
                    "market_type": "Night",
                    "oc_type": "Auto",
                    "subaccount": "",
                    "combo": False,
                }
            ),
            "status": SdkMapping(
                {
                    "id": "0f1a2b3c",
                    "exchange_ts": 1754812138.0,
                    "modified_price": 0.0,
                    "cancel_quantity": 0,
                    "order_quantity": 1,
                    "web_id": "",
                }
            ),
            "contract": SdkMapping(
                {
                    "security_type": "FUT",
                    "code": "TMFH6",
                    "exchange": "TAIFEX",
                    "delivery_month": "202608",
                    "full_code": "TMFH6",
                    "delivery_date": "2026/08/19",
                    "strike_price": 0.0,
                    "option_right": "Future",
                }
            ),
        }
    )


def _sdk_deal_payload() -> SdkMapping:
    """A ``FuturesDeal`` callback — flat, with the stub's exact keys.

    It carries ``account_id`` directly, which is how a fill satisfies the
    normalizer's account gate.
    """
    return SdkMapping(
        {
            "trade_id": "t-1",
            "seqno": "000123",
            "ordno": "AB123",
            "exchange_seq": "e-1",
            "broker_id": "F002000",
            "account_id": "1234567",
            "action": "Buy",
            "code": "TMFH6",
            "full_code": "TMFH6",
            "price": 22000.0,
            "quantity": 2,
            "subaccount": "",
            "security_type": "FUT",
            "delivery_month": "202608",
            "strike_price": 0.0,
            "option_right": "Future",
            "market_type": "Night",
            "combo": False,
            "ts": 1754812138.0,
        }
    )


# --------------------------------------------------------------------------- #
# The premise                                                                  #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_double_is_faithful_to_the_sdk_type() -> None:
    """Pins what was measured from shioaji 1.5.6. If this drifts, every other
    test in this file silently stops reproducing the outage."""
    payload = _sdk_order_payload()

    assert not isinstance(payload, dict)
    assert not isinstance(payload, abc.Mapping)
    for method in ("keys", "values", "items", "get", "__getitem__", "__iter__", "__len__", "__contains__"):
        assert hasattr(payload, method), method
    # The attribute route the normalizer falls back to yields nothing.
    assert getattr(payload, "quantity", None) is None


@pytest.mark.unit
def test_the_sdk_double_defeats_json_serialization() -> None:
    """The other half of the reported failure: `WAL Write Failed!`."""
    with pytest.raises(TypeError):
        orjson.dumps(_sdk_order_payload())


# --------------------------------------------------------------------------- #
# The conversion                                                               #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_to_plain_payload_converts_an_sdk_mapping_to_a_real_dict() -> None:
    result = to_plain_payload(_sdk_deal_payload())

    assert isinstance(result, dict)
    assert result["quantity"] == 2
    assert result["code"] == "TMFH6"


@pytest.mark.unit
def test_to_plain_payload_converts_nested_sdk_mappings() -> None:
    """A shallow conversion is not enough: ``normalize_order:178`` rejects the
    whole event when the nested ``order`` is not a dict."""
    result = to_plain_payload(_sdk_order_payload())

    assert isinstance(result["order"], dict)
    assert isinstance(result["status"], dict)
    assert isinstance(result["operation"], dict)
    assert isinstance(result["contract"], dict)
    assert result["order"]["quantity"] == 1


@pytest.mark.unit
def test_to_plain_payload_converts_sdk_mappings_nested_in_sequences() -> None:
    payload = SdkMapping({"legs": [SdkMapping({"qty": 3}), SdkMapping({"qty": 4})]})

    result = to_plain_payload(payload)

    assert [leg["qty"] for leg in result["legs"]] == [3, 4]
    assert all(isinstance(leg, dict) for leg in result["legs"])


@pytest.mark.unit
def test_converted_payload_survives_wal_serialization() -> None:
    """`HFT_RECORDER_MODE=wal_first` makes the WAL the primary write, so a
    payload that orjson refuses is lost, not degraded."""
    encoded = orjson.dumps(to_plain_payload(_sdk_order_payload()))

    decoded = orjson.loads(encoded)
    assert decoded["order"]["ordno"] == "AB123"
    assert decoded["order"]["account"]["account_id"] == "1234567"


@pytest.mark.unit
def test_to_plain_payload_leaves_plain_payloads_untouched() -> None:
    """The 1.3.3 SDK delivers real dicts and must keep working unchanged."""
    payload = {"order": {"quantity": 1}, "status": {"status": "Filled"}}

    result = to_plain_payload(payload)

    assert result == payload


@pytest.mark.unit
def test_to_plain_payload_passes_scalars_and_none_through() -> None:
    assert to_plain_payload(None) is None
    assert to_plain_payload("Submitted") == "Submitted"
    assert to_plain_payload(7) == 7


@pytest.mark.unit
def test_to_plain_payload_does_not_treat_strings_as_sequences_to_rebuild() -> None:
    result = to_plain_payload(SdkMapping({"action": "Buy"}))

    assert result["action"] == "Buy"


@pytest.mark.unit
def test_to_plain_payload_stops_at_the_depth_cap_instead_of_recursing_forever() -> None:
    """A self-referencing SDK mapping must not take the broker callback thread
    down with a RecursionError."""
    inner: dict[str, Any] = {}
    payload = SdkMapping(inner)
    inner["self"] = payload

    result = to_plain_payload(payload)

    assert isinstance(result, dict)


# --------------------------------------------------------------------------- #
# The regression the conversion is for                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_normalize_order_drops_an_unconverted_sdk_payload() -> None:
    """Reproduces the silent half of the outage before asserting the fix."""
    normalizer = ExecutionNormalizer()
    raw = RawExecEvent("order", {"state": "FuturesOrder", "payload": _sdk_order_payload()}, 1)

    assert normalizer.normalize_order(raw) is None


@pytest.mark.unit
def test_normalize_order_counts_an_unreadable_payload_instead_of_dropping_it_silently() -> None:
    """The drop at ``normalize_order:155`` logged nothing and incremented
    nothing, so a dead order path looked identical to an idle one. Two days of
    silence followed. An unreadable broker payload is an adapter defect and has
    to be countable."""
    normalizer = ExecutionNormalizer()
    counter = normalizer.metrics.order_normalization_failed_total.labels(reason="non_mapping_payload")
    before = counter._value.get()

    normalizer.normalize_order(RawExecEvent("order", {"state": "x", "payload": _sdk_order_payload()}, 1))

    assert counter._value.get() == before + 1


@pytest.mark.unit
def test_normalize_order_yields_an_event_for_a_converted_sdk_payload() -> None:
    normalizer = ExecutionNormalizer()
    raw = RawExecEvent("order", {"state": "FuturesOrder", "payload": to_plain_payload(_sdk_order_payload())}, 1)

    event = normalizer.normalize_order(raw)

    assert event is not None
    assert event.symbol == "TMFH6"
    assert event.submitted_qty == 1
    assert event.order_id == "AB123"


@pytest.mark.unit
def test_futures_order_events_carry_no_custom_field_to_attribute_with() -> None:
    """Pins the SDK schema fact behind two months of `strategy_id=UNKNOWN`.

    ``StockOrderDetailDict`` and ``StockDealEvent`` both declare ``custom_field``;
    ``FuturesOrderDetailDict`` and ``FuturesDealEvent`` declare neither. So a
    futures strategy cannot be attributed through ``custom_field`` at all, and
    attribution has to resolve ``ordno``/``seqno`` through the order id map. Any
    fix that leans on ``custom_field`` for futures is fixing the wrong thing.
    """
    order = to_plain_payload(_sdk_order_payload())["order"]
    deal = to_plain_payload(_sdk_deal_payload())

    assert "custom_field" not in order
    assert "custom_field" not in deal
    # What attribution must key off instead:
    assert order["ordno"] == deal["ordno"] == "AB123"
    assert order["seqno"] == deal["seqno"] == "000123"


@pytest.mark.unit
def test_a_broker_rejection_is_carried_in_the_operation_block() -> None:
    """The 2026-08-10 events were rejections — the account is unfunded — and a
    rejection is an order-state transition, not a fill. ``EventOrderStatusDict``
    has no ``status`` key at all, so the disposition is only readable from
    ``operation.op_type``, whose literals include ``"Reject"``."""
    payload = to_plain_payload(_sdk_order_payload(op_type="Reject", op_code="99"))

    assert payload["operation"]["op_type"] == "Reject"
    assert "status" not in payload["status"]


@pytest.mark.unit
def test_normalize_fill_reads_qty_from_a_converted_sdk_payload() -> None:
    """The `normalize_fill_zero_qty` warning: qty read 0 because the attribute
    fallback cannot interrogate a Rust mapping."""
    normalizer = ExecutionNormalizer()
    unconverted = RawExecEvent("deal", {"payload": _sdk_deal_payload()}, 1)
    converted = RawExecEvent("deal", {"payload": to_plain_payload(_sdk_deal_payload())}, 1)

    assert normalizer.normalize_fill(unconverted) is None

    fill = normalizer.normalize_fill(converted)
    assert fill is not None
    assert fill.qty == 2
    assert fill.symbol == "TMFH6"
    # FuturesDealEvent carries account_id itself, which is how the fill clears
    # the account gate — ExecutionNormalizer's default_account_id is never
    # supplied by router.py:90, so that fallback cannot be relied on.
    assert fill.account_id == "1234567"


# --------------------------------------------------------------------------- #
# The wiring                                                                   #
# --------------------------------------------------------------------------- #


class _FakeApi:
    def __init__(self) -> None:
        self.registered: Any = None

    def set_order_callback(self, cb: Any) -> None:
        self.registered = cb

    def __bool__(self) -> bool:
        return True


class _FakeClient:
    def __init__(self) -> None:
        self.api = _FakeApi()


class _FakeOrderState:
    StockDeal = "StockDeal"
    FuturesDeal = "FuturesDeal"


def _register_callbacks(monkeypatch: pytest.MonkeyPatch, on_order: Any, on_deal: Any) -> Any:
    """Register execution callbacks against a fake SDK and return the callback
    shioaji would invoke on its own thread."""
    sdk = type("_Sdk", (), {"constant": type("_Const", (), {"OrderState": _FakeOrderState})})
    monkeypatch.setattr(sub_mod, "_sj", sdk)

    client = _FakeClient()
    sub_mod.SubscriptionManager(client).set_execution_callbacks(on_order=on_order, on_deal=on_deal)
    return client.api.registered


@pytest.mark.unit
def test_order_callback_converts_the_sdk_payload_before_dispatching_an_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversion has to happen inside the adapter's callback, or production
    still hands Rust mappings to the normalizer no matter how good the helper is."""
    seen: list[Any] = []
    callback = _register_callbacks(monkeypatch, lambda _s, p: seen.append(p), lambda p: seen.append(p))

    callback("FuturesOrder", _sdk_order_payload())

    (payload,) = seen
    assert isinstance(payload, dict)
    assert isinstance(payload["order"], dict)
    assert payload["order"]["quantity"] == 1


@pytest.mark.unit
def test_order_callback_converts_the_sdk_payload_before_dispatching_a_deal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    callback = _register_callbacks(monkeypatch, lambda _s, p: seen.append(p), lambda p: seen.append(p))

    callback("FuturesDeal", _sdk_deal_payload())

    (payload,) = seen
    assert isinstance(payload, dict)
    assert payload["quantity"] == 2


@pytest.mark.unit
def test_order_callback_still_routes_deals_and_orders_to_different_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Conversion must not blur the routing the deal_states set decides."""
    orders: list[Any] = []
    deals: list[Any] = []
    callback = _register_callbacks(monkeypatch, lambda _s, p: orders.append(p), deals.append)

    callback("FuturesOrder", _sdk_order_payload())
    callback("FuturesDeal", _sdk_deal_payload())

    assert len(orders) == 1
    assert len(deals) == 1
