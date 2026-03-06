#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

DENY_KEYWORDS = {
    "insert",
    "update",
    "delete",
    "alter",
    "drop",
    "truncate",
    "optimize",
    "system",
    "attach",
    "detach",
    "create",
    "rename",
    "kill",
    "grant",
    "revoke",
}

LARGE_TABLE_PATTERNS = [
    "hft.market_data",
    "hft.orders",
    "hft.trades",
    "hft.fills",
]

TIME_COLUMNS = [
    "event_time",
    "ingest_ts",
    "ts",
    "timestamp",
    "updated_at",
    "created_at",
]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
    checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []

    lines: list[str] = []
    lines.append("# ClickHouse Query Guard Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- command: `{payload.get('command')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append(f"- query_sha256: `{payload.get('query_sha256')}`")
    lines.append("")

    lines.append("## Checks")
    lines.append("")
    lines.append("| id | status | severity | message |")
    lines.append("|---|---|---|---|")
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"| `{check.get('id')}` | `{check.get('status')}` | `{check.get('severity')}` | {check.get('message')} |"
        )

    lines.append("")
    lines.append("## Query Preview")
    lines.append("")
    lines.append("```sql")
    lines.append(str(payload.get("query_preview") or ""))
    lines.append("```")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _strip_sql_comments(sql: str) -> str:
    no_line = re.sub(r"--[^\n]*", "", sql)
    no_block = re.sub(r"/\*.*?\*/", "", no_line, flags=re.S)
    return no_block


def _normalize_sql(sql: str) -> str:
    text = _strip_sql_comments(sql).strip().lower()
    return re.sub(r"\s+", " ", text)


def _is_readonly_query(sql_norm: str) -> bool:
    if re.match(r"^(select|show|describe|desc|explain)\b", sql_norm):
        return True
    if sql_norm.startswith("with ") and " select " in f" {sql_norm} ":
        return True
    return False


def _find_denied_keywords(sql_norm: str) -> list[str]:
    found: list[str] = []
    for keyword in sorted(DENY_KEYWORDS):
        if re.search(rf"\b{re.escape(keyword)}\b", sql_norm):
            found.append(keyword)
    return found


def _references_large_table(sql_norm: str) -> bool:
    return any(pat in sql_norm for pat in LARGE_TABLE_PATTERNS)


def _has_limit(sql_norm: str) -> bool:
    return bool(re.search(r"\blimit\b\s+\d+", sql_norm))


def _has_time_filter(sql_norm: str) -> bool:
    for col in TIME_COLUMNS:
        if re.search(rf"\b{re.escape(col)}\b\s*(>=|>|<=|<|between|=)", sql_norm):
            return True
    return False


def _evaluate_sql_guard(sql: str, allow_full_scan: bool = False) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    sql_norm = _normalize_sql(sql)

    def add(
        cid: str,
        ok: bool,
        *,
        severity: str,
        expected: Any,
        current: Any,
        message: str,
        warn_only: bool = False,
    ) -> None:
        if ok:
            status = STATUS_PASS
        elif warn_only:
            status = STATUS_WARN
        else:
            status = STATUS_FAIL
        checks.append(
            {
                "id": cid,
                "status": status,
                "severity": severity,
                "expected": expected,
                "current": current,
                "message": message,
            }
        )

    add(
        "query_non_empty",
        bool(sql_norm),
        severity="critical",
        expected="non-empty",
        current=len(sql_norm),
        message="query must not be empty",
    )

    denied = _find_denied_keywords(sql_norm)
    add(
        "query_denied_keywords",
        not denied,
        severity="critical",
        expected="no mutating keywords",
        current=denied,
        message="query contains denied mutating/administrative keywords",
    )

    add(
        "query_readonly",
        _is_readonly_query(sql_norm),
        severity="critical",
        expected="SELECT/SHOW/DESCRIBE/EXPLAIN/WITH ... SELECT",
        current=sql_norm[:40],
        message="query must be read-only",
    )

    has_limit = _has_limit(sql_norm)
    has_time = _has_time_filter(sql_norm)
    large = _references_large_table(sql_norm)

    add(
        "query_has_limit",
        has_limit,
        severity="warning",
        expected="LIMIT <n>",
        current=has_limit,
        message="LIMIT is recommended to avoid oversized result set",
        warn_only=True,
    )

    full_scan_risk = large and not has_limit and not has_time
    add(
        "large_table_full_scan_guard",
        not full_scan_risk,
        severity="critical",
        expected="for large table query, provide LIMIT or time filter",
        current={"references_large_table": large, "has_limit": has_limit, "has_time_filter": has_time},
        message="large-table full scan is blocked by default",
        warn_only=allow_full_scan,
    )

    overall = STATUS_PASS
    for c in checks:
        overall = _combine_status(overall, str(c.get("status") or STATUS_WARN))

    return {
        "overall": overall,
        "normalized_sql": sql_norm,
        "checks": checks,
    }


def _build_clickhouse_command(args: argparse.Namespace, query: str) -> list[str]:
    return [
        "docker",
        "exec",
        args.container,
        "clickhouse-client",
        f"--host={args.host}",
        f"--port={args.port}",
        f"--user={args.user}",
        f"--readonly={1 if args.readonly else 0}",
        f"--max_memory_usage={args.max_memory_usage}",
        f"--max_threads={args.max_threads}",
        f"--max_execution_time={args.max_execution_time}",
        f"--max_result_rows={args.max_result_rows}",
        f"--result_overflow_mode={args.result_overflow_mode}",
        "--query",
        query,
    ]


def _run_cmd(argv: list[str], timeout_s: int = 60) -> tuple[int, str, str]:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    return proc.returncode, proc.stdout, proc.stderr


