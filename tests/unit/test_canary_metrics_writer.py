"""Tests for hft_platform.alpha.canary_metrics_writer module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from hft_platform.alpha.canary_metrics_writer import (
    CanaryMetricsWriter,
    LiveMetrics,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_promo_yaml(
    path: Path,
    alpha_id: str = "ofi_mc",
    enabled: bool = True,
    weight: float = 0.05,
    sharpe_oos: float = 1.4,
) -> Path:
    """Write a minimal promotion YAML for use in tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "alpha_id": alpha_id,
        "enabled": enabled,
        "weight": weight,
        "owner": "test_owner",
        "guardrails": {
            "max_live_slippage_bps": 3.0,
            "max_live_drawdown_contribution": 0.02,
            "max_execution_error_rate": 0.01,
        },
        "rollback": {
            "trigger": {
                "live_slippage_bps_gt": 3.0,
                "live_drawdown_contribution_gt": 0.02,
                "execution_error_rate_gt": 0.01,
            },
        },
        "scorecard_snapshot": {"sharpe_oos": sharpe_oos},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False))
    return path


def _make_ch_client(rows: list) -> MagicMock:
    """Return a mock ClickHouse client that returns *rows* from execute()."""
    client = MagicMock()
    client.execute.return_value = rows
    return client


# ---------------------------------------------------------------------------
# LiveMetrics unit tests
# ---------------------------------------------------------------------------


class TestLiveMetrics:
    def test_to_dict_without_sharpe(self) -> None:
        m = LiveMetrics(
            alpha_id="test",
            slippage_bps=1.2,
            drawdown_contribution=0.01,
            execution_error_rate=0.005,
            sessions_live=7,
        )
        d = m.to_dict()
        assert d["slippage_bps"] == 1.2
        assert d["drawdown_contribution"] == 0.01
        assert d["execution_error_rate"] == 0.005
        assert d["sessions_live"] == 7
        assert "sharpe_live" not in d

    def test_to_dict_with_sharpe(self) -> None:
        m = LiveMetrics(alpha_id="test", sharpe_live=1.8)
        d = m.to_dict()
        assert d["sharpe_live"] == 1.8

    def test_defaults_are_zero(self) -> None:
        m = LiveMetrics(alpha_id="x")
        assert m.slippage_bps == 0.0
        assert m.drawdown_contribution == 0.0
        assert m.execution_error_rate == 0.0
        assert m.sessions_live == 0
        assert m.sharpe_live is None


# ---------------------------------------------------------------------------
# _compute_metrics unit tests
# ---------------------------------------------------------------------------


class TestComputeMetrics:
    def _writer(self) -> CanaryMetricsWriter:
        return CanaryMetricsWriter()

    def test_all_fields_present(self) -> None:
        w = self._writer()
        raw = {
            "slippage_bps": 2.5,
            "drawdown_contribution": 0.015,
            "execution_error_rate": 0.003,
            "sessions_live": 12,
            "sharpe_live": 1.6,
        }
        m = w._compute_metrics("alpha_a", raw)
        assert m.slippage_bps == 2.5
        assert m.drawdown_contribution == 0.015
        assert m.execution_error_rate == 0.003
        assert m.sessions_live == 12
        assert m.sharpe_live == 1.6

    def test_empty_raw_yields_zeros(self) -> None:
        w = self._writer()
        m = w._compute_metrics("alpha_b", {})
        assert m.slippage_bps == 0.0
        assert m.sessions_live == 0
        assert m.sharpe_live is None

    def test_none_sharpe_omitted(self) -> None:
        w = self._writer()
        m = w._compute_metrics("alpha_c", {"sharpe_live": None})
        assert m.sharpe_live is None

    def test_invalid_sharpe_coerced_to_none(self) -> None:
        w = self._writer()
        m = w._compute_metrics("alpha_d", {"sharpe_live": "not_a_float"})
        assert m.sharpe_live is None

    def test_sessions_live_coerced_to_int(self) -> None:
        w = self._writer()
        m = w._compute_metrics("alpha_e", {"sessions_live": 7.9})
        assert m.sessions_live == 7
        assert isinstance(m.sessions_live, int)


# ---------------------------------------------------------------------------
# _fetch_from_clickhouse unit tests
# ---------------------------------------------------------------------------


