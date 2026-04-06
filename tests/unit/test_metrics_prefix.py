"""Tests for HFT_METRICS_PREFIX migration helper (_pn)."""

from __future__ import annotations


class TestMetricsPrefixHelper:
    """Verify _pn() helper for metric name prefixing."""

    def test_default_no_prefix(self):
        """Default (empty HFT_METRICS_PREFIX): names unchanged."""
        from hft_platform.observability import metrics

        original = metrics._METRICS_PREFIX
        try:
            metrics._METRICS_PREFIX = ""
            assert metrics._pn("feed_events_total") == "feed_events_total"
        finally:
            metrics._METRICS_PREFIX = original

    def test_prefix_applied_when_set(self):
        """When prefix is set, names get prefixed."""
        from hft_platform.observability import metrics

        original = metrics._METRICS_PREFIX
        try:
            metrics._METRICS_PREFIX = "hft_"
            assert metrics._pn("feed_events_total") == "hft_feed_events_total"
        finally:
            metrics._METRICS_PREFIX = original

    def test_no_double_prefix(self):
        """Names already starting with prefix are not double-prefixed."""
        from hft_platform.observability import metrics

        original = metrics._METRICS_PREFIX
        try:
            metrics._METRICS_PREFIX = "hft_"
            assert metrics._pn("hft_backup_last_success_ts") == "hft_backup_last_success_ts"
        finally:
            metrics._METRICS_PREFIX = original

    def test_empty_name(self):
        """Empty name returns empty (no crash)."""
        from hft_platform.observability import metrics

        original = metrics._METRICS_PREFIX
        try:
            metrics._METRICS_PREFIX = "hft_"
            assert metrics._pn("") == "hft_"
        finally:
            metrics._METRICS_PREFIX = original
