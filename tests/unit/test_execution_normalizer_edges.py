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


def test_normalize_order_seqno_map_precedence_over_custom_field(tmp_path, monkeypatch):
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
    assert event.strategy_id == "strat"
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
    norm = ExecutionNormalizer(order_id_map={"O1": "strat:9"}, default_account_id="test-acct")

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
    norm = ExecutionNormalizer(order_id_map={"S1": "strat:1"}, default_account_id="test-acct")

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
    norm = ExecutionNormalizer(default_account_id="test-acct")
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
    # Float arithmetic gives ~128ns jitter at this magnitude; accept ±1μs
    assert abs(event.match_ts_ns - 1_700_000_000_250_000_000) < 1_000


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
    norm = ExecutionNormalizer(order_id_map={"O1": mapping}, default_account_id="test-acct")

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


# ---------------------------------------------------------------------------
# M6: case-insensitive order side mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected_side"),
    [
        ("Buy", Side.BUY),  # original exact-match case
        ("buy", Side.BUY),  # lowercase
        ("BUY", Side.BUY),  # uppercase
        ("Sell", Side.SELL),
        ("sell", Side.SELL),
        ("SELL", Side.SELL),
        ("", Side.SELL),  # empty → default SELL
        (None, Side.SELL),  # missing → default SELL
    ],
)
def test_normalize_order_side_case_insensitive(tmp_path, monkeypatch, action, expected_side):
    """Order side mapping must be case-insensitive, matching fill normalizer behaviour."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()
    data: dict = {
        "ord_no": "O1",
        "status": {"status": "Submitted"},
        "contract": {"code": "AAA"},
        "order": {"price": 1.00, "quantity": 1},
    }
    if action is not None:
        data["order"]["action"] = action
    raw = RawExecEvent("order", data, time.time_ns())
    event = norm.normalize_order(raw)
    assert event is not None
    assert event.side == expected_side, f"action={action!r} expected {expected_side} got {event.side}"


# ---------------------------------------------------------------------------
# M8: Remove hardcoded sim-account-01 fallback in fill normalizer
# ---------------------------------------------------------------------------


def test_normalize_fill_with_account_id_uses_it(tmp_path, monkeypatch):
    """M8: When account_id is present, it must be used as-is."""
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
            "price": 1.00,
            "ts": 1,
            "account_id": "live-account-99",
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event is not None
    assert event.account_id == "live-account-99"


def test_normalize_fill_missing_account_id_rejects_without_default(tmp_path, monkeypatch):
    """M8: Missing account_id with no default must reject the fill (return None)."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()  # no default_account_id
    raw = RawExecEvent(
        "deal",
        {
            "seqno": "F2",
            "ordno": "O2",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.00,
            "ts": 1,
            # account_id intentionally omitted
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event is None  # rejected, not "unknown"


def test_normalize_fill_missing_account_id_uses_default(tmp_path, monkeypatch):
    """M8: Missing account_id falls back to default_account_id from session."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(default_account_id="SJ-ACCT-001")
    raw = RawExecEvent(
        "deal",
        {
            "seqno": "F2b",
            "ordno": "O2b",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.00,
            "ts": 1,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event is not None
    assert event.account_id == "SJ-ACCT-001"


def test_normalize_fill_extracts_account_from_object(tmp_path, monkeypatch):
    """M8: Shioaji-style account object should be extracted via .account_id or str()."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer()

    class FakeAccount:
        account_id = "F000123456"

    raw = RawExecEvent(
        "deal",
        {
            "seqno": "F5",
            "ordno": "O5",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.00,
            "ts": 1,
            "account": FakeAccount(),
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event is not None
    assert event.account_id == "F000123456"


def test_normalize_fill_missing_account_id_logs_critical(tmp_path, monkeypatch):
    """M8: A structlog critical must be emitted when all account_id sources exhausted."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))

    import structlog

    with structlog.testing.capture_logs() as cap_logs:
        norm = ExecutionNormalizer()
        raw = RawExecEvent(
            "deal",
            {
                "seqno": "F3",
                "ordno": "O3",
                "code": "AAA",
                "action": "Sell",
                "quantity": 2,
                "price": 1.00,
                "ts": 1,
            },
            time.time_ns(),
        )
        event = norm.normalize_fill(raw)

    assert event is None
    critical_events = [e for e in cap_logs if e.get("log_level") == "critical"]
    assert any("fill_rejected_missing_account_id" in str(e.get("event", "")) for e in critical_events), (
        f"Expected fill_rejected_missing_account_id critical, got: {cap_logs}"
    )


def test_normalize_fill_none_account_id_uses_default(tmp_path, monkeypatch):
    """M8: Explicit None account_id with default_account_id falls back to default."""
    monkeypatch.setenv("SYMBOLS_CONFIG", str(_symbols_cfg(tmp_path)))
    norm = ExecutionNormalizer(default_account_id="FALLBACK")
    raw = RawExecEvent(
        "deal",
        {
            "seqno": "F4",
            "ordno": "O4",
            "code": "AAA",
            "action": "Buy",
            "quantity": 1,
            "price": 1.00,
            "ts": 1,
            "account_id": None,
        },
        time.time_ns(),
    )
    event = norm.normalize_fill(raw)
    assert event is not None
    assert event.account_id == "FALLBACK"
