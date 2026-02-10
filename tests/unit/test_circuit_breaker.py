from hft_platform.core import timebase
from hft_platform.order.circuit_breaker import CircuitBreaker


def test_circuit_breaker_trip_and_reset(monkeypatch):
    monkeypatch.setattr(timebase, "now_s", lambda: 100.0)
    breaker = CircuitBreaker(threshold=2, timeout_s=10)

    assert breaker.failure_count == 0
    assert breaker.open_until == 0.0
    assert breaker.is_open() is False

    assert breaker.record_failure() is False
    assert breaker.failure_count == 1
    assert breaker.open_until == 0.0

    assert breaker.record_failure() is True
    assert breaker.failure_count == 2
    assert breaker.open_until == 110.0
    assert breaker.is_open() is True

    monkeypatch.setattr(timebase, "now_s", lambda: 111.0)
    assert breaker.is_open() is False

    breaker.record_success()
    assert breaker.failure_count == 0


def test_circuit_breaker_setters():
    breaker = CircuitBreaker(threshold=1, timeout_s=1)
    breaker.failure_count = 7
    breaker.open_until = 123.0
    assert breaker.failure_count == 7
    assert breaker.open_until == 123.0
