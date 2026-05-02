"""Regression: `redis_connection_health` must not exist on MetricsRegistry.

Root cause context: this gauge was declared in `observability/metrics.py` but
never `.set()` anywhere in the codebase. Engine containers do not have the
`redis` Python module installed and do not probe Redis; only the separate
`monitor` container uses Redis. The dead declaration plus the
`RedisConnectionDown` alert caused continuous false-positive alerts in prod.
"""

from __future__ import annotations


def test_metrics_registry_has_no_redis_connection_health():
    """Engine's MetricsRegistry must not declare `redis_connection_health`."""
    from hft_platform.observability.metrics import MetricsRegistry

    registry = MetricsRegistry()
    assert not hasattr(registry, "redis_connection_health"), (
        "redis_connection_health gauge must be removed — no code sets it and "
        "engine does not use Redis. See docs/incidents debug-team R2 analysis."
    )
