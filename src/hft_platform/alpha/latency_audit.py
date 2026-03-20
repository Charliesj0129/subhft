"""Latency Audit CLI Tool — scan alpha experiment scorecards for latency profile compliance.

Validates that each alpha scorecard:
- References a known latency profile
- Has profile values within 80% tolerance of the reference (same logic as Gate D)
- Records stress test passage

Usage:
    python -m hft_platform.alpha.latency_audit
    python -m hft_platform.alpha.latency_audit --project-root /path/to/project
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml

logger = structlog.get_logger("alpha.latency_audit")

# Latency fields that are numerically validated against reference profiles.
_NUMERIC_LATENCY_FIELDS: tuple[str, ...] = (
    "submit_ack_latency_ms",
    "modify_ack_latency_ms",
    "cancel_ack_latency_ms",
)

# Minimum ratio: scorecard value must be >= reference * _GATE_D_TOLERANCE
_GATE_D_TOLERANCE: float = 0.8


@dataclass(frozen=True, slots=True)
class LatencyAuditResult:
    """Audit result for a single alpha scorecard."""

    alpha_id: str
    has_profile: bool
    profile_valid: bool
    stress_tested: bool
    issues: tuple[str, ...]


def _load_latency_profiles(project_root: Path) -> dict[str, Any]:
    """Load latency profiles from config/research/latency_profiles.yaml.

    Returns an empty dict if the file is missing or malformed.
    """
    profiles_path = project_root / "config" / "research" / "latency_profiles.yaml"
    if not profiles_path.exists():
        logger.warning("latency_profiles.yaml not found", path=str(profiles_path))
        return {}
    try:
        raw = profiles_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        return (data or {}).get("profiles", {})
    except Exception:
        logger.warning("failed to load latency_profiles.yaml", path=str(profiles_path), exc_info=True)
        return {}


def _find_scorecard_paths(project_root: Path) -> list[Path]:
    """Return all scorecard.json paths under promotions/ and validations/."""
    experiments_root = project_root / "research" / "experiments"
    paths: list[Path] = []
    for subdir in ("promotions", "validations"):
        base = experiments_root / subdir
        if not base.is_dir():
            continue
        paths.extend(base.rglob("scorecard.json"))
    return paths


def _alpha_id_from_path(scorecard_path: Path, project_root: Path) -> str:
    """Derive a human-readable alpha_id from the scorecard file path.

    Tries the scorecard's own 'alpha_id' field first, then falls back to
    the directory name relative to experiments/.
    """
    try:
        data = json.loads(scorecard_path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("alpha_id"):
            return str(data["alpha_id"])
    except Exception:
        pass
    # Fallback: path relative to research/experiments/<type>/<alpha_id>/...
    experiments_root = project_root / "research" / "experiments"
    try:
        rel = scorecard_path.relative_to(experiments_root)
        # rel = validations/my_alpha/scorecard.json  or
        #       validations/my_alpha/20260318T.../scorecard.json
        parts = rel.parts
        if len(parts) >= 2:
            return parts[1]  # the alpha name directory
    except ValueError:
        pass
    return scorecard_path.parent.name


def _validate_profile(
    scorecard_latency_profile: dict[str, Any],
    reference_profiles: dict[str, Any],
) -> tuple[bool, list[str]]:
    """Validate a scorecard's latency_profile against reference profiles.

    Returns (is_valid, issues_list).
    """
    issues: list[str] = []
    profile_id = scorecard_latency_profile.get("latency_profile_id")

    if not profile_id:
        issues.append("latency_profile.latency_profile_id is missing")
        return False, issues

    if not reference_profiles:
        # Profiles YAML unavailable — cannot validate values, but profile exists
        issues.append(f"latency_profiles.yaml unavailable; cannot validate profile '{profile_id}'")
        return False, issues

    ref = reference_profiles.get(profile_id)
    if ref is None:
        issues.append(f"latency_profile_id '{profile_id}' not found in config/research/latency_profiles.yaml")
        return False, issues

    for field in _NUMERIC_LATENCY_FIELDS:
        ref_value = ref.get(field)
        sc_value = scorecard_latency_profile.get(field)

        if ref_value is None:
            # Reference profile doesn't declare this field — skip
            continue

        if sc_value is None:
            issues.append(f"latency_profile.{field} is missing (reference={ref_value})")
            continue

        try:
            sc_float = float(sc_value)
            ref_float = float(ref_value)
        except (TypeError, ValueError):
            issues.append(f"latency_profile.{field} is non-numeric: {sc_value!r}")
            continue

        # Gate D logic: scorecard value must be >= reference * tolerance
        threshold = ref_float * _GATE_D_TOLERANCE
        if sc_float < threshold:
            issues.append(
                f"latency_profile.{field}={sc_float} < reference*{_GATE_D_TOLERANCE}"
                f"={threshold:.3f} (reference={ref_float})"
            )

    return len(issues) == 0, issues


def _audit_scorecard(
    scorecard_path: Path,
    reference_profiles: dict[str, Any],
    project_root: Path,
) -> LatencyAuditResult:
    """Audit a single scorecard.json and return a LatencyAuditResult."""
    alpha_id = _alpha_id_from_path(scorecard_path, project_root)

    try:
        raw = scorecard_path.read_text(encoding="utf-8")
        data: dict[str, Any] = json.loads(raw)
    except Exception as exc:
        logger.warning("failed to read scorecard", path=str(scorecard_path), error=str(exc))
        return LatencyAuditResult(
            alpha_id=alpha_id,
            has_profile=False,
            profile_valid=False,
            stress_tested=False,
            issues=(f"scorecard parse error: {exc}",),
        )

    latency_profile = data.get("latency_profile")
    has_profile = isinstance(latency_profile, dict) and bool(latency_profile)

    issues: list[str] = []
    profile_valid = False

    if not has_profile:
        issues.append("latency_profile is missing — must record P95 broker RTT assumptions before promotion")
    else:
        assert isinstance(latency_profile, dict)
        profile_valid, profile_issues = _validate_profile(latency_profile, reference_profiles)
        issues.extend(profile_issues)

    # Stress test check
    stress_test = data.get("stress_test")
    if isinstance(stress_test, dict):
        stress_tested = bool(stress_test.get("passed", False))
        if not stress_tested:
            issues.append("stress_test.passed is False or missing")
    else:
        stress_tested = False
        if stress_test is not None:
            issues.append(f"stress_test has unexpected type: {type(stress_test).__name__}")
        # Missing stress_test is not necessarily an issue for older scorecards;
        # we record stress_tested=False but do not add a blocking issue here.

    logger.debug(
        "audited scorecard",
        alpha_id=alpha_id,
        has_profile=has_profile,
        profile_valid=profile_valid,
        stress_tested=stress_tested,
        issue_count=len(issues),
    )

    return LatencyAuditResult(
        alpha_id=alpha_id,
        has_profile=has_profile,
        profile_valid=profile_valid,
        stress_tested=stress_tested,
        issues=tuple(issues),
    )


def audit_alphas(project_root: Path) -> list[LatencyAuditResult]:
    """Scan experiment scorecards and return latency audit results.

    Scans:
    - research/experiments/promotions/*/scorecard.json
    - research/experiments/validations/*/scorecard.json  (including nested runs)

    Args:
        project_root: Root directory of the hft_platform project.

    Returns:
        List of LatencyAuditResult, one per scorecard found.
    """
    reference_profiles = _load_latency_profiles(project_root)
    scorecard_paths = _find_scorecard_paths(project_root)

    if not scorecard_paths:
        logger.info("no scorecard.json files found", project_root=str(project_root))
        return []

    results: list[LatencyAuditResult] = []
    for path in sorted(scorecard_paths):
        result = _audit_scorecard(path, reference_profiles, project_root)
        results.append(result)

    logger.info(
        "latency audit complete",
        total=len(results),
        compliant=sum(1 for r in results if r.has_profile and r.profile_valid),
        missing_profile=sum(1 for r in results if not r.has_profile),
    )
    return results


def format_audit_report(results: list[LatencyAuditResult]) -> str:
    """Format audit results as a human-readable summary table.

    Args:
        results: List of LatencyAuditResult from audit_alphas().

    Returns:
        Multi-line string with a table and summary counts.
    """
    if not results:
        return "Latency Audit Report\n====================\nNo scorecards found.\n"

    lines: list[str] = []
    lines.append("Latency Audit Report")
    lines.append("=" * 100)

    # Column widths
    col_alpha = 36
    col_profile = 12
    col_valid = 10
    col_stress = 12
    indent_width = col_alpha + col_profile + col_valid + col_stress

    header = (
        f"{'Alpha ID':<{col_alpha}}"
        f"{'Has Profile':<{col_profile}}"
        f"{'Valid':<{col_valid}}"
        f"{'Stress OK':<{col_stress}}"
        "Issues"
    )
    lines.append(header)
    lines.append("-" * 100)

    for r in sorted(results, key=lambda x: x.alpha_id):
        has_p = "YES" if r.has_profile else "NO"
        valid = "YES" if r.profile_valid else ("N/A" if not r.has_profile else "NO")
        stress = "YES" if r.stress_tested else "NO"
        issue_summary = r.issues[0] if r.issues else "OK"

        lines.append(
            f"{r.alpha_id:<{col_alpha}}{has_p:<{col_profile}}{valid:<{col_valid}}{stress:<{col_stress}}{issue_summary}"
        )

        # Append remaining issues indented under the first
        indent = " " * indent_width
        for issue in r.issues[1:]:
            lines.append(f"{indent}{issue}")

    lines.append("-" * 100)

    total = len(results)
    compliant = sum(1 for r in results if r.has_profile and r.profile_valid)
    missing = sum(1 for r in results if not r.has_profile)
    invalid = sum(1 for r in results if r.has_profile and not r.profile_valid)
    stress_ok = sum(1 for r in results if r.stress_tested)

    lines.append(f"Total scorecards : {total}")
    lines.append(f"Compliant        : {compliant}  (has_profile=True, profile_valid=True)")
    lines.append(f"Missing profile  : {missing}")
    lines.append(f"Invalid profile  : {invalid}")
    lines.append(f"Stress tested    : {stress_ok}")
    lines.append("")

    return "\n".join(lines)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit alpha scorecard latency profiles for Gate D compliance.")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Path to hft_platform project root (default: auto-detect from this file's location)",
    )
    return parser


def _detect_project_root() -> Path:
    """Auto-detect project root as three levels up from this file."""
    # src/hft_platform/alpha/latency_audit.py → project root
    return Path(__file__).resolve().parent.parent.parent.parent


if __name__ == "__main__":
    args = _build_arg_parser().parse_args()
    project_root: Path = args.project_root if args.project_root else _detect_project_root()
    project_root = project_root.resolve()

    audit_results = audit_alphas(project_root)
    report = format_audit_report(audit_results)
    print(report)
