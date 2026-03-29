"""Order lifecycle contract tests.

Verifies field propagation across intent -> decision -> command boundaries,
StormGuard gating behavior, cmd_id monotonicity, and idempotency/TTL preservation.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
import yaml

from hft_platform.contracts.strategy import (
    TIF,
    IntentType,
    OrderIntent,
    RiskDecision,
    Side,
    StormGuardState,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_intent(
    *,
    intent_id: int = 1,
    price: int | float = 1_000_000,
    qty: int = 1,
    side: Side = Side.BUY,
    intent_type: IntentType = IntentType.NEW,
    strategy_id: str = "test_strat",
    symbol: str = "2330",
    idempotency_key: str = "",
    ttl_ns: int = 0,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id=strategy_id,
        symbol=symbol,
        intent_type=intent_type,
        side=side,
        price=price,  # type: ignore[arg-type]
        qty=qty,
        tif=TIF.LIMIT,
        idempotency_key=idempotency_key,
        ttl_ns=ttl_ns,
    )


def _make_risk_engine(tmp_path, *, storm_state: StormGuardState = StormGuardState.NORMAL):
    """Create a RiskEngine with mocked dependencies."""
    cfg = {
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
        "storm_guard": {
            "warm_threshold": -200_000,
            "storm_threshold": -500_000,
            "halt_threshold": -1_000_000,
        },
    }
    cfg_path = tmp_path / "strategy_limits.yaml"
    cfg_path.write_text(yaml.dump(cfg))

    with (
        patch("hft_platform.risk.engine.MetricsRegistry") as mock_mr,
        patch("hft_platform.risk.engine.LatencyRecorder") as mock_lr,
        patch("hft_platform.recorder.audit.get_audit_writer", return_value=MagicMock()),
    ):
        mock_mr.get.return_value = None
        mock_lr.get.return_value = None
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine(str(cfg_path), asyncio.Queue(), asyncio.Queue())
        engine.metrics = None
        engine.storm_guard.state = storm_state
        return engine


# ---------------------------------------------------------------------------
# 1. Field propagation: intent -> decision
# ---------------------------------------------------------------------------


class TestIntentToDecision:
    def test_decision_contains_same_intent_object(self, tmp_path):
        """RiskDecision.intent is the same object as the input intent."""
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.intent is intent

    def test_decision_approved_carries_ok_reason(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        if decision.approved:
            assert decision.reason_code == "OK"


# ---------------------------------------------------------------------------
# 2. Field propagation: decision -> command
# ---------------------------------------------------------------------------


class TestDecisionToCommand:
    def test_command_preserves_all_intent_fields(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(
            strategy_id="s1",
            symbol="2330",
            side=Side.SELL,
            price=2_000_000,
            qty=5,
            idempotency_key="key123",
            ttl_ns=999,
        )
        decision = engine.evaluate(intent)
        assert decision.approved, f"Unexpected rejection: {decision.reason_code}"
        cmd = engine.create_command(decision.intent)

        assert cmd.intent.strategy_id == "s1"
        assert cmd.intent.symbol == "2330"
        assert cmd.intent.side == Side.SELL
        assert cmd.intent.price == 2_000_000
        assert cmd.intent.qty == 5
        assert cmd.intent.idempotency_key == "key123"
        assert cmd.intent.ttl_ns == 999


# ---------------------------------------------------------------------------
# 3. CANCEL bypasses validators
# ---------------------------------------------------------------------------


class TestCancelBypass:
    def test_cancel_bypasses_storm_guard_halt(self, tmp_path):
        """CANCEL intent is allowed even in HALT state."""
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = engine.evaluate(intent)
        # StormGuard HALT allows CANCEL
        assert decision.reason_code != "STORMGUARD_HALT"


# ---------------------------------------------------------------------------
# 4. HALT blocks NEW
# ---------------------------------------------------------------------------


class TestHaltBlocksNew:
    def test_halt_blocks_new_order(self, tmp_path):
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = _make_intent(intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "STORMGUARD_HALT"

    def test_halt_allows_cancel(self, tmp_path):
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = _make_intent(intent_type=IntentType.CANCEL)
        decision = engine.evaluate(intent)
        assert decision.reason_code != "STORMGUARD_HALT"


# ---------------------------------------------------------------------------
# 5. cmd_id monotonicity
# ---------------------------------------------------------------------------


class TestCmdIdMonotonicity:
    def test_cmd_id_increases(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        ids = []
        for _ in range(10):
            intent = _make_intent()
            decision = engine.evaluate(intent)
            if decision.approved:
                cmd = engine.create_command(decision.intent)
                ids.append(cmd.cmd_id)

        assert len(ids) >= 2, "Need at least 2 approved intents"
        for i in range(1, len(ids)):
            assert ids[i] > ids[i - 1], f"cmd_id not monotonic: {ids}"


# ---------------------------------------------------------------------------
# 6. Deadline is in the future
# ---------------------------------------------------------------------------


class TestDeadlineFuture:
    def test_deadline_greater_than_created(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved
        cmd = engine.create_command(decision.intent)
        assert cmd.deadline_ns > cmd.created_ns


# ---------------------------------------------------------------------------
# 7. storm_guard_state propagated to command
# ---------------------------------------------------------------------------


class TestStormGuardPropagation:
    def test_normal_state_propagated(self, tmp_path):
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.NORMAL)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        assert decision.approved
        cmd = engine.create_command(decision.intent)
        assert cmd.storm_guard_state == StormGuardState.NORMAL

    def test_warm_state_propagated(self, tmp_path):
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.WARM)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        if decision.approved:
            cmd = engine.create_command(decision.intent)
            assert cmd.storm_guard_state == StormGuardState.WARM


# ---------------------------------------------------------------------------
# 8. Idempotency key and TTL preserved
# ---------------------------------------------------------------------------


class TestIdempotencyAndTtl:
    def test_idempotency_key_preserved(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(idempotency_key="dedup-abc-123")
        decision = engine.evaluate(intent)
        assert decision.intent.idempotency_key == "dedup-abc-123"

    def test_ttl_ns_preserved(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(ttl_ns=500_000_000)
        decision = engine.evaluate(intent)
        assert decision.intent.ttl_ns == 500_000_000


# ---------------------------------------------------------------------------
# 9. Core fields preserved through lifecycle
# ---------------------------------------------------------------------------


class TestCoreFieldsPreserved:
    def test_all_core_fields_survive_evaluate_and_create_command(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(
            strategy_id="alpha_v2",
            symbol="TXFJ5",
            side=Side.SELL,
            price=1_500_000,
            qty=3,
        )
        decision = engine.evaluate(intent)
        assert decision.approved, f"Unexpected rejection: {decision.reason_code}"
        cmd = engine.create_command(decision.intent)

        assert cmd.intent.strategy_id == "alpha_v2"
        assert cmd.intent.symbol == "TXFJ5"
        assert cmd.intent.side == Side.SELL
        assert cmd.intent.price == 1_500_000
        assert cmd.intent.qty == 3


# ---------------------------------------------------------------------------
# 10. Float price rejection
# ---------------------------------------------------------------------------


class TestFloatPriceRejection:
    def test_float_price_rejected(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(price=123.45)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"

    def test_zero_float_rejected(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent(price=0.0)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code == "FLOAT_PRICE"


# ---------------------------------------------------------------------------
# 11. Reason code semantics
# ---------------------------------------------------------------------------


class TestReasonCodeSemantics:
    def test_approved_reason_is_ok(self, tmp_path):
        engine = _make_risk_engine(tmp_path)
        intent = _make_intent()
        decision = engine.evaluate(intent)
        if decision.approved:
            assert decision.reason_code == "OK"

    def test_rejected_reason_is_not_ok(self, tmp_path):
        engine = _make_risk_engine(tmp_path, storm_state=StormGuardState.HALT)
        intent = _make_intent(intent_type=IntentType.NEW)
        decision = engine.evaluate(intent)
        assert not decision.approved
        assert decision.reason_code != "OK"
        assert len(decision.reason_code) > 0


# ---------------------------------------------------------------------------
# 12. Hypothesis property tests
# ---------------------------------------------------------------------------

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st

    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False

    def given(*args, **kwargs):  # type: ignore[misc]
        def decorator(f):
            def wrapper(*a, **kw):
                pytest.skip("hypothesis not installed")

            return wrapper

        return decorator

    def settings(**kwargs):  # type: ignore[misc]
        def decorator(f):
            return f

        return decorator

    class _St:
        def integers(self, **kw):
            return None

    st = _St()  # type: ignore[assignment]


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(max_examples=50, deadline=None)
@given(
    price=st.integers(min_value=1, max_value=50_000_000),
    qty=st.integers(min_value=1, max_value=1000),
)
def test_hypothesis_valid_intent_never_raises(price, qty):
    """Evaluating a valid intent never raises an exception."""
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        engine = _make_risk_engine(pathlib.Path(td))
        intent = _make_intent(price=price, qty=qty)
        decision = engine.evaluate(intent)
        assert isinstance(decision, RiskDecision)


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
@settings(max_examples=50, deadline=None)
@given(n=st.integers(min_value=1, max_value=100))
def test_hypothesis_cmd_id_always_positive(n):
    """cmd_id is always a positive integer."""
    import pathlib
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        engine = _make_risk_engine(pathlib.Path(td))
        intent = _make_intent()
        decision = engine.evaluate(intent)
        if decision.approved:
            cmd = engine.create_command(decision.intent)
            assert cmd.cmd_id > 0