class TestFetchFromClickhouse:
    def test_no_client_returns_empty(self) -> None:
        w = CanaryMetricsWriter(clickhouse_client=None)
        result = w._fetch_from_clickhouse("any_alpha")
        assert result == {}

    def test_client_query_called_with_alpha_id(self) -> None:
        client = _make_ch_client([(1.0, 0.01, 0.005, 10, 1.5)])
        w = CanaryMetricsWriter(clickhouse_client=client)
        w._fetch_from_clickhouse("ofi_mc")
        client.execute.assert_called_once()
        call_kwargs = client.execute.call_args[0]
        assert call_kwargs[1] == {"alpha_id": "ofi_mc"}

    def test_valid_row_parsed_correctly(self) -> None:
        client = _make_ch_client([(2.0, 0.015, 0.003, 8, 1.7)])
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert result["slippage_bps"] == 2.0
        assert result["drawdown_contribution"] == 0.015
        assert result["execution_error_rate"] == 0.003
        assert result["sessions_live"] == 8
        assert result["sharpe_live"] == 1.7

    def test_no_sharpe_column_in_row(self) -> None:
        client = _make_ch_client([(1.0, 0.01, 0.005, 5)])
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert "sharpe_live" not in result

    def test_null_sharpe_omitted(self) -> None:
        client = _make_ch_client([(1.0, 0.01, 0.005, 5, None)])
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert "sharpe_live" not in result

    def test_empty_rows_returns_empty(self) -> None:
        client = _make_ch_client([])
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert result == {}

    def test_short_row_returns_empty(self) -> None:
        client = _make_ch_client([(1.0, 0.01)])  # fewer than 4 columns
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert result == {}

    def test_query_exception_returns_empty(self) -> None:
        client = MagicMock()
        client.execute.side_effect = RuntimeError("connection refused")
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert result == {}

    def test_null_numeric_values_default_to_zero(self) -> None:
        client = _make_ch_client([(None, None, None, None)])
        w = CanaryMetricsWriter(clickhouse_client=client)
        result = w._fetch_from_clickhouse("ofi_mc")
        assert result["slippage_bps"] == 0.0
        assert result["sessions_live"] == 0


# ---------------------------------------------------------------------------
# _find_promotion_yaml unit tests
# ---------------------------------------------------------------------------


