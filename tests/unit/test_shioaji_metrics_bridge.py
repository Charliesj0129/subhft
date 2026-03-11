"""Tests for ShioajiMetricsBridge — cached label accessors."""

from __future__ import annotations

import pytest

from hft_platform.feed_adapter.shioaji._metrics import ShioajiMetricsBridge
from hft_platform.observability.metrics import MetricsRegistry


@pytest.fixture()
def bridge() -> ShioajiMetricsBridge:
    registry = MetricsRegistry.get()
    return ShioajiMetricsBridge(registry)


class TestShioajiMetricsBridge:
    def test_api_latency_returns_child(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.api_latency("login", "ok")
        assert child is not None

    def test_api_latency_caches(self, bridge: ShioajiMetricsBridge) -> None:
        c1 = bridge.api_latency("login", "ok")
        c2 = bridge.api_latency("login", "ok")
        assert c1 is c2

    def test_api_latency_different_labels(self, bridge: ShioajiMetricsBridge) -> None:
        c1 = bridge.api_latency("login", "ok")
        c2 = bridge.api_latency("login", "error")
        assert c1 is not c2

    def test_api_errors(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.api_errors("place_order")
        assert child is not None

    def test_api_jitter(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.api_jitter("subscribe")
        assert child is not None

    def test_api_jitter_hist(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.api_jitter_hist("subscribe")
        assert child is not None

    def test_quote_route(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.quote_route("miss")
        assert child is not None

    def test_quote_callback_ingress_latency(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.quote_callback_ingress_latency()
        assert metric is not None

    def test_quote_callback_queue_depth(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.quote_callback_queue_depth()
        assert metric is not None

    def test_quote_callback_queue_dropped(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.quote_callback_queue_dropped()
        assert metric is not None

    def test_thread_alive(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.thread_alive("watchdog")
        assert child is not None

    def test_quote_pending_age(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.quote_pending_age()
        assert metric is not None

    def test_quote_pending_stall(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.quote_pending_stall("timeout")
        assert child is not None

    def test_session_lock_conflicts(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.session_lock_conflicts()
        assert metric is not None

    def test_login_fail(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.login_fail("timeout")
        assert child is not None

    def test_crash_signature(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.crash_signature("SEGV", "login")
        assert child is not None

    def test_keepalive_failures(self, bridge: ShioajiMetricsBridge) -> None:
        metric = bridge.keepalive_failures()
        assert metric is not None

    def test_contract_lookup_errors(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.contract_lookup_errors("2330")
        assert child is not None

    def test_feed_reconnect(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.feed_reconnect("ok")
        assert child is not None

    def test_feed_resubscribe(self, bridge: ShioajiMetricsBridge) -> None:
        child = bridge.feed_resubscribe("ok")
        assert child is not None

    def test_registry_property(self, bridge: ShioajiMetricsBridge) -> None:
        assert bridge.registry is not None
        assert isinstance(bridge.registry, MetricsRegistry)

    def test_missing_metric_returns_none(self) -> None:
        """If a metric name doesn't exist, _child returns None."""
        bridge = ShioajiMetricsBridge()
        result = bridge._child("nonexistent_metric_xyz", op="test")
        assert result is None

    def test_observe_latency(self, bridge: ShioajiMetricsBridge) -> None:
        """Verify we can actually call .observe() on the cached child."""
        child = bridge.api_latency("test_op", "ok")
        child.observe(42.0)  # Should not raise

    def test_inc_counter(self, bridge: ShioajiMetricsBridge) -> None:
        """Verify we can actually call .inc() on a cached counter child."""
        child = bridge.api_errors("test_op")
        child.inc()  # Should not raise
