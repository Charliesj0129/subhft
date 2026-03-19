"""Feature parity checker — validate backend and schema consistency.

Provides utilities to detect mismatches between the Python and Rust
feature computation backends, and between registry schema versions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

logger = structlog.get_logger("feature.parity")


@dataclass(frozen=True, slots=True)
class ParityMismatch:
    feature_id: str
    python_value: Any
    rust_value: Any
    abs_diff: float | None
    rel_diff: float | None
    detail: str


@dataclass
class ParityReport:
    total_features: int = 0
    mismatches: list[ParityMismatch] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def passed(self) -> bool:
        return len(self.mismatches) == 0

    @property
    def mismatch_count(self) -> int:
        return len(self.mismatches)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_features": self.total_features,
            "checked": self.checked,
            "mismatch_count": self.mismatch_count,
            "skipped_count": len(self.skipped),
            "passed": self.passed,
            "mismatches": [
                {
                    "feature_id": m.feature_id,
                    "python_value": m.python_value,
                    "rust_value": m.rust_value,
                    "abs_diff": m.abs_diff,
                    "rel_diff": m.rel_diff,
                    "detail": m.detail,
                }
                for m in self.mismatches
            ],
            "skipped": self.skipped,
        }


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


def check_schema_parity(
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
        Report with any schema mismatches.
    """
    report = ParityReport()

    reg_sets = registry_schema.get("feature_sets", {})
    live_sets = live_schema.get("feature_sets", {})
    all_set_ids = set(reg_sets) | set(live_sets)
    report.total_features = len(all_set_ids)

    for fsid in sorted(all_set_ids):
        if fsid not in reg_sets:
            report.mismatches.append(
                ParityMismatch(
                    feature_id=fsid,
                    python_value=None,
                    rust_value=live_sets.get(fsid),
                    abs_diff=None,
                    rel_diff=None,
                    detail=f"feature_set '{fsid}' missing from registry schema",
                )
            )
            report.checked += 1
            continue

        if fsid not in live_sets:
            report.mismatches.append(
                ParityMismatch(
                    feature_id=fsid,
                    python_value=reg_sets.get(fsid),
                    rust_value=None,
                    abs_diff=None,
                    rel_diff=None,
                    detail=f"feature_set '{fsid}' missing from live schema",
                )
            )
            report.checked += 1
            continue

        report.checked += 1

        reg_fs = reg_sets[fsid]
        live_fs = live_sets[fsid]

        # Check schema_version
        reg_sv = reg_fs.get("schema_version")
        live_sv = live_fs.get("schema_version")
        if reg_sv != live_sv:
            report.mismatches.append(
                ParityMismatch(
                    feature_id=f"{fsid}.schema_version",
                    python_value=reg_sv,
                    rust_value=live_sv,
                    abs_diff=None,
                    rel_diff=None,
                    detail=f"schema_version mismatch: registry={reg_sv} vs live={live_sv}",
                )
            )

        # Check feature list
        reg_features = {f["feature_id"]: f for f in reg_fs.get("features", [])}
        live_features = {f["feature_id"]: f for f in live_fs.get("features", [])}
        all_fids = set(reg_features) | set(live_features)

        for fid in sorted(all_fids):
            if fid not in reg_features:
                report.mismatches.append(
                    ParityMismatch(
                        feature_id=f"{fsid}.{fid}",
                        python_value=None,
                        rust_value=live_features[fid],
                        abs_diff=None,
                        rel_diff=None,
                        detail=f"feature '{fid}' in live but missing from registry",
                    )
                )
            elif fid not in live_features:
                report.mismatches.append(
                    ParityMismatch(
                        feature_id=f"{fsid}.{fid}",
                        python_value=reg_features[fid],
                        rust_value=None,
                        abs_diff=None,
                        rel_diff=None,
                        detail=f"feature '{fid}' in registry but missing from live",
                    )
                )
            else:
                reg_f = reg_features[fid]
                live_f = live_features[fid]
                for attr in ("dtype", "scale", "source_kind"):
                    if reg_f.get(attr) != live_f.get(attr):
                        report.mismatches.append(
                            ParityMismatch(
                                feature_id=f"{fsid}.{fid}.{attr}",
                                python_value=reg_f.get(attr),
                                rust_value=live_f.get(attr),
                                abs_diff=None,
                                rel_diff=None,
                                detail=(
                                    f"attribute '{attr}' mismatch: "
                                    f"registry={reg_f.get(attr)!r} vs live={live_f.get(attr)!r}"
                                ),
                            )
                        )

    return report
