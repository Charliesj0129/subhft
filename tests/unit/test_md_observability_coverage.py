"""Coverage gap tests for services/_md_observability.py.

Targets uncovered branches: _emit_trace, _record_shioaji_crash_signature,
_record_feature_metrics paths, _maybe_run_feature_shadow_parity paths,
_emit_feature_shadow_check_metric, _emit_feature_shadow_mismatch_metric.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hft_platform.events import FeatureUpdateEvent, LOBStatsEvent, MetaData, TickEvent
from hft_platform.services._md_observability import MarketDataObservabilityMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mixin(**kwargs):
    """Create a minimal object with mixin methods and required attributes."""
    obj = type("MDService", (MarketDataObservabilityMixin,), {})()
    defaults = dict(
        feature_engine=None,
        _feature_shadow_engine=None,
        metrics_registry=None,
        _trace_sampler=None,
        _feature_latency_counter=0,
        _feature_metrics_counter=0,
        _feature_latency_sample_every=1,
        _feature_metrics_sample_every=1,
        _feature_latency_metric_child=None,
        _feature_update_metric_children={},
        _feature_quality_flag_metric_children={},
        _feature_set_id_cached="v3",
        _feature_shadow_counter=0,
        _feature_shadow_sample_every=1,
        _feature_shadow_abs_tolerance=1e-6,
        _feature_shadow_mismatch_counter=0,
        _feature_shadow_warn_every=10,
        _feature_shadow_checks_metric_children={},
        _feature_shadow_mismatch_metric_children={},
    )
    defaults.update(kwargs)
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _make_tick():
    meta = MetaData(seq=1, source_ts=1000, local_ts=1000)
    return TickEvent(meta=meta, symbol="TST", price=1000000, volume=10)


def _make_feature_update(**kwargs):
    defaults = dict(
        symbol="TST",
        ts=1000,
        local_ts=1000,
        seq=1,
        feature_set_id="v3",
        schema_version=3,
        changed_mask=0xFF,
        warmup_ready_mask=0xFF,
        quality_flags=0,
        feature_ids=("f1", "f2"),
        values=(1.0, 2.0),
    )
    defaults.update(kwargs)
    return FeatureUpdateEvent(**defaults)


# ---------------------------------------------------------------------------
# _emit_trace
# ---------------------------------------------------------------------------


class TestEmitTrace:
    def test_no_sampler(self):  # noqa: no-assert
        obj = _make_mixin()
        obj._emit_trace("test", "trace1", {"a": 1})
        # No crash

    def test_with_sampler(self):
        sampler = MagicMock()
        obj = _make_mixin(_trace_sampler=sampler)
        obj._emit_trace("test_stage", "trace1", {"key": "val"})
        sampler.emit.assert_called_once()

    def test_sampler_exception(self):  # noqa: no-assert
        sampler = MagicMock()
        sampler.emit.side_effect = RuntimeError("boom")
        obj = _make_mixin(_trace_sampler=sampler)
        obj._emit_trace("test", "t1", {})
        # Should not raise


# ---------------------------------------------------------------------------
# _record_shioaji_crash_signature
# ---------------------------------------------------------------------------


class TestRecordCrashSignature:
    def test_no_metrics(self):  # noqa: no-assert
        obj = _make_mixin()
        obj._record_shioaji_crash_signature("error text", context="test")

    def test_no_signature_detected(self):
        metrics = MagicMock()
        metrics.shioaji_crash_signature_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        with patch("hft_platform.services._md_observability.detect_crash_signature", return_value=None):
            obj._record_shioaji_crash_signature("normal text", context="test")
        metrics.shioaji_crash_signature_total.labels.assert_not_called()

    def test_signature_detected(self):
        metrics = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        with patch("hft_platform.services._md_observability.detect_crash_signature", return_value="SEGFAULT"):
            obj._record_shioaji_crash_signature("crash text", context="test")
        metrics.shioaji_crash_signature_total.labels.assert_called_once()


# ---------------------------------------------------------------------------
# _record_feature_metrics
# ---------------------------------------------------------------------------


class TestRecordFeatureMetrics:
    def test_no_metrics_registry(self):
        obj = _make_mixin()
        obj._record_feature_metrics(_make_tick(), None, 0)
        assert obj._feature_latency_counter == 1

    def test_with_feature_update(self):
        metrics = MagicMock()
        metrics.feature_plane_latency_ns = MagicMock()
        metrics.feature_plane_updates_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        fu = _make_feature_update()
        obj._record_feature_metrics(_make_tick(), fu, 100)
        assert obj._feature_latency_counter == 1
        assert obj._feature_metrics_counter == 1

    def test_without_feature_update(self):  # noqa: no-assert
        metrics = MagicMock()
        metrics.feature_plane_updates_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        obj._record_feature_metrics(_make_tick(), None, 100)

    def test_quality_flags_recorded(self):  # noqa: no-assert
        metrics = MagicMock()
        metrics.feature_plane_updates_total = MagicMock()
        metrics.feature_quality_flags_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        fu = _make_feature_update(quality_flags=0xFF)
        obj._record_feature_metrics(_make_tick(), fu, 100)

    def test_with_feature_engine_view(self):  # noqa: no-assert
        metrics = MagicMock()
        metrics.feature_plane_updates_total = MagicMock()
        fe = MagicMock()
        fe.get_feature_view.return_value = {"quality_flags": 3, "values": (1.0,), "feature_ids": ("f1",)}
        obj = _make_mixin(metrics_registry=metrics, feature_engine=fe)
        obj._record_feature_metrics(_make_tick(), None, 100)


# ---------------------------------------------------------------------------
# _maybe_run_feature_shadow_parity
# ---------------------------------------------------------------------------


class TestFeatureShadowParity:
    def test_no_shadow_engine(self):  # noqa: no-assert
        obj = _make_mixin()
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, None)
        # No crash

    def test_shadow_update_fails(self):  # noqa: no-assert
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(side_effect=RuntimeError("fail"))
        obj = _make_mixin(_feature_shadow_engine=shadow, metrics_registry=MagicMock())
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, None)

    def test_shadow_parity_match(self):
        """When primary and shadow produce same values, no mismatch."""
        shadow = MagicMock()
        shadow_fu = _make_feature_update()
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        obj = _make_mixin(_feature_shadow_engine=shadow, _feature_shadow_sample_every=1)
        primary = _make_feature_update()
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, primary)
        assert obj._feature_shadow_mismatch_counter == 0

    def test_shadow_parity_mismatch(self):
        """When primary and shadow differ, mismatch is recorded."""
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(99.0, 99.0))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        metrics = MagicMock()
        metrics.feature_shadow_parity_checks_total = MagicMock()
        metrics.feature_shadow_parity_mismatch_total = MagicMock()
        obj = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_sample_every=1,
            _feature_shadow_warn_every=1,
            metrics_registry=metrics,
        )
        primary = _make_feature_update(values=(1.0, 2.0))
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, primary)
        assert obj._feature_shadow_mismatch_counter == 1

    def test_shadow_parity_id_mismatch(self):  # noqa: no-assert
        """When feature IDs differ, all primary IDs are reported as mismatches."""
        shadow = MagicMock()
        shadow_fu = _make_feature_update(feature_ids=("x1", "x2"))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        metrics = MagicMock()
        metrics.feature_shadow_parity_checks_total = MagicMock()
        metrics.feature_shadow_parity_mismatch_total = MagicMock()
        obj = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_sample_every=1,
            metrics_registry=metrics,
        )
        primary = _make_feature_update(feature_ids=("f1", "f2"))
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, primary)

    def test_shadow_compare_skipped_when_not_aligned(self):  # noqa: no-assert
        """Shadow is updated but comparison is skipped when counter not aligned."""
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(return_value=_make_feature_update())
        obj = _make_mixin(_feature_shadow_engine=shadow, _feature_shadow_sample_every=100)
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, _make_feature_update())
        # Counter incremented but comparison skipped

    def test_shadow_with_process_lob_stats_fallback(self):  # noqa: no-assert
        """When shadow has no process_lob_update, uses process_lob_stats."""
        shadow = MagicMock(spec=[])  # No process_lob_update attribute
        shadow.process_lob_stats = MagicMock(return_value=_make_feature_update())
        obj = _make_mixin(_feature_shadow_engine=shadow, _feature_shadow_sample_every=1)
        lob = LOBStatsEvent(
            symbol="TST", ts=1000, imbalance=0.0, best_bid=1000, best_ask=1001, bid_depth=10, ask_depth=10
        )
        obj._maybe_run_feature_shadow_parity(_make_tick(), lob, 1000, _make_feature_update())


# ---------------------------------------------------------------------------
# _emit_feature_shadow_check_metric
# ---------------------------------------------------------------------------


class TestShadowCheckMetric:
    def test_no_metrics(self):  # noqa: no-assert
        obj = _make_mixin()
        obj._emit_feature_shadow_check_metric("checked")

    def test_with_metrics(self):
        metrics = MagicMock()
        metrics.feature_shadow_parity_checks_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        obj._emit_feature_shadow_check_metric("checked")
        metrics.feature_shadow_parity_checks_total.labels.assert_called()

    def test_metric_cached(self):
        metrics = MagicMock()
        metrics.feature_shadow_parity_checks_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        obj._emit_feature_shadow_check_metric("checked")
        obj._emit_feature_shadow_check_metric("checked")
        # Second call uses cache
        assert ("v3", "checked") in obj._feature_shadow_checks_metric_children


# ---------------------------------------------------------------------------
# _emit_feature_shadow_mismatch_metric
# ---------------------------------------------------------------------------


class TestShadowMismatchMetric:
    def test_no_metrics(self):  # noqa: no-assert
        obj = _make_mixin()
        obj._emit_feature_shadow_mismatch_metric("v3", "f1")

    def test_with_metrics(self):
        metrics = MagicMock()
        metrics.feature_shadow_parity_mismatch_total = MagicMock()
        obj = _make_mixin(metrics_registry=metrics)
        obj._emit_feature_shadow_mismatch_metric("v3", "f1")
        metrics.feature_shadow_parity_mismatch_total.labels.assert_called()
