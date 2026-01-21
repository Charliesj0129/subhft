import time
from types import SimpleNamespace

import pytest

from hft_platform.contracts.execution import OrderStatus, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent


def _symbols_cfg(tmp_path):
    cfg = tmp_path / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: 'AAA'\n    exchange: 'TSE'\n    price_scale: 100\n")
    return cfg


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("PendingSubmit", OrderStatus.PENDING_SUBMIT),
        ("Submitted", OrderStatus.SUBMITTED),
        ("PreSubmitted", OrderStatus.SUBMITTED),
        ("Filled", OrderStatus.FILLED),
        ("Cancelled", OrderStatus.CANCELLED),
        ("Failed", OrderStatus.FAILED),
        ("Unknown", OrderStatus.SUBMITTED),
    ],
)
def test_normalize_order_status_mapping(tmp_path, monkeypatch, status, expected):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()
    raw = RawExecEvent(
        "order",
        {
            "ord_no": "O1",
            "status": {"status": status},
            "contract": {"code": "AAA"},
            "order": {"action": "Buy", "price": 1.23, "quantity": 1},
        },
        time.time_ns(),
    )
    event = norm.normalize_order(raw)
    assert event.status == expected


def test_normalize_order_seqno_custom_field_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(order_id_map={"S1": "strat:1"})

    raw = RawExecEvent(
        "order",
        {
            "seq_no": "S1",
            "status": {"status": "Submitted"},
            "contract": {"code": "AAA"},
            "order": {"action": "Buy", "price": 1.23, "quantity": 1},
            "custom_field": "custom",
        },
        time.time_ns(),
    )

    event = norm.normalize_order(raw)
    assert event.order_id == "S1"
    assert event.strategy_id == "custom"
    assert event.price == 123


def test_normalize_order_invalid_payload_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()

    raw = RawExecEvent(
        "order",
        {
            "ord_no": "O1",
            "status": {"status": "Submitted"},
            "order": "boom",
        },
        time.time_ns(),
    )
    assert norm.normalize_order(raw) is None


def test_normalize_order_fallback_to_seq_no_map(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(order_id_map={"S1": "strat:1"})

    raw = RawExecEvent(
        "order",
        {
            "ord_no": "O1",
            "seq_no": "S1",
            "status": {"status": "Submitted"},
            "contract": {"code": "AAA"},
            "order": {"action": "Buy", "price": 1.23, "quantity": 1},
        },
        time.time_ns(),
    )

    event = norm.normalize_order(raw)
    assert event.strategy_id == "strat"


def test_normalize_fill_contract_object_and_action_int(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(order_id_map={"O1": "strat:9"})

    raw = RawExecEvent(
        "deal",
        {
            "seq_no": "F1",
            "ord_no": "O1",
            "contract": SimpleNamespace(code="AAA"),
            "action": -1,
            "quantity": 1,
            "price": 1.23,
            "ts": 1,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event.symbol == "AAA"
    assert event.strategy_id == "strat"
    assert event.side == Side.SELL
    assert event.price == 123


def test_normalize_fill_fallback_to_seq_no_map(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(order_id_map={"S1": "strat:1"})

    raw = RawExecEvent(
        "deal",
        {
            "seq_no": "S1",
            "ord_no": "O1",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.23,
            "ts": 1,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event.strategy_id == "strat"


def test_normalize_order_uses_exchange_ts_for_broker_ts(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()
    raw = RawExecEvent(
        "order",
        {
            "ordno": "O1",
            "status": {"status": "Submitted", "exchange_ts": 1.5},
            "contract": {"code": "AAA"},
            "order": {"action": "Buy", "price": 1.23, "quantity": 1},
        },
        time.time_ns(),
    )
    event = norm.normalize_order(raw)
    assert event.broker_ts_ns == 1_500_000_000


def test_normalize_fill_ts_seconds_to_ns(tmp_path, monkeypatch):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()
    raw = RawExecEvent(
        "deal",
        {
            "seqno": "F1",
            "ordno": "O1",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.23,
            "ts": 1700000000.25,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event.match_ts_ns == 1_700_000_000_250_000_000


@pytest.mark.parametrize(
    ("mapping", "expected"),
    [
        ({"strategy_id": "S1", "intent_id": 11}, "S1"),
        (("S2", 22), "S2"),
        (["S3", 33], "S3"),
        ("S4:44", "S4"),
        ("S5", "S5"),
    ],
)
def test_normalize_fill_order_id_map_shapes(tmp_path, monkeypatch, mapping, expected):
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(order_id_map={"O1": mapping})

    raw = RawExecEvent(
        "deal",
        {
            "seq_no": "F1",
            "ord_no": "O1",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.23,
            "ts": 1,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event.strategy_id == expected
