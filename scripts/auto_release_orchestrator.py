"""Canary release orchestrator — evaluates promotion readiness on merge.

Called by CI (canary-deploy.yml) after main branch merge.
Checks latest commit for feat:/fix: prefix, evaluates gate readiness,
and creates canary promotion config if approved.

Usage:
    python scripts/auto_release_orchestrator.py [--project-root .] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from structlog import get_logger  # noqa: E402

logger = get_logger("auto_release_orchestrator")


def _get_latest_commit() -> dict[str, str]:
    """Get latest commit SHA and message."""
    try:
        sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        msg = subprocess.check_output(["git", "log", "-1", "--format=%s"], text=True).strip()
        return {"sha": sha, "message": msg}
    except Exception as exc:
        logger.error("git command failed", error=str(exc))
        return {"sha": "unknown", "message": ""}


def _is_eligible_commit(message: str) -> bool:
    """Check if commit message starts with feat: or fix:."""
    lower = message.strip().lower()
    return lower.startswith("feat:") or lower.startswith("fix:") or lower.startswith("feat(") or lower.startswith("fix(")


def _evaluate_promotion_readiness(project_root: Path) -> dict[str, bool | str]:
    """Evaluate basic promotion readiness checks."""
    checks: dict[str, bool | str] = {}

    # Check if release_channel_guard exists and can be imported
    guard_path = project_root / "scripts" / "release_channel_guard.py"
    checks["guard_script_exists"] = guard_path.exists()

    # Check for strategy promotions directory
    promotions_dir = project_root / "config" / "strategy_promotions"
    checks["promotions_dir_exists"] = promotions_dir.exists()

    # Check for passing CI (basic: just check if tests exist)
    tests_dir = project_root / "tests"
    checks["tests_dir_exists"] = tests_dir.exists()

    # All checks pass
    checks["ready"] = all(
        v for k, v in checks.items() if isinstance(v, bool) and k != "ready"
    )
    return checks


def _create_canary_config(
    project_root: Path,
    commit_sha: str,
    commit_message: str,
) -> str | None:
    """Create canary promotion config YAML."""
    promotions_dir = project_root / "config" / "strategy_promotions"
    promotions_dir.mkdir(parents=True, exist_ok=True)

    short_sha = commit_sha[:8]
    config_path = promotions_dir / f"canary_{short_sha}.yaml"

    import yaml

    config = {
        "version": "v1",
        "commit_sha": commit_sha,
        "commit_message": commit_message,
        "canary": {
            "enabled": True,
            "weight": 0.05,
            "auto_promoted": True,
        },
    }
    config_path.write_text(yaml.dump(config, default_flow_style=False))
    return str(config_path)


def run_orchestrator(
    project_root: Path,
    dry_run: bool = False,
) -> dict[str, str | bool | None]:
    """Main orchestration logic."""
    commit = _get_latest_commit()
    sha = commit["sha"]
    msg = commit["message"]

    result: dict[str, str | bool | None] = {
        "commit_sha": sha,
        "commit_message": msg,
        "eligible": False,
        "ready": False,
        "config_path": None,
        "reason": "",
    }

    if not _is_eligible_commit(msg):
        result["reason"] = f"Commit not eligible (no feat:/fix: prefix): {msg}"
        logger.info("Commit not eligible for canary", sha=sha[:8], message=msg)
        return result

    result["eligible"] = True
    readiness = _evaluate_promotion_readiness(project_root)
    result["ready"] = bool(readiness.get("ready", False))

    if not result["ready"]:
        result["reason"] = f"Promotion readiness checks failed: {readiness}"
        logger.info("Promotion not ready", sha=sha[:8], checks=readiness)
        return result

    if dry_run:
        result["reason"] = "dry-run mode — skipping config creation"
        logger.info("Dry run — would create canary config", sha=sha[:8])
        return result

    try:
        config_path = _create_canary_config(project_root, sha, msg)
        result["config_path"] = config_path
        result["reason"] = "canary config created"
        logger.info("Canary config created", path=config_path, sha=sha[:8])
    except Exception as exc:
        result["reason"] = f"Failed to create canary config: {exc}"
        logger.error("Canary config creation failed", error=str(exc))

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Auto release orchestrator")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate without creating config")
    args = parser.parse_args()

    project_root = Path(args.project_root).resolve()
    result = run_orchestrator(project_root, dry_run=args.dry_run)

    # Output for GitHub Actions step summary
    summary = json.dumps(result, indent=2)
    print(summary)

    github_step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if github_step_summary:
        with open(github_step_summary, "a") as f:
            f.write("## Canary Release Evaluation\n\n")
            f.write(f"```json\n{summary}\n```\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
