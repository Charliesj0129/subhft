"""Unit tests for hft_platform.services._md_observability."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from hft_platform.services._md_observability import MarketDataObservabilityMixin


# ---------------------------------------------------------------------------
# Helpers / Stub
# ---------------------------------------------------------------------------

# Quality flag bit values (from feature.engine)
QUALITY_FLAG_GAP = 1
QUALITY_FLAG_STATE_RESET = 2
QUALITY_FLAG_STALE_INPUT = 4
QUALITY_FLAG_OUT_OF_ORDER = 8
QUALITY_FLAG_PARTIAL = 16


def _make_stub(**overrides: Any) -> MarketDataObservabilityMixin:
    """Create a minimal concrete stub of MarketDataObservabilityMixin."""

    class _Stub(MarketDataObservabilityMixin):
        def __init__(self) -> None:
            self.feature_engine = None
            self.metrics_registry = None
            self._trace_sampler = None
            self._feature_shadow_engine = None

            # Latency / metrics counters
            self._feature_latency_counter = 0
            self._feature_metrics_counter = 0
            self._feature_latency_sample_every = 1
            self._feature_metrics_sample_every = 1
            self._feature_latency_metric_child = None

            # Child metric caches
            self._feature_update_metric_children: dict = {}
            self._feature_quality_flag_metric_children: dict = {}

            # Cached feature set id
            self._feature_set_id_cached = "lob_shared_v3"

            # Shadow parity attrs
            self._feature_shadow_counter = 0
            self._feature_shadow_sample_every = 1
            self._feature_shadow_abs_tolerance = 1e-6
            self._feature_shadow_mismatch_counter = 0
            self._feature_shadow_warn_every = 1
            self._feature_shadow_checks_metric_children: dict = {}
            self._feature_shadow_mismatch_metric_children: dict = {}

    stub = _Stub()
    for k, v in overrides.items():
        setattr(stub, k, v)
    return stub


def _make_tick_event(symbol: str = "2330") -> MagicMock:
    ev = MagicMock()
    ev.symbol = symbol
    meta = MagicMock()
    meta.local_ts = 1_000_000
    ev.meta = meta
    return ev


def _make_stats(best_bid: int = 850_0000, best_ask: int = 851_0000) -> MagicMock:
    stats = MagicMock()
    stats.best_bid = best_bid
    stats.best_ask = best_ask
    return stats


def _make_feature_update(feature_set_id: str = "lob_shared_v3", values: tuple = (1.0, 2.0), feature_ids: tuple = ("f1", "f2"), quality_flags: int = 0) -> MagicMock:
    fu = MagicMock()
    fu.feature_set_id = feature_set_id
    fu.values = list(values)
    fu.feature_ids = list(feature_ids)
    fu.quality_flags = quality_flags
    return fu


# ---------------------------------------------------------------------------
# _emit_trace
# ---------------------------------------------------------------------------


class TestEmitTrace:
    def test_no_op_when_sampler_is_none(self):
        stub = _make_stub()
        # Should not raise
        stub._emit_trace("stage", "tid", {"key": "val"})

    def test_calls_sampler_emit(self):
        sampler = MagicMock()
        stub = _make_stub(_trace_sampler=sampler)
        stub._emit_trace("my_stage", "trace123", {"x": 1})
        sampler.emit.assert_called_once_with(stage="my_stage", trace_id="trace123", payload={"x": 1})

    def test_swallows_sampler_exception(self):
        sampler = MagicMock()
        sampler.emit.side_effect = RuntimeError("boom")
        stub = _make_stub(_trace_sampler=sampler)
        # Should not raise
        stub._emit_trace("stage", "id", {})

    def test_handles_none_trace_id(self):
        sampler = MagicMock()
        stub = _make_stub(_trace_sampler=sampler)
        stub._emit_trace("stage", None, {})
        sampler.emit.assert_called_once()
        _, kwargs = sampler.emit.call_args
        assert kwargs["trace_id"] == ""


# ---------------------------------------------------------------------------
# _record_shioaji_crash_signature
# ---------------------------------------------------------------------------


class TestRecordShioajiCrashSignature:
    def test_no_op_when_no_metrics_registry(self):
        stub = _make_stub()
        # Should not raise
        stub._record_shioaji_crash_signature("Connection reset by peer", context="reconnect")

    def test_no_op_when_registry_lacks_attribute(self):
        registry = MagicMock(spec=[])  # no attributes
        stub = _make_stub(metrics_registry=registry)
        stub._record_shioaji_crash_signature("some text", context="ctx")

    @patch("hft_platform.services._md_observability.detect_crash_signature", return_value=None)
    def test_no_op_when_no_signature_detected(self, mock_detect):
        registry = MagicMock()
        stub = _make_stub(metrics_registry=registry)
        stub._record_shioaji_crash_signature("random text", context="ctx")
        registry.shioaji_crash_signature_total.labels.assert_not_called()

    @patch("hft_platform.services._md_observability.detect_crash_signature", return_value="connection_reset")
    def test_increments_metric_when_signature_found(self, mock_detect):
        registry = MagicMock()
        stub = _make_stub(metrics_registry=registry)
        stub._record_shioaji_crash_signature("Connection reset by peer", context="reconnect")
        registry.shioaji_crash_signature_total.labels.assert_called_once_with(
            signature="connection_reset", context="reconnect"
        )
        registry.shioaji_crash_signature_total.labels.return_value.inc.assert_called_once()


# ---------------------------------------------------------------------------
# _maybe_update_features
# ---------------------------------------------------------------------------


class TestMaybeUpdateFeatures:
    def test_returns_none_when_no_feature_engine(self):
        stub = _make_stub()
        event = _make_tick_event()
        result = stub._maybe_update_features(event, _make_stats())
        assert result is None

    def test_returns_none_when_stats_is_none(self):
        stub = _make_stub(feature_engine=MagicMock())
        result = stub._maybe_update_features(_make_tick_event(), None)
        assert result is None

    def test_returns_none_when_stats_missing_best_bid(self):
        stub = _make_stub(feature_engine=MagicMock())
        stats = MagicMock(spec=["best_ask"])  # only best_ask, no best_bid
        result = stub._maybe_update_features(_make_tick_event(), stats)
        assert result is None

    def test_returns_none_when_stats_missing_best_ask(self):
        stub = _make_stub(feature_engine=MagicMock())
        stats = MagicMock(spec=["best_bid"])  # only best_bid, no best_ask
        result = stub._maybe_update_features(_make_tick_event(), stats)
        assert result is None

    def test_calls_process_lob_update_when_available(self):
        fu = _make_feature_update()
        engine = MagicMock()
        engine.process_lob_update = MagicMock(return_value=fu)
        stub = _make_stub(feature_engine=engine)
        result = stub._maybe_update_features(_make_tick_event(), _make_stats())
        assert result is fu
        engine.process_lob_update.assert_called_once()

    def test_falls_back_to_process_lob_stats_when_no_process_lob_update(self):
        fu = _make_feature_update()
        engine = MagicMock(spec=["process_lob_stats"])  # no process_lob_update
        engine.process_lob_stats = MagicMock(return_value=fu)
        stub = _make_stub(feature_engine=engine)
        result = stub._maybe_update_features(_make_tick_event(), _make_stats())
        assert result is fu
        engine.process_lob_stats.assert_called_once()

    def test_returns_none_and_emits_error_on_exception(self):
        engine = MagicMock()
        engine.process_lob_update.side_effect = ValueError("bad input")
        stub = _make_stub(feature_engine=engine)
        result = stub._maybe_update_features(_make_tick_event(), _make_stats())
        assert result is None
        # Error metric counter should have incremented
        assert stub._feature_metrics_counter > 0


# ---------------------------------------------------------------------------
# _record_feature_metrics
# ---------------------------------------------------------------------------


class TestRecordFeatureMetrics:
    def test_increments_both_counters(self):
        stub = _make_stub()
        stub._record_feature_metrics(_make_tick_event(), None, 0)
        assert stub._feature_latency_counter == 1
        assert stub._feature_metrics_counter == 1

    def test_no_metrics_when_registry_is_none(self):
        stub = _make_stub()
        stub._feature_latency_sample_every = 1
        stub._feature_metrics_sample_every = 1
        # Should not raise without a registry
        stub._record_feature_metrics(_make_tick_event(), _make_feature_update(), 0)

    def test_observes_latency_at_sample_boundary(self):
        registry = MagicMock()
        latency_metric = MagicMock()
        registry.feature_plane_latency_ns = latency_metric
        stub = _make_stub(metrics_registry=registry)
        stub._feature_latency_sample_every = 1
        stub._record_feature_metrics(_make_tick_event(), _make_feature_update(), 0)
        latency_metric.observe.assert_called_once()

    def test_does_not_observe_latency_outside_sample_boundary(self):
        registry = MagicMock()
        latency_metric = MagicMock()
        registry.feature_plane_latency_ns = latency_metric
        stub = _make_stub(metrics_registry=registry)
        stub._feature_latency_sample_every = 10
        stub._feature_latency_counter = 0  # counter will be 1 after call, not divisible by 10
        stub._record_feature_metrics(_make_tick_event(), _make_feature_update(), 0)
        latency_metric.observe.assert_not_called()

    def test_increments_emitted_update_metric(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_plane_updates_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._feature_metrics_sample_every = 1
        fu = _make_feature_update(feature_set_id="lob_shared_v3")
        stub._record_feature_metrics(_make_tick_event(), fu, 0)
        child.inc.assert_called()

    def test_caches_child_metric_on_second_call(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_plane_updates_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._feature_metrics_sample_every = 1
        fu = _make_feature_update(feature_set_id="lob_shared_v3")
        stub._record_feature_metrics(_make_tick_event(), fu, 0)
        stub._record_feature_metrics(_make_tick_event(), fu, 0)
        # labels() should only be called once (cached on second call)
        assert registry.feature_plane_updates_total.labels.call_count == 1

    def test_quality_flags_emitted_for_set_bits(self):
        registry = MagicMock()
        flag_child = MagicMock()
        update_child = MagicMock()
        registry.feature_plane_updates_total.labels.return_value = update_child
        registry.feature_quality_flags_total.labels.return_value = flag_child
        stub = _make_stub(metrics_registry=registry)
        stub._feature_metrics_sample_every = 1
        # Set GAP (bit 1) and STALE_INPUT (bit 4) flags
        fu = _make_feature_update(quality_flags=QUALITY_FLAG_GAP | QUALITY_FLAG_STALE_INPUT)
        stub._record_feature_metrics(_make_tick_event(), fu, 0)
        # Two quality flag labels should be emitted
        assert registry.feature_quality_flags_total.labels.call_count == 2
        labels_calls = [c.kwargs["flag"] for c in registry.feature_quality_flags_total.labels.call_args_list]
        assert "gap" in labels_calls
        assert "stale_input" in labels_calls

    def test_no_quality_flags_when_zero(self):
        registry = MagicMock()
        registry.feature_plane_updates_total.labels.return_value = MagicMock()
        stub = _make_stub(metrics_registry=registry)
        stub._feature_metrics_sample_every = 1
        fu = _make_feature_update(quality_flags=0)
        stub._record_feature_metrics(_make_tick_event(), fu, 0)
        registry.feature_quality_flags_total.labels.assert_not_called()


# ---------------------------------------------------------------------------
# _record_feature_error_metric
# ---------------------------------------------------------------------------


class TestRecordFeatureErrorMetric:
    def test_increments_metrics_counter(self):
        stub = _make_stub()
        stub._record_feature_error_metric()
        assert stub._feature_metrics_counter == 1

    def test_emits_error_metric_at_sample_boundary(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_plane_updates_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._feature_metrics_sample_every = 1
        stub._record_feature_error_metric()
        registry.feature_plane_updates_total.labels.assert_called_once_with(
            result="error",
            feature_set=stub._feature_set_id_cached,
        )
        child.inc.assert_called_once()

    def test_no_op_without_registry(self):
        stub = _make_stub()
        stub._feature_metrics_sample_every = 1
        stub._record_feature_error_metric()
        # no exception, just counter increment
        assert stub._feature_metrics_counter == 1


# ---------------------------------------------------------------------------
# _emit_feature_shadow_check_metric
# ---------------------------------------------------------------------------


class TestEmitFeatureShadowCheckMetric:
    def test_no_op_when_no_registry(self):
        stub = _make_stub()
        stub._emit_feature_shadow_check_metric("checked")

    def test_no_op_when_registry_lacks_attribute(self):
        registry = MagicMock(spec=[])
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_check_metric("checked")

    def test_increments_check_metric(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_shadow_parity_checks_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_check_metric("checked")
        registry.feature_shadow_parity_checks_total.labels.assert_called_once_with(
            feature_set="lob_shared_v3", result="checked"
        )
        child.inc.assert_called_once()

    def test_caches_child_metric_across_calls(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_shadow_parity_checks_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_check_metric("checked")
        stub._emit_feature_shadow_check_metric("checked")
        assert registry.feature_shadow_parity_checks_total.labels.call_count == 1
        assert child.inc.call_count == 2


# ---------------------------------------------------------------------------
# _emit_feature_shadow_mismatch_metric
# ---------------------------------------------------------------------------


class TestEmitFeatureShadowMismatchMetric:
    def test_no_op_when_no_registry(self):
        stub = _make_stub()
        stub._emit_feature_shadow_mismatch_metric("lob_shared_v3", "ofi_l1")

    def test_no_op_when_registry_lacks_attribute(self):
        registry = MagicMock(spec=[])
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_mismatch_metric("lob_shared_v3", "ofi_l1")

    def test_increments_mismatch_metric(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_shadow_parity_mismatch_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_mismatch_metric("lob_shared_v3", "ofi_l1")
        registry.feature_shadow_parity_mismatch_total.labels.assert_called_once_with(
            feature_set="lob_shared_v3", feature_id="ofi_l1"
        )
        child.inc.assert_called_once()

    def test_caches_child_metric_across_calls(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_shadow_parity_mismatch_total.labels.return_value = child
        stub = _make_stub(metrics_registry=registry)
        stub._emit_feature_shadow_mismatch_metric("lob_shared_v3", "ofi_l1")
        stub._emit_feature_shadow_mismatch_metric("lob_shared_v3", "ofi_l1")
        assert registry.feature_shadow_parity_mismatch_total.labels.call_count == 1
        assert child.inc.call_count == 2


# ---------------------------------------------------------------------------
# _maybe_run_feature_shadow_parity
# ---------------------------------------------------------------------------


class TestMaybeRunFeatureShadowParity:
    def test_no_op_when_shadow_engine_is_none(self):
        stub = _make_stub()
        assert stub._feature_shadow_engine is None
        event = _make_tick_event()
        stub._maybe_run_feature_shadow_parity(event, _make_stats(), 0, None)
        # counter must remain 0
        assert stub._feature_shadow_counter == 0

    def test_increments_shadow_counter(self):
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(return_value=None)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 100  # skip compare
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, None)
        assert stub._feature_shadow_counter == 1

    def test_emits_check_metric_when_comparing(self):
        registry = MagicMock()
        child = MagicMock()
        registry.feature_shadow_parity_checks_total.labels.return_value = child
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(return_value=None)
        stub = _make_stub(metrics_registry=registry, _feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1  # compare every tick
        fu = _make_feature_update(values=(1.0,), feature_ids=("f1",))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, fu)
        child.inc.assert_called()

    def test_no_mismatch_when_values_match_integers(self):
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(100, 200), feature_ids=("f1", "f2"))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        primary_fu = _make_feature_update(values=(100, 200), feature_ids=("f1", "f2"))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        assert stub._feature_shadow_mismatch_counter == 0

    def test_detects_integer_mismatch(self):
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(100, 999), feature_ids=("f1", "f2"))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        primary_fu = _make_feature_update(values=(100, 200), feature_ids=("f1", "f2"))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        assert stub._feature_shadow_mismatch_counter == 1

    def test_no_mismatch_when_float_values_within_tolerance(self):
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(1.0 + 1e-9,), feature_ids=("f1",))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        stub._feature_shadow_abs_tolerance = 1e-6
        primary_fu = _make_feature_update(values=(1.0,), feature_ids=("f1",))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        assert stub._feature_shadow_mismatch_counter == 0

    def test_detects_float_mismatch_beyond_tolerance(self):
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(2.0,), feature_ids=("f1",))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        stub._feature_shadow_abs_tolerance = 1e-6
        primary_fu = _make_feature_update(values=(1.0,), feature_ids=("f1",))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        assert stub._feature_shadow_mismatch_counter == 1

    def test_id_mismatch_triggers_all_mismatch_metrics(self):
        registry = MagicMock()
        mismatch_child = MagicMock()
        registry.feature_shadow_parity_mismatch_total.labels.return_value = mismatch_child
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(1.0,), feature_ids=("different_id",))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(metrics_registry=registry, _feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        primary_fu = _make_feature_update(values=(1.0,), feature_ids=("f1",))
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        # Mismatch metrics emitted for each primary feature id
        registry.feature_shadow_parity_mismatch_total.labels.assert_called()

    def test_skips_compare_outside_sample_boundary(self):
        shadow = MagicMock()
        shadow_fu = _make_feature_update(values=(999,), feature_ids=("f1",))
        shadow.process_lob_update = MagicMock(return_value=shadow_fu)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 100  # very infrequent
        primary_fu = _make_feature_update(values=(1,), feature_ids=("f1",))
        # counter starts at 0, after call it is 1, which is not divisible by 100
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)
        assert stub._feature_shadow_mismatch_counter == 0

    def test_swallows_shadow_engine_exception(self):
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(side_effect=RuntimeError("crash"))
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        primary_fu = _make_feature_update()
        # Should not raise
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, primary_fu)

    def test_no_op_when_primary_values_none(self):
        shadow = MagicMock()
        shadow.process_lob_update = MagicMock(return_value=None)
        # Shadow has no get_feature_view either
        shadow.get_feature_view = MagicMock(return_value=None)
        stub = _make_stub(_feature_shadow_engine=shadow)
        stub._feature_shadow_sample_every = 1
        # Primary update is also None, and no feature engine to fall back to
        stub._maybe_run_feature_shadow_parity(_make_tick_event(), _make_stats(), 0, None)
        assert stub._feature_shadow_mismatch_counter == 0
