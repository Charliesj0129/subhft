"""Tests for DLQ STORM state check (H3) and numpy float price guard (M2)."""

import asyncio
import time
from unittest.mock import PropertyMock, patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side, StormGuardState
from hft_platform.risk.engine import RiskEngine


def _make_intent(intent_id: int = 1, price: int = 100, qty: int = 1) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.NEW, Side.BUY, price, qty, TIF.ROD, None, 0)


def _make_cancel_intent(intent_id: int = 99) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.CANCEL, Side.BUY, None, 1, TIF.ROD, None, 0)


@pytest.fixture
def engine(tmp_path):
    cfg = tmp_path / "risk.yaml"
    cfg.write_text("""
    risk:
      max_order_size: 100
      max_position: 200
      max_notional: 10000000
    """)
    q_in = asyncio.Queue()
    q_out = asyncio.Queue(maxsize=4096)
    eng = RiskEngine(str(cfg), q_in, q_out)
    eng._dlq_drain_interval = 1
    return eng


class TestDlqStormStateCheck:
    """DLQ drain must clear all entries when StormGuard is in STORM state."""

    def test_dlq_cleared_when_storm_state(self, engine: RiskEngine) -> None:
        cmd1 = engine.create_command(_make_intent(1))
        cmd2 = engine.create_command(_make_intent(2))
        now = time.monotonic_ns()
        engine._order_dlq.append((cmd1, now))
        engine._order_dlq.append((cmd2, now))

        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.STORM
        ):
            engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.empty()

    def test_dlq_cleared_via_halt_early_exit(self, engine: RiskEngine) -> None:
        """HALT >= STORM — caught by the top-level >= STORM early-exit guard."""
        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.HALT
        ):
            engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.empty()

    def test_dlq_storm_clear_increments_expired_metric(self, engine: RiskEngine) -> None:
        cmd1 = engine.create_command(_make_intent(1))
        cmd2 = engine.create_command(_make_intent(2))
        cmd3 = engine.create_command(_make_intent(3))
        now = time.monotonic_ns()
        for cmd in [cmd1, cmd2, cmd3]:
            engine._order_dlq.append((cmd, now))

        before = engine.metrics.risk_dlq_expired_total._value.get()
        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.STORM
        ):
            engine._drain_order_dlq()
        after = engine.metrics.risk_dlq_expired_total._value.get()

        assert after - before == 3

    def test_dlq_not_cleared_during_normal_state(self, engine: RiskEngine) -> None:
        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.NORMAL
        ):
            engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.qsize() == 1  # drained normally

    def test_dlq_not_cleared_during_warm_state(self, engine: RiskEngine) -> None:
        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.WARM
        ):
            engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.qsize() == 1  # drained normally

    def test_dlq_storm_clears_mid_drain(self, engine: RiskEngine) -> None:
        """STORM check triggers mid-loop when first entries pass TTL/deadline but STORM is set."""
        now = time.monotonic_ns()
        # First entry is fresh so it reaches the STORM check before TTL/deadline expire it
        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, now))

        with patch.object(
            type(engine.storm_guard), "state", new_callable=PropertyMock, return_value=StormGuardState.STORM
        ):
            engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.empty()


class TestNumpyFloatPriceGuard:
    """evaluate() must reject any non-int price (numpy floats, plain floats, Decimal, str)."""

    def test_int_price_passes_type_check(self, engine: RiskEngine) -> None:
        intent = _make_intent(price=1000000)
        # We only check the type-check stage; downstream validators may reject for other reasons.
        # A non-FLOAT_PRICE rejection is acceptable — the key assertion is it's not "FLOAT_PRICE".
        result = engine.evaluate(intent)
        assert result.reason_code != "FLOAT_PRICE"

    def test_plain_float_price_rejected(self, engine: RiskEngine) -> None:
        intent = _make_intent()
        object.__setattr__(intent, "price", 100.0)
        result = engine.evaluate(intent)
        assert result.approved is False
        assert result.reason_code == "FLOAT_PRICE"

    def test_numpy_float64_price_rejected(self, engine: RiskEngine) -> None:
        try:
            import numpy as np
        except ImportError:
            pytest.skip("numpy not available")

        intent = _make_intent()
        object.__setattr__(intent, "price", np.float64(100.0))
        result = engine.evaluate(intent)
        assert result.approved is False
        assert result.reason_code == "FLOAT_PRICE"

    def test_numpy_float32_price_rejected(self, engine: RiskEngine) -> None:
        try:
            import numpy as np
        except ImportError:
            pytest.skip("numpy not available")

        intent = _make_intent()
        object.__setattr__(intent, "price", np.float32(100.0))
        result = engine.evaluate(intent)
        assert result.approved is False
        assert result.reason_code == "FLOAT_PRICE"

    def test_string_price_rejected(self, engine: RiskEngine) -> None:
        intent = _make_intent()
        object.__setattr__(intent, "price", "100")
        result = engine.evaluate(intent)
        assert result.approved is False
        assert result.reason_code == "FLOAT_PRICE"

    def test_none_price_cancel_passes_type_check(self, engine: RiskEngine) -> None:
        """CANCEL intents with price=None must not be rejected for FLOAT_PRICE."""
        intent = _make_cancel_intent()
        result = engine.evaluate(intent)
        assert result.reason_code != "FLOAT_PRICE"
