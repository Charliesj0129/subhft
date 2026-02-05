"""Unit tests for core data contracts.

Tests cover: construction, field types, enum values, serialization,
msgspec compatibility, and default values.
"""

import sys
from pathlib import Path

import msgspec

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hft_platform.contracts.execution import (
    FillEvent,
    OrderEvent,
    OrderStatus,
    PositionDelta,
)
from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderCommand,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)


# ---------------------------------------------------------------------------
# Side Enum Tests
# ---------------------------------------------------------------------------
class TestSideEnum:
    def test_side_values(self):
        """Side enum has correct integer values."""
        assert int(Side.BUY) == 0
        assert int(Side.SELL) == 1

    def test_side_equality(self):
        """Side enum equality with integers."""
        assert Side.BUY == 0
        assert Side.SELL == 1

    def test_side_from_int(self):
        """Side can be created from integer."""
        assert Side(0) == Side.BUY
        assert Side(1) == Side.SELL


# ---------------------------------------------------------------------------
# TIF Enum Tests
# ---------------------------------------------------------------------------
class TestTIFEnum:
    def test_tif_values(self):
        """TIF enum has correct integer values."""
        assert int(TIF.LIMIT) == 0
        assert int(TIF.IOC) == 1
        assert int(TIF.FOK) == 2
        assert int(TIF.ROD) == 3


# ---------------------------------------------------------------------------
# IntentType Enum Tests
# ---------------------------------------------------------------------------
class TestIntentTypeEnum:
    def test_intent_type_values(self):
        """IntentType enum has correct integer values."""
        assert int(IntentType.NEW) == 0
        assert int(IntentType.AMEND) == 1
        assert int(IntentType.CANCEL) == 2


# ---------------------------------------------------------------------------
# OrderStatus Enum Tests
# ---------------------------------------------------------------------------
class TestOrderStatusEnum:
    def test_order_status_values(self):
        """OrderStatus enum has correct integer values."""
        assert int(OrderStatus.PENDING_SUBMIT) == 0
        assert int(OrderStatus.SUBMITTED) == 1
        assert int(OrderStatus.PARTIALLY_FILLED) == 2
        assert int(OrderStatus.FILLED) == 3
        assert int(OrderStatus.CANCELLED) == 4
        assert int(OrderStatus.FAILED) == 5


# ---------------------------------------------------------------------------
# StormGuardState Enum Tests
# ---------------------------------------------------------------------------
class TestStormGuardStateEnum:
    def test_storm_guard_values(self):
        """StormGuardState enum has correct integer values."""
        assert int(StormGuardState.NORMAL) == 0
        assert int(StormGuardState.WARM) == 1
        assert int(StormGuardState.STORM) == 2
        assert int(StormGuardState.HALT) == 3


# ---------------------------------------------------------------------------
# OrderIntent Tests
# ---------------------------------------------------------------------------
class TestOrderIntent:
    def test_construction(self):
        """OrderIntent can be constructed with required fields."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test_strategy",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,  # Fixed-point
            qty=10,
        )
        assert intent.intent_id == 1
        assert intent.strategy_id == "test_strategy"
        assert intent.symbol == "2330"
        assert intent.intent_type == IntentType.NEW
        assert intent.side == Side.BUY
        assert intent.price == 1000000
        assert intent.qty == 10

    def test_default_values(self):
        """OrderIntent has correct default values."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        assert intent.tif == TIF.LIMIT
        assert intent.target_order_id is None
        assert intent.timestamp_ns == 0
        assert intent.reason == ""
        assert intent.trace_id == ""

    def test_slots_attribute(self):
        """OrderIntent uses __slots__ for memory efficiency."""
        assert hasattr(OrderIntent, "__slots__")

    def test_amend_intent(self):
        """OrderIntent for amend includes target_order_id."""
        intent = OrderIntent(
            intent_id=2,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.AMEND,
            side=Side.BUY,
            price=1010000,
            qty=5,
            target_order_id="ORD-123",
        )
        assert intent.intent_type == IntentType.AMEND
        assert intent.target_order_id == "ORD-123"


