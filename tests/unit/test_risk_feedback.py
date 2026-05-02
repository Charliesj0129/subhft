"""Tests for risk rejection feedback channel."""


def test_on_risk_feedback_default_noop():
    from hft_platform.contracts.strategy import RiskFeedback
    from hft_platform.strategy.base import BaseStrategy

    class DummyStrategy(BaseStrategy):
        def handle_event(self, event):
            pass

    s = DummyStrategy.__new__(DummyStrategy)
    fb = RiskFeedback(intent_id=1, strategy_id="s", symbol="X", reason_code="R", timestamp_ns=0)
    result = s.on_risk_feedback(fb)  # should not raise

    # Default implementation is a no-op — returns None with no side effects
    assert result is None


def test_on_risk_feedback_override():
    from hft_platform.contracts.strategy import RiskFeedback
    from hft_platform.strategy.base import BaseStrategy

    captured = []

    class TestStrategy(BaseStrategy):
        def handle_event(self, event):
            pass

        def on_risk_feedback(self, feedback):
            captured.append(feedback)

    s = TestStrategy.__new__(TestStrategy)
    fb = RiskFeedback(intent_id=1, strategy_id="s", symbol="X", reason_code="GREEKS_DELTA_LIMIT", timestamp_ns=0)
    s.on_risk_feedback(fb)
    assert len(captured) == 1
    assert captured[0].reason_code == "GREEKS_DELTA_LIMIT"


def test_publish_state_drops_silently_when_no_sink():
    from hft_platform.strategy.base import StrategyContext

    ctx = StrategyContext.__new__(StrategyContext)
    ctx._publish_sink = None
    result = ctx.publish_state("channel", {"key": "value"})

    # No sink configured — call is silently dropped, returns None
    assert result is None


def test_publish_state_calls_sink():
    from hft_platform.strategy.base import StrategyContext

    calls = []

    def mock_sink(channel, payload):
        calls.append((channel, payload))

    ctx = StrategyContext.__new__(StrategyContext)
    ctx._publish_sink = mock_sink
    ctx.publish_state("monitor:portfolio:greeks", {"net_delta": 1.0})
    assert len(calls) == 1
    assert calls[0][0] == "monitor:portfolio:greeks"


def test_publish_state_swallows_exception():
    from hft_platform.strategy.base import StrategyContext

    def failing_sink(channel, payload):
        raise RuntimeError("Queue full")

    ctx = StrategyContext.__new__(StrategyContext)
    ctx._publish_sink = failing_sink
    result = ctx.publish_state("ch", {})  # should not raise

    # Exception from sink is swallowed — publish_state returns None
    assert result is None
