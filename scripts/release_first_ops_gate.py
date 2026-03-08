#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

SKILLS_USED: tuple[str, ...] = (
    "search-first",
    "hft-strategy-dev",
    "hft-alpha-research",
    "validation-gate",
    "troubleshoot-metrics",
    "doc-updater",
)

ROLES_USED: tuple[str, ...] = (
    "planner",
    "explorer",
    "worker",
    "default",
    "code-reviewer",
)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().astimezone().isoformat()


def _stamp() -> str:
    return _now_utc().strftime("%Y%m%dT%H%M%SZ")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def _run_shell(command: str, *, cwd: Path, env: dict[str, str] | None = None) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env,
    )
    elapsed = time.perf_counter() - started
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    return {
        "command": command,
        "returncode": int(proc.returncode),
        "duration_s": round(elapsed, 3),
        "stdout": stdout,
        "stderr": stderr,
        "stdout_tail": "\n".join(stdout.splitlines()[-80:]),
        "stderr_tail": "\n".join(stderr.splitlines()[-80:]),
    }


def _is_git_repo(root: Path) -> bool:
    row = _run_shell("git rev-parse --is-inside-work-tree", cwd=root)
    return row["returncode"] == 0 and row["stdout"].strip().lower() == "true"


def _tracked_paths(root: Path) -> list[str]:
    proc = subprocess.run(
        ["bash", "-lc", "git ls-files -z"],
        cwd=str(root),
        capture_output=True,
        text=False,
    )
    if proc.returncode != 0:
        return []
    out: list[str] = []
    for raw in (proc.stdout or b"").split(b"\x00"):
        if not raw:
            continue
        out.append(raw.decode("utf-8", errors="ignore"))
    return sorted(out)


