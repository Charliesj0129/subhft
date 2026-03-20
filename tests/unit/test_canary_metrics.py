"""Tests for hft_platform.alpha.canary_metrics module."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from hft_platform.alpha.canary import CanaryMonitor, CanaryStatus
from hft_platform.alpha.canary_metrics import (
    CanaryMetricsSource,
    ClickHouseCanarySource,
    HybridCanarySource,
    RedisCanarySource,
    evaluate_with_source,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_canary_yaml(
    path: Path,
    alpha_id: str = "test_alpha",
    weight: float = 0.02,
    enabled: bool = True,
    max_slippage: float = 3.0,
    max_dd_contrib: float = 0.02,
    max_error_rate: float = 0.01,
    sharpe_oos: float = 1.5,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test",
        "guardrails": {
            "max_live_slippage_bps": max_slippage,
            "max_live_drawdown_contribution": max_dd_contrib,
            "max_execution_error_rate": max_error_rate,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": max_slippage,
                "live_drawdown_contribution_gt": max_dd_contrib,
                "execution_error_rate_gt": max_error_rate,
            },
            "action": {"set_weight_to_zero": True, "open_incident": True},
        },
        "scorecard_snapshot": {"sharpe_oos": sharpe_oos},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def _mock_clickhouse_client(rows: list[tuple[Any, ...]]) -> MagicMock:
    client = MagicMock()
    client.execute.return_value = rows
    return client


def _mock_redis_client(data: dict[str, str]) -> MagicMock:
    client = MagicMock()
    client.get.side_effect = lambda key: data.get(key)
    return client


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
    def test_none_client_returns_empty(self):
        source = ClickHouseCanarySource(client=None)
        result = source.get_live_metrics("alpha_x")
        assert result == {}

    def test_returns_correct_metrics(self):
        client = _mock_clickhouse_client([(1.5, 0.01, 0.002, 7)])
        source = ClickHouseCanarySource(client=client)

        result = source.get_live_metrics("alpha_x")

        assert result["slippage_bps"] == pytest.approx(1.5)
        assert result["drawdown_contribution"] == pytest.approx(0.01)
        assert result["execution_error_rate"] == pytest.approx(0.002)
        assert result["sessions_live"] == 7

    def test_empty_rows_returns_empty(self):
        client = _mock_clickhouse_client([])
        source = ClickHouseCanarySource(client=client)
        assert source.get_live_metrics("alpha_x") == {}

    def test_none_row_values_default_to_zero(self):
        client = _mock_clickhouse_client([(None, None, None, None)])
        source = ClickHouseCanarySource(client=client)

        result = source.get_live_metrics("alpha_x")

        assert result["slippage_bps"] == pytest.approx(0.0)
        assert result["drawdown_contribution"] == pytest.approx(0.0)
        assert result["execution_error_rate"] == pytest.approx(0.0)
        assert result["sessions_live"] == 0

    def test_query_exception_returns_empty(self):
        client = MagicMock()
        client.execute.side_effect = RuntimeError("connection refused")
        source = ClickHouseCanarySource(client=client)

        result = source.get_live_metrics("alpha_x")
        assert result == {}

    def test_query_uses_alpha_id_param(self):
        client = _mock_clickhouse_client([(2.0, 0.02, 0.005, 3)])
        source = ClickHouseCanarySource(client=client)

        source.get_live_metrics("my_alpha")

        call_args = client.execute.call_args
        assert call_args[0][1]["alpha_id"] == "my_alpha"

    def test_row_too_short_returns_empty(self):
        client = _mock_clickhouse_client([(1.0, 0.01)])  # only 2 columns
        source = ClickHouseCanarySource(client=client)

        result = source.get_live_metrics("alpha_x")
        assert result == {}


# ---------------------------------------------------------------------------
# RedisCanarySource
# ---------------------------------------------------------------------------


class TestRedisCanarySource:
    def test_none_client_returns_empty(self):
        source = RedisCanarySource(client=None)
        result = source.get_live_metrics("alpha_x")
        assert result == {}

    def test_returns_correct_metrics(self):
        data = {
            "canary:alpha_x:slippage_bps": "1.2",
            "canary:alpha_x:drawdown_contribution": "0.015",
            "canary:alpha_x:execution_error_rate": "0.003",
            "canary:alpha_x:sessions_live": "5",
            "canary:alpha_x:sharpe_live": "1.8",
        }
        source = RedisCanarySource(client=_mock_redis_client(data))

        result = source.get_live_metrics("alpha_x")

        assert result["slippage_bps"] == pytest.approx(1.2)
        assert result["drawdown_contribution"] == pytest.approx(0.015)
        assert result["execution_error_rate"] == pytest.approx(0.003)
        assert result["sessions_live"] == 5
        assert result["sharpe_live"] == pytest.approx(1.8)

    def test_missing_keys_are_absent_from_result(self):
        # Only slippage_bps present
        data = {"canary:alpha_x:slippage_bps": "2.0"}
        source = RedisCanarySource(client=_mock_redis_client(data))

        result = source.get_live_metrics("alpha_x")

        assert "slippage_bps" in result
        assert "sessions_live" not in result
        assert "sharpe_live" not in result

    def test_all_keys_missing_returns_empty(self):
        source = RedisCanarySource(client=_mock_redis_client({}))
        assert source.get_live_metrics("alpha_x") == {}

    def test_sessions_live_is_int(self):
        data = {"canary:alpha_x:sessions_live": "42"}
        source = RedisCanarySource(client=_mock_redis_client(data))
        result = source.get_live_metrics("alpha_x")
        assert isinstance(result["sessions_live"], int)
        assert result["sessions_live"] == 42

    def test_invalid_value_skipped(self):
        data = {
            "canary:alpha_x:slippage_bps": "not_a_number",
            "canary:alpha_x:sessions_live": "10",
        }
        source = RedisCanarySource(client=_mock_redis_client(data))
        result = source.get_live_metrics("alpha_x")

        # slippage_bps should be skipped; sessions_live should still be present
        assert "slippage_bps" not in result
        assert result["sessions_live"] == 10

    def test_redis_get_exception_skips_key(self):
        client = MagicMock()

        # Raise for slippage_bps, return normally for sessions_live
        def side_effect(key: str) -> str | None:
            if "slippage_bps" in key:
                raise ConnectionError("Redis unavailable")
            if "sessions_live" in key:
                return "3"
            return None

        client.get.side_effect = side_effect
        source = RedisCanarySource(client=client)
        result = source.get_live_metrics("alpha_x")

        assert "slippage_bps" not in result
        assert result["sessions_live"] == 3

    def test_uses_alpha_id_in_key(self):
        client = _mock_redis_client({"canary:my_alpha:sessions_live": "1"})
        source = RedisCanarySource(client=client)
        result = source.get_live_metrics("my_alpha")
        assert result.get("sessions_live") == 1


# ---------------------------------------------------------------------------
# HybridCanarySource
# ---------------------------------------------------------------------------


class TestHybridCanarySource:
    def test_uses_redis_when_has_data(self):
        redis_data = {
            "canary:alpha_x:slippage_bps": "0.5",
            "canary:alpha_x:sessions_live": "3",
        }
        redis_source = RedisCanarySource(client=_mock_redis_client(redis_data))
        ch_source = ClickHouseCanarySource(client=_mock_clickhouse_client([(9.9, 0.99, 0.99, 99)]))

        hybrid = HybridCanarySource(redis_source, ch_source)
        result = hybrid.get_live_metrics("alpha_x")

        assert result["slippage_bps"] == pytest.approx(0.5)
        assert result["sessions_live"] == 3
        # ClickHouse not queried
        ch_source._client.execute.assert_not_called()

    def test_falls_back_to_clickhouse_when_redis_empty(self):
        redis_source = RedisCanarySource(client=None)
        ch_source = ClickHouseCanarySource(client=_mock_clickhouse_client([(1.0, 0.01, 0.001, 5)]))

        hybrid = HybridCanarySource(redis_source, ch_source)
        result = hybrid.get_live_metrics("alpha_x")

        assert result["sessions_live"] == 5
        assert result["slippage_bps"] == pytest.approx(1.0)

    def test_falls_back_to_clickhouse_when_redis_sessions_zero(self):
        redis_data = {
            "canary:alpha_x:slippage_bps": "1.0",
            "canary:alpha_x:sessions_live": "0",
        }
        redis_source = RedisCanarySource(client=_mock_redis_client(redis_data))
        ch_source = ClickHouseCanarySource(client=_mock_clickhouse_client([(2.0, 0.02, 0.002, 8)]))

        hybrid = HybridCanarySource(redis_source, ch_source)
        result = hybrid.get_live_metrics("alpha_x")

        # sessions_live == 0 triggers fallback
        assert result["sessions_live"] == 8
        assert result["slippage_bps"] == pytest.approx(2.0)

    def test_falls_back_when_redis_has_no_sessions_key(self):
        # sessions_live absent → treated as 0 → fallback
        redis_data = {"canary:alpha_x:slippage_bps": "1.0"}
        redis_source = RedisCanarySource(client=_mock_redis_client(redis_data))
        ch_source = ClickHouseCanarySource(client=_mock_clickhouse_client([(3.0, 0.03, 0.003, 4)]))

        hybrid = HybridCanarySource(redis_source, ch_source)
        result = hybrid.get_live_metrics("alpha_x")

        assert result["sessions_live"] == 4

    def test_both_sources_empty_returns_empty(self):
        hybrid = HybridCanarySource(RedisCanarySource(client=None), ClickHouseCanarySource(client=None))
        assert hybrid.get_live_metrics("alpha_x") == {}


# ---------------------------------------------------------------------------
# evaluate_with_source
# ---------------------------------------------------------------------------


class TestEvaluateWithSource:
    def _make_monitor(self, tmp_path: Path, alpha_id: str = "test_alpha") -> CanaryMonitor:
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / f"{alpha_id}.yaml", alpha_id=alpha_id)
        return CanaryMonitor(promotions_dir=str(promo_dir))

    def test_returns_canary_status(self, tmp_path: Path):
        monitor = self._make_monitor(tmp_path)

        # Source with healthy metrics
        source = MagicMock(spec=CanaryMetricsSource)
        source.get_live_metrics.return_value = {
            "slippage_bps": 1.0,
            "drawdown_contribution": 0.005,
            "execution_error_rate": 0.001,
            "sessions_live": 3,
        }

        result = evaluate_with_source(monitor, "test_alpha", source)

        assert isinstance(result, CanaryStatus)
        assert result.alpha_id == "test_alpha"
        assert result.state == "canary"
        source.get_live_metrics.assert_called_once_with("test_alpha")

    def test_rollback_when_source_returns_bad_metrics(self, tmp_path: Path):
        monitor = self._make_monitor(tmp_path)

        source = MagicMock(spec=CanaryMetricsSource)
        source.get_live_metrics.return_value = {
            "slippage_bps": 10.0,  # above max_slippage=3.0
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 5,
        }

        result = evaluate_with_source(monitor, "test_alpha", source)

        assert result.state == "rolled_back"
        assert "slippage_bps" in result.reason

    def test_not_found_when_no_canary_config(self, tmp_path: Path):
        monitor = CanaryMonitor(promotions_dir=str(tmp_path / "empty"))

        source = MagicMock(spec=CanaryMetricsSource)
        source.get_live_metrics.return_value = {"sessions_live": 1}

        result = evaluate_with_source(monitor, "unknown_alpha", source)

        assert result.state == "not_found"

    def test_empty_metrics_from_source_uses_defaults(self, tmp_path: Path):
        monitor = self._make_monitor(tmp_path)

        source = MagicMock(spec=CanaryMetricsSource)
        source.get_live_metrics.return_value = {}

        result = evaluate_with_source(monitor, "test_alpha", source)

        # All metrics default to zero → all checks pass → hold
        assert result.state == "canary"

    def test_escalation_via_source(self, tmp_path: Path):
        promo_dir = tmp_path / "promos"
        _write_canary_yaml(promo_dir / "test_alpha.yaml", alpha_id="test_alpha", weight=0.02, sharpe_oos=1.5)
        monitor = CanaryMonitor(promotions_dir=str(promo_dir))
        monitor.escalation_sessions = 5

        source = MagicMock(spec=CanaryMetricsSource)
        source.get_live_metrics.return_value = {
            "slippage_bps": 0.5,
            "drawdown_contribution": 0.001,
            "execution_error_rate": 0.0,
            "sessions_live": 10,
            "sharpe_live": 1.5,  # >= 1.5 * 0.8 = 1.2
        }

        result = evaluate_with_source(monitor, "test_alpha", source)

        assert result.state == "escalated"

    def test_clickhouse_source_end_to_end(self, tmp_path: Path):
        monitor = self._make_monitor(tmp_path)
        client = _mock_clickhouse_client([(1.0, 0.005, 0.001, 4)])
        source = ClickHouseCanarySource(client=client)

        result = evaluate_with_source(monitor, "test_alpha", source)

        assert isinstance(result, CanaryStatus)
        assert result.state == "canary"

    def test_redis_source_end_to_end(self, tmp_path: Path):
        monitor = self._make_monitor(tmp_path)
        data = {
            "canary:test_alpha:slippage_bps": "1.0",
            "canary:test_alpha:drawdown_contribution": "0.005",
            "canary:test_alpha:execution_error_rate": "0.001",
            "canary:test_alpha:sessions_live": "2",
        }
        source = RedisCanarySource(client=_mock_redis_client(data))

        result = evaluate_with_source(monitor, "test_alpha", source)

        assert isinstance(result, CanaryStatus)
        assert result.state == "canary"
