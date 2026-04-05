"""Tests for _VALID_STAGES allowlist in LatencyRecorder."""
from unittest.mock import MagicMock

import pytest

from hft_platform.observability.latency import LatencyRecorder, _VALID_STAGES


class TestValidStagesAllowlist:
    def test_valid_stages_allowlist_exists(self) -> None:
        assert isinstance(_VALID_STAGES, frozenset)
        expected = {
            "normalize",
            "lob",
            "lob_only",
            "lob_process",
            "feature",
            "strategy",
            "risk",
            "order",
            "execution",
            "gateway",
            "record",
            "bus_publish",
        }
        assert _VALID_STAGES == expected

    def test_record_skips_invalid_stage(self) -> None:
        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        mock_metrics = MagicMock()
        recorder.metrics = mock_metrics

        recorder.record("__invalid_stage__", latency_ns=1000)

        mock_metrics.pipeline_latency_ns.labels.assert_not_called()

    def test_record_emits_for_valid_stage(self) -> None:
        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        recorder._metrics_sample_every = 1  # always sample
        recorder._metrics_counter = 0

        mock_histogram = MagicMock()
        mock_metrics = MagicMock()
        mock_metrics.pipeline_latency_ns.labels.return_value = mock_histogram
        recorder.metrics = mock_metrics
        recorder._stage_metric_cache.clear()
        recorder._stage_metric_cache_owner_id = id(mock_metrics)

        recorder.record("normalize", latency_ns=500)

        mock_metrics.pipeline_latency_ns.labels.assert_called_once_with(stage="normalize")
        mock_histogram.observe.assert_called_once_with(500)

    def test_record_skips_negative_latency(self) -> None:
        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        mock_metrics = MagicMock()
        recorder.metrics = mock_metrics

        recorder.record("normalize", latency_ns=-1)

        mock_metrics.pipeline_latency_ns.labels.assert_not_called()