def _tracked_fingerprint(root: Path, tracked_paths: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for rel in tracked_paths:
        path = root / rel
        if not path.exists():
            out[rel] = {"exists": False}
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            out[rel] = {"exists": False, "error": str(exc)}
            continue
        out[rel] = {
            "exists": True,
            "size": int(stat.st_size),
            "mtime_ns": int(stat.st_mtime_ns),
        }
    return out


def _normalize_rel_path(value: str) -> str:
    normalized = str(Path(value)).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def _is_allowed_path(path: str, allowed_prefixes: list[str]) -> bool:
    normalized = _normalize_rel_path(path)
    for prefix in allowed_prefixes:
        current = _normalize_rel_path(prefix)
        if not current:
            continue
        if normalized == current or normalized.startswith(current + "/"):
            return True
    return False


def _unexpected_tracked_changes(
    *,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    allowed_prefixes: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(set(before) | set(after)):
        if _is_allowed_path(path, allowed_prefixes):
            continue
        if before.get(path) == after.get(path):
            continue
        rows.append(
            {
                "path": path,
                "before": before.get(path),
                "after": after.get(path),
            }
        )
    return rows


def _latest_matching(root: Path, pattern: str) -> Path | None:
    raw = Path(pattern)
    if raw.is_absolute():
        paths = sorted(raw.parent.glob(raw.name))
    else:
        paths = sorted(root.glob(pattern))
    if not paths:
        return None
    return paths[-1]


def _extract_overall(report_path: Path | None) -> str | None:
    if report_path is None or not report_path.exists():
        return None
    try:
        payload = _read_json(report_path)
    except Exception:
        return None
    result = payload.get("result")
    if isinstance(result, dict):
        overall = result.get("overall")
        if isinstance(overall, str):
            return overall
    overall = payload.get("overall")
    if isinstance(overall, str):
        return overall
    return None


def _release_converge_command(*, tree_depth: int, output_dir: str) -> str:
    return (
        "(.venv/bin/python scripts/release_converge.py "
        f"--project-root . --output-dir {output_dir} --tree-depth {tree_depth} "
        "--skip-clean --skip-gate "
        "2>/dev/null || "
        "python3 scripts/release_converge.py "
        f"--project-root . --output-dir {output_dir} --tree-depth {tree_depth} "
        "--skip-clean --skip-gate)"
    )


def _roadmap_execute_command(*, output_dir: str) -> str:
    return (
        "(.venv/bin/python scripts/roadmap_delivery_executor.py "
        "--project-root . --todo docs/TODO.md --roadmap ROADMAP.md "
        "--benchmark benchmark.json --paper-index research/knowledge/paper_index.json "
        f"--output-dir {output_dir} 2>/dev/null || "
        "python3 scripts/roadmap_delivery_executor.py "
        "--project-root . --todo docs/TODO.md --roadmap ROADMAP.md "
        "--benchmark benchmark.json --paper-index research/knowledge/paper_index.json "
        f"--output-dir {output_dir})"
    )


def _roadmap_guard_command(*, execution_dir: str, output_dir: str, max_artifact_age_hours: float) -> str:
    return (
        "(.venv/bin/python scripts/roadmap_delivery_guard.py "
        "--todo docs/TODO.md --roadmap ROADMAP.md "
        f"--execution-dir {execution_dir} --max-artifact-age-hours {max_artifact_age_hours} "
        f"--output-dir {output_dir} 2>/dev/null || "
        "python3 scripts/roadmap_delivery_guard.py "
        "--todo docs/TODO.md --roadmap ROADMAP.md "
        f"--execution-dir {execution_dir} --max-artifact-age-hours {max_artifact_age_hours} "
        f"--output-dir {output_dir})"
    )


def _release_unit_tests_command() -> str:
    return (
        "(.venv/bin/python -m pytest --no-cov "
        "tests/unit/test_release_converge.py "
        "tests/unit/test_release_channel_guard.py "
        "tests/unit/test_roadmap_delivery_guard.py "
        "tests/unit/test_roadmap_delivery_executor.py "
        "tests/unit/test_reliability_review_pack.py "
        "tests/unit/test_release_first_ops_gate.py "
        "-q 2>/dev/null || "
        "python3 -m pytest --no-cov "
        "tests/unit/test_release_converge.py "
        "tests/unit/test_release_channel_guard.py "
        "tests/unit/test_roadmap_delivery_guard.py "
        "tests/unit/test_roadmap_delivery_executor.py "
        "tests/unit/test_reliability_review_pack.py "
        "tests/unit/test_release_first_ops_gate.py "
        "-q)"
    )


def _ruff_command() -> str:
    return "(.venv/bin/python -m ruff check src scripts tests 2>/dev/null || python3 -m ruff check src scripts tests)"


def _typecheck_command() -> str:
    return "(.venv/bin/python -m mypy src scripts 2>/dev/null || python3 -m mypy src scripts)"


def _release_channel_gate_command(
    *,
    output_dir: str,
    soak_dir: str,
    change_id: str,
    min_trading_days: int,
    max_report_age_hours: float,
) -> str:
    return (
        "(.venv/bin/python scripts/release_channel_guard.py gate "
        f"--project-root . --output-dir {output_dir} --soak-dir {soak_dir} "
        f"--change-id {change_id} --min-trading-days {min_trading_days} "
        f"--max-report-age-hours {max_report_age_hours} 2>/dev/null || "
        "python3 scripts/release_channel_guard.py gate "
        f"--project-root . --output-dir {output_dir} --soak-dir {soak_dir} "
        f"--change-id {change_id} --min-trading-days {min_trading_days} "
        f"--max-report-age-hours {max_report_age_hours})"
    )


def _reliability_pack_command(
    *,
    month: str,
    project_root: str,
    soak_dir: str,
    deploy_dir: str,
    query_guard_dir: str,
    feature_canary_dir: str,
    callback_latency_dir: str,
    output_dir: str,
    disk_paths: list[str],
    min_disk_free_gb: float,
    backlog_p95_budget: float,
    backlog_p99_budget: float,
    min_query_guard_runs: int,
    min_query_guard_suite_runs: int,
    min_feature_canary_runs: int,
    min_callback_latency_runs: int,
) -> str:
    disk_args = " ".join(f"--disk-path {item}" for item in disk_paths)
    return (
        "(.venv/bin/python scripts/reliability_review_pack.py "
        f"--project-root {project_root} --soak-dir {soak_dir} --deploy-dir {deploy_dir} "
        f"--query-guard-dir {query_guard_dir} --feature-canary-dir {feature_canary_dir} "
        f"--callback-latency-dir {callback_latency_dir} --output-dir {output_dir} --month {month} "
        f"{disk_args} --min-disk-free-gb {min_disk_free_gb} "
        f"--backlog-p95-budget {backlog_p95_budget} --backlog-p99-budget {backlog_p99_budget} "
        f"--min-query-guard-runs {min_query_guard_runs} "
        f"--min-query-guard-suite-runs {min_query_guard_suite_runs} "
        f"--min-feature-canary-runs {min_feature_canary_runs} "
        f"--min-callback-latency-runs {min_callback_latency_runs} 2>/dev/null || "
        "python3 scripts/reliability_review_pack.py "
        f"--project-root {project_root} --soak-dir {soak_dir} --deploy-dir {deploy_dir} "
        f"--query-guard-dir {query_guard_dir} --feature-canary-dir {feature_canary_dir} "
        f"--callback-latency-dir {callback_latency_dir} --output-dir {output_dir} --month {month} "
        f"{disk_args} --min-disk-free-gb {min_disk_free_gb} "
        f"--backlog-p95-budget {backlog_p95_budget} --backlog-p99-budget {backlog_p99_budget} "
        f"--min-query-guard-runs {min_query_guard_runs} "
        f"--min-query-guard-suite-runs {min_query_guard_suite_runs} "
        f"--min-feature-canary-runs {min_feature_canary_runs} "
        f"--min-callback-latency-runs {min_callback_latency_runs})"
    )


def _build_steps(args: argparse.Namespace) -> list[dict[str, Any]]:
    release_converge_dir = str(Path(args.release_converge_dir))
    roadmap_execution_dir = str(Path(args.roadmap_execution_dir))
    roadmap_guard_dir = str(Path(args.roadmap_guard_dir))
    deploy_dir = str(Path(args.deploy_dir))
    soak_dir = str(Path(args.soak_dir))
    reliability_dir = str(Path(args.reliability_dir))
    query_guard_dir = str(Path(args.query_guard_dir))
    feature_canary_dir = str(Path(args.feature_canary_dir))
    callback_latency_dir = str(Path(args.callback_latency_dir))
    disk_paths = list(args.disk_path or [".", ".wal"])

    return [
        {
            "step": "alpha_audit_enabled",
            "command": "__check_alpha_audit__",
            "allowed_tracked_change_prefixes": [],
            "report_path": None,
        },
        {
            "step": "release_converge_no_clean",
            "command": _release_converge_command(tree_depth=int(args.tree_depth), output_dir=release_converge_dir),
            "allowed_tracked_change_prefixes": [],
            "report_path": Path(release_converge_dir) / "latest.json",
        },
        {
            "step": "roadmap_delivery_execute_strict",
            "command": _roadmap_execute_command(output_dir=roadmap_execution_dir),
            "allowed_tracked_change_prefixes": [],
            "report_path": Path(roadmap_execution_dir) / "summary" / "latest.json",
        },
        {
                "step": "roadmap_delivery_guard_strict",
                "command": _roadmap_guard_command(
                    execution_dir=roadmap_execution_dir,
                    output_dir=roadmap_guard_dir,
                    max_artifact_age_hours=float(args.max_report_age_hours),
                ),
            "allowed_tracked_change_prefixes": [],
            "report_path": Path(roadmap_guard_dir) / "latest.json",
        },
        {
            "step": "release_operational_unit_tests",
            "command": _release_unit_tests_command(),
            "allowed_tracked_change_prefixes": [],
            "report_path": None,
        },
        {
            "step": "release_operational_ruff",
            "command": _ruff_command(),
            "allowed_tracked_change_prefixes": [],
            "report_path": None,
        },
        {
            "step": "release_operational_typecheck",
            "command": _typecheck_command(),
            "allowed_tracked_change_prefixes": [],
            "report_path": None,
        },
        {
            "step": "release_channel_gate",
            "command": _release_channel_gate_command(
                output_dir=deploy_dir,
                soak_dir=soak_dir,
                change_id=str(args.change_id),
                min_trading_days=int(args.min_trading_days),
                max_report_age_hours=float(args.max_report_age_hours),
            ),
            "allowed_tracked_change_prefixes": [],
            "report_path_glob": str(Path(deploy_dir) / "release_channel" / "decisions" / "release_gate_*.json"),
        },
        {
            "step": "reliability_monthly_pack",
            "command": _reliability_pack_command(
                month=str(args.month),
                project_root=".",
                soak_dir=soak_dir,
                deploy_dir=deploy_dir,
                query_guard_dir=query_guard_dir,
                feature_canary_dir=feature_canary_dir,
                callback_latency_dir=callback_latency_dir,
                output_dir=reliability_dir,
                disk_paths=disk_paths,
                min_disk_free_gb=float(args.min_disk_free_gb),
                backlog_p95_budget=float(args.backlog_p95_budget),
                backlog_p99_budget=float(args.backlog_p99_budget),
                min_query_guard_runs=int(args.min_query_guard_runs),
                min_query_guard_suite_runs=int(args.min_query_guard_suite_runs),
                min_feature_canary_runs=int(args.min_feature_canary_runs),
                min_callback_latency_runs=int(args.min_callback_latency_runs),
            ),
            "allowed_tracked_change_prefixes": [],
            "report_path_glob": str(Path(reliability_dir) / f"monthly_{args.month}_*.json"),
        },
    ]


def _resolve_report_path(root: Path, step: dict[str, Any]) -> Path | None:
    direct = step.get("report_path")
    if isinstance(direct, Path):
        return direct if direct.is_absolute() else (root / direct)
    if isinstance(direct, str) and direct.strip():
        path = Path(direct)
        return path if path.is_absolute() else (root / path)
    pattern = step.get("report_path_glob")
    if isinstance(pattern, str) and pattern.strip():
        return _latest_matching(root, pattern)
    return None


def _build_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# First Operational Release Gate")
    lines.append("")
    lines.append(f"- generated_at: `{report.get('generated_at')}`")
    lines.append(f"- change_id: `{report.get('change_id')}`")
    lines.append(f"- month: `{report.get('month')}`")
    lines.append(f"- overall: `{report.get('result', {}).get('overall')}`")
    lines.append(f"- recommendation: `{report.get('result', {}).get('recommendation')}`")
    lines.append("")
    lines.append("## Skills / Roles")
    lines.append("")
    lines.append(f"- skills: {', '.join('`' + s + '`' for s in report.get('skills_used', []))}")
    lines.append(f"- roles: {', '.join('`' + s + '`' for s in report.get('roles_used', []))}")
    lines.append("")
    lines.append("## Steps")
    lines.append("")
    lines.append("| step | rc | overall | report | notes |")
    lines.append("|---|---|---|---|---|")
    for row in report.get("steps", []):
        notes: list[str] = []
        if row.get("boundary_violation"):
            notes.append("tracked_change_boundary")
        if row.get("skipped"):
            notes.append("skipped")
        unexpected = row.get("unexpected_tracked_changes")
        if isinstance(unexpected, list) and unexpected:
            notes.append(f"unexpected={len(unexpected)}")
        lines.append(
            f"| `{row.get('step')}` | `{row.get('returncode')}` | `{row.get('report_overall') or ''}` | "
            f"`{row.get('report_path') or ''}` | `{','.join(notes)}` |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Aggregate fail-closed gate for the first operational release.")
    parser.add_argument("--project-root", default=".", help="Project root")
    parser.add_argument("--output-dir", default="outputs/release_first_ops", help="Aggregate report output dir")
    parser.add_argument("--release-converge-dir", default="outputs/release_converge", help="release_converge output dir")
    parser.add_argument(
        "--roadmap-execution-dir",
        default="outputs/roadmap_execution",
        help="roadmap delivery executor output dir",
    )
    parser.add_argument(
        "--roadmap-guard-dir",
        default="outputs/roadmap_delivery",
        help="roadmap delivery guard output dir",
    )
    parser.add_argument("--deploy-dir", default="outputs/deploy_guard", help="deploy guard output dir")
    parser.add_argument("--soak-dir", default="outputs/soak_reports", help="soak report dir")
    parser.add_argument("--query-guard-dir", default="outputs/query_guard", help="query guard dir")
    parser.add_argument("--feature-canary-dir", default="outputs/feature_canary", help="feature canary dir")
    parser.add_argument("--callback-latency-dir", default="outputs/callback_latency", help="callback latency dir")
    parser.add_argument("--reliability-dir", default="outputs/reliability/monthly", help="monthly pack output dir")
    parser.add_argument("--change-id", required=True, help="release change id")
    parser.add_argument("--month", default=dt.date.today().strftime("%Y-%m"), help="target month YYYY-MM")
    parser.add_argument("--tree-depth", type=int, default=2, help="inventory depth for release_converge")
    parser.add_argument("--min-trading-days", type=int, default=5, help="minimum canary trading days")
    parser.add_argument(
        "--max-report-age-hours",
        type=float,
        default=72.0,
        help="maximum age for release channel / roadmap evidence",
    )
    parser.add_argument("--disk-path", action="append", default=None, help="disk path for reliability pack")
    parser.add_argument("--min-disk-free-gb", type=float, default=20.0, help="min free GB for reliability pack")
    parser.add_argument("--backlog-p95-budget", type=float, default=20.0, help="WAL backlog p95 budget")
    parser.add_argument("--backlog-p99-budget", type=float, default=100.0, help="WAL backlog p99 budget")
    parser.add_argument("--min-query-guard-runs", type=int, default=1, help="minimum query-guard runs")
    parser.add_argument("--min-query-guard-suite-runs", type=int, default=1, help="minimum query-guard suites")
    parser.add_argument("--min-feature-canary-runs", type=int, default=1, help="minimum feature canary runs")
    parser.add_argument("--min-callback-latency-runs", type=int, default=1, help="minimum callback latency runs")
    parser.add_argument(
        "--allow-tracked-change-prefix",
        action="append",
        default=None,
        help="tracked-change boundary exception path prefix (repeatable)",
    )
    parser.add_argument("--continue-on-error", action="store_true", help="continue after a failing step")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = Path(args.project_root).resolve()
    out_dir = (root / str(args.output_dir)).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = _stamp()

    is_git_repo = _is_git_repo(root)
    tracked_paths = _tracked_paths(root) if is_git_repo else []
    allowed_prefixes = [_normalize_rel_path(item) for item in (args.allow_tracked_change_prefix or []) if str(item).strip()]
    steps = _build_steps(args)
    env = os.environ.copy()

    rows: list[dict[str, Any]] = []
    overall = STATUS_PASS

    for step in steps:
        step_name = str(step.get("step") or "step")
        command = str(step.get("command") or "")
        before = _tracked_fingerprint(root, tracked_paths) if is_git_repo else {}

        if command == "__check_alpha_audit__":
            started = time.perf_counter()
            enabled = env.get("HFT_ALPHA_AUDIT_ENABLED", "0") == "1"
            elapsed = time.perf_counter() - started
            row: dict[str, Any] = {
                "step": step_name,
                "command": command,
                "returncode": 0 if enabled else 2,
                "duration_s": round(elapsed, 3),
                "stdout": "HFT_ALPHA_AUDIT_ENABLED=1" if enabled else "",
                "stderr": "" if enabled else "HFT_ALPHA_AUDIT_ENABLED must be set to 1",
                "stdout_tail": "HFT_ALPHA_AUDIT_ENABLED=1" if enabled else "",
                "stderr_tail": "" if enabled else "HFT_ALPHA_AUDIT_ENABLED must be set to 1",
            }
        else:
            row = _run_shell(command, cwd=root, env=env)
            row["step"] = step_name

        after = _tracked_fingerprint(root, tracked_paths) if is_git_repo else {}
        unexpected = _unexpected_tracked_changes(
            before=before,
            after=after,
            allowed_prefixes=allowed_prefixes + list(step.get("allowed_tracked_change_prefixes", [])),
        )
        if unexpected:
            row["returncode"] = 97
            stderr = str(row.get("stderr", ""))
            row["stderr"] = (stderr + "\ntracked change boundary violated").strip()
            row["stderr_tail"] = row["stderr"]
            row["boundary_violation"] = True
            row["unexpected_tracked_changes"] = unexpected
        else:
            row["boundary_violation"] = False
            row["unexpected_tracked_changes"] = []

        report_path = _resolve_report_path(root, step)
        row["report_path"] = str(report_path) if report_path else ""
        row["report_overall"] = _extract_overall(report_path)
        rows.append(row)

        if int(row.get("returncode", 1)) != 0:
            overall = STATUS_FAIL
            if not args.continue_on_error:
                break

    recommendation = "go" if overall == STATUS_PASS else "block"
    report = {
        "generated_at": _now_iso(),
        "project_root": str(root),
        "change_id": str(args.change_id),
        "month": str(args.month),
        "skills_used": list(SKILLS_USED),
        "roles_used": list(ROLES_USED),
        "mode": {
            "continue_on_error": bool(args.continue_on_error),
            "allow_tracked_change_prefix": allowed_prefixes,
        },
        "steps": rows,
        "result": {
            "overall": overall,
            "recommendation": recommendation,
        },
    }

    json_path = out_dir / f"release_first_ops_{ts}.json"
    md_path = out_dir / f"release_first_ops_{ts}.md"
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"

    _write_json(json_path, report)
    _write_text(md_path, _build_markdown(report))
    _write_json(latest_json, report)
    _write_text(latest_md, md_path.read_text(encoding="utf-8"))

    print(f"[first-ops] json: {json_path}")
    print(f"[first-ops] md  : {md_path}")
    print(f"[first-ops] overall: {overall}")

    return 0 if overall == STATUS_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
