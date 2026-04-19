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
    # Replace _metrics after construction so tests can inspect/control it
    if metrics is not None:
        mgr._metrics = metrics
    return mgr


# ─── record_success — metrics exception is swallowed (lines 130-131) ─────────


def test_record_success_does_not_raise_when_metrics_explode():
    """record_success must not raise even if metrics.circuit_breaker_state raises."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state.labels.side_effect = RuntimeError("metrics broken")
    mgr = _make_mgr(metrics=broken_metrics)

    # Must not raise
    mgr.record_success("strat1")

    # Breaker still exists and is not tripped
    assert not mgr.get_breaker("strat1").is_open()


def test_record_success_swallows_attribute_error():
    """record_success handles AttributeError from broken metrics gracefully."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state = None  # causes AttributeError on .labels()
    mgr = _make_mgr(metrics=broken_metrics)

    mgr.record_success("strat2")  # must not raise
    assert True  # reached without error


# ─── record_failure — tripped path + metrics exception (lines 138-140) ───────


def test_record_failure_tripped_with_broken_metrics_does_not_raise():
    """record_failure must not raise when metrics explode on the trip path."""
    broken_metrics = MagicMock()
    broken_metrics.circuit_breaker_state.labels.side_effect = Exception("Prometheus dead")
    mgr = _make_mgr(metrics=broken_metrics)

    # Trip the breaker by exceeding threshold (3 failures needed)
    tripped = False
    for _ in range(4):
        tripped = mgr.record_failure("strat_trip")  # last call triggers tripped=True

    # Should have tripped but never raised
    assert tripped is True


def test_record_failure_not_tripped_does_not_call_metrics_set():
    """record_failure with tripped=False must not call .set(1) on metrics."""
    metrics = MagicMock()
    mgr = _make_mgr(metrics=metrics)

    # Single failure — not tripped yet (threshold=3)
    mgr.record_failure("strat_ok")

    # circuit_breaker_state.labels(...).set(1) should NOT be called
    metrics.circuit_breaker_state.labels.return_value.set.assert_not_called()


def test_record_failure_sets_metric_1_on_trip():
    """record_failure calls .set(1) on circuit_breaker_state when breaker trips."""
    metrics = MagicMock()
    label_mock = MagicMock()
    metrics.circuit_breaker_state.labels.return_value = label_mock
    mgr = _make_mgr(metrics=metrics)

    # Trip the breaker
    for _ in range(4):
        mgr.record_failure("strat_trip2")

    # set(1) must have been called at least once
    label_mock.set.assert_called_with(1)


# ─── _evict_idle — del self._breakers[sid] (line 147) ────────────────────────


def test_evict_idle_removes_healthy_zero_failure_breakers():
    """_evict_idle removes breakers that are healthy and have zero failures."""
    mgr = _make_mgr()

    # Touch two breakers so they exist in _breakers
    mgr.get_breaker("idle_one")
    mgr.get_breaker("idle_two")

    assert "idle_one" in mgr._breakers
    assert "idle_two" in mgr._breakers

    # Both have zero failures and are not open -> should be evicted
    mgr._evict_idle()

    assert "idle_one" not in mgr._breakers
    assert "idle_two" not in mgr._breakers


def test_evict_idle_preserves_breakers_with_failures():
    """_evict_idle keeps breakers that have recorded failures (even if not tripped)."""
    mgr = _make_mgr()

    mgr.record_failure("active")  # 1 failure, not open

    mgr._evict_idle()

    # active breaker should still exist (has failure_count > 0)
    assert "active" in mgr._breakers


def test_evict_idle_preserves_open_breakers():
    """_evict_idle keeps open circuit breakers regardless of failure count."""
    mgr = _make_mgr()

    # Trip the breaker
    for _ in range(4):
        mgr.record_failure("tripped")

    assert mgr.get_breaker("tripped").is_open()

    mgr._evict_idle()

    # Tripped breaker must NOT be evicted
    assert "tripped" in mgr._breakers


def test_evict_idle_on_empty_breakers_is_noop():
    """_evict_idle with no registered breakers does nothing."""
    mgr = _make_mgr()
    assert len(mgr._breakers) == 0
    mgr._evict_idle()  # must not raise
    assert len(mgr._breakers) == 0
