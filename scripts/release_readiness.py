#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().astimezone().isoformat()


def _stamp() -> str:
    return _now_utc().strftime("%Y%m%dT%H%M%SZ")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
    checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []

    lines = [
        "# Release Readiness Report",
        "",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- milestone_date: `{payload.get('milestone_date')}`",
        f"- prod_date: `{payload.get('prod_date')}`",
        f"- overall: `{result.get('overall')}`",
        f"- recommendation: `{result.get('recommendation')}`",
        "",
        "| id | status | severity | message |",
        "|---|---|---|---|",
    ]
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"| `{check.get('id')}` | `{check.get('status')}` | `{check.get('severity')}` | {check.get('message')} |"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _run_shell(command: str, *, cwd: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["bash", "-lc", command],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _as_text(exc.stdout)
        stderr = _as_text(exc.stderr)
        return {
            "command": command,
            "returncode": 124,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_tail": "\n".join(stdout.splitlines()[-40:]),
            "stderr_tail": "\n".join(stderr.splitlines()[-40:]),
        }
    return {
        "command": command,
        "returncode": int(proc.returncode),
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "stdout_tail": "\n".join((proc.stdout or "").splitlines()[-40:]),
        "stderr_tail": "\n".join((proc.stderr or "").splitlines()[-40:]),
    }


def _candidate_python_command(root: Path) -> str:
    venv_python = root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return str(venv_python)
    return "python3"


def _command_snapshot(root: Path) -> dict[str, dict[str, Any]]:
    py = _candidate_python_command(root)
    return {
        "git_preconditions": _run_shell("bash scripts/check_git_preconditions.sh --full", cwd=root),
        "test_hygiene": _run_shell(f"{py} scripts/check_test_hygiene.py", cwd=root),
        "health_contract": _run_shell(
            f"{py} -m pytest --no-cov tests/unit/test_health_endpoint.py "
            "tests/unit/test_risk_engine.py tests/unit/test_gateway_service.py -q",
            cwd=root,
        ),
    }


def _artifact_sprawl(root: Path) -> list[str]:
    found: list[str] = []
    for pattern in ("coverage.json", "tests/unit/test_*_cov.py", "tests/benchmark/baselines"):
        for path in sorted(root.glob(pattern)):
            found.append(str(path.relative_to(root)))
    return found


def _targets_present(makefile_text: str, targets: list[str]) -> list[str]:
    missing: list[str] = []
    for target in targets:
        if f"{target}:" not in makefile_text:
            missing.append(target)
    return missing


def _ci_markers_missing(ci_text: str, markers: list[str]) -> list[str]:
    return [marker for marker in markers if marker not in ci_text]


def _scripts_missing(root: Path, paths: list[str]) -> list[str]:
    return [path for path in paths if not (root / path).exists()]


def _evaluate_release_readiness(
    *,
    commands: dict[str, dict[str, Any]],
    artifact_sprawl: list[str],
    missing_make_targets: list[str],
    missing_ci_markers: list[str],
    missing_paths: list[str],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        cid: str,
        ok: bool,
        *,
        severity: str,
        message: str,
        current: Any,
        expected: Any,
        allow_warn: bool = False,
    ) -> None:
        status = STATUS_PASS if ok else STATUS_WARN if allow_warn else STATUS_FAIL
        checks.append(
            {
                "id": cid,
                "status": status,
                "severity": severity,
                "message": message,
                "current": current,
                "expected": expected,
            }
        )

    add(
        "git_preconditions_full",
        commands["git_preconditions"]["returncode"] == 0,
        severity="critical",
        message="git full preconditions must pass before canary",
        current=commands["git_preconditions"]["returncode"],
        expected=0,
    )
    add(
        "test_hygiene_gate",
        commands["test_hygiene"]["returncode"] == 0,
        severity="critical",
        message="core pytest suites must pass hygiene gate",
        current=commands["test_hygiene"]["returncode"],
        expected=0,
    )
    add(
        "health_contract_tests",
        commands["health_contract"]["returncode"] == 0,
        severity="critical",
        message="health/risk/gateway readiness contract tests must pass",
        current=commands["health_contract"]["returncode"],
        expected=0,
    )
    add(
        "coverage_artifact_sprawl",
        not artifact_sprawl,
        severity="critical",
        message="coverage-only files and benchmark baseline dumps must stay out of active worktree state",
        current=artifact_sprawl,
        expected=[],
    )
    add(
        "release_make_targets_present",
        not missing_make_targets,
        severity="high",
        message="Makefile must expose canary and release-readiness operations",
        current=missing_make_targets,
        expected=[],
    )
    add(
        "ci_release_markers_present",
        not missing_ci_markers,
        severity="high",
        message="CI must include hygiene and release-readiness markers",
        current=missing_ci_markers,
        expected=[],
    )
    add(
        "release_runtime_files_present",
        not missing_paths,
        severity="high",
        message="release/runtime readiness scripts and health endpoint modules must exist",
        current=missing_paths,
        expected=[],
    )

    overall = STATUS_PASS
    for check in checks:
        overall = _combine_status(overall, str(check.get("status") or STATUS_WARN))

    recommendation = {
        STATUS_PASS: "canary_ready",
        STATUS_WARN: "canary_with_manual_review",
        STATUS_FAIL: "block_canary_until_failures_cleared",
    }[overall]
    return {"overall": overall, "recommendation": recommendation, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate repo and runtime readiness for canary release")
    parser.add_argument("--project-root", default=".", help="Project root")
    parser.add_argument("--output-dir", default="outputs/release_readiness", help="Artifact output directory")
    parser.add_argument("--milestone-date", default="2026-03-30", help="Target canary milestone date")
    parser.add_argument("--prod-date", default="2026-04-03", help="Target production date")
    args = parser.parse_args(argv)

    root = Path(args.project_root).resolve()
    out_dir = Path(args.output_dir).resolve()
    makefile_text = (root / "Makefile").read_text(encoding="utf-8")
    ci_text = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    result = _evaluate_release_readiness(
        commands=_command_snapshot(root),
        artifact_sprawl=_artifact_sprawl(root),
        missing_make_targets=_targets_present(
            makefile_text,
            [
                "soak-canary-report",
                "deploy-pre-sync-template",
                "release-channel-gate",
                "release-first-ops-gate",
                "release-readiness-check",
            ],
        ),
        missing_ci_markers=_ci_markers_missing(
            ci_text,
            [
                "Core test hygiene gate",
                "Canary release-readiness gate",
                "Test assertion enforcement gate",
                "Architecture conformance gate",
            ],
        ),
        missing_paths=_scripts_missing(
            root,
            [
                "scripts/pre_market_check.py",
                "scripts/release_channel_guard.py",
                "scripts/release_first_ops_gate.py",
                "src/hft_platform/observability/health.py",
                "tests/unit/test_health_endpoint.py",
            ],
        ),
    )

    payload = {
        "generated_at": _now_iso(),
        "milestone_date": args.milestone_date,
        "prod_date": args.prod_date,
        "result": result,
    }
    stamp = _stamp()
    json_path = out_dir / f"release_readiness_{stamp}.json"
    md_path = out_dir / f"release_readiness_{stamp}.md"
    _write_json(json_path, payload)
    _write_markdown(md_path, payload)

    print(f"[release-readiness] json: {json_path}")
    print(f"[release-readiness] md  : {md_path}")
    print(f"[release-readiness] overall={result['overall']} recommendation={result['recommendation']}")
    return 0 if result["overall"] == STATUS_PASS else 1


if __name__ == "__main__":
    raise SystemExit(main())
