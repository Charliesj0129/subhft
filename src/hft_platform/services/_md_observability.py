"""Market data observability helpers: metrics emission, trace sampling, shadow parity.

Private module — imported only by ``market_data.py``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

from structlog import get_logger

from hft_platform.events import BidAskEvent, FeatureUpdateEvent, LOBStatsEvent, TickEvent
from hft_platform.feed_adapter.shioaji.signatures import detect_crash_signature

from ._md_ingestion import _FEATURE_QUALITY_FLAG_LABELS

if TYPE_CHECKING:
    from hft_platform.feature.engine import FeatureEngine
    from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("service.market_data")


# ---------------------------------------------------------------------------
# Mixin: observability methods extracted from MarketDataService
# ---------------------------------------------------------------------------


class MarketDataObservabilityMixin:
    """Methods for metrics, tracing, and feature-shadow parity checks.

    Designed to be mixed into ``MarketDataService``.
    All instance attributes referenced here are set in ``MarketDataService.__init__``.
    """

    # -- trace helpers -------------------------------------------------------

    def _emit_trace(self, stage: str, trace_id: str, payload: dict[str, Any]) -> None:
        sampler = getattr(self, "_trace_sampler", None)
        if sampler is None:
            return
        try:
            sampler.emit(stage=stage, trace_id=str(trace_id or ""), payload=payload)
        except Exception:
            return

    # -- crash signature recording -------------------------------------------

    def _record_shioaji_crash_signature(self, text: str | None, *, context: str) -> None:
        metrics_registry: MetricsRegistry | None = getattr(self, "metrics_registry", None)
        if not metrics_registry or not hasattr(metrics_registry, "shioaji_crash_signature_total"):
            return
        signature = detect_crash_signature(text)
        if not signature:
            return
        try:
            metrics_registry.shioaji_crash_signature_total.labels(signature=signature, context=context).inc()
        except Exception:
            return

    # -- feature engine update -----------------------------------------------

    def _maybe_update_features(
        self,
        event: TickEvent | BidAskEvent,
        stats: object | None,
    ) -> FeatureUpdateEvent | None:
        feature_engine: FeatureEngine | None = getattr(self, "feature_engine", None)
        if feature_engine is None or stats is None:
            return None
        if not hasattr(stats, "best_bid") or not hasattr(stats, "best_ask"):
            return None
        meta = getattr(event, "meta", None)
        local_ts_ns = int(getattr(meta, "local_ts", 0) or 0) if meta is not None else 0
        start_ns = time.perf_counter_ns()
        try:
            process_lob_update = getattr(feature_engine, "process_lob_update", None)
            if callable(process_lob_update):
                feature_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
            else:
                feature_update = feature_engine.process_lob_stats(cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns)
            self._maybe_run_feature_shadow_parity(event, stats, local_ts_ns, feature_update)
            self._record_feature_metrics(event, feature_update, start_ns)
            return feature_update
        except Exception as exc:
            self._emit_trace(
                "feature_update_error",
                "",
                {"symbol": getattr(event, "symbol", ""), "reason": str(exc)},
            )
            self._record_feature_error_metric()
            logger.warning("feature_engine_update_failed", reason=str(exc))
            return None

    def _record_feature_metrics(  # noqa: C901
        self,
        event: TickEvent | BidAskEvent,
        feature_update: FeatureUpdateEvent | None,
        start_ns: int,
    ) -> None:
        self._feature_latency_counter += 1  # type: ignore[attr-defined]
        self._feature_metrics_counter += 1  # type: ignore[attr-defined]
        metrics_registry: MetricsRegistry | None = getattr(self, "metrics_registry", None)
        if not metrics_registry:
            return
        if self._feature_latency_counter % self._feature_latency_sample_every == 0:  # type: ignore[attr-defined]
            try:
                if self._feature_latency_metric_child is None and hasattr(  # type: ignore[attr-defined]
                    metrics_registry, "feature_plane_latency_ns"
                ):
                    self._feature_latency_metric_child = metrics_registry.feature_plane_latency_ns  # type: ignore[attr-defined]
                if self._feature_latency_metric_child is not None:  # type: ignore[attr-defined]
                    self._feature_latency_metric_child.observe(time.perf_counter_ns() - start_ns)  # type: ignore[attr-defined]
            except Exception:
                pass
        if self._feature_metrics_counter % self._feature_metrics_sample_every == 0:  # type: ignore[attr-defined]
            try:
                feature_engine: FeatureEngine | None = getattr(self, "feature_engine", None)
                if feature_update is not None:
                    feature_set_id = str(getattr(feature_update, "feature_set_id", self._feature_set_id_cached))  # type: ignore[attr-defined]
                    self._feature_set_id_cached = feature_set_id  # type: ignore[attr-defined]
                    result = "emitted"
                    qflags = int(getattr(feature_update, "quality_flags", 0) or 0)
                else:
                    feature_set_id = self._feature_set_id_cached  # type: ignore[attr-defined]
                    result = "updated"
                    qflags = 0
                    try:
                        if feature_engine and hasattr(feature_engine, "get_feature_view"):
                            state_view = feature_engine.get_feature_view(getattr(event, "symbol", ""))
                        else:
                            state_view = None
                    except Exception:
                        state_view = None
                    if isinstance(state_view, dict):
                        qflags = int(state_view.get("quality_flags", 0) or 0)
                if hasattr(metrics_registry, "feature_plane_updates_total"):
                    key = (result, feature_set_id)
                    child = self._feature_update_metric_children.get(key)  # type: ignore[attr-defined]
                    if child is None:
                        child = metrics_registry.feature_plane_updates_total.labels(
                            result=result,
                            feature_set=feature_set_id,
                        )
                        self._feature_update_metric_children[key] = child  # type: ignore[attr-defined]
                    child.inc()
                if qflags and hasattr(metrics_registry, "feature_quality_flags_total"):
                    for bit, label in _FEATURE_QUALITY_FLAG_LABELS:
                        if qflags & bit:
                            qchild = self._feature_quality_flag_metric_children.get(label)  # type: ignore[attr-defined]
                            if qchild is None:
                                qchild = metrics_registry.feature_quality_flags_total.labels(flag=label)
                                self._feature_quality_flag_metric_children[label] = qchild  # type: ignore[attr-defined]
                            qchild.inc()
            except Exception:
                pass

    def _record_feature_error_metric(self) -> None:
        self._feature_metrics_counter += 1  # type: ignore[attr-defined]
        metrics_registry: MetricsRegistry | None = getattr(self, "metrics_registry", None)
        if metrics_registry and self._feature_metrics_counter % self._feature_metrics_sample_every == 0:  # type: ignore[attr-defined]
            try:
                if hasattr(metrics_registry, "feature_plane_updates_total"):
                    key = ("error", self._feature_set_id_cached)  # type: ignore[attr-defined]
                    child = self._feature_update_metric_children.get(key)  # type: ignore[attr-defined]
                    if child is None:
                        child = metrics_registry.feature_plane_updates_total.labels(
                            result="error",
                            feature_set=self._feature_set_id_cached,  # type: ignore[attr-defined]
                        )
                        self._feature_update_metric_children[key] = child  # type: ignore[attr-defined]
                    child.inc()
            except Exception:
                pass

    # -- feature shadow parity -----------------------------------------------

    def _init_feature_shadow_engine(self) -> None:
        """Initialise the shadow feature engine for parity checking."""
        import os

        from hft_platform.feature.engine import FeatureEngine

        feature_engine: FeatureEngine | None = getattr(self, "feature_engine", None)
        if feature_engine is None:
            return
        enabled = os.getenv("HFT_FEATURE_SHADOW_PARITY", "0").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return
        try:
            primary_backend = feature_engine.kernel_backend() if hasattr(feature_engine, "kernel_backend") else "python"
        except Exception:
            primary_backend = "python"
        requested = os.getenv("HFT_FEATURE_SHADOW_BACKEND", "").strip().lower()
        shadow_backend = requested or ("rust" if primary_backend == "python" else "python")
        try:
            shadow = FeatureEngine(
                feature_set_id=(feature_engine.feature_set_id() if hasattr(feature_engine, "feature_set_id") else None),
                emit_events=True,
                kernel_backend=shadow_backend,
            )
            if (
                requested == ""
                and hasattr(shadow, "kernel_backend")
                and shadow.kernel_backend() == primary_backend == "python"
            ):
                return
            self._feature_shadow_engine = shadow  # type: ignore[attr-defined]
        except Exception as exc:
            logger.warning("feature_shadow_engine_init_failed", reason=str(exc))
            self._feature_shadow_engine = None  # type: ignore[attr-defined]

    def _maybe_run_feature_shadow_parity(  # noqa: C901
        self,
        event: TickEvent | BidAskEvent,
        stats: object,
        local_ts_ns: int,
        primary_update: FeatureUpdateEvent | None,
    ) -> None:
        shadow: FeatureEngine | None = getattr(self, "_feature_shadow_engine", None)
        if shadow is None:
            return
        self._feature_shadow_counter += 1  # type: ignore[attr-defined]
        compare_now = self._feature_shadow_counter % self._feature_shadow_sample_every == 0  # type: ignore[attr-defined]
        try:
            process_lob_update = getattr(shadow, "process_lob_update", None)
            if callable(process_lob_update):
                shadow_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
            else:
                shadow_update = shadow.process_lob_stats(cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns)
        except Exception as exc:
            logger.warning("feature_shadow_engine_update_failed", reason=str(exc))
            self._emit_feature_shadow_check_metric("skipped")
            return

        if not compare_now:
            return
        self._emit_feature_shadow_check_metric("checked")
        primary_feature_set = str(getattr(primary_update, "feature_set_id", self._feature_set_id_cached))  # type: ignore[attr-defined]
        primary_values = None
        primary_ids = None
        if primary_update is not None:
            primary_values = tuple(primary_update.values)
            primary_ids = tuple(primary_update.feature_ids)
        else:
            feature_engine: FeatureEngine | None = getattr(self, "feature_engine", None)
            try:
                view = feature_engine.get_feature_view(getattr(event, "symbol", "")) if feature_engine else None
            except Exception:
                view = None
            if isinstance(view, dict):
                primary_values = tuple(view.get("values", ()))
                primary_ids = tuple(view.get("feature_ids", ()))
                primary_feature_set = str(view.get("feature_set_id", primary_feature_set))

        shadow_values = None
        shadow_ids = None
        if shadow_update is not None:
            shadow_values = tuple(shadow_update.values)
            shadow_ids = tuple(shadow_update.feature_ids)
        else:
            try:
                sview = shadow.get_feature_view(getattr(event, "symbol", ""))
            except Exception:
                sview = None
            if isinstance(sview, dict):
                shadow_values = tuple(sview.get("values", ()))
                shadow_ids = tuple(sview.get("feature_ids", ()))

        if primary_values is None or shadow_values is None or primary_ids is None or shadow_ids is None:
            return
        if primary_ids != shadow_ids or len(primary_values) != len(shadow_values):
            for fid in primary_ids:
                self._emit_feature_shadow_mismatch_metric(primary_feature_set, str(fid))
            return
        mismatched: list[str] = []
        tol = float(self._feature_shadow_abs_tolerance)  # type: ignore[attr-defined]
        for fid, pv, sv in zip(primary_ids, primary_values, shadow_values, strict=False):
            if isinstance(pv, float) or isinstance(sv, float):
                if abs(float(pv) - float(sv)) > tol:
                    mismatched.append(str(fid))
            else:
                if int(pv) != int(sv):
                    mismatched.append(str(fid))
        if mismatched:
            self._feature_shadow_mismatch_counter += 1  # type: ignore[attr-defined]
            for fid in mismatched:
                self._emit_feature_shadow_mismatch_metric(primary_feature_set, fid)
            self._emit_trace(
                "feature_shadow_mismatch",
                "",
                {
                    "symbol": getattr(event, "symbol", ""),
                    "feature_set_id": primary_feature_set,
                    "mismatch_count": len(mismatched),
                    "mismatched_features": mismatched[:16],
                },
            )
            if self._feature_shadow_mismatch_counter % self._feature_shadow_warn_every == 1:  # type: ignore[attr-defined]
                logger.warning(
                    "feature_shadow_parity_mismatch",
                    symbol=getattr(event, "symbol", ""),
                    feature_set=primary_feature_set,
                    mismatch_count=len(mismatched),
                    mismatched_features=mismatched[:8],
                )

    def _emit_feature_shadow_check_metric(self, result: str) -> None:
        metrics_registry: MetricsRegistry | None = getattr(self, "metrics_registry", None)
        if not metrics_registry or not hasattr(metrics_registry, "feature_shadow_parity_checks_total"):
            return
        feature_set_id = self._feature_set_id_cached  # type: ignore[attr-defined]
        key = (feature_set_id, str(result))
        try:
            child = self._feature_shadow_checks_metric_children.get(key)  # type: ignore[attr-defined]
            if child is None:
                child = metrics_registry.feature_shadow_parity_checks_total.labels(
                    feature_set=feature_set_id,
                    result=str(result),
                )
                self._feature_shadow_checks_metric_children[key] = child  # type: ignore[attr-defined]
            child.inc()
        except Exception:
            pass

    def _emit_feature_shadow_mismatch_metric(self, feature_set_id: str, feature_id: str) -> None:
        metrics_registry: MetricsRegistry | None = getattr(self, "metrics_registry", None)
        if not metrics_registry or not hasattr(metrics_registry, "feature_shadow_parity_mismatch_total"):
            return
        key = (str(feature_set_id), str(feature_id))
        try:
            child = self._feature_shadow_mismatch_metric_children.get(key)  # type: ignore[attr-defined]
            if child is None:
                child = metrics_registry.feature_shadow_parity_mismatch_total.labels(
                    feature_set=str(feature_set_id),
                    feature_id=str(feature_id),
                )
                self._feature_shadow_mismatch_metric_children[key] = child  # type: ignore[attr-defined]
            child.inc()
        except Exception:
            pass
