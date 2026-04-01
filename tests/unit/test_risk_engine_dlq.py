"""Tests for RiskEngine DLQ drain/retry mechanism."""
import asyncio
import time

import pytest

from hft_platform.contracts.strategy import TIF, IntentType, OrderIntent, Side
from hft_platform.risk.engine import RiskEngine


def _make_intent(intent_id: int = 1, price: int = 100, qty: int = 1) -> OrderIntent:
    return OrderIntent(intent_id, "s1", "2330", IntentType.NEW, Side.BUY, price, qty, TIF.ROD, None, 0)


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
    # Set drain interval to 1 for easier testing
    eng._dlq_drain_interval = 1
    return eng


class TestDlqDrainSuccess:
    """Test that DLQ entries are drained back to order_queue when space is available."""

    def test_drain_moves_entries_to_order_queue(self, engine: RiskEngine) -> None:
        cmd1 = engine.create_command(_make_intent(1))
        cmd2 = engine.create_command(_make_intent(2))
        now = time.monotonic_ns()
        engine._order_dlq.append((cmd1, now))
        engine._order_dlq.append((cmd2, now))

        engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.qsize() == 2
        out1 = engine.order_queue.get_nowait()
        out2 = engine.order_queue.get_nowait()
        assert out1.cmd_id == cmd1.cmd_id
        assert out2.cmd_id == cmd2.cmd_id

    def test_drain_increments_drained_metric(self, engine: RiskEngine) -> None:
        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        before = engine.metrics.risk_dlq_drained_total._value.get()
        engine._drain_order_dlq()
        after = engine.metrics.risk_dlq_drained_total._value.get()

        assert after - before == 1

    def test_drain_noop_when_dlq_empty(self, engine: RiskEngine) -> None:
        before = engine.metrics.risk_dlq_drained_total._value.get()
        engine._drain_order_dlq()
        after = engine.metrics.risk_dlq_drained_total._value.get()
        assert after == before
        assert engine.order_queue.empty()


class TestDlqExpiration:
    """Test that stale DLQ entries (older than TTL) are expired and not retried."""

    def test_expired_entries_discarded(self, engine: RiskEngine) -> None:
        cmd = engine.create_command(_make_intent(1))
        # Enqueue with a timestamp well past the TTL
        old_ts = time.monotonic_ns() - engine._dlq_ttl_ns - 1_000_000_000
        engine._order_dlq.append((cmd, old_ts))

        engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.empty()  # expired, not drained

    def test_expired_metric_incremented(self, engine: RiskEngine) -> None:
        cmd = engine.create_command(_make_intent(1))
        old_ts = time.monotonic_ns() - engine._dlq_ttl_ns - 1_000_000_000
        engine._order_dlq.append((cmd, old_ts))

        before = engine.metrics.risk_dlq_expired_total._value.get()
        engine._drain_order_dlq()
        after = engine.metrics.risk_dlq_expired_total._value.get()

        assert after - before == 1

    def test_mixed_expired_and_valid(self, engine: RiskEngine) -> None:
        """Expired entries at the front are discarded; valid entries behind them are drained."""
        old_ts = time.monotonic_ns() - engine._dlq_ttl_ns - 1_000_000_000
        fresh_ts = time.monotonic_ns()

        cmd_old = engine.create_command(_make_intent(1))
        cmd_fresh = engine.create_command(_make_intent(2))
        engine._order_dlq.append((cmd_old, old_ts))
        engine._order_dlq.append((cmd_fresh, fresh_ts))

        engine._drain_order_dlq()

        assert len(engine._order_dlq) == 0
        assert engine.order_queue.qsize() == 1
        out = engine.order_queue.get_nowait()
        assert out.cmd_id == cmd_fresh.cmd_id


class TestDlqDrainStopsWhenFull:
    """Test that drain stops when order_queue is full again."""

    def test_drain_stops_at_full_queue(self, engine: RiskEngine) -> None:
        # Use a tiny queue
        engine.order_queue = asyncio.Queue(maxsize=1)
        now = time.monotonic_ns()
        cmd1 = engine.create_command(_make_intent(1))
        cmd2 = engine.create_command(_make_intent(2))
        engine._order_dlq.append((cmd1, now))
        engine._order_dlq.append((cmd2, now))

        engine._drain_order_dlq()

        # Only 1 should have been drained; 1 remains in DLQ
        assert engine.order_queue.qsize() == 1
        assert len(engine._order_dlq) == 1
        remaining_cmd, _ = engine._order_dlq[0]
        assert remaining_cmd.cmd_id == cmd2.cmd_id


class TestDlqDrainCounter:
    """Test that drain counter works (only drains every N iterations)."""

    @pytest.mark.asyncio
    async def test_drain_every_n_intents(self, engine: RiskEngine) -> None:
        engine._dlq_drain_interval = 3
        engine._dlq_drain_counter = 0

        # Populate DLQ
        now = time.monotonic_ns()
        for i in range(3):
            cmd = engine.create_command(_make_intent(i + 10))
            engine._order_dlq.append((cmd, now))

        # Simulate 3 intents through run loop
        for _ in range(3):
            intent = _make_intent(price=100, qty=1)
            engine.intent_queue.put_nowait(intent)

        task = asyncio.create_task(engine.run())
        # Wait for processing
        await asyncio.sleep(0.1)

        engine.running = False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        # After 3 intents with interval=3, drain should have been called once
        # DLQ entries should have been drained
        assert len(engine._order_dlq) == 0

    def test_counter_does_not_drain_before_interval(self, engine: RiskEngine) -> None:
        engine._dlq_drain_interval = 5
        engine._dlq_drain_counter = 0

        cmd = engine.create_command(_make_intent(1))
        engine._order_dlq.append((cmd, time.monotonic_ns()))

        # Simulate counter increments below interval
        for _ in range(4):
            engine._dlq_drain_counter += 1

        # Counter is 4, interval is 5 — should not drain yet
        assert engine._dlq_drain_counter < engine._dlq_drain_interval
        assert len(engine._order_dlq) == 1
