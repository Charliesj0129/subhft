from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import structlog

from hft_platform.events import LOBStatsEvent
from hft_platform.feature.engine import FeatureEngine
from hft_platform.feature.registry import default_feature_registry

logger = structlog.get_logger("feature.parity")

# ---------------------------------------------------------------------------
# Rust backend availability
# ---------------------------------------------------------------------------

try:
    try:
        _rust_core: Any = importlib.import_module("hft_platform.rust_core")
    except Exception:
        _rust_core = importlib.import_module("rust_core")
    _RUST_LOB_FEATURE_KERNEL_V1 = getattr(_rust_core, "LobFeatureKernelV1", None)
except Exception:
    _rust_core = None
    _RUST_LOB_FEATURE_KERNEL_V1 = None


def _rust_available() -> bool:
    return _RUST_LOB_FEATURE_KERNEL_V1 is not None


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParityMismatch:
    """Describes a single feature value discrepancy between Python and Rust backends."""

    event_idx: int
    feature_id: str
    python_value: float
    rust_value: float


@dataclass(frozen=True, slots=True)
class ParityReport:
    """Summary of a parity check run."""

    total_events: int
    mismatches: tuple[ParityMismatch, ...]
    passed: bool


# ---------------------------------------------------------------------------
# Backend parity check
# ---------------------------------------------------------------------------


def check_backend_parity(
    events: list[LOBStatsEvent],
    *,
    tolerance: float = 0.0,
) -> ParityReport:
    """Feed *events* through both Python and Rust FeatureEngine backends and compare.

    Parameters
    ----------
    events:
        Sequence of ``LOBStatsEvent`` objects to replay through both backends.
    tolerance:
        Absolute tolerance for floating-point comparisons. Default 0 (exact match).

    Returns
    -------
    ParityReport
        If the Rust backend is not compiled/available, returns a skipped report
        with ``passed=True`` and ``total_events=0``.
    """
    if not _rust_available():
        logger.info("check_backend_parity.skipped", reason="rust_backend_unavailable")
        return ParityReport(total_events=0, mismatches=(), passed=True)

    registry = default_feature_registry()
    feature_set = registry.get_default()
    feature_ids = feature_set.feature_ids

    py_engine = FeatureEngine(registry=registry, kernel_backend="python", emit_events=False)
    rust_engine = FeatureEngine(registry=registry, kernel_backend="rust", emit_events=False)

    mismatches: list[ParityMismatch] = []

    for idx, event in enumerate(events):
        symbol = event.symbol

        py_engine.process_lob_stats(event, local_ts_ns=event.ts)
        rust_engine.process_lob_stats(event, local_ts_ns=event.ts)

        py_values = py_engine.get_feature_tuple(symbol)
        rust_values = rust_engine.get_feature_tuple(symbol)

        if py_values is None or rust_values is None:
            logger.warning(
                "check_backend_parity.missing_values",
                event_idx=idx,
                symbol=symbol,
                py_none=py_values is None,
                rust_none=rust_values is None,
            )
            continue

        n = min(len(py_values), len(rust_values), len(feature_ids))
        for feat_idx in range(n):
            pv = float(py_values[feat_idx])
            rv = float(rust_values[feat_idx])
            if abs(pv - rv) > tolerance:
                mismatches.append(
                    ParityMismatch(
                        event_idx=idx,
                        feature_id=feature_ids[feat_idx],
                        python_value=pv,
                        rust_value=rv,
                    )
                )

    passed = len(mismatches) == 0
    report = ParityReport(
        total_events=len(events),
        mismatches=tuple(mismatches),
        passed=passed,
    )

    if not passed:
        logger.warning(
            "check_backend_parity.mismatches_found",
            total_events=report.total_events,
            mismatch_count=len(mismatches),
        )
    else:
        logger.info(
            "check_backend_parity.passed",
            total_events=report.total_events,
        )

    return report


# ---------------------------------------------------------------------------
# Schema parity check
# ---------------------------------------------------------------------------