# ---------------------------------------------------------------------------
# FillEvent Tests
# ---------------------------------------------------------------------------
class TestFillEvent:
    def test_construction(self):
        """FillEvent can be constructed with all fields."""
        fill = FillEvent(
            fill_id="FILL-001",
            account_id="ACC-001",
            order_id="ORD-001",
            strategy_id="strategy_a",
            symbol="2330",
            side=Side.BUY,
            qty=10,
            price=1000000,  # Fixed-point x10000
            fee=50,
            tax=30,
            ingest_ts_ns=1700000000000000000,
            match_ts_ns=1700000000000000000,
        )
        assert fill.fill_id == "FILL-001"
        assert fill.qty == 10
        assert fill.price == 1000000
        assert fill.fee == 50
        assert fill.tax == 30

    def test_slots_attribute(self):
        """FillEvent uses __slots__ for memory efficiency."""
        assert hasattr(FillEvent, "__slots__")


# ---------------------------------------------------------------------------
# OrderEvent Tests
# ---------------------------------------------------------------------------
class TestOrderEvent:
    def test_construction(self):
        """OrderEvent can be constructed with all fields."""
        event = OrderEvent(
            order_id="ORD-001",
            strategy_id="strategy_a",
            symbol="2330",
            status=OrderStatus.SUBMITTED,
            submitted_qty=10,
            filled_qty=0,
            remaining_qty=10,
            price=1000000,
            side=Side.BUY,
            ingest_ts_ns=1700000000000000000,
            broker_ts_ns=1700000000000000000,
        )
        assert event.order_id == "ORD-001"
        assert event.status == OrderStatus.SUBMITTED
        assert event.remaining_qty == 10

    def test_status_transitions(self):
        """OrderEvent status values represent valid order lifecycle."""
        # Verify status enum covers full lifecycle
        statuses = [
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
        ]
        assert len(statuses) == 6


# ---------------------------------------------------------------------------
# PositionDelta Tests
# ---------------------------------------------------------------------------
class TestPositionDelta:
    def test_construction(self):
        """PositionDelta can be constructed with all fields."""
        delta = PositionDelta(
            account_id="ACC-001",
            strategy_id="strategy_a",
            symbol="2330",
            net_qty=10,
            avg_price=1000000,
            realized_pnl=50000,
            unrealized_pnl=30000,
            delta_source="FILL",
        )
        assert delta.net_qty == 10
        assert delta.avg_price == 1000000
        assert delta.realized_pnl == 50000
        assert delta.delta_source == "FILL"

    def test_delta_sources(self):
        """PositionDelta supports various delta sources."""
        for source in ["FILL", "RECONCILE", "MARK"]:
            delta = PositionDelta(
                account_id="ACC",
                strategy_id="STR",
                symbol="SYM",
                net_qty=0,
                avg_price=0,
                realized_pnl=0,
                unrealized_pnl=0,
                delta_source=source,
            )
            assert delta.delta_source == source


# ---------------------------------------------------------------------------
# RiskDecision Tests
# ---------------------------------------------------------------------------
class TestRiskDecision:
    def test_construction(self):
        """RiskDecision can be constructed."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        decision = RiskDecision(approved=True, intent=intent)
        assert decision.approved is True
        assert decision.intent == intent

    def test_default_values(self):
        """RiskDecision has correct defaults."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        decision = RiskDecision(approved=True, intent=intent)
        assert decision.reason_code == "OK"
        assert decision.modified is False

    def test_rejected_decision(self):
        """RiskDecision can represent rejection."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        decision = RiskDecision(approved=False, intent=intent, reason_code="MAX_POSITION_EXCEEDED")
        assert decision.approved is False
        assert decision.reason_code == "MAX_POSITION_EXCEEDED"


# ---------------------------------------------------------------------------
# OrderCommand Tests
# ---------------------------------------------------------------------------
class TestOrderCommand:
    def test_construction(self):
        """OrderCommand can be constructed."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        cmd = OrderCommand(
            cmd_id=1,
            intent=intent,
            deadline_ns=1700000000000000000,
            storm_guard_state=StormGuardState.NORMAL,
        )
        assert cmd.cmd_id == 1
        assert cmd.deadline_ns == 1700000000000000000
        assert cmd.storm_guard_state == StormGuardState.NORMAL


