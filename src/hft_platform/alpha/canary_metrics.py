"""Canary metrics — data sources for live canary monitoring.

Provides Protocol-based abstraction over ClickHouse, Redis, and hybrid
data sources for evaluating promoted canary alpha performance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import structlog

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

    def fetch(self, alpha_id: str) -> CanaryMetricsSnapshot:
        """Fetch the latest metrics snapshot for an alpha."""
        ...

    def source_name(self) -> str:
        """Return the human-readable source name."""
        ...


class ClickHouseCanarySource:
    """Fetch canary metrics from ClickHouse."""

    def __init__(self, host: str = "localhost", port: int = 8123, database: str = "hft") -> None:
        self._host = host
        self._port = port
        self._database = database

    def source_name(self) -> str:
        return f"clickhouse://{self._host}:{self._port}/{self._database}"

    def fetch(self, alpha_id: str) -> CanaryMetricsSnapshot:
        """Fetch metrics from ClickHouse.

        In production this would run SQL queries. This implementation
        provides a structured interface; subclasses override _query().
        """
        raw = self._query(alpha_id)
        return CanaryMetricsSnapshot(
            alpha_id=alpha_id,
            session_count=int(raw.get("session_count", 0)),
            drift_alerts=int(raw.get("drift_alerts", 0)),
            execution_reject_rate=float(raw.get("execution_reject_rate", 0.0)),
            live_slippage_bps=float(raw.get("live_slippage_bps", 0.0)),
            live_drawdown_contribution=float(raw.get("live_drawdown_contribution", 0.0)),
            source=self.source_name(),
            raw=raw,
        )

    def _query(self, alpha_id: str) -> dict[str, Any]:
        """Execute ClickHouse query. Override in tests or subclasses."""
        return {}


class RedisCanarySource:
    """Fetch canary metrics from Redis live cache."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 6379,
        key_prefix: str = "canary",
        password: str = "",
    ) -> None:
        self._host = host
        self._port = port
        self._key_prefix = key_prefix
        self._password = password

    def source_name(self) -> str:
        return f"redis://{self._host}:{self._port}/{self._key_prefix}"

    def fetch(self, alpha_id: str) -> CanaryMetricsSnapshot:
        """Fetch metrics from Redis."""
        raw = self._get(alpha_id)
        return CanaryMetricsSnapshot(
            alpha_id=alpha_id,
            session_count=int(raw.get("session_count", 0)),
            drift_alerts=int(raw.get("drift_alerts", 0)),
            execution_reject_rate=float(raw.get("execution_reject_rate", 0.0)),
            live_slippage_bps=float(raw.get("live_slippage_bps", 0.0)),
            live_drawdown_contribution=float(raw.get("live_drawdown_contribution", 0.0)),
            source=self.source_name(),
            raw=raw,
        )

    def _get(self, alpha_id: str) -> dict[str, Any]:
        """Get from Redis. Override in tests or subclasses."""
        return {}


class HybridCanarySource:
    """Hybrid source: prefer Redis for recency, fall back to ClickHouse."""

    def __init__(
        self,
        primary: CanaryMetricsSource,
        fallback: CanaryMetricsSource,
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    def source_name(self) -> str:
        return f"hybrid(primary={self._primary.source_name()}, fallback={self._fallback.source_name()})"

    def fetch(self, alpha_id: str) -> CanaryMetricsSnapshot:
        """Try primary source first; fall back on any error."""
        try:
            snapshot = self._primary.fetch(alpha_id)
            logger.debug(
                "canary_metrics.hybrid.primary_ok",
                alpha_id=alpha_id,
                source=self._primary.source_name(),
            )
            return snapshot
        except Exception as exc:
            logger.warning(
                "canary_metrics.hybrid.primary_error",
                alpha_id=alpha_id,
                source=self._primary.source_name(),
                error=str(exc),
            )
        return self._fallback.fetch(alpha_id)


def evaluate_with_source(
    alpha_id: str,
    source: CanaryMetricsSource,
    max_live_slippage_bps: float = 3.0,
    max_live_drawdown_contribution: float = 0.02,
    max_execution_reject_rate: float = 0.01,
) -> dict[str, Any]:
    """Evaluate canary metrics against guardrails using the given source.

    Returns a dict with 'passed', 'checks', and 'snapshot' keys.
    """
    snapshot = source.fetch(alpha_id)

    checks: dict[str, dict[str, Any]] = {
        "live_slippage_bps": {
            "value": snapshot.live_slippage_bps,
            "max": max_live_slippage_bps,
            "pass": snapshot.live_slippage_bps <= max_live_slippage_bps,
        },
        "live_drawdown_contribution": {
            "value": snapshot.live_drawdown_contribution,
            "max": max_live_drawdown_contribution,
            "pass": snapshot.live_drawdown_contribution <= max_live_drawdown_contribution,
        },
        "execution_reject_rate": {
            "value": snapshot.execution_reject_rate,
            "max": max_execution_reject_rate,
            "pass": snapshot.execution_reject_rate <= max_execution_reject_rate,
        },
        "drift_alerts": {
            "value": snapshot.drift_alerts,
            "max": 0,
            "pass": snapshot.drift_alerts == 0,
        },
    }

    passed = all(c["pass"] for c in checks.values())
    return {
        "alpha_id": alpha_id,
        "passed": passed,
        "source": snapshot.source,
        "checks": checks,
        "snapshot": snapshot,
    }
