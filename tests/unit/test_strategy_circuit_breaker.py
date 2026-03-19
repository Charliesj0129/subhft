"Tests for WU-09: Per-Strategy Circuit Breaker."
from hft_platform.order.circuit_breaker import StrategyCircuitBreakerManager

class TestDefaults:
    def test_defaults(self):
        mgr = StrategyCircuitBreakerManager()
        assert mgr._default_threshold == 5
        assert mgr._default_timeout_s == 60
    def test_env(self, monkeypatch):
        monkeypatch.setenv("HFT_STRATEGY_CB_THRESHOLD", "3")
        monkeypatch.setenv("HFT_STRATEGY_CB_TIMEOUT_S", "30")
        mgr = StrategyCircuitBreakerManager()
        assert mgr._default_threshold == 3

class TestBehavior:
    def test_creates_new(self):
        mgr = StrategyCircuitBreakerManager()
        b = mgr.get_breaker("a")
        assert b.threshold == 5
    def test_returns_same(self):
        mgr = StrategyCircuitBreakerManager()
        assert mgr.get_breaker("a") is mgr.get_breaker("a")
    def test_success_resets(self):
        mgr = StrategyCircuitBreakerManager(default_threshold=3)
        mgr.record_failure("a")
        mgr.record_success("a")
        assert mgr.get_breaker("a").failure_count == 0
    def test_trips(self):
        mgr = StrategyCircuitBreakerManager(default_threshold=2, default_timeout_s=60)
        mgr.record_failure("a")
        assert mgr.record_failure("a") is True
        assert mgr.is_open("a") is True
    def test_limits(self):
        mgr = StrategyCircuitBreakerManager(
            default_threshold=5,
            strategy_limits={"a": {"cb_threshold": 2, "cb_timeout_s": 10}},
        )
        b = mgr.get_breaker("a")
        assert b.threshold == 2