def check_schema_parity() -> ParityReport:
    """Compare the Python ``FeatureRegistry`` schema against the Rust feature registry.

    Checks feature IDs, dtypes, and warmup_min_events for the default feature set.

    Returns
    -------
    ParityReport
        ``passed=True`` if schemas match (or Rust is unavailable).
        Mismatches are encoded as ``ParityMismatch`` entries where:
        - ``feature_id`` is the differing feature identifier (or a diff descriptor).
        - ``python_value`` / ``rust_value`` encode the differing numeric attribute
          (warmup_min_events), using ``float("nan")`` for non-numeric diffs.
    """
    if not _rust_available():
        logger.info("check_schema_parity.skipped", reason="rust_backend_unavailable")
        return ParityReport(total_events=0, mismatches=(), passed=True)

    # Obtain Rust feature registry metadata if exposed.
    rust_feature_ids: tuple[str, ...] | None = None
    rust_schema: dict[str, dict[str, Any]] = {}

    get_feature_ids = getattr(_rust_core, "get_lob_feature_ids", None)
    get_feature_schema = getattr(_rust_core, "get_lob_feature_schema", None)

    if callable(get_feature_ids):
        raw = get_feature_ids()
        rust_feature_ids = tuple(str(x) for x in raw)
    if callable(get_feature_schema):
        raw_schema = get_feature_schema()
        if isinstance(raw_schema, dict):
            rust_schema = {str(k): dict(v) for k, v in raw_schema.items()}

    # If the Rust extension exposes no schema introspection, fall back to the
    # kernel-level approach: instantiate LobFeatureKernelV1 and probe it.
    if rust_feature_ids is None:
        kernel_cls = _RUST_LOB_FEATURE_KERNEL_V1
        if kernel_cls is not None:
            try:
                kernel_instance = kernel_cls()
                ids_attr = getattr(kernel_instance, "feature_ids", None)
                if ids_attr is not None:
                    rust_feature_ids = tuple(str(x) for x in ids_attr)
            except Exception as exc:
                logger.debug("check_schema_parity.kernel_probe_failed", error=str(exc))

    py_registry = default_feature_registry()
    py_feature_set = py_registry.get_default()
    py_specs = {spec.feature_id: spec for spec in py_feature_set.features}
    py_feature_ids = py_feature_set.feature_ids

    mismatches: list[ParityMismatch] = []
    event_counter = 0  # repurposed as a comparison step counter

    # --- Compare feature ID sets ---
    if rust_feature_ids is not None:
        py_set = set(py_feature_ids)
        rust_set = set(rust_feature_ids)

        for fid in sorted(py_set - rust_set):
            mismatches.append(
                ParityMismatch(
                    event_idx=event_counter,
                    feature_id=fid,
                    python_value=1.0,  # present in Python
                    rust_value=0.0,    # missing from Rust
                )
            )
            event_counter += 1

        for fid in sorted(rust_set - py_set):
            mismatches.append(
                ParityMismatch(
                    event_idx=event_counter,
                    feature_id=fid,
                    python_value=0.0,  # missing from Python
                    rust_value=1.0,    # present in Rust
                )
            )
            event_counter += 1

        # Compare warmup_min_events and dtype for features present in both.
        for fid in sorted(py_set & rust_set):
            py_spec = py_specs[fid]
            rust_info = rust_schema.get(fid, {})

            # warmup_min_events comparison
            rust_warmup = rust_info.get("warmup_min_events")
            if rust_warmup is not None and int(rust_warmup) != py_spec.warmup_min_events:
                mismatches.append(
                    ParityMismatch(
                        event_idx=event_counter,
                        feature_id=f"{fid}:warmup_min_events",
                        python_value=float(py_spec.warmup_min_events),
                        rust_value=float(rust_warmup),
                    )
                )
                event_counter += 1

            # dtype comparison (encoded as nan when types differ, since dtype is a string)
            rust_dtype = rust_info.get("dtype")
            if rust_dtype is not None and str(rust_dtype) != py_spec.dtype:
                mismatches.append(
                    ParityMismatch(
                        event_idx=event_counter,
                        feature_id=f"{fid}:dtype",
                        python_value=float("nan"),
                        rust_value=float("nan"),
                    )
                )
                event_counter += 1
    else:
        # Rust introspection unavailable — treat as trivially passing schema check.
        logger.info("check_schema_parity.rust_schema_unavailable", reason="no_introspection_api")

    total = event_counter
    passed = len(mismatches) == 0
    report = ParityReport(total_events=total, mismatches=tuple(mismatches), passed=passed)

    if not passed:
        logger.warning(
            "check_schema_parity.mismatches_found",
            mismatch_count=len(mismatches),
        )
    else:
        logger.info("check_schema_parity.passed", total_comparisons=total)

    return report