# ---------------------------------------------------------------------------
# msgspec Compatibility Tests
# ---------------------------------------------------------------------------
class TestMsgspecCompatibility:
    """Test that contracts work with msgspec serialization."""

    def test_side_enum_json_encode(self):
        """Side enum can be encoded to JSON via msgspec."""
        # IntEnum serializes as integer
        encoded = msgspec.json.encode(Side.BUY)
        assert encoded == b"0"

        encoded = msgspec.json.encode(Side.SELL)
        assert encoded == b"1"

    def test_side_enum_json_decode(self):
        """Side enum can be decoded from JSON."""
        # Decode as int, then convert
        val = msgspec.json.decode(b"0")
        assert Side(val) == Side.BUY

    def test_dict_conversion_for_serialization(self):
        """Dataclasses can be converted to dict for serialization."""
        fill = FillEvent(
            fill_id="FILL-001",
            account_id="ACC-001",
            order_id="ORD-001",
            strategy_id="strategy_a",
            symbol="2330",
            side=Side.BUY,
            qty=10,
            price=1000000,
            fee=50,
            tax=30,
            ingest_ts_ns=1700000000000000000,
            match_ts_ns=1700000000000000000,
        )
        # Convert to dict using dataclasses
        from dataclasses import asdict

        d = asdict(fill)
        assert d["fill_id"] == "FILL-001"
        assert d["side"] == Side.BUY

        # Encode dict to JSON
        encoded = msgspec.json.encode(d)
        assert b"FILL-001" in encoded

    def test_msgspec_struct_alternative(self):
        """Demonstrate msgspec Struct as alternative for new contracts."""
        # This test shows that msgspec Struct could be used
        # for new contracts if needed

        class TestStruct(msgspec.Struct):
            symbol: str
            price: int
            qty: int

        s = TestStruct(symbol="2330", price=1000000, qty=10)
        encoded = msgspec.json.encode(s)
        decoded = msgspec.json.decode(encoded, type=TestStruct)
        assert decoded.symbol == "2330"
        assert decoded.price == 1000000


# ---------------------------------------------------------------------------
# Field Type Validation Tests
# ---------------------------------------------------------------------------
class TestFieldTypes:
    def test_fill_event_integer_fields(self):
        """FillEvent integer fields accept integers."""
        fill = FillEvent(
            fill_id="F1",
            account_id="A1",
            order_id="O1",
            strategy_id="S1",
            symbol="SYM",
            side=Side.BUY,
            qty=10,
            price=1000000,
            fee=50,
            tax=30,
            ingest_ts_ns=1700000000000000000,
            match_ts_ns=1700000000000000000,
        )
        assert isinstance(fill.qty, int)
        assert isinstance(fill.price, int)
        assert isinstance(fill.fee, int)
        assert isinstance(fill.tax, int)

    def test_position_delta_integer_fields(self):
        """PositionDelta uses integers for all financial fields."""
        delta = PositionDelta(
            account_id="A1",
            strategy_id="S1",
            symbol="SYM",
            net_qty=10,
            avg_price=1000000,
            realized_pnl=50000,
            unrealized_pnl=30000,
            delta_source="FILL",
        )
        assert isinstance(delta.net_qty, int)
        assert isinstance(delta.avg_price, int)
        assert isinstance(delta.realized_pnl, int)
        assert isinstance(delta.unrealized_pnl, int)

    def test_order_intent_integer_price(self):
        """OrderIntent price is integer (fixed-point)."""
        intent = OrderIntent(
            intent_id=1,
            strategy_id="test",
            symbol="2330",
            intent_type=IntentType.NEW,
            side=Side.BUY,
            price=1000000,
            qty=10,
        )
        assert isinstance(intent.price, int)
        assert isinstance(intent.qty, int)


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------
class TestEdgeCases:
    def test_zero_values(self):
        """Contracts handle zero values correctly."""
        delta = PositionDelta(
            account_id="A1",
            strategy_id="S1",
            symbol="SYM",
            net_qty=0,
            avg_price=0,
            realized_pnl=0,
            unrealized_pnl=0,
            delta_source="RECONCILE",
        )
        assert delta.net_qty == 0
        assert delta.avg_price == 0

    def test_negative_values(self):
        """Contracts handle negative values (short positions, losses)."""
        delta = PositionDelta(
            account_id="A1",
            strategy_id="S1",
            symbol="SYM",
            net_qty=-100,  # Short position
            avg_price=1000000,
            realized_pnl=-50000,  # Loss
            unrealized_pnl=-30000,
            delta_source="MARK",
        )
        assert delta.net_qty == -100
        assert delta.realized_pnl == -50000

    def test_large_timestamps(self):
        """Contracts handle large nanosecond timestamps."""
        fill = FillEvent(
            fill_id="F1",
            account_id="A1",
            order_id="O1",
            strategy_id="S1",
            symbol="SYM",
            side=Side.BUY,
            qty=10,
            price=1000000,
            fee=0,
            tax=0,
            ingest_ts_ns=9_999_999_999_999_999_999,  # Large ns value
            match_ts_ns=9_999_999_999_999_999_999,
        )
        assert fill.ingest_ts_ns == 9_999_999_999_999_999_999
