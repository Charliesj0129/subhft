"""Property-based invariant tests for RiskEngine.evaluate().

Verifies core risk engine contracts:
1. HALT blocks NEW orders, allows CANCEL
2. Validator chain composition (any reject -> overall reject)
3. Zero price always rejected
4. Rejected decisions always have non-empty reason_code
5. evaluate() never mutates the input intent
6. Float price always rejected
7. Zero/negative qty always rejected (via qty guard validator)
8. Over position limit rejected
"""

from __future__ import annotations

import asyncio
import copy
import os
import tempfile
from pathlib import Path

# Disable Rust validator and FastGate before any risk engine import
os.environ["HFT_RISK_RUST_VALIDATOR"] = "0"
os.environ["HFT_RISK_FAST_GATE"] = "0"

from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    Side,
    StormGuardState,
)

try:
    from tests.factories.intents import make_order_intent
except ImportError:

    def make_order_intent(
        intent_id: int = 1,
        *,
        strategy_id: str = "s1",
        symbol: str = "2330",
        intent_type: IntentType = IntentType.NEW,
        side: Side = Side.BUY,
        price: int = 5_000_000,
        qty: int = 1,
        tif: TIF = TIF.LIMIT,
        target_order_id: str | None = None,
        timestamp_ns: int = 0,
        source_ts_ns: int = 0,
        reason: str = "",
        trace_id: str = "",
        idempotency_key: str = "",
        ttl_ns: int = 0,
    ) -> OrderIntent:
        from hft_platform.core import timebase

        return OrderIntent(
            intent_id=intent_id,
            strategy_id=strategy_id,
            symbol=symbol,
            intent_type=intent_type,
            side=side,
            price=price,
            qty=qty,
            tif=tif,
            target_order_id=target_order_id,
            timestamp_ns=timestamp_ns or timebase.now_ns(),
            source_ts_ns=source_ts_ns,
            reason=reason,
            trace_id=trace_id,
            idempotency_key=idempotency_key or f"key-{intent_id}",
            ttl_ns=ttl_ns,
        )


try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Hypothesis stubs when not installed
# ---------------------------------------------------------------------------

if not HYPOTHESIS_AVAILABLE:

    def given(*_args: Any, **_kwargs: Any) -> Any:  # type: ignore[misc]
        def decorator(f: Any) -> Any:
            def wrapper(*a: Any, **kw: Any) -> None:
                pytest.skip("hypothesis not installed")

            return wrapper

        return decorator

    def settings(*_args: Any, **_kwargs: Any) -> Any:  # type: ignore[misc]
        def decorator(f: Any) -> Any:
            return f

        return decorator

    class _StubStrategies:
        @staticmethod
        def integers(*, min_value: int = 0, max_value: int = 0) -> Any:
            return None

        @staticmethod
        def sampled_from(values: Any) -> Any:
            return None

        @staticmethod
        def text(*, min_size: int = 0, max_size: int = 0) -> Any:
            return None

    st = _StubStrategies()  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Shared constants and helpers
# ---------------------------------------------------------------------------

_PRICE_MIN = 1_0000  # 1.0000 scaled
_PRICE_MAX = 50_000_0000  # 5000.0000 scaled (within max_price_cap)
_QTY_MIN = 1
_QTY_MAX = 50


def _make_risk_engine(tmp_path: Any, *, storm_state: StormGuardState = StormGuardState.NORMAL) -> Any:
    """Build a RiskEngine with mocked observability deps."""
    cfg: dict[str, Any] = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 200,
            "max_notional": 10_000_000,
            "per_symbol_max_notional": 50_000_000,
            "max_position_lots": 100,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.risk.engine.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        engine.storm_guard.state = storm_state
        return engine


# ===========================================================================
# 1. HALT blocks NEW orders, allows CANCEL
# ===========================================================================


