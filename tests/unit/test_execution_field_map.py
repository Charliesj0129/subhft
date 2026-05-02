"""CR-2: ExecutionNormalizer must resolve broker-specific field names via
BrokerExecFieldMap (MB-04). Fubon payloads using `order_no`/`filled_qty`/`symbol`
must normalize without relying on Shioaji-only aliases.
"""

from __future__ import annotations

from hft_platform.contracts.execution import Side
from hft_platform.execution.field_map import (
    BrokerExecFieldMap,
    FubonExecFieldMap,
    ShioajiExecFieldMap,
    get_field_map,
)
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent


def test_shioaji_field_map_is_default():
    nm = ExecutionNormalizer()
    assert isinstance(nm.field_map, ShioajiExecFieldMap)


def test_field_map_protocol_conformance():
    assert isinstance(ShioajiExecFieldMap(), BrokerExecFieldMap)
    assert isinstance(FubonExecFieldMap(), BrokerExecFieldMap)


def test_get_field_map_selects_impl():
    assert isinstance(get_field_map("shioaji"), ShioajiExecFieldMap)
    assert isinstance(get_field_map("fubon"), FubonExecFieldMap)
    assert isinstance(get_field_map("unknown_broker"), ShioajiExecFieldMap)


def test_normalize_fill_resolves_fubon_field_names():
    nm = ExecutionNormalizer(field_map=FubonExecFieldMap(), default_account_id="ACC1")

    raw = RawExecEvent(
        topic="deal",
        ingest_ts_ns=1_700_000_000_000_000_000,
        data={
            "symbol": "2330",  # fubon uses `symbol`, not `full_code`
            "order_no": "FBN12345",  # fubon order id alias
            "seq_no": "SEQ1",
            "quantity": 2,
            "price": 505.5,
            "action": "BUY",
            "account_id": "ACC1",
            "ts": 1_700_000_000_000_000_000,
        },
    )

    fill = nm.normalize_fill(raw)
    assert fill is not None
    assert fill.symbol == "2330"
    assert fill.order_id == "FBN12345"
    assert fill.qty == 2
    assert fill.side == Side.BUY


def test_normalize_order_resolves_fubon_filled_qty():
    nm = ExecutionNormalizer(field_map=FubonExecFieldMap())

    raw = RawExecEvent(
        topic="order",
        ingest_ts_ns=1_700_000_000_000_000_000,
        data={
            "order": {
                "order_no": "FBN777",
                "quantity": 5,
                "filled_qty": 3,  # fubon-specific
                "price": 100.0,
                "action": "Buy",
            },
            "contract": {"symbol": "2330"},
            "status": {"status": "Filled"},
        },
    )

    order = nm.normalize_order(raw)
    assert order is not None
    assert order.order_id == "FBN777"
    assert order.submitted_qty == 5
    assert order.filled_qty == 3
    assert order.remaining_qty == 2


def test_normalize_fill_shioaji_still_works():
    """Ensure Shioaji default path is not broken by refactor."""
    nm = ExecutionNormalizer(default_account_id="ACC_SJ")

    raw = RawExecEvent(
        topic="deal",
        ingest_ts_ns=1_700_000_000_000_000_000,
        data={
            "full_code": "TMFD6",
            "ordno": "SJ999",
            "seqno": "SJSEQ",
            "quantity": 1,
            "price": 17500.0,
            "action": "sell",
            "ts": 1_700_000_000_000_000_000,
        },
    )

    fill = nm.normalize_fill(raw)
    assert fill is not None
    assert fill.symbol == "TMFD6"
    assert fill.order_id == "SJ999"
    assert fill.side == Side.SELL
