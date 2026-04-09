"""Tests for P3b: split LOB-only vs combined lob_process latency metrics.

Verifies:
- lob_only_latency_ns histogram is observed independently of lob_process
- feature_plane_latency_ns sampling is aligned with _md_latency_sample_every
- _VALID_STAGES contains lob_only and lob_process
- lob_only_latency_ns is registered in MetricsRegistry
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_symbols_config() -> tuple[tempfile.TemporaryDirectory, Path]:
    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "symbols.yaml"
    cfg.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    return tmp, cfg


def _make_service(**kwargs):
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    tmp, cfg = _make_symbols_config()
    env = {
        "HFT_FEATURE_ENGINE_ENABLED": "0",
        "SYMBOLS_CONFIG": str(cfg),
        "HFT_MONITOR_LIVE_ENABLED": "0",
        "HFT_MD_LATENCY_SAMPLE_EVERY": "1",  # always sample
    }

    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue(maxsize=100)
    client = MagicMock()
    client.login = MagicMock(return_value=None)
    client.validate_symbols = MagicMock(return_value=None)
    client.fetch_snapshots = MagicMock(return_value=[])
    client.subscribe_basket = MagicMock(return_value=None)

    with patch.dict(os.environ, env):
        svc = MarketDataService(bus, raw_queue, client, **kwargs)
    svc._tmp = tmp  # keep alive
    return svc, bus


# ---------------------------------------------------------------------------
# 1. _VALID_STAGES allowlist
# ---------------------------------------------------------------------------


class TestValidStagesContainLobStages:
    def test_lob_only_in_valid_stages(self) -> None:
        from hft_platform.observability.latency import _VALID_STAGES

        assert "lob_only" in _VALID_STAGES

    def test_lob_process_in_valid_stages(self) -> None:
        from hft_platform.observability.latency import _VALID_STAGES

        assert "lob_process" in _VALID_STAGES


# ---------------------------------------------------------------------------
# 2. MetricsRegistry has lob_only_latency_ns
# ---------------------------------------------------------------------------


class TestMetricsRegistryLobOnlyHistogram:
    def test_lob_only_latency_ns_attribute_exists(self) -> None:
        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        assert hasattr(registry, "lob_only_latency_ns")

    def test_lob_only_latency_ns_is_histogram(self) -> None:
        from prometheus_client import Histogram

        from hft_platform.observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        assert isinstance(registry.lob_only_latency_ns, Histogram)


# ---------------------------------------------------------------------------
# 3. LatencyRecorder records lob_only and lob_process stages
# ---------------------------------------------------------------------------


class TestLatencyRecorderLobStages:
    def test_lob_only_stage_is_recorded(self) -> None:
        from hft_platform.observability.latency import LatencyRecorder

        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        recorder._metrics_sample_every = 1
        recorder._metrics_counter = 0

        mock_histogram = MagicMock()
        mock_metrics = MagicMock()
        mock_metrics.pipeline_latency_ns.labels.return_value = mock_histogram
        recorder.metrics = mock_metrics
        recorder._stage_metric_cache.clear()
        recorder._stage_metric_cache_owner_id = id(mock_metrics)

        recorder.record("lob_only", latency_ns=2000)

        mock_metrics.pipeline_latency_ns.labels.assert_called_once_with(stage="lob_only")
        mock_histogram.observe.assert_called_once_with(2000)

    def test_lob_process_stage_is_recorded(self) -> None:
        from hft_platform.observability.latency import LatencyRecorder

        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        recorder._metrics_sample_every = 1
        recorder._metrics_counter = 0

        mock_histogram = MagicMock()
        mock_metrics = MagicMock()
        mock_metrics.pipeline_latency_ns.labels.return_value = mock_histogram
        recorder.metrics = mock_metrics
        recorder._stage_metric_cache.clear()
        recorder._stage_metric_cache_owner_id = id(mock_metrics)

        recorder.record("lob_process", latency_ns=5000)

        mock_metrics.pipeline_latency_ns.labels.assert_called_with(stage="lob_process")
        mock_histogram.observe.assert_called_with(5000)

    def test_lob_only_and_lob_process_recorded_independently(self) -> None:
        from hft_platform.observability.latency import LatencyRecorder

        LatencyRecorder.reset_for_tests()
        recorder = LatencyRecorder()
        recorder.metrics_enabled = True
        recorder._metrics_sample_every = 1
        recorder._metrics_counter = 0

        mock_metrics = MagicMock()
        only_histogram = MagicMock()
        combined_histogram = MagicMock()

        def label_side_effect(stage):
            if stage == "lob_only":
                return only_histogram
            if stage == "lob_process":
                return combined_histogram
            return MagicMock()

        mock_metrics.pipeline_latency_ns.labels.side_effect = label_side_effect
        recorder.metrics = mock_metrics
        recorder._stage_metric_cache.clear()
        recorder._stage_metric_cache_owner_id = id(mock_metrics)

        recorder.record("lob_only", latency_ns=1500)
        recorder.record("lob_process", latency_ns=6000)

        only_histogram.observe.assert_called_once_with(1500)
        combined_histogram.observe.assert_called_once_with(6000)


# ---------------------------------------------------------------------------
# 4. _process_raw records lob_only_latency_ns via metrics_registry
# ---------------------------------------------------------------------------


class TestProcessRawLobOnlyMetric:
    def test_lob_only_latency_observed_on_process_raw(self) -> None:
        """_process_raw must call lob_only_latency_ns.observe() independently."""
        from hft_platform.events import TickEvent

        svc, bus = _make_service()

        mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
        svc.normalizer = MagicMock()
        svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
        svc.lob = MagicMock()
        svc.lob.process_event = MagicMock(return_value=None)
        svc.feature_engine = None

        # Wire a mock metrics_registry with a lob_only_latency_ns histogram
        mock_histogram = MagicMock()
        mock_registry = MagicMock()
        mock_registry.lob_only_latency_ns = mock_histogram
        svc.metrics_registry = mock_registry

        # Force sampling on every call
        svc._md_latency_counter = 0
        svc._md_latency_sample_every = 1
        svc._lob_only_latency_metric_child = None

        raw = {"code": "2330", "close": 500.0, "volume": 100}
        svc._process_raw(raw)

        # lob_only_latency_ns.observe() must have been called exactly once
        assert mock_histogram.observe.call_count == 1
        observed_val = mock_histogram.observe.call_args[0][0]
        assert observed_val >= 0, "lob_only duration must be non-negative"

    def test_lob_only_latency_strictly_lte_lob_process_duration(self) -> None:
        """lob_only must be <= combined (lob + feature) duration."""
        from hft_platform.events import TickEvent

        svc, bus = _make_service()

        mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
        svc.normalizer = MagicMock()
        svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
        svc.lob = MagicMock()
        svc.lob.process_event = MagicMock(return_value=None)
        svc.feature_engine = None

        observed_only: list[int] = []
        observed_combined: list[int] = []

        # Intercept latency recorder
        mock_latency = MagicMock()

        def record_side_effect(stage, duration, **_kw):
            if stage == "lob_only":
                observed_only.append(duration)
            elif stage == "lob_process":
                observed_combined.append(duration)

        mock_latency.record.side_effect = record_side_effect
        svc.latency = mock_latency

        svc._md_latency_counter = 0
        svc._md_latency_sample_every = 1

        raw = {"code": "2330", "close": 500.0, "volume": 100}
        svc._process_raw(raw)

        assert len(observed_only) == 1, "lob_only must be recorded once"
        assert len(observed_combined) == 1, "lob_process must be recorded once"
        assert observed_only[0] <= observed_combined[0], (
            f"lob_only ({observed_only[0]}) must be <= lob_process ({observed_combined[0]})"
        )

    def test_lob_only_not_recorded_when_sampling_skips(self) -> None:
        """When the sampling counter does not trigger, no lob_only observation occurs."""
        from hft_platform.events import TickEvent

        svc, bus = _make_service()

        mock_event = TickEvent(meta=None, symbol="2330", price=5000000, volume=100)
        svc.normalizer = MagicMock()
        svc.normalizer.normalize_tick = MagicMock(return_value=mock_event)
        svc.lob = MagicMock()
        svc.lob.process_event = MagicMock(return_value=None)
        svc.feature_engine = None

        mock_histogram = MagicMock()
        mock_registry = MagicMock()
        mock_registry.lob_only_latency_ns = mock_histogram
        svc.metrics_registry = mock_registry

        # Set counter so the sample condition is NOT met this call:
        # counter starts at 0, sample_every = 4 → counter becomes 1 after inc, 1 % 4 != 0
        svc._md_latency_counter = 0
        svc._md_latency_sample_every = 4

        raw = {"code": "2330", "close": 500.0, "volume": 100}
        svc._process_raw(raw)

        # The counter is incremented to 1 before the check, 1 % 4 != 0 → no observe
        mock_histogram.observe.assert_not_called()


# ---------------------------------------------------------------------------
# 5. feature_plane_latency sampling aligned with _md_latency_sample_every
# ---------------------------------------------------------------------------


class TestFeatureLatencySamplingAlignment:
    def test_feature_latency_uses_md_latency_counter(self) -> None:
        """feature_plane_latency_ns must be observed when _md_latency_counter aligns."""
        import numpy as np

        from hft_platform.events import BidAskEvent, LOBStatsEvent

        svc, bus = _make_service()

        bids = np.array([[5000000, 10]], dtype=np.int64)
        asks = np.array([[5001000, 5]], dtype=np.int64)
        mock_event = BidAskEvent(meta=None, symbol="2330", bids=bids, asks=asks, is_snapshot=False)

        mock_stats = MagicMock(spec=LOBStatsEvent)
        svc.normalizer = MagicMock()
        svc.normalizer.normalize_bidask = MagicMock(return_value=mock_event)
        svc.lob = MagicMock()
        svc.lob.process_event = MagicMock(return_value=mock_stats)

        # Enable feature engine with mock
        mock_fe = MagicMock()
        mock_fe.process_lob_stats = MagicMock(return_value=None)
        mock_fe.feature_set_id = MagicMock(return_value="lob_shared_v3")
        svc.feature_engine = mock_fe
        svc._fe_process_lob_update = None

        # Wire metrics
        mock_feature_hist = MagicMock()
        mock_registry = MagicMock()
        mock_registry.feature_plane_latency_ns = mock_feature_hist
        svc.metrics_registry = mock_registry

        # Force sampling on every call via md_latency counter
        svc._md_latency_counter = 0
        svc._md_latency_sample_every = 1
        svc._feature_latency_metric_child = mock_feature_hist

        raw = {"bid_price": [5000000], "ask_price": [5001000], "bid_volume": [10]}
        svc._process_raw(raw)

        # feature_plane_latency_ns must have been observed
        assert mock_feature_hist.observe.call_count >= 1, (
            "feature_plane_latency_ns.observe() must be called when md_latency counter aligns"
        )
