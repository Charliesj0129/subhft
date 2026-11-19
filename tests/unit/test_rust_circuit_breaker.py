"""Parity tests: RustCircuitBreaker vs Python circuit breaker FSM.

Validates the 3-state FSM (normal → degraded → halted → cooldown → degraded → normal)
produces identical transitions as the Python implementation in StrategyRunner.
"""

import pytest

try:
    try:
        from hft_platform import rust_core

        RustCircuitBreaker = rust_core.RustCircuitBreaker
    except Exception:
        import rust_core

        RustCircuitBreaker = rust_core.RustCircuitBreaker
except Exception:
    RustCircuitBreaker = None


@pytest.fixture
def cb():
    if RustCircuitBreaker is None:
        pytest.skip("Rust extension not available")
    return RustCircuitBreaker(10, 5, 60_000_000_000)


@pytest.mark.skipif(RustCircuitBreaker is None, reason="Rust extension not available")
class TestRustCircuitBreaker:
    def test_initial_state_is_normal(self, cb):
        assert cb.get_state("strat1") == RustCircuitBreaker.NORMAL

    def test_normal_to_degraded(self, cb):
        for i in range(4):
            new_state, should_disable = cb.record_failure("s1", 1000 + i)
            assert should_disable is False
            assert new_state == RustCircuitBreaker.NORMAL

        new_state, should_disable = cb.record_failure("s1", 1005)
        assert new_state == RustCircuitBreaker.DEGRADED
        assert should_disable is False

    def test_degraded_to_halted(self, cb):
        for i in range(9):
            cb.record_failure("s1", 1000 + i)
        new_state, should_disable = cb.record_failure("s1", 1010)
        assert new_state == RustCircuitBreaker.HALTED
        assert should_disable is True

    def test_halted_cooldown_not_elapsed(self, cb):
        for i in range(10):
            cb.record_failure("s1", 1000 + i)
        should_reenable, state = cb.check_cooldown("s1", 1010 + 30_000_000_000)
        assert should_reenable is False
        assert state == RustCircuitBreaker.HALTED

    def test_halted_cooldown_elapsed(self, cb):
        for i in range(10):
            cb.record_failure("s1", 1000 + i)
        should_reenable, state = cb.check_cooldown("s1", 1010 + 61_000_000_000)
        assert should_reenable is True
        assert state == RustCircuitBreaker.DEGRADED

    def test_degraded_recovery_via_successes(self, cb):
        for i in range(5):
            cb.record_failure("s1", 1000 + i)
        assert cb.get_state("s1") == RustCircuitBreaker.DEGRADED

        for i in range(4):
            new_state, recovered = cb.record_success("s1")
            assert recovered is False
            assert new_state == RustCircuitBreaker.DEGRADED

        new_state, recovered = cb.record_success("s1")
        assert recovered is True
        assert new_state == RustCircuitBreaker.NORMAL

    def test_failure_resets_success_count(self, cb):
        for i in range(5):
            cb.record_failure("s1", 1000 + i)
        for _ in range(3):
            cb.record_success("s1")
        cb.record_failure("s1", 2000)
        for _ in range(4):
            _state, recovered = cb.record_success("s1")
            assert recovered is False
        _state, recovered = cb.record_success("s1")
        assert recovered is True

    def test_success_in_normal_is_noop(self, cb):
        new_state, recovered = cb.record_success("s1")
        assert new_state == RustCircuitBreaker.NORMAL
        assert recovered is False

    def test_reset_clears_state(self, cb):
        for i in range(10):
            cb.record_failure("s1", 1000 + i)
        assert cb.get_state("s1") == RustCircuitBreaker.HALTED
        cb.reset("s1")
        assert cb.get_state("s1") == RustCircuitBreaker.NORMAL
        assert cb.get_failure_count("s1") == 0

    def test_multiple_strategies_independent(self, cb):
        for i in range(5):
            cb.record_failure("s1", 1000 + i)
        assert cb.get_state("s1") == RustCircuitBreaker.DEGRADED
        assert cb.get_state("s2") == RustCircuitBreaker.NORMAL

        for i in range(10):
            cb.record_failure("s2", 2000 + i)
        assert cb.get_state("s2") == RustCircuitBreaker.HALTED
        assert cb.get_state("s1") == RustCircuitBreaker.DEGRADED

    def test_class_constants(self):
        assert RustCircuitBreaker.NORMAL == 0
        assert RustCircuitBreaker.DEGRADED == 1
        assert RustCircuitBreaker.HALTED == 2

    def test_unknown_strategy_cooldown(self, cb):
        should_reenable, state = cb.check_cooldown("unknown", 999)
        assert should_reenable is False
        assert state == RustCircuitBreaker.NORMAL


@pytest.mark.skipif(RustCircuitBreaker is None, reason="Rust extension not available")
class TestRustCircuitBreakerParity:
    """Verify Rust circuit breaker matches Python behavior exactly."""

    def test_full_lifecycle_parity(self, cb):
        """Complete lifecycle: normal → degraded → halted → cooldown → degraded → normal."""
        sid = "lifecycle"
        now = 1_000_000_000_000

        for i in range(5):
            state, disable = cb.record_failure(sid, now + i)
        assert state == RustCircuitBreaker.DEGRADED
        assert disable is False

        for i in range(5):
            state, disable = cb.record_failure(sid, now + 10 + i)
        assert state == RustCircuitBreaker.HALTED
        assert disable is True

        reenable, state = cb.check_cooldown(sid, now + 30_000_000_000)
        assert reenable is False

        reenable, state = cb.check_cooldown(sid, now + 61_000_000_000)
        assert reenable is True
        assert state == RustCircuitBreaker.DEGRADED

        for _ in range(5):
            state, recovered = cb.record_success(sid)
        assert recovered is True
        assert state == RustCircuitBreaker.NORMAL
