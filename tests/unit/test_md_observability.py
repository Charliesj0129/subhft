"""Tests for src/hft_platform/services/_md_observability.py."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

from hft_platform.services._md_observability import MarketDataObservabilityMixin


class FakeSvc(MarketDataObservabilityMixin):
    """Minimal host for the mixin under test."""

    def __init__(self) -> None:
        self.feature_engine: Any = None
        self._feature_shadow_engine: Any = None
        self.metrics_registry: Any = None
        self._trace_sampler: Any = None
        self._feature_latency_counter: int = 0
        self._feature_metrics_counter: int = 0
        self._feature_latency_sample_every: int = 1
        self._feature_metrics_sample_every: int = 1
        self._feature_latency_metric_child: Any = None
        self._feature_update_metric_children: dict[tuple[str, str], Any] = {}
        self._feature_quality_flag_metric_children: dict[str, Any] = {}
        self._feature_set_id_cached: str = "default"
        self._feature_shadow_counter: int = 0
        self._feature_shadow_sample_every: int = 1
        self._feature_shadow_abs_tolerance: float = 1e-6
        self._feature_shadow_mismatch_counter: int = 0
        self._feature_shadow_warn_every: int = 10
        self._feature_shadow_checks_metric_children: dict[tuple[str, str], Any] = {}
        self._feature_shadow_mismatch_metric_children: dict[tuple[str, str], Any] = {}


def _make_svc() -> FakeSvc:
    return FakeSvc()


# ---------------------------------------------------------------------------
# TestEmitTrace
# ---------------------------------------------------------------------------


class TestEmitTrace:
    """_emit_trace: trace sampling / no-op when sampler is absent."""

    def test_no_sampler_no_error(self) -> None:
        svc = _make_svc()
        svc._trace_sampler = None
        # Should silently return without error
        svc._emit_trace("stage_x", "trace-1", {"k": "v"})

    def test_sampler_called(self) -> None:
        svc = _make_svc()
        sampler = MagicMock()
        svc._trace_sampler = sampler
        svc._emit_trace("normalize", "t-42", {"sym": "2330"})
        sampler.emit.assert_called_once_with(
            stage="normalize",
            trace_id="t-42",
            payload={"sym": "2330"},
        )

    def test_sampler_exception_swallowed(self) -> None:
        svc = _make_svc()
        sampler = MagicMock()
        sampler.emit.side_effect = RuntimeError("boom")
        svc._trace_sampler = sampler
        # Should not raise
        svc._emit_trace("stage", "id", {})

    def test_none_trace_id_coerced(self) -> None:
        svc = _make_svc()
        sampler = MagicMock()
        svc._trace_sampler = sampler
        svc._emit_trace("s", None, {})  # type: ignore[arg-type]
        sampler.emit.assert_called_once_with(stage="s", trace_id="", payload={})


# ---------------------------------------------------------------------------
# TestRecordCrashSignature
# ---------------------------------------------------------------------------


class TestRecordCrashSignature:
    """_record_shioaji_crash_signature: metrics emission on crash pattern."""

    def test_no_metrics_no_error(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        svc._record_shioaji_crash_signature("some text", context="tick")

    def test_no_signature_returns(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        # None text -> detect_crash_signature returns None
        svc._record_shioaji_crash_signature(None, context="tick")
        registry.shioaji_crash_signature_total.labels.assert_not_called()

    def test_with_signature_calls_inc(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        with patch(
            "hft_platform.services._md_observability.detect_crash_signature",
            return_value="disconnect",
        ):
            svc._record_shioaji_crash_signature("connection lost", context="bidask")
        registry.shioaji_crash_signature_total.labels.assert_called_once_with(
            signature="disconnect",
            context="bidask",
        )
        registry.shioaji_crash_signature_total.labels().inc.assert_called_once()

    def test_missing_metric_attr_no_error(self) -> None:
        svc = _make_svc()
        registry = MagicMock(spec=[])  # no attributes at all
        svc.metrics_registry = registry
        svc._record_shioaji_crash_signature("text", context="tick")


# ---------------------------------------------------------------------------
# TestMaybeUpdateFeatures
# ---------------------------------------------------------------------------


class TestMaybeUpdateFeatures:
    """_maybe_update_features: feature engine invocation."""

    def test_no_engine_returns_none(self) -> None:
        svc = _make_svc()
        svc.feature_engine = None
        event = MagicMock()
        result = svc._maybe_update_features(event, MagicMock())
        assert result is None

    def test_no_stats_returns_none(self) -> None:
        svc = _make_svc()
        svc.feature_engine = MagicMock()
        result = svc._maybe_update_features(MagicMock(), None)
        assert result is None

    def test_stats_missing_best_bid_returns_none(self) -> None:
        svc = _make_svc()
        svc.feature_engine = MagicMock()
        stats = MagicMock(spec=[])  # no best_bid / best_ask
        result = svc._maybe_update_features(MagicMock(), stats)
        assert result is None

    def test_process_lob_update_called(self) -> None:
        svc = _make_svc()
        engine = MagicMock()
        expected = MagicMock()
        engine.process_lob_update.return_value = expected
        svc.feature_engine = engine

        stats = MagicMock()
        stats.best_bid = 100
        stats.best_ask = 101
        event = MagicMock()
        event.meta.local_ts = 1000

        result = svc._maybe_update_features(event, stats)
        assert result is expected
        engine.process_lob_update.assert_called_once()

    def test_fallback_process_lob_stats(self) -> None:
        svc = _make_svc()
        engine = MagicMock(spec=["process_lob_stats"])
        expected = MagicMock()
        engine.process_lob_stats.return_value = expected
        svc.feature_engine = engine

        stats = MagicMock()
        stats.best_bid = 100
        stats.best_ask = 101
        event = MagicMock()
        event.meta.local_ts = 0

        result = svc._maybe_update_features(event, stats)
        assert result is expected

    def test_engine_exception_returns_none(self) -> None:
        svc = _make_svc()
        engine = MagicMock()
        engine.process_lob_update.side_effect = ValueError("bad")
        svc.feature_engine = engine

        stats = MagicMock()
        stats.best_bid = 1
        stats.best_ask = 2
        event = MagicMock()
        event.meta.local_ts = 0

        result = svc._maybe_update_features(event, stats)
        assert result is None


# ---------------------------------------------------------------------------
# TestRecordFeatureMetrics
# ---------------------------------------------------------------------------


class TestRecordFeatureMetrics:
    """_record_feature_metrics: counter bumps and metric calls."""

    def test_no_metrics_increments_counters(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        start = time.perf_counter_ns()
        svc._record_feature_metrics(MagicMock(), None, start)
        assert svc._feature_latency_counter == 1
        assert svc._feature_metrics_counter == 1

    def test_with_metrics_observes_latency(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        start = time.perf_counter_ns()
        svc._record_feature_metrics(MagicMock(), None, start)
        # Should have attempted to observe latency
        assert svc._feature_latency_counter == 1

    def test_with_feature_update_records_emitted(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        update = MagicMock()
        update.feature_set_id = "fs-1"
        update.quality_flags = 0
        start = time.perf_counter_ns()
        svc._record_feature_metrics(MagicMock(), update, start)
        assert svc._feature_set_id_cached == "fs-1"

    def test_record_feature_error_metric_no_registry(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        svc._record_feature_error_metric()
        assert svc._feature_metrics_counter == 1

    def test_record_feature_error_metric_with_registry(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        svc._record_feature_error_metric()
        assert svc._feature_metrics_counter == 1


# ---------------------------------------------------------------------------
# TestShadowMetrics
# ---------------------------------------------------------------------------


class TestShadowMetrics:
    """_emit_feature_shadow_check_metric / _emit_feature_shadow_mismatch_metric."""

    def test_check_metric_no_registry(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        # Should not raise
        svc._emit_feature_shadow_check_metric("checked")

    def test_check_metric_missing_attr(self) -> None:
        svc = _make_svc()
        registry = MagicMock(spec=[])
        svc.metrics_registry = registry
        svc._emit_feature_shadow_check_metric("checked")

    def test_check_metric_calls_inc(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        svc._emit_feature_shadow_check_metric("checked")
        registry.feature_shadow_parity_checks_total.labels.assert_called_once_with(
            feature_set="default",
            result="checked",
        )
        child = registry.feature_shadow_parity_checks_total.labels()
        child.inc.assert_called()

    def test_check_metric_caches_child(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        svc._emit_feature_shadow_check_metric("checked")
        svc._emit_feature_shadow_check_metric("checked")
        # labels() called once for creation, then child reused
        assert ("default", "checked") in svc._feature_shadow_checks_metric_children

    def test_mismatch_metric_no_registry(self) -> None:
        svc = _make_svc()
        svc.metrics_registry = None
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")

    def test_mismatch_metric_missing_attr(self) -> None:
        svc = _make_svc()
        registry = MagicMock(spec=[])
        svc.metrics_registry = registry
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")

    def test_mismatch_metric_calls_inc(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")
        registry.feature_shadow_parity_mismatch_total.labels.assert_called_once_with(
            feature_set="fs-1",
            feature_id="feat-a",
        )

    def test_mismatch_metric_caches_child(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        svc.metrics_registry = registry
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")
        assert ("fs-1", "feat-a") in svc._feature_shadow_mismatch_metric_children

    def test_check_metric_exception_swallowed(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        registry.feature_shadow_parity_checks_total.labels.side_effect = RuntimeError
        svc.metrics_registry = registry
        # Should not raise
        svc._emit_feature_shadow_check_metric("checked")

    def test_mismatch_metric_exception_swallowed(self) -> None:
        svc = _make_svc()
        registry = MagicMock()
        registry.feature_shadow_parity_mismatch_total.labels.side_effect = RuntimeError
        svc.metrics_registry = registry
        # Should not raise
        svc._emit_feature_shadow_mismatch_metric("fs-1", "feat-a")
