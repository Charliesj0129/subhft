"""Feature engine shadow-parity and feature-update helpers for MarketDataService.

Extracted from market_data.py to reduce module size.
All functions accept the service instance (*svc*) as their first argument.
"""

from __future__ import annotations

import os
import time
from typing import Any, cast

from structlog import get_logger

from hft_platform.events import BidAskEvent, FeatureUpdateEvent, LOBStatsEvent, TickEvent
from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_OUT_OF_ORDER,
    QUALITY_FLAG_PARTIAL,
    QUALITY_FLAG_STALE_INPUT,
    QUALITY_FLAG_STATE_RESET,
    FeatureEngine,
)

logger = get_logger("service.market_data")

_FEATURE_QUALITY_FLAG_LABELS = (
    (QUALITY_FLAG_GAP, "gap"),
    (QUALITY_FLAG_STATE_RESET, "state_reset"),
    (QUALITY_FLAG_STALE_INPUT, "stale_input"),
    (QUALITY_FLAG_OUT_OF_ORDER, "out_of_order"),
    (QUALITY_FLAG_PARTIAL, "partial"),
)


def init_feature_shadow_engine(svc: Any) -> None:
    """Initialise a shadow ``FeatureEngine`` for parity testing.

    Only activates when ``HFT_FEATURE_SHADOW_PARITY=1`` and a primary
    ``feature_engine`` exists on *svc*.
    """
    if svc.feature_engine is None:
        return
    enabled = os.getenv("HFT_FEATURE_SHADOW_PARITY", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return
    try:
        primary_backend = (
            svc.feature_engine.kernel_backend() if hasattr(svc.feature_engine, "kernel_backend") else "python"
        )
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        primary_backend = "python"
    requested = os.getenv("HFT_FEATURE_SHADOW_BACKEND", "").strip().lower()
    shadow_backend = requested or ("rust" if primary_backend == "python" else "python")
    try:
        shadow = FeatureEngine(
            feature_set_id=(
                svc.feature_engine.feature_set_id() if hasattr(svc.feature_engine, "feature_set_id") else None
            ),
            emit_events=True,
            kernel_backend=shadow_backend,
        )
        # If backend fallback happened and becomes identical to primary due missing Rust,
        # still allow compare if explicitly requested.
        if (
            requested == ""
            and hasattr(shadow, "kernel_backend")
            and shadow.kernel_backend() == primary_backend == "python"
        ):
            # Auto mode could not create meaningful alternate backend.
            return
        svc._feature_shadow_engine = shadow
    except Exception as exc:
        logger.warning("feature_shadow_engine_init_failed", reason=str(exc))
        svc._feature_shadow_engine = None


def maybe_update_features(
    svc: Any,
    event: TickEvent | BidAskEvent,
    stats: object | None,
) -> FeatureUpdateEvent | None:
    """Run the primary ``FeatureEngine`` on a new LOB stats event.

    Returns the emitted ``FeatureUpdateEvent`` or ``None``.
    """
    if svc.feature_engine is None or stats is None:
        return None
    if not hasattr(stats, "best_bid") or not hasattr(stats, "best_ask"):
        return None
    meta = getattr(event, "meta", None)
    local_ts_ns = int(getattr(meta, "local_ts", 0) or 0) if meta is not None else 0
    start_ns = time.perf_counter_ns()
    try:
        process_lob_update = getattr(svc.feature_engine, "process_lob_update", None)
        if callable(process_lob_update):
            feature_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
        else:
            feature_update = svc.feature_engine.process_lob_stats(cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns)
        maybe_run_feature_shadow_parity(svc, event, stats, local_ts_ns, feature_update)
        svc._feature_latency_counter += 1
        svc._feature_metrics_counter += 1
        if svc.metrics_registry:
            if svc._feature_latency_counter % svc._feature_latency_sample_every == 0:
                try:
                    if svc._feature_latency_metric_child is None and hasattr(
                        svc.metrics_registry, "feature_plane_latency_ns"
                    ):
                        svc._feature_latency_metric_child = svc.metrics_registry.feature_plane_latency_ns
                    if svc._feature_latency_metric_child is not None:
                        svc._feature_latency_metric_child.observe(time.perf_counter_ns() - start_ns)
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
            if svc._feature_metrics_counter % svc._feature_metrics_sample_every == 0:
                try:
                    if feature_update is not None:
                        feature_set_id = str(getattr(feature_update, "feature_set_id", svc._feature_set_id_cached))
                        svc._feature_set_id_cached = feature_set_id
                        result = "emitted"
                        qflags = int(getattr(feature_update, "quality_flags", 0) or 0)
                    else:
                        feature_set_id = svc._feature_set_id_cached
                        result = "updated"
                        qflags = 0
                        try:
                            if hasattr(svc.feature_engine, "get_feature_view"):
                                state_view = svc.feature_engine.get_feature_view(getattr(event, "symbol", ""))
                            else:
                                state_view = None
                        except Exception as exc:
                            logger.debug("operation_fallback", error=str(exc))
                            state_view = None
                        if isinstance(state_view, dict):
                            qflags = int(state_view.get("quality_flags", 0) or 0)
                    if hasattr(svc.metrics_registry, "feature_plane_updates_total"):
                        key = (result, feature_set_id)
                        child = svc._feature_update_metric_children.get(key)
                        if child is None:
                            child = svc.metrics_registry.feature_plane_updates_total.labels(
                                result=result,
                                feature_set=feature_set_id,
                            )
                            svc._feature_update_metric_children[key] = child
                        child.inc()
                    if qflags and hasattr(svc.metrics_registry, "feature_quality_flags_total"):
                        for bit, label in _FEATURE_QUALITY_FLAG_LABELS:
                            if qflags & bit:
                                qchild = svc._feature_quality_flag_metric_children.get(label)
                                if qchild is None:
                                    qchild = svc.metrics_registry.feature_quality_flags_total.labels(flag=label)
                                    svc._feature_quality_flag_metric_children[label] = qchild
                                qchild.inc()
                except Exception as exc:
                    logger.debug("operation_fallback", error=str(exc))
                    pass
        return feature_update
    except Exception as exc:
        svc._emit_trace(
            "feature_update_error",
            "",
            {"symbol": getattr(event, "symbol", ""), "reason": str(exc)},
        )
        svc._feature_metrics_counter += 1
        if svc.metrics_registry and svc._feature_metrics_counter % svc._feature_metrics_sample_every == 0:
            try:
                if hasattr(svc.metrics_registry, "feature_plane_updates_total"):
                    key = ("error", svc._feature_set_id_cached)
                    child = svc._feature_update_metric_children.get(key)
                    if child is None:
                        child = svc.metrics_registry.feature_plane_updates_total.labels(
                            result="error",
                            feature_set=svc._feature_set_id_cached,
                        )
                        svc._feature_update_metric_children[key] = child
                    child.inc()
            except Exception as metric_exc:
                logger.debug("operation_fallback", error=str(metric_exc))
                pass
        logger.warning("feature_engine_update_failed", reason=str(exc))
        return None


def maybe_run_feature_shadow_parity(
    svc: Any,
    event: TickEvent | BidAskEvent,
    stats: object,
    local_ts_ns: int,
    primary_update: FeatureUpdateEvent | None,
) -> None:
    """Compare primary vs shadow ``FeatureEngine`` outputs for parity testing."""
    shadow = svc._feature_shadow_engine
    if shadow is None:
        return
    svc._feature_shadow_counter += 1
    compare_now = svc._feature_shadow_counter % svc._feature_shadow_sample_every == 0
    try:
        process_lob_update = getattr(shadow, "process_lob_update", None)
        if callable(process_lob_update):
            shadow_update = process_lob_update(event, stats, local_ts_ns=local_ts_ns)
        else:
            shadow_update = shadow.process_lob_stats(cast(LOBStatsEvent, stats), local_ts_ns=local_ts_ns)
    except Exception as exc:
        logger.warning("feature_shadow_engine_update_failed", reason=str(exc))
        _emit_feature_shadow_check_metric(svc, "skipped")
        return

    if not compare_now:
        return
    _emit_feature_shadow_check_metric(svc, "checked")
    primary_feature_set = str(getattr(primary_update, "feature_set_id", svc._feature_set_id_cached))
    primary_values = None
    primary_ids = None
    if primary_update is not None:
        primary_values = tuple(primary_update.values)
        primary_ids = tuple(primary_update.feature_ids)
    else:
        try:
            view = svc.feature_engine.get_feature_view(getattr(event, "symbol", "")) if svc.feature_engine else None
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
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
        except Exception as exc:
            logger.debug("operation_fallback", error=str(exc))
            sview = None
        if isinstance(sview, dict):
            shadow_values = tuple(sview.get("values", ()))
            shadow_ids = tuple(sview.get("feature_ids", ()))

    if primary_values is None or shadow_values is None or primary_ids is None or shadow_ids is None:
        return
    if primary_ids != shadow_ids or len(primary_values) != len(shadow_values):
        for fid in primary_ids:
            _emit_feature_shadow_mismatch_metric(svc, primary_feature_set, str(fid))
        return
    mismatched: list[str] = []
    tol = float(svc._feature_shadow_abs_tolerance)
    for fid, pv, sv in zip(primary_ids, primary_values, shadow_values):
        if isinstance(pv, float) or isinstance(sv, float):
            if abs(float(pv) - float(sv)) > tol:
                mismatched.append(str(fid))
        else:
            if int(pv) != int(sv):
                mismatched.append(str(fid))
    if mismatched:
        svc._feature_shadow_mismatch_counter += 1
        for fid in mismatched:
            _emit_feature_shadow_mismatch_metric(svc, primary_feature_set, fid)
        svc._emit_trace(
            "feature_shadow_mismatch",
            "",
            {
                "symbol": getattr(event, "symbol", ""),
                "feature_set_id": primary_feature_set,
                "mismatch_count": len(mismatched),
                "mismatched_features": mismatched[:16],
            },
        )
        if svc._feature_shadow_mismatch_counter % svc._feature_shadow_warn_every == 1:
            logger.warning(
                "feature_shadow_parity_mismatch",
                symbol=getattr(event, "symbol", ""),
                feature_set=primary_feature_set,
                mismatch_count=len(mismatched),
                mismatched_features=mismatched[:8],
            )


def _emit_feature_shadow_check_metric(svc: Any, result: str) -> None:
    """Increment the shadow parity check counter metric."""
    if not svc.metrics_registry or not hasattr(svc.metrics_registry, "feature_shadow_parity_checks_total"):
        return
    feature_set_id = svc._feature_set_id_cached
    key = (feature_set_id, str(result))
    try:
        child = svc._feature_shadow_checks_metric_children.get(key)
        if child is None:
            child = svc.metrics_registry.feature_shadow_parity_checks_total.labels(
                feature_set=feature_set_id,
                result=str(result),
            )
            svc._feature_shadow_checks_metric_children[key] = child
        child.inc()
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        pass


def _emit_feature_shadow_mismatch_metric(svc: Any, feature_set_id: str, feature_id: str) -> None:
    """Increment the shadow parity mismatch counter metric."""
    if not svc.metrics_registry or not hasattr(svc.metrics_registry, "feature_shadow_parity_mismatch_total"):
        return
    key = (str(feature_set_id), str(feature_id))
    try:
        child = svc._feature_shadow_mismatch_metric_children.get(key)
        if child is None:
            child = svc.metrics_registry.feature_shadow_parity_mismatch_total.labels(
                feature_set=str(feature_set_id),
                feature_id=str(feature_id),
            )
            svc._feature_shadow_mismatch_metric_children[key] = child
        child.inc()
    except Exception as exc:
        logger.debug("operation_fallback", error=str(exc))
        pass
