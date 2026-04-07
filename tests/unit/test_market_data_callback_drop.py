"""Tests for md_callback_drop_total Prometheus counter in MarketDataService."""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from hft_platform.observability.metrics import MetricsRegistry


@pytest.fixture(autouse=True)
def _symbols_config(tmp_path):
    cfg_path = tmp_path / "symbols.yaml"
    cfg_path.write_text("symbols:\n  - code: '2330'\n    exchange: 'TSE'\n    price_scale: 10000\n")
    old = os.environ.get("SYMBOLS_CONFIG")
    os.environ["SYMBOLS_CONFIG"] = str(cfg_path)
    yield
    if old is None:
        os.environ.pop("SYMBOLS_CONFIG", None)
    else:
        os.environ["SYMBOLS_CONFIG"] = old


@pytest.fixture()
def metrics_registry():
    """Fresh MetricsRegistry for each test."""
    MetricsRegistry._instance = None
    reg = MetricsRegistry.get()
    yield reg
    MetricsRegistry._instance = None


@pytest.fixture()
def service(metrics_registry):
    from hft_platform.engine.event_bus import RingBufferBus
    from hft_platform.services.market_data import MarketDataService

    bus = MagicMock(spec=RingBufferBus)
    raw_queue = asyncio.Queue()
    client = MagicMock()
    svc = MarketDataService(bus, raw_queue, client)
    # Attach loop so we can control the parse_miss vs loop_missing path
    svc.loop = MagicMock()
    return svc


_PATCH_PREFIX = "hft_platform.services.market_data"


class TestMdCallbackDropParseMiss:
    """Verify parse_miss counter increments when callback yields no parseable msg."""

    def test_parse_miss_increments_counter(self, service):
        """When callback args cannot be parsed into a message, parse_miss counter increments."""
        assert service._cb_drop_parse_miss is not None
        before = service._cb_drop_parse_miss._value.get()

        # Call with zero args so fast-extract returns (None, None) and fallback
        # loop has nothing to iterate, leaving msg=None.
        service._on_shioaji_event()

        after = service._cb_drop_parse_miss._value.get()
        assert after == before + 1, f"Expected parse_miss to increment by 1, got {after - before}"

    def test_parse_miss_no_increment_on_valid_msg(self, service):
        """When fast extract returns a valid message, parse_miss counter does NOT increment."""
        assert service._cb_drop_parse_miss is not None
        before = service._cb_drop_parse_miss._value.get()

        mock_tick = MagicMock()
        mock_tick.code = "2330"

        with patch(
            f"{_PATCH_PREFIX}.try_fast_extract_callback_payload",
            return_value=("TSE", mock_tick),
        ):
            service._on_shioaji_event("TSE", mock_tick)

        after = service._cb_drop_parse_miss._value.get()
        assert after == before, f"Expected parse_miss unchanged, but got delta {after - before}"


class TestMdCallbackDropLoopMissing:
    """Verify loop_missing counter increments when self.loop is absent."""

    def test_loop_missing_increments_counter(self, service):
        """When self.loop is missing, loop_missing counter increments."""
        assert service._cb_drop_loop_missing is not None
        # Remove the loop attribute
        if hasattr(service, "loop"):
            delattr(service, "loop")

        before = service._cb_drop_loop_missing._value.get()

        mock_tick = MagicMock()
        mock_tick.code = "2330"

        # Provide a valid msg so we reach the loop-missing branch
        with patch(
            f"{_PATCH_PREFIX}.try_fast_extract_callback_payload",
            return_value=("TSE", mock_tick),
        ):
            service._on_shioaji_event("TSE", mock_tick)

        after = service._cb_drop_loop_missing._value.get()
        assert after == before + 1, f"Expected loop_missing to increment by 1, got {after - before}"


class TestMdCallbackDropCallbackError:
    """Verify callback_error counter increments when an exception occurs."""

    def test_callback_error_increments_counter(self, service):
        """When _on_shioaji_event raises internally, callback_error counter increments."""
        assert service._cb_drop_callback_error is not None
        before = service._cb_drop_callback_error._value.get()

        service._record_shioaji_crash_signature = MagicMock()

        with patch(
            f"{_PATCH_PREFIX}.try_fast_extract_callback_payload",
            side_effect=RuntimeError("boom"),
        ):
            service._on_shioaji_event("TSE", MagicMock())

        after = service._cb_drop_callback_error._value.get()
        assert after == before + 1, f"Expected callback_error to increment by 1, got {after - before}"

    def test_callback_error_still_logs_crash_signature(self, service):
        """Crash signature recording still happens after counter increment."""
        service._record_shioaji_crash_signature = MagicMock()

        with patch(
            f"{_PATCH_PREFIX}.try_fast_extract_callback_payload",
            side_effect=RuntimeError("test_boom"),
        ):
            service._on_shioaji_event("TSE", MagicMock())

        service._record_shioaji_crash_signature.assert_called_once_with(
            "test_boom", context="md_callback"
        )


class TestMdCallbackDropMetricExists:
    """Verify the metric is properly registered in MetricsRegistry."""

    def test_metric_registered(self, metrics_registry):
        """md_callback_drop_total should exist on MetricsRegistry."""
        assert hasattr(metrics_registry, "md_callback_drop_total")

    def test_pre_resolved_children_populated(self, service):
        """Pre-resolved counter children should be non-None when registry is available."""
        assert service._cb_drop_parse_miss is not None
        assert service._cb_drop_loop_missing is not None
        assert service._cb_drop_callback_error is not None
