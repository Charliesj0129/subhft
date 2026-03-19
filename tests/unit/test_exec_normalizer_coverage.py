"""Coverage tests for execution/normalizer.py — fill normalization, order normalization, field mapping."""

from __future__ import annotations

from hft_platform.contracts.execution import FillEvent, OrderEvent, OrderStatus, Side
from hft_platform.execution.normalizer import ExecutionNormalizer, RawExecEvent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_raw(topic: str, data: dict, ts: int = 1_000_000_000) -> RawExecEvent:
    return RawExecEvent(topic=topic, data=data, ingest_ts_ns=ts)


def _fill_data(
    *,
    price: float = 100.5,
    qty: int = 10,
    action: str = "Buy",
    code: str = "2330",
    seqno: str = "SEQ001",
    ordno: str = "ORD001",
    ts: float | None = None,
) -> dict:
    d: dict = {
        "price": price,
        "quantity": qty,
        "action": action,
        "code": code,
        "seqno": seqno,
        "ordno": ordno,
    }
    if ts is not None:
        d["ts"] = ts
    return d


def _order_data(
    *,
    ordno: str = "ORD001",
    action: str = "Buy",
    price: float = 100.0,
    qty: int = 5,
    code: str = "2330",
    status: str | None = "Submitted",
) -> dict:
    d: dict = {
        "order": {
            "ordno": ordno,
            "action": action,
            "price": price,
            "quantity": qty,
        },
        "contract": {"code": code},
    }
    if status is not None:
        d["status"] = {"status": status}
    return d


# ---------------------------------------------------------------------------
# normalize_fill
# ---------------------------------------------------------------------------


