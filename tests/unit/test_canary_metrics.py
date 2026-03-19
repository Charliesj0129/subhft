"""Tests for canary_metrics.py data sources and evaluate_with_source."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from hft_platform.alpha.canary_metrics import (
    CanaryMetricsSnapshot,
    CanaryMetricsSource,
    ClickHouseCanarySource,
    HybridCanarySource,
    RedisCanarySource,
    evaluate_with_source,
)

# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_clickhouse_is_source(self) -> None:
        assert isinstance(ClickHouseCanarySource(), CanaryMetricsSource)

    def test_redis_is_source(self) -> None:
        assert isinstance(RedisCanarySource(), CanaryMetricsSource)

    def test_hybrid_is_source(self) -> None:
        primary = ClickHouseCanarySource()
        fallback = RedisCanarySource()
        hybrid = HybridCanarySource(primary, fallback)
        assert isinstance(hybrid, CanaryMetricsSource)


# ---------------------------------------------------------------------------
# ClickHouseCanarySource
# ---------------------------------------------------------------------------


class TestClickHouseCanarySource:
    def test_source_name(self) -> None:
        src = ClickHouseCanarySource(host="myhost", port=8123, database="hft")
        assert "clickhouse" in src.source_name()
        assert "myhost" in src.source_name()

    def test_fetch_empty_query_returns_defaults(self) -> None:
        src = ClickHouseCanarySource()
        snap = src.fetch("test_alpha")
        assert snap.alpha_id == "test_alpha"
        assert snap.session_count == 0
        assert snap.drift_alerts == 0

    def test_fetch_with_overridden_query(self) -> None:
        class MockCHSource(ClickHouseCanarySource):
            def _query(self, alpha_id: str) -> dict[str, Any]:
                return {
                    "session_count": 10,
                    "drift_alerts": 1,
                    "execution_reject_rate": 0.005,
                    "live_slippage_bps": 1.5,
                    "live_drawdown_contribution": 0.01,
                }

        src = MockCHSource()
        snap = src.fetch("alpha1")
        assert snap.session_count == 10
        assert snap.drift_alerts == 1
        assert snap.execution_reject_rate == pytest.approx(0.005)
        assert snap.live_slippage_bps == pytest.approx(1.5)
        assert snap.source == src.source_name()


# ---------------------------------------------------------------------------
# RedisCanarySource
# ---------------------------------------------------------------------------


class TestRedisCanarySource:
    def test_source_name(self) -> None:
        src = RedisCanarySource(host="redis-host", port=6379, key_prefix="canary")
        assert "redis" in src.source_name()
        assert "redis-host" in src.source_name()

    def test_fetch_empty_returns_defaults(self) -> None:
        src = RedisCanarySource()
        snap = src.fetch("test_alpha")
        assert snap.alpha_id == "test_alpha"
        assert snap.session_count == 0

    def test_fetch_with_overridden_get(self) -> None:
        class MockRedisSource(RedisCanarySource):
            def _get(self, alpha_id: str) -> dict[str, Any]:
                return {
                    "session_count": 5,
                    "drift_alerts": 0,
                    "execution_reject_rate": 0.002,
                    "live_slippage_bps": 0.8,
                    "live_drawdown_contribution": 0.005,
                }

        src = MockRedisSource()
        snap = src.fetch("alpha2")
        assert snap.session_count == 5
        assert snap.live_slippage_bps == pytest.approx(0.8)


# ---------------------------------------------------------------------------
# HybridCanarySource
# ---------------------------------------------------------------------------


class TestHybridCanarySource:
    def _make_snapshot(
        self,
        alpha_id: str = "a",
        source_name: str = "test",
        session_count: int = 5,
    ) -> CanaryMetricsSnapshot:
        return CanaryMetricsSnapshot(
            alpha_id=alpha_id,
            session_count=session_count,
            drift_alerts=0,
            execution_reject_rate=0.0,
            live_slippage_bps=0.0,
            live_drawdown_contribution=0.0,
            source=source_name,
            raw={},
        )

    def test_uses_primary_when_ok(self) -> None:
        primary = MagicMock(spec=CanaryMetricsSource)
        fallback = MagicMock(spec=CanaryMetricsSource)
        primary.source_name.return_value = "primary"
        fallback.source_name.return_value = "fallback"
        primary.fetch.return_value = self._make_snapshot(source_name="primary")

        hybrid = HybridCanarySource(primary, fallback)
        snap = hybrid.fetch("a")
        assert snap.source == "primary"
        fallback.fetch.assert_not_called()

    def test_falls_back_on_primary_error(self) -> None:
        primary = MagicMock(spec=CanaryMetricsSource)
        fallback = MagicMock(spec=CanaryMetricsSource)
        primary.source_name.return_value = "primary"
        fallback.source_name.return_value = "fallback"
        primary.fetch.side_effect = ConnectionError("redis down")
        fallback.fetch.return_value = self._make_snapshot(source_name="fallback")

        hybrid = HybridCanarySource(primary, fallback)
        snap = hybrid.fetch("a")
        assert snap.source == "fallback"

    def test_source_name_includes_both(self) -> None:
        primary = MagicMock(spec=CanaryMetricsSource)
        fallback = MagicMock(spec=CanaryMetricsSource)
        primary.source_name.return_value = "redis://localhost"
        fallback.source_name.return_value = "clickhouse://localhost"
        hybrid = HybridCanarySource(primary, fallback)
        name = hybrid.source_name()
        assert "redis://localhost" in name
        assert "clickhouse://localhost" in name


# ---------------------------------------------------------------------------
# evaluate_with_source
# ---------------------------------------------------------------------------


class TestEvaluateWithSource:
    def _make_source(self, **snap_fields: Any) -> CanaryMetricsSource:
        class StubSource:
            def source_name(self) -> str:
                return "stub"

            def fetch(self, alpha_id: str) -> CanaryMetricsSnapshot:
                defaults = {
                    "alpha_id": alpha_id,
                    "session_count": 10,
                    "drift_alerts": 0,
                    "execution_reject_rate": 0.001,
                    "live_slippage_bps": 1.0,
                    "live_drawdown_contribution": 0.005,
                    "source": "stub",
                    "raw": {},
                }
                defaults.update(snap_fields)
                return CanaryMetricsSnapshot(**defaults)

        return StubSource()  # type: ignore[return-value]

    def test_all_checks_pass(self) -> None:
        src = self._make_source()
        result = evaluate_with_source("alpha_a", src)
        assert result["passed"] is True
        assert all(c["pass"] for c in result["checks"].values())

    def test_high_slippage_fails(self) -> None:
        src = self._make_source(live_slippage_bps=10.0)
        result = evaluate_with_source("alpha_a", src, max_live_slippage_bps=3.0)
        assert result["passed"] is False
        assert result["checks"]["live_slippage_bps"]["pass"] is False

    def test_drift_alerts_fail(self) -> None:
        src = self._make_source(drift_alerts=2)
        result = evaluate_with_source("alpha_a", src)
        assert result["passed"] is False
        assert result["checks"]["drift_alerts"]["pass"] is False

    def test_reject_rate_fail(self) -> None:
        src = self._make_source(execution_reject_rate=0.05)
        result = evaluate_with_source("alpha_a", src, max_execution_reject_rate=0.01)
        assert result["passed"] is False
        assert result["checks"]["execution_reject_rate"]["pass"] is False

    def test_drawdown_fail(self) -> None:
        src = self._make_source(live_drawdown_contribution=0.1)
        result = evaluate_with_source("alpha_a", src, max_live_drawdown_contribution=0.02)
        assert result["passed"] is False
        assert result["checks"]["live_drawdown_contribution"]["pass"] is False

    def test_result_contains_snapshot(self) -> None:
        src = self._make_source()
        result = evaluate_with_source("alpha_a", src)
        assert isinstance(result["snapshot"], CanaryMetricsSnapshot)
        assert result["alpha_id"] == "alpha_a"
