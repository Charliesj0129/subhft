"""Tests for Round 3 Decision plane fixes (R3-1, R3-2, R3-3).

Covers:
- Fix A: typed-intent QueueFull feedback identity extraction (R3-3)
- Fix B: OrderAdapter dispatch-failure rejection feedback (R3-2)
- Fix C: RiskEngine DLQ rejection sink overflow logging (R3-1)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Fix A: typed-intent identity extraction (R3-3)
# ---------------------------------------------------------------------------

class TestTypedIntentIdentity:
    """Verify _typed_intent_identity handles both tuples and OrderIntent objects."""

    def test_extracts_from_typed_tuple(self):
        from hft_platform.strategy.runner import _typed_intent_identity

        # typed_intent_v1: (tag, intent_id, strategy_id, symbol, intent_type, side, ...)
        intent = ("typed_intent_v1", 42, "strat_r47", "TXFD6", 0, 1, 200000, 1, 0)
        iid, sid, sym, side = _typed_intent_identity(intent)

        assert iid == 42
        assert sid == "strat_r47"
        assert sym == "TXFD6"
        assert side == 1

    def test_extracts_from_order_intent_object(self):
        from hft_platform.strategy.runner import _typed_intent_identity

        intent = MagicMock()
        intent.intent_id = 99
        intent.strategy_id = "my_strat"
        intent.symbol = "2330"
        intent.side = 0

        iid, sid, sym, side = _typed_intent_identity(intent)

        assert iid == 99
        assert sid == "my_strat"
        assert sym == "2330"
        assert side == 0

    def test_handles_short_tuple_gracefully(self):
        from hft_platform.strategy.runner import _typed_intent_identity

        # 4 elements: tag + intent_id + strategy_id + symbol (minimum for tuple path)
        intent = ("typed_intent_v1", 1, "s", "X")
        iid, sid, sym, side = _typed_intent_identity(intent)

        assert iid == 1
        assert sid == "s"
        assert sym == "X"
        assert side is None  # only 4 elements, no side at index 5

    def test_handles_non_typed_tuple(self):
        from hft_platform.strategy.runner import _typed_intent_identity

        intent = ("tick", 123456, "TXFD6")  # not a typed_intent_v1
        iid, sid, sym, side = _typed_intent_identity(intent)

        # Falls through to getattr path, tuple has no named attrs
        assert iid == 0
        assert sid == ""

    def test_getattr_fallback_returns_defaults_for_plain_tuple(self):
        """Verify the OLD behavior (getattr on tuple) returns defaults."""
        intent = ("typed_intent_v1", 42, "strat_r47", "TXFD6", 0, 1)

        # This is what the OLD code did — always returns default
        assert getattr(intent, "strategy_id", "") == ""
        assert getattr(intent, "intent_id", 0) == 0
        assert getattr(intent, "symbol", "") == ""

    def test_new_helper_returns_correct_values_for_same_tuple(self):
        """Verify the NEW behavior extracts correctly."""
        from hft_platform.strategy.runner import _typed_intent_identity

        intent = ("typed_intent_v1", 42, "strat_r47", "TXFD6", 0, 1)
        iid, sid, sym, side = _typed_intent_identity(intent)

        assert iid == 42
        assert sid == "strat_r47"
        assert sym == "TXFD6"
        assert side == 1


# ---------------------------------------------------------------------------
# Fix B: OrderAdapter dispatch-failure rejection feedback (R3-2)
# ---------------------------------------------------------------------------

class TestOrderAdapterRejectionSink:
    """Verify OrderAdapter sends RiskFeedback on dispatch failures."""

    def test_send_dispatch_rejection_enqueues_feedback(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter(
            config_path="config/base/main.yaml",
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )
        sink = asyncio.Queue(maxsize=10)
        adapter.set_rejection_sink(sink)

        intent = MagicMock()
        intent.intent_id = 5
        intent.strategy_id = "strat_a"
        intent.symbol = "TXFD6"
        intent.side = 1

        adapter._send_dispatch_rejection(intent, "dispatch_failed")

        assert sink.qsize() == 1
        fb = sink.get_nowait()
        assert fb.strategy_id == "strat_a"
        assert fb.symbol == "TXFD6"
        assert fb.reason_code == "dispatch_failed"
        assert fb.side == 1

    def test_send_dispatch_rejection_noop_when_no_sink(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter(
            config_path="config/base/main.yaml",
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )
        # No sink set — should not raise
        intent = MagicMock(strategy_id="s", symbol="X", side=None)
        adapter._send_dispatch_rejection(intent, "test")

    def test_send_dispatch_rejection_handles_full_sink(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter(
            config_path="config/base/main.yaml",
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )
        sink = asyncio.Queue(maxsize=1)
        adapter.set_rejection_sink(sink)
        adapter.metrics = MagicMock()

        intent = MagicMock(strategy_id="s", symbol="X", side=None, intent_id=0)

        # Fill sink
        sink.put_nowait("dummy")
        # Should not raise, should increment metric
        adapter._send_dispatch_rejection(intent, "test")

        adapter.metrics.rejection_sink_overflow_total.inc.assert_called_once()

    def test_set_rejection_sink(self):
        from hft_platform.order.adapter import OrderAdapter

        adapter = OrderAdapter(
            config_path="config/base/main.yaml",
            order_queue=asyncio.Queue(),
            broker_client=MagicMock(),
        )
        assert adapter._rejection_sink is None
        sink = asyncio.Queue()
        adapter.set_rejection_sink(sink)
        assert adapter._rejection_sink is sink


# ---------------------------------------------------------------------------
# Fix C: RiskEngine DLQ rejection overflow logging (R3-1)
# ---------------------------------------------------------------------------

class TestRiskDLQRejectionOverflowLogging:
    """Verify _send_dlq_rejection logs on sink overflow."""

    def test_overflow_logs_warning(self):
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine.__new__(RiskEngine)
        engine.metrics = MagicMock()

        # Create a full sink
        sink = asyncio.Queue(maxsize=1)
        sink.put_nowait("dummy")
        engine._rejection_sink = sink

        cmd = MagicMock()
        cmd.cmd_id = 99
        cmd.intent = MagicMock()
        cmd.intent.intent_id = 1
        cmd.intent.strategy_id = "strat_a"
        cmd.intent.symbol = "TXFD6"
        cmd.intent.side = 1

        with patch("hft_platform.risk.engine.logger") as mock_logger:
            engine._send_dlq_rejection(cmd, "dlq_ttl_expired")

        engine.metrics.rejection_sink_overflow_total.inc.assert_called_once()
        mock_logger.warning.assert_called_once()
        call_kwargs = mock_logger.warning.call_args
        assert "dlq_rejection_feedback_lost" in call_kwargs[0]
        assert call_kwargs[1]["strategy_id"] == "strat_a"
        assert call_kwargs[1]["reason"] == "dlq_ttl_expired"

    def test_successful_send_includes_side(self):
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine.__new__(RiskEngine)
        engine.metrics = MagicMock()
        sink = asyncio.Queue(maxsize=10)
        engine._rejection_sink = sink

        cmd = MagicMock()
        cmd.cmd_id = 1
        cmd.intent = MagicMock()
        cmd.intent.intent_id = 5
        cmd.intent.strategy_id = "strat_b"
        cmd.intent.symbol = "TXFD6"
        cmd.intent.side = 0  # BUY

        engine._send_dlq_rejection(cmd, "dlq_storm_cleared")

        fb = sink.get_nowait()
        assert fb.strategy_id == "strat_b"
        assert fb.reason_code == "dlq_storm_cleared"
        assert fb.side == 0

    def test_none_sink_is_noop(self):
        from hft_platform.risk.engine import RiskEngine

        engine = RiskEngine.__new__(RiskEngine)
        engine._rejection_sink = None
        cmd = MagicMock()
        cmd.intent = MagicMock(strategy_id="s", symbol="X")

        # Should not raise
        engine._send_dlq_rejection(cmd, "test")
