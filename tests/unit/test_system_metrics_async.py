"""Tests for P1-2 fix: psutil calls in update_system_metrics must not block event loop.

The Async Law forbids blocking IO > 1ms on the main event loop.
psutil.cpu_percent() reads /proc and can take several milliseconds, so
_supervise() must offload it via run_in_executor rather than calling it directly.
"""

from __future__ import annotations

import inspect


def test_supervise_uses_run_in_executor_for_system_metrics():
    """update_system_metrics() must be dispatched via run_in_executor, not called directly."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    # The direct sync call must not be present
    assert "metrics.update_system_metrics()" not in source, (
        "_supervise() must not call update_system_metrics() synchronously on the event loop"
    )

    # The executor-offloaded form must be present
    assert "run_in_executor" in source, (
        "_supervise() should use run_in_executor to offload blocking psutil calls"
    )


def test_supervise_passes_update_system_metrics_to_executor():
    """Confirm the exact executor call targets update_system_metrics."""
    from hft_platform.services.system import HFTSystem

    source = inspect.getsource(HFTSystem._supervise)

    assert "update_system_metrics" in source, (
        "_supervise() must still invoke update_system_metrics (via executor)"
    )
    # Ensure the awaited executor pattern is present
    assert "await" in source, "_supervise() must await the executor future"


def test_update_system_metrics_is_plain_callable():
    """update_system_metrics must remain a plain (non-async) callable for run_in_executor."""
    from hft_platform.observability.metrics import MetricsRegistry

    method = MetricsRegistry.update_system_metrics
    assert callable(method), "update_system_metrics must be callable"
    assert not inspect.iscoroutinefunction(method), (
        "update_system_metrics must stay synchronous — run_in_executor requires a plain callable"
    )
