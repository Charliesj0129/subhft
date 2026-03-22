#!/usr/bin/env python3
"""Rollback drill — verify rollback procedure works.

Records current SHA, deploys previous SHA, verifies health, restores.
For local/staging use only — never runs against production automatically.

Usage:
    python scripts/rollback_drill.py [--dry-run] [--target staging|local]
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("rollback_drill")

DEPLOY_SCRIPT = Path("scripts/deploy.sh")
HEALTH_ENDPOINT = "http://localhost:9090/metrics"
HEALTH_RETRIES, HEALTH_INTERVAL_S = 5, 5
OUTPUT_DIR = Path("outputs/reliability/drills")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stamp() -> str:
    return _now_utc().strftime("%Y%m%d_%H%M%S")


def _git_rev(ref: str = "HEAD") -> str:
    """Return the git SHA for a given ref."""
    result = subprocess.run(
        ["git", "rev-parse", ref], capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        log.warning("git rev-parse %s failed: %s", ref, result.stderr.strip())
        return "unknown"
    return result.stdout.strip()


def _run_deploy(sha: str, *, dry_run: bool) -> bool:
    """Execute deploy.sh --rollback <sha>, or simulate in dry-run mode."""
    if dry_run:
        log.info("[dry-run] would run: %s --rollback %s", DEPLOY_SCRIPT, sha)
        return True

    if not DEPLOY_SCRIPT.exists():
        log.error("deploy script not found: %s", DEPLOY_SCRIPT)
        return False

    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT), "--rollback", sha],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    if result.returncode != 0:
        log.error("deploy failed (rc=%d): %s", result.returncode, result.stderr.strip())
        return False
    log.info("deploy to %s succeeded", sha)
    return True


def _check_health(*, dry_run: bool) -> bool:
    """Verify the service is healthy via /metrics endpoint."""
    if dry_run:
        log.info("[dry-run] would check health at %s", HEALTH_ENDPOINT)
        return True

    for attempt in range(1, HEALTH_RETRIES + 1):
        result = subprocess.run(
            ["curl", "-sf", HEALTH_ENDPOINT],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if result.returncode == 0:
            log.info("health check passed (attempt %d/%d)", attempt, HEALTH_RETRIES)
            return True
        log.warning(
            "health check failed (attempt %d/%d), retrying in %ds",
            attempt,
            HEALTH_RETRIES,
            HEALTH_INTERVAL_S,
        )
        time.sleep(HEALTH_INTERVAL_S)

    log.error("health check failed after %d attempts", HEALTH_RETRIES)
    return False


def _write_result(report: dict[str, Any], output_dir: Path) -> Path:
    """Write drill result JSON."""
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"rollback_{_stamp()}.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("drill result written to %s", path)
    return path


def run_drill(*, target: str, dry_run: bool, output_dir: Path) -> dict[str, Any]:
    """Execute the full rollback drill sequence."""
    t0 = time.monotonic()
    original_sha = _git_rev("HEAD")
    rollback_sha = _git_rev("HEAD~1")

    log.info("original_sha=%s rollback_sha=%s target=%s dry_run=%s", original_sha, rollback_sha, target, dry_run)

    # Step 1: Deploy rollback SHA
    rollback_ok = _run_deploy(rollback_sha, dry_run=dry_run)

    # Step 2: Health check after rollback
    health_ok = _check_health(dry_run=dry_run) if rollback_ok else False

    # Step 3: Restore original SHA
    restore_ok = _run_deploy(original_sha, dry_run=dry_run) if rollback_ok else False

    # Step 4: Health check after restore
    restore_health = _check_health(dry_run=dry_run) if restore_ok else False

    duration_s = round(time.monotonic() - t0, 2)
    overall = "pass" if (rollback_ok and health_ok and restore_ok and restore_health) else "fail"

    report: dict[str, Any] = {
        "drill": "rollback",
        "timestamp": _now_utc().isoformat(),
        "target": target,
        "dry_run": dry_run,
        "original_sha": original_sha,
        "rollback_sha": rollback_sha,
        "health_check_passed": health_ok,
        "restore_success": restore_ok and restore_health,
        "duration_s": duration_s,
        "result": overall,
    }

    _write_result(report, output_dir)
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Rollback drill — verify rollback procedure")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Simulate without actual deploy (default: true)",
    )
    parser.add_argument(
        "--no-dry-run",
        action="store_false",
        dest="dry_run",
        help="Execute actual deploy commands",
    )
    parser.add_argument(
        "--target",
        choices=["local", "staging"],
        default="local",
        help="Deployment target (default: local)",
    )
    parser.add_argument(
        "--output-dir",
        default=str(OUTPUT_DIR),
        help="Output directory for drill results",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for rollback drill."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    args = _build_parser().parse_args(argv)
    report = run_drill(
        target=args.target,
        dry_run=args.dry_run,
        output_dir=Path(args.output_dir),
    )

    if report["result"] == "pass":
        log.info("rollback drill PASSED (%.1fs)", report["duration_s"])
        return 0
    log.error("rollback drill FAILED (%.1fs)", report["duration_s"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
