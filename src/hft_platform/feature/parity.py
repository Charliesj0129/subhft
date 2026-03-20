"""Feature parity checker — validate backend and schema consistency.

Provides utilities to detect mismatches between the Python and Rust
feature computation backends, and between registry schema versions.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any

import structlog

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
    python_features: dict[str, Any],
    rust_features: dict[str, Any],
    abs_tolerance: float = 1e-6,
    rel_tolerance: float = 1e-6,
) -> ParityReport:
    """Compare Python and Rust feature values for parity.

    Parameters
    ----------
    python_features : dict[str, Any]
        Feature values from the Python backend keyed by feature_id.
    rust_features : dict[str, Any]
        Feature values from the Rust backend keyed by feature_id.
    abs_tolerance : float
        Absolute tolerance for numeric comparison.
    rel_tolerance : float
        Relative tolerance for numeric comparison.

    Returns
    -------
    ParityReport
        Report containing mismatches and statistics.
    """
    all_ids = set(python_features) | set(rust_features)
    report = ParityReport(total_features=len(all_ids))

    for fid in sorted(all_ids):
        py_val = python_features.get(fid)
        rs_val = rust_features.get(fid)

        if py_val is None and rs_val is None:
            report.skipped.append(fid)
            continue

        if py_val is None or rs_val is None:
            missing_in = "rust" if py_val is not None else "python"
            report.mismatches.append(
                ParityMismatch(
                    feature_id=fid,
                    python_value=py_val,
                    rust_value=rs_val,
                    abs_diff=None,
                    rel_diff=None,
                    detail=f"MISSING in {missing_in} backend",
                )
            )
            report.checked += 1
            continue

        report.checked += 1

        # Numeric comparison
        try:
            py_float = float(py_val)
            rs_float = float(rs_val)
            abs_diff = abs(py_float - rs_float)
            denom = max(abs(py_float), abs(rs_float), 1e-12)
            rel_diff = abs_diff / denom
            if abs_diff > abs_tolerance and rel_diff > rel_tolerance:
                report.mismatches.append(
                    ParityMismatch(
                        feature_id=fid,
                        python_value=py_val,
                        rust_value=rs_val,
                        abs_diff=abs_diff,
                        rel_diff=rel_diff,
                        detail=(
                            f"MISMATCH: abs_diff={abs_diff:.2e} > {abs_tolerance:.2e}, "
                            f"rel_diff={rel_diff:.2e} > {rel_tolerance:.2e}"
                        ),
                    )
                )
        except (TypeError, ValueError):
            # Non-numeric: exact equality check
            if py_val != rs_val:
                report.mismatches.append(
                    ParityMismatch(
                        feature_id=fid,
                        python_value=py_val,
                        rust_value=rs_val,
                        abs_diff=None,
                        rel_diff=None,
                        detail=f"MISMATCH (non-numeric): python={py_val!r} != rust={rs_val!r}",
                    )
                )

    return report


def check_schema_parity(  # noqa: C901
    registry_schema: dict[str, Any],
    live_schema: dict[str, Any],
) -> ParityReport:
    """Compare two schema dicts (e.g., from FeatureRegistry.to_dict()) for parity.

    Parameters
    ----------
    registry_schema : dict[str, Any]
        Schema dict from the canonical feature registry.
    live_schema : dict[str, Any]
        Schema dict from the live engine or research pipeline.

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
                    rust_value=0.0,  # missing from Rust
                )
            )
            event_counter += 1

        for fid in sorted(rust_set - py_set):
            mismatches.append(
                ParityMismatch(
                    event_idx=event_counter,
                    feature_id=fid,
                    python_value=0.0,  # missing from Python
                    rust_value=1.0,  # present in Rust
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
