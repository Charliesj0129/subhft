"""Coverage tests for order/circuit_breaker.py — targets 5 missing lines.

Missing lines:
  130-131: record_success — metrics exception swallowed
  139-140: record_failure — metrics exception swallowed (tripped=True path)
  147:     _evict_idle — del self._breakers[sid]
"""

from __future__ import annotations

from unittest.mock import MagicMock

from hft_platform.order.circuit_breaker import StrategyCircuitBreakerManager


def _make_mgr(metrics=None) -> StrategyCircuitBreakerManager:
    from unittest.mock import patch as _patch

    with _patch("hft_platform.order.circuit_breaker.MetricsRegistry") as mm:
        if metrics is not None:
            mm.get.return_value = metrics
        else:
            mm.get.return_value = MagicMock()
        mgr = StrategyCircuitBreakerManager(
            default_threshold=3,
            default_timeout_s=60,
        )
    if metrics is not None:
        mgr._metrics = metrics
    return mgr


def test_record_success_does_not_raise_when_metrics_explode():
    """record_success must not raise even if metrics.circuit_breaker_state raises."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state.labels.side_effect = RuntimeError("metrics broken")
    mgr = _make_mgr(metrics=broken_metrics)

    mgr.record_success("strat1")

    assert not mgr.get_breaker("strat1").is_open()


def test_record_success_swallows_attribute_error():
    """record_success handles AttributeError from broken metrics gracefully."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state = None
    mgr = _make_mgr(metrics=broken_metrics)

    mgr.record_success("strat2")
    assert True


def test_record_failure_tripped_with_broken_metrics_does_not_raise():
    """record_failure must not raise when metrics explode on the trip path."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state.labels.side_effect = Exception("Prometheus dead")
    mgr = _make_mgr(metrics=broken_metrics)

    tripped = False
    for _ in range(4):
        tripped = mgr.record_failure("strat_trip")

    assert tripped is True


def test_record_failure_not_tripped_does_not_call_metrics_set():
    """record_failure with tripped=False must not call .set(1) on metrics."""
    metrics = MagicMock()
    mgr = _make_mgr(metrics=metrics)

    mgr.record_failure("strat_ok")

    metrics.circuit_breaker_state.labels.return_value.set.assert_not_called()


def test_record_failure_sets_metric_1_on_trip():
    """record_failure calls .set(1) on circuit_breaker_state when breaker trips."""
    metrics = MagicMock()
    label_mock = MagicMock()
    metrics.circuit_breaker_state.labels.return_value = label_mock
    mgr = _make_mgr(metrics=metrics)

    for _ in range(4):
        mgr.record_failure("strat_trip2")

    label_mock.set.assert_called_with(1)


def test_evict_idle_removes_healthy_zero_failure_breakers():
    """_evict_idle removes breakers that are healthy and have zero failures."""
    mgr = _make_mgr()

    mgr.get_breaker("idle_one")
    mgr.get_breaker("idle_two")

    assert "idle_one" in mgr._breakers
    assert "idle_two" in mgr._breakers

    mgr._evict_idle()

    assert "idle_one" not in mgr._breakers
    assert "idle_two" not in mgr._breakers


def test_evict_idle_preserves_breakers_with_failures():
    """_evict_idle keeps breakers that have recorded failures (even if not tripped)."""
    mgr = _make_mgr()

    mgr.record_failure("active")

    mgr._evict_idle()

    assert "active" in mgr._breakers


def test_evict_idle_preserves_open_breakers():
    """_evict_idle keeps open circuit breakers regardless of failure count."""
    mgr = _make_mgr()

    for _ in range(4):
        mgr.record_failure("tripped")

    assert mgr.get_breaker("tripped").is_open()

    mgr._evict_idle()

    assert "tripped" in mgr._breakers


def test_evict_idle_on_empty_breakers_is_noop():
    """_evict_idle with no registered breakers does nothing."""
    mgr = _make_mgr()
    assert len(mgr._breakers) == 0
    mgr._evict_idle()
    assert len(mgr._breakers) == 0
