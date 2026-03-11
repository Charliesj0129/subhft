"""Tests for broker-agnostic crash detector injection in MarketDataService."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from hft_platform.services.market_data import MarketDataService


def _make_service(crash_detector=None):
    """Create a minimal MarketDataService with mocked dependencies."""
    bus = MagicMock()
    raw_queue = asyncio.Queue()
    client = MagicMock()
    return MarketDataService(
        bus=bus,
        raw_queue=raw_queue,
        client=client,
        crash_detector=crash_detector,
    )


class TestCrashDetectorInjection:
    """Verify that MarketDataService accepts and uses an optional crash_detector."""

    def test_accepts_none_crash_detector(self):
        svc = _make_service(crash_detector=None)
        assert svc._crash_detector is None
        # Should not raise when recording with no detector
        svc._record_broker_crash_signature("some error", context="test")

    def test_calls_crash_detector_when_set(self):
        detector = MagicMock(return_value="test_signature")
        svc = _make_service(crash_detector=detector)

        # Set up a mock metrics_registry with broker_crash_signature_total
        mock_registry = MagicMock()
        mock_counter = MagicMock()
        mock_registry.broker_crash_signature_total = mock_counter
        mock_counter.labels.return_value = MagicMock()
        svc.metrics_registry = mock_registry

        svc._record_broker_crash_signature("crash text", context="md_callback")

        detector.assert_called_once_with("crash text")
        mock_counter.labels.assert_called_once_with(
            signature="test_signature", context="md_callback"
        )
        mock_counter.labels.return_value.inc.assert_called_once()

    def test_crash_detector_returning_none_skips_metric(self):
        detector = MagicMock(return_value=None)
        svc = _make_service(crash_detector=detector)

        mock_registry = MagicMock()
        mock_counter = MagicMock()
        mock_registry.broker_crash_signature_total = mock_counter
        svc.metrics_registry = mock_registry

        svc._record_broker_crash_signature("benign text", context="test")

        detector.assert_called_once_with("benign text")
        mock_counter.labels.assert_not_called()

    def test_no_shioaji_crash_metric_attribute(self):
        """When metrics_registry lacks broker_crash_signature_total, return early."""
        detector = MagicMock(return_value="sig")
        svc = _make_service(crash_detector=detector)

        mock_registry = MagicMock(spec=[])  # No attributes
        svc.metrics_registry = mock_registry

        # Should not raise
        svc._record_broker_crash_signature("err", context="test")
        detector.assert_not_called()


class TestBrokerEventMethodExists:
    """Verify the old _on_shioaji_event name is gone and _on_broker_event exists."""

    def test_on_broker_event_exists(self):
        svc = _make_service()
        assert hasattr(svc, "_on_broker_event")
        assert callable(svc._on_broker_event)

    def test_on_shioaji_event_does_not_exist(self):
        svc = _make_service()
        assert not hasattr(svc, "_on_shioaji_event")
