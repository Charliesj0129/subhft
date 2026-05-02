"""Tests for recorder_schema_init_failed metric.

Covers:
- MetricsRegistry exposes recorder_schema_init_failed attribute
- _init_schema sets metric to 1 on failure
- _init_schema sets metric to 0 on success
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# MetricsRegistry attribute
# ---------------------------------------------------------------------------


class TestMetricsRegistryAttribute:
    def test_recorder_schema_init_failed_attribute_exists(self):
        """MetricsRegistry must expose recorder_schema_init_failed as a Gauge."""
        from prometheus_client import Gauge

        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        assert hasattr(registry, "recorder_schema_init_failed"), (
            "MetricsRegistry missing recorder_schema_init_failed attribute"
        )
        assert isinstance(registry.recorder_schema_init_failed, Gauge)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_writer_with_mock_metrics(monkeypatch):
    """Build a DataWriter with CH disabled and a mock metrics object."""
    monkeypatch.setenv("HFT_CLICKHOUSE_ENABLED", "0")
    for var in (
        "HFT_CLICKHOUSE_USER",
        "HFT_CLICKHOUSE_USERNAME",
        "CLICKHOUSE_USER",
        "CLICKHOUSE_USERNAME",
        "HFT_CLICKHOUSE_PASSWORD",
        "CLICKHOUSE_PASSWORD",
        "HFT_CLICKHOUSE_HOST",
        "HFT_CLICKHOUSE_PORT",
        "HFT_DISABLE_CLICKHOUSE",
    ):
        monkeypatch.delenv(var, raising=False)

    mock_metrics = MagicMock()

    with (
        patch("hft_platform.observability.metrics.MetricsRegistry") as mock_mr,
        patch("hft_platform.recorder.writer.WALWriter"),
    ):
        mock_mr.get.return_value = mock_metrics
        from hft_platform.recorder.writer import DataWriter

        writer = DataWriter(ch_host="localhost", ch_port=9000)
        writer.metrics = mock_metrics
        return writer, mock_metrics


# ---------------------------------------------------------------------------
# _init_schema metric behaviour
# ---------------------------------------------------------------------------


class TestInitSchemaMetric:
    def test_metric_set_to_1_on_schema_failure(self, monkeypatch):
        """When apply_schema raises, recorder_schema_init_failed must be set to 1."""
        writer, mock_metrics = _make_writer_with_mock_metrics(monkeypatch)
        writer.ch_client = MagicMock()

        with patch(
            "hft_platform.recorder.writer.apply_schema",
            side_effect=RuntimeError("migration failed"),
        ):
            writer._init_schema()

        mock_metrics.recorder_schema_init_failed.set.assert_called_with(1)
        assert writer.connected is False
        assert writer._schema_initialized is False

    def test_metric_set_to_0_on_schema_success(self, monkeypatch):
        """When apply_schema succeeds, recorder_schema_init_failed must be set to 0."""
        writer, mock_metrics = _make_writer_with_mock_metrics(monkeypatch)
        writer.ch_client = MagicMock()

        with (
            patch("hft_platform.recorder.writer.apply_schema"),
            patch("hft_platform.recorder.writer.ensure_price_scaled_views"),
        ):
            writer._init_schema()

        mock_metrics.recorder_schema_init_failed.set.assert_called_with(0)
        assert writer._schema_initialized is True

    def test_metric_failure_does_not_propagate(self, monkeypatch):
        """If the metric set itself raises, _init_schema must not propagate the error."""
        writer, mock_metrics = _make_writer_with_mock_metrics(monkeypatch)
        writer.ch_client = MagicMock()
        mock_metrics.recorder_schema_init_failed.set.side_effect = Exception("prometheus error")

        with patch(
            "hft_platform.recorder.writer.apply_schema",
            side_effect=RuntimeError("migration failed"),
        ):
            # Must not raise despite the metric set failing
            writer._init_schema()

        assert writer.connected is False

    def test_metric_not_called_when_metrics_is_none(self, monkeypatch):
        """When writer.metrics is None, _init_schema must complete without error."""
        writer, _ = _make_writer_with_mock_metrics(monkeypatch)
        writer.metrics = None
        writer.ch_client = MagicMock()

        with patch(
            "hft_platform.recorder.writer.apply_schema",
            side_effect=RuntimeError("migration failed"),
        ):
            writer._init_schema()

        assert writer.connected is False