class TestFindPromotionYaml:
    def test_returns_none_when_dir_missing(self, tmp_path: Path) -> None:
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path / "nonexistent"))
        assert w._find_promotion_yaml("any") is None

    def test_returns_none_when_alpha_not_found(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "other_alpha.yaml", alpha_id="other")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path))
        assert w._find_promotion_yaml("missing_alpha") is None

    def test_finds_matching_yaml(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "20260318" / "ofi_mc.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path))
        found = w._find_promotion_yaml("ofi_mc")
        assert found is not None
        assert found.name == "ofi_mc.yaml"

    def test_returns_latest_when_multiple_matches(self, tmp_path: Path) -> None:
        older = _write_promo_yaml(tmp_path / "20260301" / "ofi_mc.yaml", alpha_id="ofi_mc")
        newer = _write_promo_yaml(tmp_path / "20260318" / "ofi_mc.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path))
        found = w._find_promotion_yaml("ofi_mc")
        assert found == newer

    def test_skips_invalid_yaml_files(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text(":::invalid:::")
        _write_promo_yaml(tmp_path / "good.yaml", alpha_id="alpha_x")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path))
        found = w._find_promotion_yaml("alpha_x")
        assert found is not None


# ---------------------------------------------------------------------------
# _write_metrics_to_yaml unit tests
# ---------------------------------------------------------------------------


class TestWriteMetricsToYaml:
    def test_creates_live_metrics_block(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "alpha.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter()
        metrics = LiveMetrics(
            alpha_id="ofi_mc",
            slippage_bps=1.5,
            drawdown_contribution=0.01,
            execution_error_rate=0.002,
            sessions_live=5,
            sharpe_live=1.3,
        )
        w._write_metrics_to_yaml(yaml_path, metrics)

        updated = yaml.safe_load(yaml_path.read_text())
        assert "live_metrics" in updated
        lm = updated["live_metrics"]
        assert lm["slippage_bps"] == 1.5
        assert lm["drawdown_contribution"] == 0.01
        assert lm["execution_error_rate"] == 0.002
        assert lm["sessions_live"] == 5
        assert lm["sharpe_live"] == 1.3

    def test_overwrites_existing_live_metrics_block(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "alpha.yaml", alpha_id="ofi_mc")
        # Write initial metrics
        w = CanaryMetricsWriter()
        first = LiveMetrics(alpha_id="ofi_mc", slippage_bps=0.5, sessions_live=2)
        w._write_metrics_to_yaml(yaml_path, first)
        # Overwrite with updated metrics
        second = LiveMetrics(alpha_id="ofi_mc", slippage_bps=2.0, sessions_live=7)
        w._write_metrics_to_yaml(yaml_path, second)

        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["live_metrics"]["slippage_bps"] == 2.0
        assert updated["live_metrics"]["sessions_live"] == 7

    def test_preserves_other_yaml_fields(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "alpha.yaml", alpha_id="ofi_mc", weight=0.07)
        w = CanaryMetricsWriter()
        w._write_metrics_to_yaml(yaml_path, LiveMetrics(alpha_id="ofi_mc"))
        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["weight"] == 0.07
        assert updated["alpha_id"] == "ofi_mc"

    def test_idempotent_write(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "alpha.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter()
        metrics = LiveMetrics(alpha_id="ofi_mc", slippage_bps=1.1, sessions_live=3)
        w._write_metrics_to_yaml(yaml_path, metrics)
        first_mtime = yaml_path.stat().st_mtime

        # Call again with identical metrics — file content should be the same.
        w._write_metrics_to_yaml(yaml_path, metrics)
        updated = yaml.safe_load(yaml_path.read_text())
        assert updated["live_metrics"]["slippage_bps"] == 1.1


# ---------------------------------------------------------------------------
# CanaryMetricsWriter.update integration tests
# ---------------------------------------------------------------------------


class TestUpdate:
    def test_update_returns_error_when_no_yaml(self, tmp_path: Path) -> None:
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path / "empty"))
        result = w.update("unknown_alpha")
        assert result.updated is False
        assert result.error is not None
        assert "No promotion YAML found" in result.error

    def test_update_with_no_ch_client_writes_zeros(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path), clickhouse_client=None)
        result = w.update("ofi_mc")
        assert result.updated is True
        assert result.error is None
        assert result.metrics is not None
        assert result.metrics.slippage_bps == 0.0
        assert result.metrics.sessions_live == 0

    def test_update_with_ch_client_writes_fetched_metrics(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        client = _make_ch_client([(1.8, 0.012, 0.003, 9, 1.4)])
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path), clickhouse_client=client)
        result = w.update("ofi_mc")
        assert result.updated is True
        assert result.metrics is not None
        assert result.metrics.slippage_bps == 1.8
        assert result.metrics.sessions_live == 9
        assert result.metrics.sharpe_live == pytest.approx(1.4)

    def test_update_persists_metrics_to_yaml(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        client = _make_ch_client([(2.2, 0.01, 0.002, 6, None)])
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path), clickhouse_client=client)
        w.update("ofi_mc")

        on_disk = yaml.safe_load(yaml_path.read_text())
        assert "live_metrics" in on_disk
        assert on_disk["live_metrics"]["slippage_bps"] == 2.2
        assert "sharpe_live" not in on_disk["live_metrics"]

    def test_update_is_idempotent(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        client = _make_ch_client([(1.0, 0.005, 0.001, 4, 1.2)])
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path), clickhouse_client=client)
        r1 = w.update("ofi_mc")
        r2 = w.update("ofi_mc")
        assert r1.updated is True
        assert r2.updated is True
        assert r1.metrics is not None
        assert r2.metrics is not None
        assert r1.metrics.slippage_bps == r2.metrics.slippage_bps

    def test_update_result_contains_yaml_path(self, tmp_path: Path) -> None:
        yaml_path = _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path))
        result = w.update("ofi_mc")
        assert result.yaml_path == str(yaml_path)

    def test_update_error_captured_not_raised(self, tmp_path: Path) -> None:
        _write_promo_yaml(tmp_path / "ofi_mc.yaml", alpha_id="ofi_mc")
        client = MagicMock()
        client.execute.side_effect = RuntimeError("db_down")
        w = CanaryMetricsWriter(promotions_dir=str(tmp_path), clickhouse_client=client)
        # Should not raise; error captured in result.
        result = w.update("ofi_mc")
        # ClickHouse error does not block the write — zeros are used instead.
        assert result.updated is True
        assert result.metrics is not None
        assert result.metrics.slippage_bps == 0.0