def _load_query(args: argparse.Namespace) -> tuple[str, str]:
    if bool(args.query) == bool(args.query_file):
        raise ValueError("provide exactly one of --query or --query-file")
    if args.query:
        return str(args.query), "inline"

    path = Path(str(args.query_file))
    if not path.exists():
        raise ValueError(f"query file not found: {path}")
    return path.read_text(encoding="utf-8"), str(path.resolve())


def _artifact_paths(output_dir: Path, prefix: str) -> tuple[Path, Path]:
    base = f"{prefix}_{_stamp()}"
    json_path = output_dir / f"{base}.json"
    md_path = output_dir / f"{base}.md"
    return json_path, md_path


def _run_check(args: argparse.Namespace) -> int:
    try:
        query, source = _load_query(args)
    except ValueError as exc:
        print(f"[query-guard] {exc}")
        return 2

    result = _evaluate_sql_guard(query, allow_full_scan=bool(args.allow_full_scan))
    output_dir = Path(args.output_dir) / "checks"
    json_path, md_path = _artifact_paths(output_dir, "check")

    report = {
        "generated_at": _now_iso(),
        "command": "check",
        "query_source": source,
        "query_sha256": _sha256_text(query),
        "query_preview": query.strip()[:800],
        "policy": {
            "allow_full_scan": bool(args.allow_full_scan),
        },
        "result": result,
    }
    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[query-guard] check json: {json_path}")
    print(f"[query-guard] check md  : {md_path}")
    print(f"[query-guard] overall   : {result['overall']}")

    if result["overall"] == STATUS_FAIL:
        return 2
    if result["overall"] == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


def _run_execute(args: argparse.Namespace) -> int:
    try:
        query, source = _load_query(args)
    except ValueError as exc:
        print(f"[query-guard] {exc}")
        return 2

    result = _evaluate_sql_guard(query, allow_full_scan=bool(args.allow_full_scan))
    output_dir = Path(args.output_dir) / "runs"
    json_path, md_path = _artifact_paths(output_dir, "run")

    allowed = result["overall"] == STATUS_PASS or (
        result["overall"] == STATUS_WARN and bool(args.allow_warn_execute)
    )

    command = _build_clickhouse_command(args, query)
    rc = None
    stdout_tail = ""
    stderr_tail = ""

    if allowed and not args.dry_run:
        cmd_rc, out, err = _run_cmd(command, timeout_s=int(args.timeout_s))
        rc = cmd_rc
        stdout_tail = "\n".join(out.splitlines()[-80:])
        stderr_tail = "\n".join(err.splitlines()[-80:])
    elif not allowed:
        rc = 2

    run_status = STATUS_PASS if allowed and (args.dry_run or rc == 0) else STATUS_FAIL

    report = {
        "generated_at": _now_iso(),
        "command": "run",
        "query_source": source,
        "query_sha256": _sha256_text(query),
        "query_preview": query.strip()[:800],
        "policy": {
            "allow_full_scan": bool(args.allow_full_scan),
            "allow_warn_execute": bool(args.allow_warn_execute),
        },
        "result": result,
        "execution": {
            "allowed": allowed,
            "dry_run": bool(args.dry_run),
            "exit_code": rc,
            "status": run_status,
            "command": " ".join(shlex.quote(x) for x in command),
            "stdout_tail": stdout_tail,
            "stderr_tail": stderr_tail,
        },
    }

    _write_json(json_path, report)
    _write_markdown(md_path, report)

    print(f"[query-guard] run json: {json_path}")
    print(f"[query-guard] run md  : {md_path}")
    print(f"[query-guard] guard   : {result['overall']}")
    print(f"[query-guard] allowed : {allowed}")

    if not allowed:
        print("[query-guard] blocked by guard policy")
        return 2
    if args.dry_run:
        return 0
    return 0 if rc == 0 else 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ClickHouse query guard for operations")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--query", default=None, help="Inline SQL query")
        p.add_argument("--query-file", default=None, help="SQL file path")
        p.add_argument("--output-dir", default="outputs/query_guard", help="Artifact output directory")
        p.add_argument(
            "--allow-full-scan",
            action="store_true",
            help="Downgrade large-table full-scan block to warning",
        )

    check = sub.add_parser("check", help="Check query against guard policy")
    add_common(check)
    check.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit 0 when overall guard status is warn",
    )

    run = sub.add_parser("run", help="Run guarded query through clickhouse-client")
    add_common(run)
    run.add_argument("--container", default="clickhouse", help="ClickHouse container name")
    run.add_argument("--host", default="localhost", help="ClickHouse host inside container")
    run.add_argument("--port", type=int, default=9000, help="ClickHouse native port")
    run.add_argument("--user", default="default", help="ClickHouse user")
    run.set_defaults(readonly=True)
    run.add_argument("--no-readonly", dest="readonly", action="store_false", help="Disable readonly mode")
    run.add_argument("--max-memory-usage", type=int, default=2_147_483_648, help="max_memory_usage")
    run.add_argument("--max-threads", type=int, default=2, help="max_threads")
    run.add_argument("--max-execution-time", type=int, default=30, help="max_execution_time seconds")
    run.add_argument("--max-result-rows", type=int, default=50_000, help="max_result_rows")
    run.add_argument("--result-overflow-mode", default="break", help="result_overflow_mode")
    run.add_argument("--timeout-s", type=int, default=60, help="Command timeout seconds")
    run.add_argument(
        "--allow-warn-execute",
        action="store_true",
        help="Allow execution when guard overall is warn",
    )
    run.add_argument("--dry-run", action="store_true", help="Only evaluate guard and print command artifact")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "check":
        return _run_check(args)
    if args.command == "run":
        return _run_execute(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
