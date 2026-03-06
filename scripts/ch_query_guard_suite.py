#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"
STATUS_SKIPPED = "skipped"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _status_from_check_rc(rc: int) -> str:
    if rc == 0:
        return STATUS_PASS
    if rc == 1:
        return STATUS_WARN
    return STATUS_FAIL


def _status_from_run_rc(rc: int) -> str:
    return STATUS_PASS if rc == 0 else STATUS_FAIL


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
    queries = payload.get("queries", []) if isinstance(payload.get("queries"), list) else []

    lines: list[str] = []
    lines.append("# ClickHouse Query Guard Suite Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- profile: `{payload.get('profile_id')}`")
    lines.append(f"- profile_path: `{payload.get('profile_path')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(result, indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    lines.append("## Query Results")
    lines.append("")
    lines.append("| id | check | run | check_json | run_json |")
    lines.append("|---|---|---|---|---|")

    for row in queries:
        if not isinstance(row, dict):
            continue
        check = row.get("check", {}) if isinstance(row.get("check"), dict) else {}
        run = row.get("run", {}) if isinstance(row.get("run"), dict) else {}
        lines.append(
            f"| `{row.get('id')}` | `{check.get('status')}` | `{run.get('status')}` | "
            f"{check.get('artifact_json') or '-'} | {run.get('artifact_json') or '-'} |"
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _load_profile(path: Path) -> dict[str, Any]:
    payload = _read_json(path)
    if payload is None:
        raise ValueError(f"profile is not valid json: {path}")

    raw_queries = payload.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("profile.queries must be a non-empty list")

    queries: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_queries, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"profile.queries[{idx - 1}] must be an object")

        qid = str(row.get("id") or f"q{idx}").strip()
        if not qid:
            raise ValueError(f"profile.queries[{idx - 1}] has empty id")

        sql = row.get("sql")
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError(f"profile.queries[{idx - 1}] sql must be non-empty string")

        queries.append(
            {
                "id": qid,
                "sql": sql,
                "description": str(row.get("description") or ""),
                "allow_full_scan": bool(row.get("allow_full_scan", False)),
                "allow_warn_execute": bool(row.get("allow_warn_execute", False)),
            }
        )

    return {
        "profile_id": str(payload.get("profile_id") or path.stem),
        "queries": queries,
        "path": str(path.resolve()),
    }


def _extract_artifact(stdout: str, marker: str) -> str | None:
    for line in stdout.splitlines():
        text = line.strip()
        if text.startswith(marker):
            return text.replace(marker, "", 1).strip()
    return None


def _run_cmd(argv: list[str], timeout_s: int) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        if not stderr:
            stderr = f"command timeout after {timeout_s}s"
        return 124, stdout, stderr
    return proc.returncode, proc.stdout, proc.stderr


def _build_check_command(
    args: argparse.Namespace, query: str, *, allow_full_scan: bool
) -> list[str]:
    cmd = [
        args.python_bin,
        str(args.guard_script),
        "check",
        "--query",
        query,
        "--output-dir",
        args.output_dir,
    ]
    if allow_full_scan:
        cmd.append("--allow-full-scan")
    return cmd


def _build_run_command(
    args: argparse.Namespace,
    query: str,
    *,
    allow_full_scan: bool,
    allow_warn_execute: bool,
) -> list[str]:
    cmd = [
        args.python_bin,
        str(args.guard_script),
        "run",
        "--query",
        query,
        "--output-dir",
        args.output_dir,
        "--container",
        args.container,
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--user",
        args.user,
        "--max-memory-usage",
        str(args.max_memory_usage),
        "--max-threads",
        str(args.max_threads),
        "--max-execution-time",
        str(args.max_execution_time),
        "--max-result-rows",
        str(args.max_result_rows),
        "--result-overflow-mode",
        args.result_overflow_mode,
        "--timeout-s",
        str(args.timeout_s),
    ]
    if not args.readonly:
        cmd.append("--no-readonly")
    if allow_full_scan:
        cmd.append("--allow-full-scan")
    if allow_warn_execute:
        cmd.append("--allow-warn-execute")
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def _run_suite(args: argparse.Namespace) -> int:
    profile_path = Path(args.profile)
    guard_script = Path(args.guard_script)
    if not profile_path.exists():
        print(f"[query-guard-suite] profile not found: {profile_path}")
        return 2
    if not guard_script.exists():
        print(f"[query-guard-suite] guard script not found: {guard_script}")
        return 2

    try:
        profile = _load_profile(profile_path)
    except ValueError as exc:
        print(f"[query-guard-suite] {exc}")
        return 2

    overall = STATUS_PASS
    query_rows: list[dict[str, Any]] = []
    check_counts = {STATUS_PASS: 0, STATUS_WARN: 0, STATUS_FAIL: 0}
    run_counts = {STATUS_PASS: 0, STATUS_FAIL: 0, STATUS_SKIPPED: 0}

    for row in profile["queries"]:
        query_id = str(row["id"])
        query_text = str(row["sql"])
        allow_full_scan = bool(row["allow_full_scan"])
        allow_warn_execute = bool(row["allow_warn_execute"]) or bool(args.allow_warn_execute)

        check_cmd = _build_check_command(args, query_text, allow_full_scan=allow_full_scan)
        check_rc, check_out, check_err = _run_cmd(check_cmd, timeout_s=int(args.timeout_s))
        check_status = _status_from_check_rc(check_rc)
        check_counts[check_status] = check_counts.get(check_status, 0) + 1
        overall = _combine_status(overall, check_status)

        run_status = STATUS_SKIPPED
        run_rc: int | None = None
        run_out = ""
        run_err = ""

        if check_status != STATUS_FAIL or args.run_on_fail:
            run_cmd = _build_run_command(
                args,
                query_text,
                allow_full_scan=allow_full_scan,
                allow_warn_execute=allow_warn_execute,
            )
            run_rc, run_out, run_err = _run_cmd(run_cmd, timeout_s=int(args.timeout_s))
            run_status = _status_from_run_rc(run_rc)
            run_counts[run_status] = run_counts.get(run_status, 0) + 1
            overall = _combine_status(overall, run_status)
        else:
            run_counts[STATUS_SKIPPED] = run_counts.get(STATUS_SKIPPED, 0) + 1

        query_rows.append(
            {
                "id": query_id,
                "description": row.get("description"),
                "check": {
                    "status": check_status,
                    "exit_code": check_rc,
                    "artifact_json": _extract_artifact(check_out, "[query-guard] check json:"),
                    "artifact_md": _extract_artifact(check_out, "[query-guard] check md  :"),
                    "stdout_tail": "\n".join(check_out.splitlines()[-20:]),
                    "stderr_tail": "\n".join(check_err.splitlines()[-20:]),
                },
                "run": {
                    "status": run_status,
                    "exit_code": run_rc,
                    "artifact_json": _extract_artifact(run_out, "[query-guard] run json:"),
                    "artifact_md": _extract_artifact(run_out, "[query-guard] run md  :"),
                    "stdout_tail": "\n".join(run_out.splitlines()[-20:]),
                    "stderr_tail": "\n".join(run_err.splitlines()[-20:]),
                },
            }
        )

    report = {
        "generated_at": _now_iso(),
        "profile_id": profile["profile_id"],
        "profile_path": profile["path"],
        "result": {
            "overall": overall,
            "query_count": len(profile["queries"]),
            "check_status_counts": check_counts,
            "run_status_counts": run_counts,
        },
        "queries": query_rows,
    }

    out_dir = Path(args.output_dir) / "suites"
    stem = f"suite_{_stamp()}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[query-guard-suite] suite json: {json_path}")
    print(f"[query-guard-suite] suite md  : {md_path}")
    print(f"[query-guard-suite] overall   : {overall}")

    if overall == STATUS_FAIL:
        return 2
    if overall == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Batch runner for ClickHouse query-guard baseline suite")
    parser.add_argument(
        "--profile",
        default="config/monitoring/query_guard_suite_baseline.json",
        help="JSON profile path containing query suite definitions",
    )
    parser.add_argument(
        "--guard-script",
        default="scripts/ch_query_guard.py",
        help="Path to ch_query_guard.py",
    )
    parser.add_argument("--python-bin", default=sys.executable, help="Python interpreter used to invoke guard script")
    parser.add_argument("--output-dir", default="outputs/query_guard", help="Artifact output directory")
    parser.add_argument("--timeout-s", type=int, default=60, help="Timeout for each guard subprocess invocation")
    parser.add_argument("--run-on-fail", action="store_true", help="Run execution stage even if check stage failed")
    parser.add_argument("--allow-warn-exit-zero", action="store_true", help="Exit 0 when suite overall is warn")

    parser.add_argument("--container", default="clickhouse", help="ClickHouse container name")
    parser.add_argument("--host", default="localhost", help="ClickHouse host inside container")
    parser.add_argument("--port", type=int, default=9000, help="ClickHouse native port")
    parser.add_argument("--user", default="default", help="ClickHouse user")
    parser.set_defaults(readonly=True)
    parser.add_argument("--no-readonly", dest="readonly", action="store_false", help="Disable readonly mode")
    parser.add_argument("--max-memory-usage", type=int, default=2_147_483_648, help="max_memory_usage")
    parser.add_argument("--max-threads", type=int, default=2, help="max_threads")
    parser.add_argument("--max-execution-time", type=int, default=30, help="max_execution_time seconds")
    parser.add_argument("--max-result-rows", type=int, default=50_000, help="max_result_rows")
    parser.add_argument("--result-overflow-mode", default="break", help="result_overflow_mode")
    parser.add_argument("--allow-warn-execute", action="store_true", help="Allow warn queries to execute globally")
    parser.add_argument("--dry-run", action="store_true", help="Dry-run execution stage for all queries")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _run_suite(args)


if __name__ == "__main__":
    sys.exit(main())