class TestNormalizeFill:
    def test_basic_buy_fill(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(price=100.5, qty=10, action="Buy"))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert isinstance(fill, FillEvent)
        assert fill.side == Side.BUY
        assert fill.qty == 10
        assert fill.symbol == "2330"
        # price must be scaled int (x10000 default for unknown symbol)
        assert isinstance(fill.price, int)
        assert fill.price > 0

    def test_sell_fill(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(action="Sell"))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.side == Side.SELL

    def test_sell_action_lowercase(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(action="sell"))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.side == Side.SELL

    def test_fill_id_from_seqno(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(seqno="S42"))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.fill_id == "S42"

    def test_fill_order_id_from_ordno(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(ordno="O99"))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.order_id == "O99"

    def test_fill_zero_price(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", _fill_data(price=0))
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.price == 0

    def test_fill_missing_qty_defaults_zero(self) -> None:
        norm = ExecutionNormalizer()
        data = {"price": 100.0, "action": "Buy", "code": "2330"}
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.qty == 0

    def test_fill_with_contract_dict_for_symbol(self) -> None:
        norm = ExecutionNormalizer()
        data = {"price": 50.0, "quantity": 1, "action": "Buy", "contract": {"code": "TXFD6"}}
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.symbol == "TXFD6"

    def test_fill_with_contract_object_for_symbol(self) -> None:
        norm = ExecutionNormalizer()

        class FakeContract:
            code = "TXFD6"

        data = {"price": 50.0, "quantity": 1, "action": "Buy", "contract": FakeContract()}
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.symbol == "TXFD6"

    def test_fill_with_payload_wrapper(self) -> None:
        """Test data wrapped in a payload envelope."""
        norm = ExecutionNormalizer()
        inner = _fill_data(price=99.0, qty=5, action="Sell", code="2317")
        raw = _make_raw("deal", {"payload": inner})
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.symbol == "2317"
        assert fill.side == Side.SELL
        assert fill.qty == 5

    def test_fill_bad_data_returns_none(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("deal", {"price": "not_a_number_at_all!!!!", "quantity": "bad"})
        fill = norm.normalize_fill(raw)
        # Should gracefully return None on parse failure
        assert fill is None


# ---------------------------------------------------------------------------
# normalize_order
# ---------------------------------------------------------------------------


class TestNormalizeOrder:
    def test_basic_order(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data())
        order = norm.normalize_order(raw)
        assert order is not None
        assert isinstance(order, OrderEvent)
        assert order.order_id == "ORD001"
        assert order.symbol == "2330"
        assert order.status == OrderStatus.SUBMITTED
        assert order.side == Side.BUY

    def test_sell_order(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(action="Sell"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.side == Side.SELL

    def test_pending_status(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(status="PendingSubmit"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.PENDING_SUBMIT

    def test_filled_status(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(status="Filled"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.FILLED

    def test_cancelled_status(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(status="Cancelled"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.CANCELLED

    def test_canceled_us_spelling(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(status="Canceled"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.CANCELLED

    def test_failed_status(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(status="Failed"))
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.FAILED

    def test_op_type_cancel(self) -> None:
        norm = ExecutionNormalizer()
        data = _order_data(status=None)
        data["operation"] = {"op_type": "Cancel", "op_code": "00"}
        raw = _make_raw("order", data)
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.CANCELLED

    def test_op_type_with_error_code(self) -> None:
        norm = ExecutionNormalizer()
        data = _order_data(status=None)
        data["operation"] = {"op_type": "New", "op_code": "99"}
        raw = _make_raw("order", data)
        order = norm.normalize_order(raw)
        assert order is not None
        assert order.status == OrderStatus.FAILED

    def test_non_dict_data_returns_none(self) -> None:
        norm = ExecutionNormalizer()
        raw = RawExecEvent(topic="order", data="not_a_dict", ingest_ts_ns=1)  # type: ignore[arg-type]
        order = norm.normalize_order(raw)
        assert order is None

    def test_order_price_scaled(self) -> None:
        norm = ExecutionNormalizer()
        raw = _make_raw("order", _order_data(price=100.0))
        order = norm.normalize_order(raw)
        assert order is not None
        # Price should be scaled integer
        assert isinstance(order.price, int)


# ---------------------------------------------------------------------------
# _normalize_ts_ns
# ---------------------------------------------------------------------------


class TestNormalizeTsNs:
    def test_none_returns_now(self) -> None:
        norm = ExecutionNormalizer()
        ts = norm._normalize_ts_ns(None)
        assert isinstance(ts, int)
        assert ts > 0

    def test_nanosecond_passthrough(self) -> None:
        norm = ExecutionNormalizer()
        # > 1e17 -> ns passthrough
        ts = norm._normalize_ts_ns(1_700_000_000_000_000_000)
        assert ts == 1_700_000_000_000_000_000

    def test_microsecond_conversion(self) -> None:
        norm = ExecutionNormalizer()
        # > 1e14 -> us * 1000
        us = 1_700_000_000_000_000
        ts = norm._normalize_ts_ns(us)
        assert ts == us * 1000

    def test_millisecond_conversion(self) -> None:
        norm = ExecutionNormalizer()
        # > 1e11 -> ms * 1_000_000
        ms = 1_700_000_000_000
        ts = norm._normalize_ts_ns(ms)
        assert ts == ms * 1_000_000

    def test_second_conversion(self) -> None:
        norm = ExecutionNormalizer()
        # <= 1e11 -> s * 1_000_000_000
        s = 1_700_000_000
        ts = norm._normalize_ts_ns(s)
        assert ts == s * 1_000_000_000

    def test_invalid_value_returns_now(self) -> None:
        norm = ExecutionNormalizer()
        ts = norm._normalize_ts_ns("not_a_number")
        assert isinstance(ts, int)
        assert ts > 0

    def test_zero_returns_now(self) -> None:
        norm = ExecutionNormalizer()
        ts = norm._normalize_ts_ns(0)
        assert ts > 0

    def test_negative_returns_now(self) -> None:
        norm = ExecutionNormalizer()
        ts = norm._normalize_ts_ns(-100)
        assert ts > 0


# ---------------------------------------------------------------------------
# Strategy ID resolution
# ---------------------------------------------------------------------------


class TestStrategyIdResolution:
    def test_resolve_from_custom_field(self) -> None:
        norm = ExecutionNormalizer()
        data = _fill_data()
        data["custom_field"] = "my_strategy"
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "my_strategy"

    def test_resolve_from_order_custom_field(self) -> None:
        norm = ExecutionNormalizer()
        data = {"price": 100.0, "quantity": 1, "action": "Buy", "code": "2330", "order": {"custom_field": "strat_x"}}
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "strat_x"

    def test_resolve_from_order_id_map(self) -> None:
        norm = ExecutionNormalizer(order_id_map={"ORD_ABC": "mapped_strategy"})
        data = _fill_data(ordno="ORD_ABC")
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "mapped_strategy"

    def test_unknown_strategy_fallback(self) -> None:
        norm = ExecutionNormalizer()
        data = {"price": 100.0, "quantity": 1, "action": "Buy", "code": "2330"}
        raw = _make_raw("deal", data)
        fill = norm.normalize_fill(raw)
        assert fill is not None
        assert fill.strategy_id == "UNKNOWN"


# ---------------------------------------------------------------------------
# _map_status edge cases
# ---------------------------------------------------------------------------


class TestMapStatus:
    def test_presubmitted(self) -> None:
        norm = ExecutionNormalizer()
        assert norm._map_status("PreSubmitted") == OrderStatus.SUBMITTED

    def test_none_defaults_submitted(self) -> None:
        norm = ExecutionNormalizer()
        assert norm._map_status(None) == OrderStatus.SUBMITTED

    def test_op_type_update_code_ok(self) -> None:
        norm = ExecutionNormalizer()
        assert norm._map_status(None, op_type="Update", op_code="00") == OrderStatus.SUBMITTED

    def test_op_type_new_code_ok(self) -> None:
        norm = ExecutionNormalizer()
        assert norm._map_status(None, op_type="New", op_code="00") == OrderStatus.SUBMITTED

    def test_op_type_cancel_code_ok(self) -> None:
        norm = ExecutionNormalizer()
        assert norm._map_status(None, op_type="Cancel", op_code="00") == OrderStatus.CANCELLED
