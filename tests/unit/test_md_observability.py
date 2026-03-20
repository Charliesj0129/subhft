"""Tests for MarketDataObservabilityMixin and helpers in _md_observability."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.events import FeatureUpdateEvent, MetaData, TickEvent
from hft_platform.services._md_observability import MarketDataObservabilityMixin

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mixin(**overrides: Any) -> MarketDataObservabilityMixin:
    """Build a mixin instance with all required attributes set to safe defaults."""
    obj = MarketDataObservabilityMixin()
    defaults: dict[str, Any] = {
        "feature_engine": None,
        "metrics_registry": None,
        "_trace_sampler": None,
        "_feature_latency_counter": 0,
        "_feature_metrics_counter": 0,
        "_feature_latency_sample_every": 1,
        "_feature_metrics_sample_every": 1,
        "_feature_latency_metric_child": None,
        "_feature_update_metric_children": {},
        "_feature_quality_flag_metric_children": {},
        "_feature_set_id_cached": "default",
        "_feature_shadow_engine": None,
        "_feature_shadow_counter": 0,
        "_feature_shadow_sample_every": 1,
        "_feature_shadow_abs_tolerance": 1e-6,
        "_feature_shadow_mismatch_counter": 0,
        "_feature_shadow_warn_every": 10,
        "_feature_shadow_checks_metric_children": {},
        "_feature_shadow_mismatch_metric_children": {},
    }
    defaults.update(overrides)
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


def _tick(symbol: str = "2330") -> TickEvent:
    meta = MetaData(seq=1, source_ts=100, local_ts=200)
    return TickEvent(meta=meta, symbol=symbol, price=100_0000, volume=10)


def _stats(best_bid: int = 100_0000, best_ask: int = 101_0000) -> SimpleNamespace:
    return SimpleNamespace(best_bid=best_bid, best_ask=best_ask)


def _feature_update(
    symbol: str = "2330",
    feature_set_id: str = "fs1",
    feature_ids: tuple[str, ...] = ("f1", "f2"),
    values: tuple[float | int, ...] = (1.0, 2.0),
    quality_flags: int = 0,
) -> FeatureUpdateEvent:
    return FeatureUpdateEvent(
        symbol=symbol,
        ts=100,
        local_ts=200,
        seq=1,
        feature_set_id=feature_set_id,
        schema_version=1,
        changed_mask=0,
        warmup_ready_mask=0,
        quality_flags=quality_flags,
        feature_ids=feature_ids,
        values=values,
    )


def _metric_child() -> MagicMock:
    child = MagicMock()
    child.inc = MagicMock()
    child.observe = MagicMock()
    return child


def _metrics_registry(**extra_attrs: Any) -> MagicMock:
    reg = MagicMock()
    reg.feature_plane_latency_ns = _metric_child()
    reg.feature_plane_updates_total = MagicMock()
    reg.feature_plane_updates_total.labels = MagicMock(return_value=_metric_child())
    reg.feature_quality_flags_total = MagicMock()
    reg.feature_quality_flags_total.labels = MagicMock(return_value=_metric_child())
    reg.shioaji_crash_signature_total = MagicMock()
    reg.shioaji_crash_signature_total.labels = MagicMock(return_value=_metric_child())
    reg.feature_shadow_parity_checks_total = MagicMock()
    reg.feature_shadow_parity_checks_total.labels = MagicMock(return_value=_metric_child())
    reg.feature_shadow_parity_mismatch_total = MagicMock()
    reg.feature_shadow_parity_mismatch_total.labels = MagicMock(return_value=_metric_child())
    for k, v in extra_attrs.items():
        setattr(reg, k, v)
    return reg


# ===================================================================
# _emit_trace
# ===================================================================


class TestEmitTrace:
    def test_noop_when_sampler_is_none(self) -> None:
        m = _make_mixin(_trace_sampler=None)
        # Should not raise
        m._emit_trace("stage", "tid", {"k": "v"})
        assert m._trace_sampler is None

    def test_calls_sampler_emit(self) -> None:
        sampler = MagicMock()
        m = _make_mixin(_trace_sampler=sampler)
        m._emit_trace("my_stage", "trace1", {"data": 42})
        sampler.emit.assert_called_once_with(stage="my_stage", trace_id="trace1", payload={"data": 42})

    def test_coerces_none_trace_id_to_empty_string(self) -> None:
        sampler = MagicMock()
        m = _make_mixin(_trace_sampler=sampler)
        m._emit_trace("stage", None, {})  # type: ignore[arg-type]
        sampler.emit.assert_called_once_with(stage="stage", trace_id="", payload={})

    def test_swallows_sampler_exception(self) -> None:
        sampler = MagicMock()
        sampler.emit.side_effect = RuntimeError("boom")
        m = _make_mixin(_trace_sampler=sampler)
        # Should not raise
        m._emit_trace("stage", "t", {})
        assert sampler.emit.called


# ===================================================================
# _record_shioaji_crash_signature
# ===================================================================


class TestRecordCrashSignature:
    def test_noop_without_metrics_registry(self) -> None:
        m = _make_mixin(metrics_registry=None)
        m._record_shioaji_crash_signature("api error", context="tick")
        assert True  # no exception

    def test_noop_when_text_is_none(self) -> None:
        reg = _metrics_registry()
        m = _make_mixin(metrics_registry=reg)
        m._record_shioaji_crash_signature(None, context="tick")
        reg.shioaji_crash_signature_total.labels.assert_not_called()

    def test_noop_when_no_signature_detected(self) -> None:
        reg = _metrics_registry()
        m = _make_mixin(metrics_registry=reg)
        with patch(
            "hft_platform.services._md_observability.detect_crash_signature",
            return_value=None,
        ):
            m._record_shioaji_crash_signature("normal text", context="tick")
        reg.shioaji_crash_signature_total.labels.assert_not_called()

    def test_increments_metric_on_signature(self) -> None:
        reg = _metrics_registry()
        child = _metric_child()
        reg.shioaji_crash_signature_total.labels.return_value = child
        m = _make_mixin(metrics_registry=reg)
        with patch(
            "hft_platform.services._md_observability.detect_crash_signature",
            return_value="disconnect",
        ):
            m._record_shioaji_crash_signature("Connection lost", context="bidask")
        reg.shioaji_crash_signature_total.labels.assert_called_once_with(signature="disconnect", context="bidask")
        child.inc.assert_called_once()

    def test_swallows_metric_exception(self) -> None:
        reg = _metrics_registry()
        reg.shioaji_crash_signature_total.labels.side_effect = RuntimeError("oops")
        m = _make_mixin(metrics_registry=reg)
        with patch(
            "hft_platform.services._md_observability.detect_crash_signature",
            return_value="disconnect",
        ):
            m._record_shioaji_crash_signature("err", context="tick")
        assert True  # no exception


# ===================================================================
# _maybe_update_features
# ===================================================================


class TestMaybeUpdateFeatures:
    def test_returns_none_when_no_feature_engine(self) -> None:
        m = _make_mixin(feature_engine=None)
        result = m._maybe_update_features(_tick(), _stats())
        assert result is None

    def test_returns_none_when_stats_is_none(self) -> None:
        m = _make_mixin(feature_engine=MagicMock())
        result = m._maybe_update_features(_tick(), None)
        assert result is None

    def test_returns_none_when_stats_lacks_best_bid(self) -> None:
        m = _make_mixin(feature_engine=MagicMock())
        result = m._maybe_update_features(_tick(), SimpleNamespace(best_ask=1))
        assert result is None

    def test_returns_none_when_stats_lacks_best_ask(self) -> None:
        m = _make_mixin(feature_engine=MagicMock())
        result = m._maybe_update_features(_tick(), SimpleNamespace(best_bid=1))
        assert result is None

    def test_calls_process_lob_update_when_available(self) -> None:
        fu = _feature_update()
        engine = MagicMock()
        engine.process_lob_update = MagicMock(return_value=fu)
        m = _make_mixin(feature_engine=engine, metrics_registry=None)
        result = m._maybe_update_features(_tick(), _stats())
        assert result is fu
        engine.process_lob_update.assert_called_once()

    def test_falls_back_to_process_lob_stats(self) -> None:
        fu = _feature_update()
        engine = MagicMock(spec=[])  # no process_lob_update attribute
        engine.process_lob_stats = MagicMock(return_value=fu)
        m = _make_mixin(feature_engine=engine, metrics_registry=None)
        result = m._maybe_update_features(_tick(), _stats())
        assert result is fu

    def test_returns_none_on_engine_exception(self) -> None:
        engine = MagicMock()
        engine.process_lob_update.side_effect = RuntimeError("boom")
        m = _make_mixin(feature_engine=engine, metrics_registry=None)
        result = m._maybe_update_features(_tick(), _stats())
        assert result is None

    def test_error_increments_feature_metrics_counter(self) -> None:
        engine = MagicMock()
        engine.process_lob_update.side_effect = RuntimeError("boom")
        m = _make_mixin(
            feature_engine=engine,
            metrics_registry=None,
            _feature_metrics_counter=0,
        )
        m._maybe_update_features(_tick(), _stats())
        # _record_feature_error_metric increments the counter
        assert m._feature_metrics_counter >= 1


# ===================================================================
# _record_feature_metrics
# ===================================================================


class TestRecordFeatureMetrics:
    def test_increments_counters(self) -> None:
        m = _make_mixin(
            _feature_latency_counter=0,
            _feature_metrics_counter=0,
            metrics_registry=None,
        )
        m._record_feature_metrics(_tick(), None, 0)
        assert m._feature_latency_counter == 1
        assert m._feature_metrics_counter == 1

    def test_observes_latency_on_sample(self) -> None:
        reg = _metrics_registry()
        latency_child = _metric_child()
        reg.feature_plane_latency_ns = latency_child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_latency_counter=0,
            _feature_latency_sample_every=1,
            _feature_latency_metric_child=None,
        )
        m._record_feature_metrics(_tick(), None, 0)
        assert m._feature_latency_metric_child is latency_child
        latency_child.observe.assert_called_once()

    def test_skips_latency_when_not_sample_time(self) -> None:
        reg = _metrics_registry()
        latency_child = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_latency_counter=0,
            _feature_latency_sample_every=10,
            _feature_latency_metric_child=latency_child,
        )
        m._record_feature_metrics(_tick(), None, 0)
        # counter becomes 1, 1 % 10 != 0, so no observe
        latency_child.observe.assert_not_called()

    def test_records_emitted_result_when_feature_update_present(self) -> None:
        reg = _metrics_registry()
        update_child = _metric_child()
        reg.feature_plane_updates_total.labels.return_value = update_child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
        )
        fu = _feature_update(feature_set_id="fs-x")
        m._record_feature_metrics(_tick(), fu, 0)
        reg.feature_plane_updates_total.labels.assert_called_with(result="emitted", feature_set="fs-x")
        update_child.inc.assert_called_once()

    def test_records_updated_result_when_feature_update_none(self) -> None:
        reg = _metrics_registry()
        update_child = _metric_child()
        reg.feature_plane_updates_total.labels.return_value = update_child
        m = _make_mixin(
            metrics_registry=reg,
            feature_engine=None,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
        )
        m._record_feature_metrics(_tick(), None, 0)
        reg.feature_plane_updates_total.labels.assert_called_with(result="updated", feature_set="default")
        update_child.inc.assert_called_once()

    def test_caches_feature_set_id(self) -> None:
        reg = _metrics_registry()
        reg.feature_plane_updates_total.labels.return_value = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
            _feature_set_id_cached="old",
        )
        fu = _feature_update(feature_set_id="new-set")
        m._record_feature_metrics(_tick(), fu, 0)
        assert m._feature_set_id_cached == "new-set"

    def test_records_quality_flags(self) -> None:
        reg = _metrics_registry()
        reg.feature_plane_updates_total.labels.return_value = _metric_child()
        flag_child = _metric_child()
        reg.feature_quality_flags_total.labels.return_value = flag_child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
        )
        # Use quality_flags=1 which should match the first flag bit
        fu = _feature_update(quality_flags=1)
        m._record_feature_metrics(_tick(), fu, 0)
        assert flag_child.inc.called

    def test_no_quality_flags_skips_flag_metric(self) -> None:
        reg = _metrics_registry()
        reg.feature_plane_updates_total.labels.return_value = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
        )
        fu = _feature_update(quality_flags=0)
        m._record_feature_metrics(_tick(), fu, 0)
        reg.feature_quality_flags_total.labels.assert_not_called()

    def test_reuses_cached_metric_children(self) -> None:
        reg = _metrics_registry()
        cached_child = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
            _feature_update_metric_children={("emitted", "fs1"): cached_child},
        )
        fu = _feature_update(feature_set_id="fs1")
        m._record_feature_metrics(_tick(), fu, 0)
        # Should reuse cached child, not call labels again
        cached_child.inc.assert_called_once()

    def test_uses_feature_view_when_update_none(self) -> None:
        """When feature_update is None, tries to get view from engine."""
        reg = _metrics_registry()
        reg.feature_plane_updates_total.labels.return_value = _metric_child()
        engine = MagicMock()
        engine.get_feature_view.return_value = {"quality_flags": 3, "feature_set_id": "v1"}
        m = _make_mixin(
            metrics_registry=reg,
            feature_engine=engine,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
        )
        m._record_feature_metrics(_tick(), None, 0)
        engine.get_feature_view.assert_called_once_with("2330")


# ===================================================================
# _record_feature_error_metric
# ===================================================================


class TestRecordFeatureErrorMetric:
    def test_increments_counter(self) -> None:
        m = _make_mixin(metrics_registry=None, _feature_metrics_counter=0)
        m._record_feature_error_metric()
        assert m._feature_metrics_counter == 1

    def test_records_error_on_sample(self) -> None:
        reg = _metrics_registry()
        error_child = _metric_child()
        reg.feature_plane_updates_total.labels.return_value = error_child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
            _feature_set_id_cached="fs-err",
        )
        m._record_feature_error_metric()
        reg.feature_plane_updates_total.labels.assert_called_with(result="error", feature_set="fs-err")
        error_child.inc.assert_called_once()

    def test_skips_when_not_sample_time(self) -> None:
        reg = _metrics_registry()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=10,
        )
        m._record_feature_error_metric()
        reg.feature_plane_updates_total.labels.assert_not_called()

    def test_caches_error_metric_child(self) -> None:
        reg = _metrics_registry()
        child = _metric_child()
        reg.feature_plane_updates_total.labels.return_value = child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_metrics_counter=0,
            _feature_metrics_sample_every=1,
            _feature_set_id_cached="fs1",
        )
        m._record_feature_error_metric()
        assert ("error", "fs1") in m._feature_update_metric_children


# ===================================================================
# _emit_feature_shadow_check_metric
# ===================================================================


class TestEmitFeatureShadowCheckMetric:
    def test_noop_without_registry(self) -> None:
        m = _make_mixin(metrics_registry=None)
        m._emit_feature_shadow_check_metric("checked")
        assert True

    def test_creates_and_caches_child(self) -> None:
        reg = _metrics_registry()
        child = _metric_child()
        reg.feature_shadow_parity_checks_total.labels.return_value = child
        m = _make_mixin(
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        m._emit_feature_shadow_check_metric("checked")
        reg.feature_shadow_parity_checks_total.labels.assert_called_once_with(feature_set="fs1", result="checked")
        child.inc.assert_called_once()
        assert ("fs1", "checked") in m._feature_shadow_checks_metric_children

    def test_reuses_cached_child(self) -> None:
        reg = _metrics_registry()
        cached_child = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
            _feature_shadow_checks_metric_children={("fs1", "skipped"): cached_child},
        )
        m._emit_feature_shadow_check_metric("skipped")
        reg.feature_shadow_parity_checks_total.labels.assert_not_called()
        cached_child.inc.assert_called_once()

    def test_noop_when_attr_missing(self) -> None:
        reg = MagicMock(spec=[])  # no feature_shadow_parity_checks_total
        m = _make_mixin(metrics_registry=reg)
        m._emit_feature_shadow_check_metric("checked")
        assert True


# ===================================================================
# _emit_feature_shadow_mismatch_metric
# ===================================================================


class TestEmitFeatureShadowMismatchMetric:
    def test_noop_without_registry(self) -> None:
        m = _make_mixin(metrics_registry=None)
        m._emit_feature_shadow_mismatch_metric("fs1", "f1")
        assert True

    def test_creates_and_caches_child(self) -> None:
        reg = _metrics_registry()
        child = _metric_child()
        reg.feature_shadow_parity_mismatch_total.labels.return_value = child
        m = _make_mixin(metrics_registry=reg)
        m._emit_feature_shadow_mismatch_metric("fs1", "f1")
        reg.feature_shadow_parity_mismatch_total.labels.assert_called_once_with(feature_set="fs1", feature_id="f1")
        child.inc.assert_called_once()
        assert ("fs1", "f1") in m._feature_shadow_mismatch_metric_children

    def test_reuses_cached_child(self) -> None:
        reg = _metrics_registry()
        cached = _metric_child()
        m = _make_mixin(
            metrics_registry=reg,
            _feature_shadow_mismatch_metric_children={("fs1", "f1"): cached},
        )
        m._emit_feature_shadow_mismatch_metric("fs1", "f1")
        reg.feature_shadow_parity_mismatch_total.labels.assert_not_called()
        cached.inc.assert_called_once()

    def test_noop_when_attr_missing(self) -> None:
        reg = MagicMock(spec=[])
        m = _make_mixin(metrics_registry=reg)
        m._emit_feature_shadow_mismatch_metric("fs1", "f1")
        assert True

    def test_swallows_exception(self) -> None:
        reg = _metrics_registry()
        reg.feature_shadow_parity_mismatch_total.labels.side_effect = RuntimeError
        m = _make_mixin(metrics_registry=reg)
        m._emit_feature_shadow_mismatch_metric("fs1", "f1")
        assert True


# ===================================================================
# _maybe_run_feature_shadow_parity
# ===================================================================


class TestMaybeRunFeatureShadowParity:
    def test_noop_when_no_shadow_engine(self) -> None:
        m = _make_mixin(_feature_shadow_engine=None)
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, None)
        assert m._feature_shadow_counter == 0

    def test_increments_shadow_counter(self) -> None:
        shadow = MagicMock()
        shadow.process_lob_update.return_value = None
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=100,
            metrics_registry=None,
        )
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, None)
        assert m._feature_shadow_counter == 1

    def test_skips_comparison_when_not_sample_time(self) -> None:
        shadow = MagicMock()
        shadow.process_lob_update.return_value = _feature_update()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=10,
            metrics_registry=None,
        )
        # counter becomes 1, 1 % 10 != 0 -> no comparison
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, _feature_update())
        assert m._feature_shadow_counter == 1

    def test_detects_matching_values(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(values=(1.0, 2.0), feature_ids=("f1", "f2"))
        shadow.process_lob_update.return_value = shadow_fu
        reg = _metrics_registry()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_abs_tolerance=1e-6,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        primary_fu = _feature_update(values=(1.0, 2.0), feature_ids=("f1", "f2"))
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, primary_fu)
        # No mismatch
        assert m._feature_shadow_mismatch_counter == 0

    def test_detects_float_mismatch(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(values=(1.0, 99.0), feature_ids=("f1", "f2"))
        shadow.process_lob_update.return_value = shadow_fu
        reg = _metrics_registry()
        reg.feature_shadow_parity_mismatch_total.labels.return_value = _metric_child()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_abs_tolerance=1e-6,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        primary_fu = _feature_update(values=(1.0, 2.0), feature_ids=("f1", "f2"))
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, primary_fu)
        assert m._feature_shadow_mismatch_counter == 1

    def test_detects_int_mismatch(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(values=(1, 99), feature_ids=("f1", "f2"))
        shadow.process_lob_update.return_value = shadow_fu
        reg = _metrics_registry()
        reg.feature_shadow_parity_mismatch_total.labels.return_value = _metric_child()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_abs_tolerance=1e-6,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        primary_fu = _feature_update(values=(1, 2), feature_ids=("f1", "f2"))
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, primary_fu)
        assert m._feature_shadow_mismatch_counter == 1

    def test_handles_mismatched_feature_ids(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(feature_ids=("a", "b"))
        shadow.process_lob_update.return_value = shadow_fu
        reg = _metrics_registry()
        reg.feature_shadow_parity_mismatch_total.labels.return_value = _metric_child()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        primary_fu = _feature_update(feature_ids=("f1", "f2"))
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, primary_fu)
        # Mismatched IDs should emit mismatch metrics for primary features
        assert reg.feature_shadow_parity_mismatch_total.labels.called

    def test_shadow_engine_exception_emits_skipped(self) -> None:
        shadow = MagicMock()
        shadow.process_lob_update.side_effect = RuntimeError("shadow crash")
        reg = _metrics_registry()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, None)
        # Should emit "skipped" check metric
        reg.feature_shadow_parity_checks_total.labels.assert_called_with(feature_set="fs1", result="skipped")

    def test_uses_get_feature_view_when_primary_update_none(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(values=(1.0,), feature_ids=("f1",))
        shadow.process_lob_update.return_value = shadow_fu
        engine = MagicMock()
        engine.get_feature_view.return_value = {
            "values": (1.0,),
            "feature_ids": ("f1",),
            "feature_set_id": "fs1",
        }
        reg = _metrics_registry()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            feature_engine=engine,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, None)
        engine.get_feature_view.assert_called_once_with("2330")
        # Values match, so no mismatch
        assert m._feature_shadow_mismatch_counter == 0

    def test_returns_when_primary_values_none(self) -> None:
        """When primary has no update and engine view is missing, skip comparison."""
        shadow = MagicMock()
        shadow.process_lob_update.return_value = _feature_update()
        reg = _metrics_registry()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            feature_engine=None,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, None)
        assert m._feature_shadow_mismatch_counter == 0

    def test_within_tolerance_no_mismatch(self) -> None:
        shadow = MagicMock()
        shadow_fu = _feature_update(values=(1.0000001,), feature_ids=("f1",))
        shadow.process_lob_update.return_value = shadow_fu
        reg = _metrics_registry()
        reg.feature_shadow_parity_checks_total.labels.return_value = _metric_child()
        m = _make_mixin(
            _feature_shadow_engine=shadow,
            _feature_shadow_counter=0,
            _feature_shadow_sample_every=1,
            _feature_shadow_abs_tolerance=1e-4,
            _feature_shadow_mismatch_counter=0,
            metrics_registry=reg,
            _feature_set_id_cached="fs1",
        )
        primary_fu = _feature_update(values=(1.0,), feature_ids=("f1",))
        m._maybe_run_feature_shadow_parity(_tick(), _stats(), 0, primary_fu)
        assert m._feature_shadow_mismatch_counter == 0


# ===================================================================
# _init_feature_shadow_engine
# ===================================================================


class TestInitFeatureShadowEngine:
    def test_noop_when_no_feature_engine(self) -> None:
        m = _make_mixin(feature_engine=None)
        m._init_feature_shadow_engine()
        assert m._feature_shadow_engine is None

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "0"})
    def test_noop_when_disabled(self) -> None:
        m = _make_mixin(feature_engine=MagicMock())
        m._init_feature_shadow_engine()
        assert m._feature_shadow_engine is None

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "1"})
    def test_creates_shadow_engine(self) -> None:
        engine = MagicMock()
        engine.kernel_backend.return_value = "python"
        engine.feature_set_id.return_value = "fs1"
        with patch("hft_platform.services._md_observability.FeatureEngine") as FECls:
            shadow_inst = MagicMock()
            shadow_inst.kernel_backend.return_value = "rust"
            FECls.return_value = shadow_inst
            m = _make_mixin(feature_engine=engine)
            m._init_feature_shadow_engine()
            assert m._feature_shadow_engine is shadow_inst
            FECls.assert_called_once_with(
                feature_set_id="fs1",
                emit_events=True,
                kernel_backend="rust",
            )

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "1", "HFT_FEATURE_SHADOW_BACKEND": "python"})
    def test_uses_explicit_backend(self) -> None:
        engine = MagicMock()
        engine.kernel_backend.return_value = "rust"
        engine.feature_set_id.return_value = "fs1"
        with patch("hft_platform.services._md_observability.FeatureEngine") as FECls:
            shadow_inst = MagicMock()
            shadow_inst.kernel_backend.return_value = "python"
            FECls.return_value = shadow_inst
            m = _make_mixin(feature_engine=engine)
            m._init_feature_shadow_engine()
            FECls.assert_called_once_with(
                feature_set_id="fs1",
                emit_events=True,
                kernel_backend="python",
            )

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "1"})
    def test_skips_when_same_backend_auto(self) -> None:
        """When no explicit backend and shadow resolves to same as primary, skip."""
        engine = MagicMock()
        engine.kernel_backend.return_value = "python"
        engine.feature_set_id.return_value = "fs1"
        with patch("hft_platform.services._md_observability.FeatureEngine") as FECls:
            shadow_inst = MagicMock()
            shadow_inst.kernel_backend.return_value = "python"
            FECls.return_value = shadow_inst
            m = _make_mixin(feature_engine=engine)
            m._init_feature_shadow_engine()
            # Should skip because both are python and no explicit backend
            assert m._feature_shadow_engine is None

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "1"})
    def test_handles_init_failure(self) -> None:
        engine = MagicMock()
        engine.kernel_backend.return_value = "python"
        engine.feature_set_id.return_value = "fs1"
        with patch("hft_platform.services._md_observability.FeatureEngine") as FECls:
            FECls.side_effect = RuntimeError("init failed")
            m = _make_mixin(feature_engine=engine)
            m._init_feature_shadow_engine()
            assert m._feature_shadow_engine is None

    @patch.dict("os.environ", {"HFT_FEATURE_SHADOW_PARITY": "1"})
    def test_handles_kernel_backend_exception(self) -> None:
        engine = MagicMock()
        engine.kernel_backend.side_effect = RuntimeError("no backend")
        engine.feature_set_id.return_value = None
        with patch("hft_platform.services._md_observability.FeatureEngine") as FECls:
            shadow_inst = MagicMock()
            shadow_inst.kernel_backend.return_value = "rust"
            FECls.return_value = shadow_inst
            m = _make_mixin(feature_engine=engine)
            m._init_feature_shadow_engine()
            # Falls back to "python" primary, so shadow = "rust"
            FECls.assert_called_once_with(
                feature_set_id=None,
                emit_events=True,
                kernel_backend="rust",
            )


# ===================================================================
# _SupportsObservability Protocol (smoke)
# ===================================================================


class TestSupportsObservabilityProtocol:
    def test_mixin_has_expected_methods(self) -> None:
        m = _make_mixin()
        assert callable(getattr(m, "_emit_trace", None))
        assert callable(getattr(m, "_record_shioaji_crash_signature", None))
        assert callable(getattr(m, "_maybe_update_features", None))
        assert callable(getattr(m, "_record_feature_metrics", None))
        assert callable(getattr(m, "_record_feature_error_metric", None))
        assert callable(getattr(m, "_init_feature_shadow_engine", None))
        assert callable(getattr(m, "_maybe_run_feature_shadow_parity", None))
        assert callable(getattr(m, "_emit_feature_shadow_check_metric", None))
        assert callable(getattr(m, "_emit_feature_shadow_mismatch_metric", None))
