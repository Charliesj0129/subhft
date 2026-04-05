"""Tests for RiskEngine TTL expiry check on OrderIntent."""
import asyncio
from unittest.mock import patch

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, RiskFeedback, Side
from hft_platform.risk.engine import RiskEngine


def _make_intent(
    intent_id: int = 1,
    price: int = 100,
    qty: int = 1,
    ttl_ns: int = 0,
    timestamp_ns: int = 0,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        strategy_id="s1",
        symbol="2330",
        intent_type=IntentType.NEW,
        side=Side.BUY,
        price=price,
        qty=qty,
        tif=TIF.ROD,
        timestamp_ns=timestamp_ns,
        ttl_ns=ttl_ns,
    )


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
    rejection_sink = asyncio.Queue(maxsize=64)
    eng = RiskEngine(str(cfg), q_in, q_out, rejection_sink=rejection_sink)
    return eng


class TestTtlExpiry:
    """TTL checks in RiskEngine.run() before risk evaluation."""

    @pytest.mark.asyncio
    async def test_expired_intent_rejected_by_ttl(self, engine: RiskEngine) -> None:
        """Intent with ttl_ns=1ms but timestamp 10ms old is rejected without evaluate()."""
        now_ns = 1_000_000_000
        ttl_ns = 1_000_000  # 1ms
        ts_ns = now_ns - 10_000_000  # 10ms ago — expired

        intent = _make_intent(intent_id=1, ttl_ns=ttl_ns, timestamp_ns=ts_ns)
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        with patch("hft_platform.core.timebase.now_ns", return_value=now_ns):
            await asyncio.sleep(0.05)
        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Intent was rejected — nothing in order_queue
        assert engine.order_queue.empty()

    @pytest.mark.asyncio
    async def test_fresh_intent_passes_ttl_check(self, engine: RiskEngine) -> None:
        """Intent with ttl_ns=1s and recent timestamp passes through to evaluate()."""
        now_ns = 1_000_000_000_000
        ttl_ns = 1_000_000_000  # 1s
        ts_ns = now_ns - 1_000_000  # 1ms ago — fresh

        intent = _make_intent(intent_id=2, ttl_ns=ttl_ns, timestamp_ns=ts_ns)
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        with patch("hft_platform.core.timebase.now_ns", return_value=now_ns):
            await asyncio.sleep(0.05)
        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # Intent passed TTL check and was approved — must appear in order_queue
        assert not engine.order_queue.empty()

    @pytest.mark.asyncio
    async def test_zero_ttl_means_no_expiry(self, engine: RiskEngine) -> None:
        """Intent with ttl_ns=0 always passes regardless of age (backward compat)."""
        # timestamp very old, ttl=0 → never expire
        intent = _make_intent(intent_id=3, ttl_ns=0, timestamp_ns=1)
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        await asyncio.sleep(0.05)
        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # ttl=0 skips expiry check → approved and forwarded
        assert not engine.order_queue.empty()

    @pytest.mark.asyncio
    async def test_ttl_rejection_sends_feedback(self, engine: RiskEngine) -> None:
        """Expired intent sends RiskFeedback with reason_code=TTL_EXPIRED to rejection_sink."""
        now_ns = 2_000_000_000
        ttl_ns = 1_000_000  # 1ms
        ts_ns = now_ns - 10_000_000  # 10ms ago — expired

        intent = _make_intent(intent_id=42, ttl_ns=ttl_ns, timestamp_ns=ts_ns)
        engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        with patch("hft_platform.core.timebase.now_ns", return_value=now_ns):
            await asyncio.sleep(0.05)
        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert engine._rejection_sink is not None
        assert not engine._rejection_sink.empty()
        feedback: RiskFeedback = engine._rejection_sink.get_nowait()
        assert feedback.intent_id == 42
        assert feedback.strategy_id == "s1"
        assert feedback.symbol == "2330"
        assert feedback.reason_code == "TTL_EXPIRED"