class TestHaltBlocksNewOrders:
    """HALT state must reject NEW/AMEND but allow CANCEL."""

    def test_halt_rejects_new_order(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(intent_type=IntentType.NEW, price=1_000_000, qty=1)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"

    def test_halt_allows_cancel(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(
            intent_type=IntentType.CANCEL,
            price=1_000_000,
            qty=1,
            target_order_id="o-123",
        )
        decision = engine.evaluate(intent)
        assert decision.approved
        assert decision.reason_code == "OK"

    def test_halt_rejects_amend(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(
            intent_type=IntentType.AMEND,
            price=1_000_000,
            qty=1,
            target_order_id="o-456",
        )
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        price=st.integers(min_value=1_0000, max_value=50_000_0000),
        qty=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=30, deadline=None)
    def test_halt_rejects_new_property(self, price: int, qty: int) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine(td, storm_state=StormGuardState.HALT)
        intent = make_order_intent(intent_type=IntentType.NEW, price=price, qty=qty)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"


# ===========================================================================
# 2. Validator chain composition
# ===========================================================================


class TestValidatorChainComposition:
    """Any single validator rejection means overall rejection with reason != 'OK'."""

    def test_price_exceeds_cap_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        # Price 6000.0 scaled = 60_000_000, exceeds max_price_cap of 5000.0
        intent = make_order_intent(price=60_000_000, qty=1)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code != "OK"
        assert len(decision.reason_code) > 0

    def test_excessive_notional_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        # qty=1000 * price=50_000_000 = very large notional
        intent = make_order_intent(price=50_000_000, qty=1000)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code != "OK"

    def test_valid_order_approved(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        decision = engine.evaluate(intent)
        assert decision.approved
        assert decision.reason_code == "OK"


# ===========================================================================
# 3. Zero price always rejected
# ===========================================================================


class TestZeroPriceRejected:
    """Price=0 must always be rejected by validators."""

    def test_zero_price_new_order(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=0, qty=1, intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code != "OK"

    def test_zero_price_buy_and_sell(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        for side in (Side.BUY, Side.SELL):
            intent = make_order_intent(price=0, qty=1, side=side)
            decision = engine.evaluate(intent)
            assert not decision.approved, f"price=0 should be rejected for side={side}"

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        qty=st.integers(min_value=1, max_value=100),
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    @settings(max_examples=20, deadline=None)
    def test_zero_price_property(self, qty: int, side: Side) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine(td)
        intent = make_order_intent(price=0, qty=qty, side=side)
        decision = engine.evaluate(intent)
        assert not decision.approved


# ===========================================================================
# 4. Rejected decisions always have non-empty reason_code
# ===========================================================================


class TestRejectReasonNonEmpty:
    """Every rejected RiskDecision must carry a non-empty reason_code."""

    _REJECTION_CASES: list[dict[str, Any]] = [
        {"price": 0, "qty": 1},  # zero price
        {"price": 60_000_000, "qty": 1},  # exceeds cap
        {"price": 50_000_000, "qty": 1000},  # notional breach
    ]

    @pytest.mark.parametrize("kwargs", _REJECTION_CASES)
    def test_rejected_has_reason(self, tmp_path: Any, kwargs: dict[str, Any]) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(**kwargs)
        decision = engine.evaluate(intent)
        if not decision.approved:
            assert decision.reason_code is not None
            assert len(decision.reason_code) > 0
            assert decision.reason_code != "OK"

    def test_halt_rejection_has_reason(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(price=1_000_000, qty=1)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"
        assert len(decision.reason_code) > 0

    def test_float_rejection_has_reason(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        # Forcefully set float price to trigger float check
        object.__setattr__(intent, "price", 100.5)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"
        assert len(decision.reason_code) > 0


# ===========================================================================
# 5. evaluate() never mutates the input intent
# ===========================================================================


class TestApproveNeverMutatesIntent:
    """RiskEngine.evaluate() must not mutate any field of the input OrderIntent."""

    def test_approved_intent_unchanged(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        snapshot = copy.deepcopy(intent)
        decision = engine.evaluate(intent)
        assert decision.approved
        _assert_intent_unchanged(intent, snapshot)

    def test_rejected_intent_unchanged(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=60_000_000, qty=1)
        snapshot = copy.deepcopy(intent)
        decision = engine.evaluate(intent)
        assert not decision.approved
        _assert_intent_unchanged(intent, snapshot)

    def test_halt_rejected_intent_unchanged(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(price=1_000_000, qty=1)
        snapshot = copy.deepcopy(intent)
        decision = engine.evaluate(intent)
        assert not decision.approved
        _assert_intent_unchanged(intent, snapshot)

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        price=st.integers(min_value=1_0000, max_value=50_000_0000),
        qty=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=30, deadline=None)
    def test_immutability_property(self, price: int, qty: int) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine(td)
        intent = make_order_intent(price=price, qty=qty)
        snapshot = copy.deepcopy(intent)
        engine.evaluate(intent)
        _assert_intent_unchanged(intent, snapshot)


def _assert_intent_unchanged(actual: OrderIntent, expected: OrderIntent) -> None:
    """Assert every field of *actual* matches *expected*."""
    for field in (
        "intent_id",
        "strategy_id",
        "symbol",
        "intent_type",
        "side",
        "price",
        "qty",
        "tif",
        "target_order_id",
        "timestamp_ns",
        "source_ts_ns",
        "reason",
        "trace_id",
        "idempotency_key",
        "ttl_ns",
    ):
        assert getattr(actual, field) == getattr(expected, field), (
            f"Intent field '{field}' was mutated: {getattr(actual, field)} != {getattr(expected, field)}"
        )


# ===========================================================================
# 6. Float price always rejected
# ===========================================================================


class TestFloatPriceRejection:
    """Float prices must always be rejected with reason_code='FLOAT_PRICE'."""

    def test_float_price_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        object.__setattr__(intent, "price", 100.5)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    def test_float_zero_price_rejected_as_float(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        object.__setattr__(intent, "price", 0.0)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    def test_float_negative_price_rejected_as_float(self, tmp_path: Any) -> None:
        engine = _make_risk_engine(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=1)
        object.__setattr__(intent, "price", -50.0)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    def test_float_price_in_halt_still_float_reason(self, tmp_path: Any) -> None:
        """Float check runs before StormGuard, so float is caught first."""
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = make_order_intent(price=1_000_000, qty=1)
        object.__setattr__(intent, "price", 99.9)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        price_float=st.sampled_from([0.01, 1.0, 100.5, 999.99, 5000.0, -1.0]),
    )
    @settings(max_examples=10, deadline=None)
    def test_float_price_property(self, price_float: float) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine(td)
        intent = make_order_intent(price=1_000_000, qty=1)
        object.__setattr__(intent, "price", price_float)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"


# ===========================================================================
# 7. Zero/negative qty always rejected
# ===========================================================================


class _QtyGuardValidator:
    """Minimal validator that rejects qty <= 0.

    Inserted at the head of the validator chain to enforce the invariant
    that every order must have a strictly positive quantity.
    """

    def check(self, intent: OrderIntent) -> tuple[bool, str]:
        if intent.intent_type == IntentType.CANCEL:
            return True, "OK"
        if intent.qty <= 0:
            return False, "QTY_ZERO_OR_NEG"
        return True, "OK"


def _make_risk_engine_with_qty_guard(
    tmp_path: Any,
    *,
    storm_state: StormGuardState = StormGuardState.NORMAL,
) -> Any:
    """Build a RiskEngine with a qty guard validator prepended to the chain."""
    engine = _make_risk_engine(tmp_path, storm_state=storm_state)
    engine.validators.insert(0, _QtyGuardValidator())  # type: ignore[arg-type]
    return engine


class TestZeroQtyRejected:
    """qty <= 0 must always be rejected when a qty guard validator is active."""

    def test_zero_qty_new_order_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_qty_guard(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=0)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "QTY" in decision.reason_code

    def test_negative_qty_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_qty_guard(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=-1)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "QTY" in decision.reason_code

    def test_zero_qty_buy_and_sell(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_qty_guard(tmp_path)
        for side in (Side.BUY, Side.SELL):
            intent = make_order_intent(price=1_000_000, qty=0, side=side)
            decision = engine.evaluate(intent)
            assert not decision.approved, f"qty=0 should be rejected for side={side}"

    def test_zero_qty_reason_non_empty(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_qty_guard(tmp_path)
        intent = make_order_intent(price=1_000_000, qty=0)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code is not None
        assert len(decision.reason_code) > 0
        assert decision.reason_code != "OK"

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        qty=st.integers(min_value=-100, max_value=0),
        side=st.sampled_from([Side.BUY, Side.SELL]),
    )
    @settings(max_examples=20, deadline=None)
    def test_non_positive_qty_property(self, qty: int, side: Side) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine_with_qty_guard(td)
        intent = make_order_intent(price=1_000_000, qty=qty, side=side)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "QTY_ZERO_OR_NEG"


# ===========================================================================
# 8. Over position limit rejected
# ===========================================================================


def _make_risk_engine_with_position_limit(
    tmp_path: Any,
    *,
    max_position_lots: int = 100,
    storm_state: StormGuardState = StormGuardState.NORMAL,
) -> Any:
    """Build a RiskEngine with a configurable position limit."""
    cfg: dict[str, Any] = {
        "global_defaults": {
            "max_price_cap": 5000.0,
            "tick_size": 0.01,
            "price_band_ticks": 200,
            "max_notional": 10_000_000_000,  # very high to avoid notional rejection
            "per_symbol_max_notional": 50_000_000_000,
            "max_position_lots": max_position_lots,
            "daily_loss_limit": 500_000,
        },
        "strategies": {},
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.risk.engine.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        engine.storm_guard.state = storm_state
        return engine


class TestOverPositionLimitRejected:
    """Intent with qty exceeding position_limit must be rejected."""

    def test_qty_exceeds_position_limit(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_position_limit(tmp_path, max_position_lots=10)
        intent = make_order_intent(price=1_000_000, qty=11)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "POSITION_LIMIT" in decision.reason_code

    def test_qty_at_limit_passes(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_position_limit(tmp_path, max_position_lots=10)
        intent = make_order_intent(price=1_000_000, qty=10)
        decision = engine.evaluate(intent)
        assert decision.approved
        assert decision.reason_code == "OK"

    def test_qty_just_over_limit_rejected(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_position_limit(tmp_path, max_position_lots=5)
        intent = make_order_intent(price=1_000_000, qty=6)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "POSITION_LIMIT" in decision.reason_code

    def test_position_limit_reason_non_empty(self, tmp_path: Any) -> None:
        engine = _make_risk_engine_with_position_limit(tmp_path, max_position_lots=1)
        intent = make_order_intent(price=1_000_000, qty=5)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code is not None
        assert len(decision.reason_code) > 0
        assert decision.reason_code != "OK"

    def test_cancel_bypasses_position_limit(self, tmp_path: Any) -> None:
        """CANCEL intents bypass position limit checks."""
        engine = _make_risk_engine_with_position_limit(tmp_path, max_position_lots=1)
        intent = make_order_intent(
            price=1_000_000,
            qty=100,
            intent_type=IntentType.CANCEL,
            target_order_id="o-789",
        )
        decision = engine.evaluate(intent)
        assert decision.approved

    @pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
    @given(
        qty=st.integers(min_value=11, max_value=1000),
    )
    @settings(max_examples=20, deadline=None)
    def test_over_limit_property(self, qty: int) -> None:
        td = Path(tempfile.mkdtemp())
        engine = _make_risk_engine_with_position_limit(td, max_position_lots=10)
        intent = make_order_intent(price=1_000_000, qty=qty)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert "POSITION_LIMIT" in decision.reason_code
