"""Latency audit CLI — scan research artifacts for latency realism compliance.

Standalone CLI tool for auditing alpha scorecards against the latency floor
policy (scorecard values must be >= 80% of the named broker profile).

Usage:
    python -m hft_platform.alpha.latency_audit [--project-root PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from hft_platform.alpha._gate_d import _check_latency_values, _load_latency_profiles

logger = structlog.get_logger("alpha.latency_audit")

_LATENCY_FIELDS = ("submit_ack_latency_ms", "modify_ack_latency_ms", "cancel_ack_latency_ms")


@dataclass(frozen=True, slots=True)
class LatencyAuditResult:
    alpha_id: str
    has_profile: bool
    profile_valid: bool
    stress_tested: bool
    issues: tuple[str, ...]


def _scan_scorecard_dirs(project_root: Path) -> list[Path]:
    """Collect all scorecard.json paths from promotions and validations."""
    dirs_to_scan = [
        project_root / "research" / "experiments" / "promotions",
        project_root / "research" / "experiments" / "validations",
    ]
    found: list[Path] = []
    for base in dirs_to_scan:
        if not base.exists():
            continue
        # Pattern: <base>/<alpha_id>/<run_dir>/scorecard.json
        for sc_path in sorted(base.rglob("scorecard.json")):
            found.append(sc_path)
    return found


def _alpha_id_from_path(sc_path: Path, project_root: Path) -> str:
    """Derive alpha_id from the scorecard path relative to research/experiments/."""
    try:
        rel = sc_path.relative_to(project_root / "research" / "experiments")
        parts = rel.parts
        # parts[0] = promotions|validations, parts[1] = alpha_id or run_dir
        if len(parts) >= 3:
            return parts[1]
        if len(parts) >= 2:
            return parts[1]
    except ValueError:
        pass
    return sc_path.parent.name


def audit_alphas(project_root: Path) -> list[LatencyAuditResult]:
    """Scan all scorecard.json files and audit latency realism compliance."""
    profiles = _load_latency_profiles(str(project_root))
    sc_paths = _scan_scorecard_dirs(project_root)
    results: list[LatencyAuditResult] = []

    for sc_path in sc_paths:
        alpha_id = _alpha_id_from_path(sc_path, project_root)
        try:
            scorecard: dict[str, Any] = json.loads(sc_path.read_text())
        except Exception as exc:
            logger.warning("latency_audit.read_error", path=str(sc_path), error=str(exc))
            results.append(
                LatencyAuditResult(
                    alpha_id=alpha_id,
                    has_profile=False,
                    profile_valid=False,
                    stress_tested=False,
                    issues=(f"Failed to read scorecard: {exc}",),
                )
            )
            continue

        lp = scorecard.get("latency_profile")
        has_profile = bool(lp)
        issues: list[str] = []

        if not has_profile:
            issues.append("Missing latency_profile in scorecard")
            results.append(
                LatencyAuditResult(
                    alpha_id=alpha_id,
                    has_profile=False,
                    profile_valid=False,
                    stress_tested=False,
                    issues=tuple(issues),
                )
            )
            continue

        profile_valid = True
        if isinstance(lp, dict):
            check = _check_latency_values(lp, profiles)
            if not check["pass"]:
                profile_valid = False
                issues.append(check["detail"])
        elif isinstance(lp, str):
            # String profile reference — just check it's not empty
            if not lp.strip():
                profile_valid = False
                issues.append("latency_profile is an empty string")
        else:
            profile_valid = False
            issues.append(f"latency_profile has unexpected type: {type(lp).__name__}")

        stress_test = scorecard.get("stress_test") or {}
        stress_tested = bool(stress_test.get("passed")) if isinstance(stress_test, dict) else False
        if not stress_tested:
            issues.append("stress_test.passed is not True")

        results.append(
            LatencyAuditResult(
                alpha_id=alpha_id,
                has_profile=has_profile,
                profile_valid=profile_valid,
                stress_tested=stress_tested,
                issues=tuple(issues),
            )
        )

    return results


def format_audit_report(results: list[LatencyAuditResult]) -> str:
    """Format audit results as a human-readable table."""
    if not results:
        return "No scorecards found.\n"

    lines: list[str] = []
    lines.append(f"{'Alpha ID':<40} {'HasProfile':<12} {'ProfileValid':<14} {'StressTested':<14} Issues")
    lines.append("-" * 110)

    ok_count = 0
    warn_count = 0
    for r in sorted(results, key=lambda x: x.alpha_id):
        status_icon = "OK" if not r.issues else "WARN"
        if not r.issues:
            ok_count += 1
        else:
            warn_count += 1
        issues_str = "; ".join(r.issues) if r.issues else "-"
        lines.append(
            f"{r.alpha_id:<40} {str(r.has_profile):<12} {str(r.profile_valid):<14} "
            f"{str(r.stress_tested):<14} [{status_icon}] {issues_str}"
        )

    lines.append("")
    lines.append(f"Summary: {ok_count} OK, {warn_count} with issues (total {len(results)})")
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit alpha scorecards for latency realism compliance.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Path to project root (default: current directory)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    project_root = Path(args.project_root).resolve()
    results = audit_alphas(project_root)

    if args.json:
        import dataclasses

        print(json.dumps([dataclasses.asdict(r) for r in results], indent=2))
    else:
        print(format_audit_report(results))

    # Exit with non-zero if any issues found
    return 1 if any(r.issues for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
