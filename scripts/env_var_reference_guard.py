#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

HFT_VAR_PATTERN = re.compile(r"\bHFT_[A-Z0-9_]+\b")
RUNBOOK_LINK_PATTERN = re.compile(r"\[[^\]]+\]\(\.\./runbooks(?:[^)]*)\)")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    checks = payload.get("checks", [])
    missing = payload.get("missing_vars", [])
    runbook_files = payload.get("runbook_files", [])

    lines: list[str] = []
    lines.append("# Env Vars Reference Guard Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- overall: `{payload.get('overall')}`")
    lines.append(f"- runbook_vars: `{payload.get('runbook_var_count')}`")
    lines.append(f"- documented_vars: `{payload.get('reference_var_count')}`")
    lines.append(f"- missing_vars: `{len(missing)}`")
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
    lines.append("## Runbook Files")
    lines.append("")
    for file_path in runbook_files:
        lines.append(f"- `{file_path}`")

    lines.append("")
    lines.append("## Missing Variables")
    lines.append("")
    if missing:
        for var in missing:
            lines.append(f"- `{var}`")
    else:
        lines.append("- none")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    return incoming if order.get(incoming, 0) > order.get(current, 0) else current


def _glob_files(project_root: Path, patterns: list[str]) -> list[Path]:
    files: set[Path] = set()
    for pattern in patterns:
        for path in project_root.glob(pattern):
            if path.is_file():
                files.add(path.resolve())
    return sorted(files, key=lambda p: str(p))


def _extract_hft_vars(text: str) -> set[str]:
    return set(HFT_VAR_PATTERN.findall(text))


def _evaluate_reference_guard(
    project_root: Path, reference_doc: Path, runbook_files: list[Path]
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        check_id: str,
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
                "id": check_id,
                "status": status,
                "severity": severity,
                "expected": expected,
                "current": current,
                "message": message,
            }
        )

    add(
        "reference_doc_exists",
        reference_doc.exists(),
        severity="critical",
        expected="docs/operations/env-vars-reference.md exists",
        current=str(reference_doc),
        message="reference document must exist",
    )

    add(
        "runbook_files_discovered",
        bool(runbook_files),
        severity="critical",
        expected="at least one runbook markdown file",
        current=len(runbook_files),
        message="runbook files should be discoverable",
    )

    reference_text = reference_doc.read_text(encoding="utf-8") if reference_doc.exists() else ""
    reference_vars = _extract_hft_vars(reference_text)
    runbook_vars: set[str] = set()
    runbook_var_sources: dict[str, list[str]] = {}

    for file_path in runbook_files:
        text = file_path.read_text(encoding="utf-8")
        vars_in_file = sorted(_extract_hft_vars(text))
        for var in vars_in_file:
            runbook_vars.add(var)
            runbook_var_sources.setdefault(var, []).append(str(file_path.relative_to(project_root)))

    missing_vars = sorted(runbook_vars - reference_vars)
    extra_reference_vars = sorted(reference_vars - runbook_vars)
    runbook_links = RUNBOOK_LINK_PATTERN.findall(reference_text)

    add(
        "runbook_vars_detected",
        bool(runbook_vars),
        severity="warning",
        expected="runbook contains HFT_* variables",
        current=len(runbook_vars),
        message="no runbook HFT_* variable found",
        warn_only=True,
    )

    add(
        "runbook_vars_documented_in_reference",
        not missing_vars,
        severity="critical",
        expected="all runbook HFT_* variables documented in env-vars reference",
        current={"missing_count": len(missing_vars), "missing": missing_vars[:20]},
        message="runbook variable is missing from env-vars reference",
    )

    add(
        "reference_has_runbook_links",
        bool(runbook_links),
        severity="critical",
        expected="reference includes runbook links",
        current=len(runbook_links),
        message="env-vars reference should include runbook cross-links",
    )

    overall = STATUS_PASS
    for check in checks:
        overall = _combine_status(overall, str(check.get("status") or STATUS_WARN))

    return {
        "generated_at": _now_iso(),
        "overall": overall,
        "project_root": str(project_root),
        "reference_doc": str(reference_doc.relative_to(project_root)) if reference_doc.exists() else str(reference_doc),
        "runbook_files": [str(p.relative_to(project_root)) for p in runbook_files],
        "runbook_var_count": len(runbook_vars),
        "reference_var_count": len(reference_vars),
        "missing_vars": missing_vars,
        "extra_reference_vars_not_in_runbooks": extra_reference_vars,
        "runbook_var_sources": runbook_var_sources,
        "checks": checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Guard docs/operations/env-vars-reference.md coverage for runbook HFT_* variables."
    )
    parser.add_argument("--project-root", default=".", help="Project root path (default: .)")
    parser.add_argument(
        "--reference-doc",
        default="docs/operations/env-vars-reference.md",
        help="Reference markdown path relative to project root.",
    )
    parser.add_argument(
        "--runbook-glob",
        action="append",
        default=[],
        help=(
            "Glob pattern(s) relative to project root for runbook sources. "
            "Defaults to docs/runbooks.md and docs/runbooks/*.md."
        ),
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/env_var_guard",
        help="Output directory for JSON/Markdown artifacts.",
    )
    parser.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Return 0 when result is warn.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    reference_doc = (project_root / args.reference_doc).resolve()
    runbook_globs = args.runbook_glob or ["docs/runbooks.md", "docs/runbooks/*.md"]
    runbook_files = _glob_files(project_root, runbook_globs)

    payload = _evaluate_reference_guard(project_root, reference_doc, runbook_files)
    stamp = _stamp()
    output_dir = (project_root / args.output_dir).resolve()
    json_path = output_dir / "checks" / f"env_vars_guard_{stamp}.json"
    md_path = output_dir / "checks" / f"env_vars_guard_{stamp}.md"
    _write_json(json_path, payload)
    _write_markdown(md_path, payload)

    print(
        "[env-vars-guard]",
        f"overall={payload['overall']}",
        f"runbook_vars={payload['runbook_var_count']}",
        f"documented_vars={payload['reference_var_count']}",
        f"missing={len(payload['missing_vars'])}",
    )
    if payload["missing_vars"]:
        print("[env-vars-guard] missing:", ", ".join(payload["missing_vars"]))
    print("[env-vars-guard] report:", json_path.relative_to(project_root))

    overall = payload["overall"]
    if overall == STATUS_FAIL:
        return 2
    if overall == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
