"""Canary metrics — data sources for live canary monitoring.

Provides Protocol-based abstraction over ClickHouse, Redis, and hybrid
data sources for evaluating promoted canary alpha performance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

if TYPE_CHECKING:
    from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus

logger = structlog.get_logger("alpha.canary_metrics")


@dataclass(frozen=True, slots=True)
class CanaryMetricsSnapshot:
    alpha_id: str
    session_count: int
    drift_alerts: int
    execution_reject_rate: float
    live_slippage_bps: float
    live_drawdown_contribution: float
    source: str
    raw: dict[str, Any]


@runtime_checkable
class CanaryMetricsSource(Protocol):
    """Protocol for canary metrics data sources."""

    def get_live_metrics(self, alpha_id: str) -> dict[str, Any]:
        """Fetch the latest live metrics payload for an alpha."""
        ...


class ClickHouseCanarySource:
    """Canary metrics source backed by ClickHouse.

    Executes an aggregation query over ``hft.alpha_canary_metrics`` and
    returns a metrics dict compatible with ``CanaryMonitor.evaluate()``.
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def get_live_metrics(self, alpha_id: str) -> dict[str, Any]:
        """Query ClickHouse for aggregated live metrics for *alpha_id*.

        Returns an empty dict when no client is configured or when the
        query returns no rows.
        """
        if self._client is None:
            logger.debug("canary_metrics.clickhouse: no client configured", alpha_id=alpha_id)
            return {}

        query = (
            "SELECT "
            "avg(slippage_bps), "
            "max(drawdown_contribution), "
            "avg(execution_error_rate), "
            "count(*) "
            "FROM hft.alpha_canary_metrics "
            "WHERE alpha_id = %(alpha_id)s"
        )
        try:
            rows = self._client.execute(query, {"alpha_id": alpha_id})
        except Exception:
            logger.warning("canary_metrics.clickhouse: query failed", alpha_id=alpha_id, exc_info=True)
            return {}

        if not rows:
            logger.debug("canary_metrics.clickhouse: no rows returned", alpha_id=alpha_id)
            return {}

        row = rows[0]
        if len(row) < 4:
            logger.warning("canary_metrics.clickhouse: unexpected row shape", alpha_id=alpha_id, row=row)
            return {}

        avg_slippage, max_dd, avg_error_rate, session_count = row
        metrics: dict[str, Any] = {
            "slippage_bps": float(avg_slippage) if avg_slippage is not None else 0.0,
            "drawdown_contribution": float(max_dd) if max_dd is not None else 0.0,
            "execution_error_rate": float(avg_error_rate) if avg_error_rate is not None else 0.0,
            "sessions_live": int(session_count) if session_count is not None else 0,
        }
        logger.debug("canary_metrics.clickhouse: metrics fetched", alpha_id=alpha_id, metrics=metrics)
        return metrics


class RedisCanarySource:
    """Canary metrics source backed by Redis.

    Reads per-key values using the naming convention
    ``canary:{alpha_id}:{field}`` and assembles them into a metrics dict.
    """

    _FIELDS = (
        "slippage_bps",
        "drawdown_contribution",
        "execution_error_rate",
        "sessions_live",
        "sharpe_live",
    )

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def get_live_metrics(self, alpha_id: str) -> dict[str, Any]:
        """Read live metrics for *alpha_id* from Redis.

        Missing keys are silently skipped.  Returns an empty dict when no
        client is configured or when all keys are absent.
        """
        if self._client is None:
            logger.debug("canary_metrics.redis: no client configured", alpha_id=alpha_id)
            return {}

        metrics: dict[str, Any] = {}
        for field in self._FIELDS:
            key = f"canary:{alpha_id}:{field}"
            try:
                raw = self._client.get(key)
            except Exception:
                logger.warning("canary_metrics.redis: get failed", key=key, exc_info=True)
                continue

            if raw is None:
                continue

            try:
                value: int | float
                if field == "sessions_live":
                    value = int(raw)
                else:
                    value = float(raw)
                metrics[field] = value
            except (ValueError, TypeError):
                logger.warning("canary_metrics.redis: cannot convert value", key=key, raw=raw)

        logger.debug("canary_metrics.redis: metrics fetched", alpha_id=alpha_id, metrics=metrics)
        return metrics


class HybridCanarySource:
    """Canary metrics source that prefers Redis over ClickHouse.

    Uses *redis_source* first; falls back to *clickhouse_source* when the
    Redis result is empty or reports ``sessions_live == 0``.
    """

    def __init__(
        self,
        redis_source: CanaryMetricsSource,
        clickhouse_source: CanaryMetricsSource,
    ) -> None:
        self._redis = redis_source
        self._clickhouse = clickhouse_source

    def get_live_metrics(self, alpha_id: str) -> dict[str, Any]:
        """Return live metrics, preferring Redis if it has fresh data."""
        redis_metrics = self._redis.get_live_metrics(alpha_id)

        if redis_metrics and int(redis_metrics.get("sessions_live", 0)) > 0:
            logger.debug("canary_metrics.hybrid: using redis", alpha_id=alpha_id)
            return redis_metrics

        logger.debug("canary_metrics.hybrid: falling back to clickhouse", alpha_id=alpha_id)
        return self._clickhouse.get_live_metrics(alpha_id)


def evaluate_with_source(
    monitor: CanaryMonitor,
    alpha_id: str,
    source: CanaryMetricsSource,
) -> CanaryStatus:
    """Fetch live metrics via *source* and evaluate them with *monitor*.

    Convenience helper that combines ``CanaryMetricsSource.get_live_metrics``
    with ``CanaryMonitor.evaluate`` in a single call.
    """
    metrics = source.get_live_metrics(alpha_id)
    logger.debug("evaluate_with_source: calling monitor.evaluate", alpha_id=alpha_id, metrics=metrics)
    return monitor.evaluate(alpha_id, metrics)
