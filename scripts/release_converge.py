#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import time
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

SKILLS_USED: tuple[str, ...] = (
    "iterative-retrieval",
    "fix",
    "doc-updater",
)
ROLES_USED: tuple[str, ...] = (
    "planner",
    "refactor-cleaner",
    "code-reviewer",
)


def _now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _now_iso() -> str:
    return _now_utc().astimezone().isoformat()


def _stamp() -> str:
    return _now_utc().strftime("%Y%m%dT%H%M%SZ")


def _run_shell(command: str, *, cwd: Path) -> dict[str, Any]:
    started = time.perf_counter()
    proc = subprocess.run(
        ["bash", "-lc", command],
        cwd=str(cwd),
        capture_output=True,
        text=True,
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


def _tracked_paths(root: Path) -> set[str]:
    proc = subprocess.run(
        ["bash", "-lc", "git ls-files -z"],
        cwd=str(root),
        capture_output=True,
        text=False,
    )
    if proc.returncode != 0:
        return set()
    blob = proc.stdout or b""
    out: set[str] = set()
    for raw in blob.split(b"\x00"):
        if not raw:
            continue
        out.add(raw.decode("utf-8", errors="ignore"))
    return out


def _tracked_dirs(tracked: set[str]) -> set[str]:
    out: set[str] = set()
    for rel in tracked:
        p = Path(rel)
        parent = p.parent
        while str(parent) not in ("", "."):
            out.add(str(parent))
            parent = parent.parent
    return out


def _safe_prune_untracked(root: Path, *, tracked: set[str], tracked_dirs: set[str]) -> dict[str, Any]:
    removed_files = 0
    removed_dirs = 0
    skipped_tracked = 0
    removed_bytes = 0

    for rel in (".coverage", "coverage.xml"):
        p = (root / rel).resolve()
        if not p.exists():
            continue
        if rel in tracked:
            skipped_tracked += 1
            continue
        if p.is_file():
            removed_bytes += p.stat().st_size
            p.unlink(missing_ok=True)
            removed_files += 1

    for p in root.rglob("*.prof"):
        rel = str(p.relative_to(root))
        if rel in tracked:
            skipped_tracked += 1
            continue
        if p.is_file():
            removed_bytes += p.stat().st_size
            p.unlink(missing_ok=True)
            removed_files += 1

    candidate_dirs = (
        root / ".benchmarks",
        root / ".hypothesis",
        root / ".tmp_wal_ckprobe",
        root / "tests/benchmark/.benchmarks",
    )
    for base in candidate_dirs:
        if not base.exists():
            continue
        for node in sorted(base.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            rel = str(node.relative_to(root))
            if rel in tracked or rel in tracked_dirs:
                skipped_tracked += 1
                continue
            if node.is_file() or node.is_symlink():
                try:
                    removed_bytes += node.stat().st_size
                except OSError:
                    pass
                node.unlink(missing_ok=True)
                removed_files += 1
                continue
            if node.is_dir():
                try:
                    if not any(node.iterdir()):
                        node.rmdir()
                        removed_dirs += 1
                except OSError:
                    pass
        base_rel = str(base.relative_to(root))
        if base_rel not in tracked and base_rel not in tracked_dirs and base.is_dir():
            try:
                if not any(base.iterdir()):
                    base.rmdir()
                    removed_dirs += 1
            except OSError:
                pass

    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "removed_bytes": removed_bytes,
        "skipped_tracked": skipped_tracked,
    }


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _snapshot_sizes(root: Path) -> dict[str, str]:
    targets = [
        ".",
        "research",
        ".wal",
        "data",
        "target",
        ".venv",
        "outputs",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        ".benchmarks",
    ]
    out: dict[str, str] = {}
    for rel in targets:
        path = root / rel
        if not path.exists():
            continue
        row = _run_shell(f"du -sh {rel}", cwd=root)
        if row["returncode"] == 0:
            first = (row["stdout"].splitlines() or [""])[0]
            out[rel] = first
        else:
            out[rel] = "n/a"
    return out


def _collect_inventory(root: Path, *, tree_depth: int) -> dict[str, Any]:
    ls_row = _run_shell("ls -la", cwd=root)
    tree_cmd = f"tree -a -L {tree_depth} . | sed -n '1,500p'"
    tree_row = _run_shell(tree_cmd, cwd=root)
    if tree_row["returncode"] != 0 and ("tree: command not found" in tree_row["stderr"]):
        tree_row = _run_shell(f"find . -maxdepth {tree_depth} | sed -n '1,500p'", cwd=root)
    git_status = _run_shell("git status --short", cwd=root)
    return {
        "ls_a": ls_row,
        "tree_a": tree_row,
        "git_status": git_status,
        "sizes": _snapshot_sizes(root),
    }


def _normalize_rel_path(value: str) -> str:
    normalized = str(Path(value)).replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.rstrip("/")
    if normalized == "":
        return "."
    return normalized


def _unique_paths(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalized = _normalize_rel_path(str(raw).strip())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _resolve_cleanup_flags(
    *,
    cleanup_profile: str,
    clean_outputs: bool,
    clean_reports: bool,
    clean_state: bool,
    clean_venv: bool,
    clean_wal: bool,
    clean_data: bool,
) -> dict[str, bool]:
    extended = cleanup_profile == "extended"
    return {
        "clean_outputs": bool(clean_outputs or extended),
        "clean_reports": bool(clean_reports or extended),
        "clean_state": bool(clean_state or extended),
        "clean_venv": bool(clean_venv or extended),
        "clean_wal": bool(clean_wal),
        "clean_data": bool(clean_data),
    }


def _cleanup_steps(*, clean_rust: bool, cleanup_flags: dict[str, bool]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [
        {
            "step": "make_clean",
            "command": "make clean",
            "destructive": True,
            "targets": [],
            "risk": "low",
        },
        {
            "step": "research_clean",
            "command": "(.venv/bin/python -m research.factory clean 2>/dev/null || python3 -m research.factory clean)",
            "destructive": True,
            "targets": [],
            "risk": "low",
        },
        {
            "step": "safe_prune_untracked",
            "command": "__safe_prune_untracked__",
            "destructive": True,
            "targets": [],
            "risk": "low",
        },
    ]
    if clean_rust:
        steps.append(
            {
                "step": "make_clean_rust",
                "command": "make clean-rust",
                "destructive": True,
                "targets": ["target"],
                "risk": "low",
            }
        )

    optional_cleanup: list[tuple[str, str, str]] = [
        ("clean_outputs", "rm -rf outputs", "outputs"),
        ("clean_reports", "rm -rf reports", "reports"),
        ("clean_state", "rm -rf .state", ".state"),
        ("clean_venv", "rm -rf .venv", ".venv"),
        ("clean_wal", "rm -rf .wal", ".wal"),
        ("clean_data", "rm -rf data", "data"),
    ]
    for step, command, target in optional_cleanup:
        if not cleanup_flags.get(step, False):
            continue
        steps.append(
            {
                "step": step,
                "command": command,
                "destructive": True,
                "targets": [target],
                "risk": "high" if step in {"clean_wal", "clean_data"} else "medium",
            }
        )
    return steps


def _default_guard_targets(cleanup_steps: list[dict[str, Any]]) -> list[str]:
    targets: list[str] = []
    for row in cleanup_steps:
        raw_targets = row.get("targets", [])
        if not isinstance(raw_targets, list):
            continue
        for target in raw_targets:
            if isinstance(target, str):
                targets.append(target)
    return _unique_paths(targets)


def _tracked_matches(tracked: set[str], target: str) -> list[str]:
    normalized = _normalize_rel_path(target)
    if normalized == ".":
        return sorted(tracked)
    prefix = f"{normalized}/"
    return sorted(path for path in tracked if path == normalized or path.startswith(prefix))


def _guard_no_tracked_path_rows(
    *,
    guard_targets: list[str],
    tracked: set[str],
    is_git_repo: bool,
) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    blocked: set[str] = set()
    normalized_targets = _unique_paths(guard_targets)
    for target in normalized_targets:
        row: dict[str, Any] = {
            "step": f"guard_no_tracked_path:{target}",
            "command": f"guard-no-tracked-path {target}",
            "duration_s": 0.0,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
            "target": target,
            "guard": True,
        }
        if not is_git_repo:
            row["returncode"] = 0
            row["stdout"] = "skipped: not a git work tree"
            row["stdout_tail"] = row["stdout"]
            row["skipped"] = True
            rows.append(row)
            continue

        matches = _tracked_matches(tracked, target)
        if matches:
            blocked.add(target)
            sample = matches[:30]
            row["returncode"] = 3
            row["stderr"] = f"tracked paths detected under: {target}"
            row["stderr_tail"] = row["stderr"]
            row["stdout"] = "\n".join(sample)
            row["stdout_tail"] = "\n".join(sample[-10:])
            row["tracked_paths"] = sample
        else:
            row["returncode"] = 0
            row["stdout"] = f"ok: no tracked paths under {target}"
            row["stdout_tail"] = row["stdout"]
        rows.append(row)
    return rows, blocked


def _gate_steps(*, gate_profile: str) -> list[tuple[str, str]]:
    steps: list[tuple[str, str]] = [
        ("roadmap_delivery_check", "make roadmap-delivery-check ALLOW_WARN=1"),
        (
            "roadmap_unit_tests",
            "(.venv/bin/python -m pytest --no-cov tests/unit/test_roadmap_delivery_guard.py tests/unit/test_roadmap_delivery_executor.py -q 2>/dev/null || python3 -m pytest --no-cov tests/unit/test_roadmap_delivery_guard.py tests/unit/test_roadmap_delivery_executor.py -q)",
        ),
        (
            "roadmap_ruff",
            "(.venv/bin/python -m ruff check scripts/roadmap_delivery_guard.py scripts/roadmap_delivery_executor.py 2>/dev/null || python3 -m ruff check scripts/roadmap_delivery_guard.py scripts/roadmap_delivery_executor.py)",
        ),
    ]
    if gate_profile in {"smoke", "full"}:
        steps.extend(
            [
                (
                    "smoke_ruff",
                    "(.venv/bin/python -m ruff check src scripts tests 2>/dev/null || python3 -m ruff check src scripts tests)",
                ),
                (
                    "smoke_typecheck",
                    "(.venv/bin/python -m mypy src scripts 2>/dev/null || python3 -m mypy src scripts)",
                ),
            ]
        )
    if gate_profile == "full":
        steps.append(
            (
                "full_pytest",
                "(.venv/bin/python -m pytest --no-cov -q 2>/dev/null || python3 -m pytest --no-cov -q)",
            )
        )
    return steps


def _status_for(results: list[dict[str, Any]]) -> str:
    if any(int(r.get("returncode", 1)) != 0 for r in results):
        return STATUS_FAIL
    return STATUS_PASS


def _mode_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=True)
    return str(value)


def _md_cell(value: Any) -> str:
    return _mode_value(value).replace("|", "\\|").replace("\n", "<br>")


def _build_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# Release Convergence Report")
    lines.append("")
    lines.append(f"- generated_at: `{report.get('generated_at')}`")
    lines.append(f"- overall: `{report.get('result', {}).get('overall')}`")
    lines.append(f"- recommendation: `{report.get('result', {}).get('recommendation')}`")
    lines.append("")
    lines.append("## Mode")
    lines.append("")
    lines.append("| key | value |")
    lines.append("|---|---|")
    mode = report.get("mode", {})
    if isinstance(mode, dict):
        for key in sorted(mode):
            lines.append(f"| `{_md_cell(key)}` | `{_md_cell(mode.get(key))}` |")
    lines.append("")
    lines.append("## Skills / Roles")
    lines.append("")
    lines.append(f"- skills: {', '.join('`' + s + '`' for s in report.get('skills_used', []))}")
    lines.append(f"- roles: {', '.join('`' + s + '`' for s in report.get('roles_used', []))}")
    lines.append("")
    lines.append("## Size Snapshot (Before -> After)")
    lines.append("")
    lines.append("| path | before | after |")
    lines.append("|---|---|---|")
    before = report.get("before", {}).get("sizes", {})
    after = report.get("after", {}).get("sizes", {})
    keys = sorted(set(before) | set(after))
    for key in keys:
        lines.append(f"| `{key}` | `{before.get(key, '')}` | `{after.get(key, '')}` |")
    lines.append("")
    lines.append("## Cleanup Steps")
    lines.append("")
    lines.append("| step | rc | duration_s | command | notes |")
    lines.append("|---|---|---|---|---|")
    for row in report.get("cleanup_steps", []):
        notes: list[str] = []
        if row.get("dry_run"):
            notes.append("dry_run")
        if row.get("blocked_by_guard"):
            notes.append("blocked_by_guard")
        if row.get("skipped"):
            notes.append("skipped")
        if row.get("risk"):
            notes.append(f"risk={row.get('risk')}")
        lines.append(
            f"| `{_md_cell(row.get('step'))}` | `{_md_cell(row.get('returncode'))}` | `{_md_cell(row.get('duration_s'))}` | `{_md_cell(row.get('command'))}` | `{_md_cell(','.join(notes))}` |"
        )
    lines.append("")
    lines.append("## Gate Steps")
    lines.append("")
    lines.append("| step | rc | duration_s | command | notes |")
    lines.append("|---|---|---|---|---|")
    for row in report.get("gate_steps", []):
        notes: list[str] = []
        if row.get("skipped"):
            notes.append("skipped")
        lines.append(
            f"| `{_md_cell(row.get('step'))}` | `{_md_cell(row.get('returncode'))}` | `{_md_cell(row.get('duration_s'))}` | `{_md_cell(row.get('command'))}` | `{_md_cell(','.join(notes))}` |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Deep clean + release convergence workflow.")
    p.add_argument("--project-root", default=".", help="Project root path")
    p.add_argument("--output-dir", default="outputs/release_converge", help="Report output directory")
    p.add_argument("--tree-depth", type=int, default=3, help="Depth for tree inventory")
    p.add_argument("--skip-clean", action="store_true", help="Skip cleanup steps")
    p.add_argument("--skip-gate", action="store_true", help="Skip release gate checks")
    p.add_argument("--dry-run", action="store_true", help="Do not execute destructive cleanup steps")
    p.add_argument("--cleanup-profile", choices=("safe", "extended"), default="safe", help="Cleanup profile")
    p.add_argument("--clean-outputs", action="store_true", help="Remove outputs/ directory")
    p.add_argument("--clean-reports", action="store_true", help="Remove reports/ directory")
    p.add_argument("--clean-state", action="store_true", help="Remove .state/ directory")
    p.add_argument("--clean-venv", action="store_true", help="Remove .venv/ directory")
    p.add_argument("--clean-wal", action="store_true", help="High-risk cleanup: remove .wal/ directory")
    p.add_argument("--clean-data", action="store_true", help="High-risk cleanup: remove data/ directory")
    p.add_argument(
        "--guard-no-tracked-path",
        action="append",
        default=None,
        help="Path guard: fail cleanup if tracked files exist under the path (repeatable)",
    )
    p.add_argument(
        "--gate-profile",
        choices=("roadmap", "smoke", "full"),
        default="roadmap",
        help="Gate profile",
    )
    p.add_argument("--continue-on-error", action="store_true", help="Continue executing steps after failures")
    p.add_argument("--clean-rust", action="store_true", help="Also run make clean-rust")
    p.add_argument("--allow-warn-exit-zero", action="store_true", help="Reserved for compatibility")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    root = Path(args.project_root).resolve()
    out_dir = (root / str(args.output_dir)).resolve()
    ts = _stamp()

    before = _collect_inventory(root, tree_depth=max(1, int(args.tree_depth)))
    is_git_repo = _is_git_repo(root)
    tracked = _tracked_paths(root) if is_git_repo else set()
    tracked_dirs = _tracked_dirs(tracked)
    cleanup_flags = _resolve_cleanup_flags(
        cleanup_profile=str(args.cleanup_profile),
        clean_outputs=bool(args.clean_outputs),
        clean_reports=bool(args.clean_reports),
        clean_state=bool(args.clean_state),
        clean_venv=bool(args.clean_venv),
        clean_wal=bool(args.clean_wal),
        clean_data=bool(args.clean_data),
    )
    cleanup_plan = _cleanup_steps(clean_rust=bool(args.clean_rust), cleanup_flags=cleanup_flags)
    guard_targets = _unique_paths(list(args.guard_no_tracked_path or [])) or _default_guard_targets(cleanup_plan)

    cleanup_rows: list[dict[str, Any]] = []
    gate_rows: list[dict[str, Any]] = []

    if not args.skip_clean:
        guard_rows, blocked_targets = _guard_no_tracked_path_rows(
            guard_targets=guard_targets,
            tracked=tracked,
            is_git_repo=is_git_repo,
        )
        cleanup_rows.extend(guard_rows)
        guard_failed = any(int(row.get("returncode", 1)) != 0 for row in guard_rows)
        guard_block_cleanup = guard_failed and not bool(args.continue_on_error)

        for item in cleanup_plan:
            step = str(item.get("step", "cleanup"))
            cmd = str(item.get("command", ""))
            destructive = bool(item.get("destructive", True))
            step_targets = _unique_paths(list(item.get("targets", [])))
            blocked = any(target in blocked_targets for target in step_targets)

            if guard_block_cleanup:
                if blocked:
                    stderr = f"blocked by guard_no_tracked_path: {','.join(step_targets)}"
                    row = {
                        "step": step,
                        "command": cmd,
                        "returncode": 3,
                        "duration_s": 0.0,
                        "stdout": "",
                        "stderr": stderr,
                        "stdout_tail": "",
                        "stderr_tail": stderr,
                        "blocked_by_guard": True,
                        "skipped": True,
                    }
                else:
                    row = {
                        "step": step,
                        "command": cmd,
                        "returncode": 0,
                        "duration_s": 0.0,
                        "stdout": "",
                        "stderr": "skipped: guard_no_tracked_path precheck failed",
                        "stdout_tail": "",
                        "stderr_tail": "skipped: guard_no_tracked_path precheck failed",
                        "skipped": True,
                    }
            elif blocked:
                stderr = f"blocked by guard_no_tracked_path: {','.join(step_targets)}"
                row = {
                    "step": step,
                    "command": cmd,
                    "returncode": 3,
                    "duration_s": 0.0,
                    "stdout": "",
                    "stderr": stderr,
                    "stdout_tail": "",
                    "stderr_tail": stderr,
                    "blocked_by_guard": True,
                    "skipped": True,
                }
            elif args.dry_run and destructive:
                row = {
                    "step": step,
                    "command": cmd,
                    "returncode": 0,
                    "duration_s": 0.0,
                    "stdout": "dry-run: destructive step skipped",
                    "stderr": "",
                    "stdout_tail": "dry-run: destructive step skipped",
                    "stderr_tail": "",
                    "dry_run": True,
                    "skipped": True,
                }
            elif cmd == "__safe_prune_untracked__":
                started = time.perf_counter()
                summary = _safe_prune_untracked(root, tracked=tracked, tracked_dirs=tracked_dirs)
                elapsed = time.perf_counter() - started
                summary_json = json.dumps(summary, ensure_ascii=False)
                row = {
                    "step": step,
                    "command": cmd,
                    "returncode": 0,
                    "duration_s": round(elapsed, 3),
                    "stdout": summary_json,
                    "stderr": "",
                    "stdout_tail": summary_json,
                    "stderr_tail": "",
                    "summary": summary,
                }
            else:
                row = _run_shell(cmd, cwd=root)
                row["step"] = step

            row["risk"] = item.get("risk", "low")
            if step_targets:
                row["targets"] = step_targets
            cleanup_rows.append(row)
            if row["returncode"] != 0 and not args.continue_on_error:
                break

    cleanup_status = _status_for(cleanup_rows) if cleanup_rows else STATUS_PASS

    should_run_gate = (not args.skip_gate) and (cleanup_status == STATUS_PASS or bool(args.continue_on_error))
    if should_run_gate:
        for step, cmd in _gate_steps(gate_profile=str(args.gate_profile)):
            row = _run_shell(cmd, cwd=root)
            row["step"] = step
            gate_rows.append(row)
            if row["returncode"] != 0 and not args.continue_on_error:
                break

    gate_status = _status_for(gate_rows) if gate_rows else STATUS_PASS
    after = _collect_inventory(root, tree_depth=max(1, int(args.tree_depth)))

    overall = STATUS_PASS
    if cleanup_status == STATUS_FAIL or gate_status == STATUS_FAIL:
        overall = STATUS_FAIL
    recommendation = "release_ready" if overall == STATUS_PASS else "block"

    report: dict[str, Any] = {
        "generated_at": _now_iso(),
        "project_root": str(root),
        "mode": {
            "skip_clean": bool(args.skip_clean),
            "skip_gate": bool(args.skip_gate),
            "dry_run": bool(args.dry_run),
            "cleanup_profile": str(args.cleanup_profile),
            "gate_profile": str(args.gate_profile),
            "continue_on_error": bool(args.continue_on_error),
            "clean_rust": bool(args.clean_rust),
            "clean_outputs": bool(cleanup_flags.get("clean_outputs", False)),
            "clean_reports": bool(cleanup_flags.get("clean_reports", False)),
            "clean_state": bool(cleanup_flags.get("clean_state", False)),
            "clean_venv": bool(cleanup_flags.get("clean_venv", False)),
            "clean_wal": bool(cleanup_flags.get("clean_wal", False)),
            "clean_data": bool(cleanup_flags.get("clean_data", False)),
            "guard_no_tracked_path": guard_targets,
        },
        "skills_used": list(SKILLS_USED),
        "roles_used": list(ROLES_USED),
        "before": before,
        "after": after,
        "cleanup_steps": cleanup_rows,
        "cleanup_status": cleanup_status,
        "gate_steps": gate_rows,
        "gate_status": gate_status,
        "result": {
            "overall": overall,
            "recommendation": recommendation,
        },
    }

    json_path = out_dir / f"release_converge_{ts}.json"
    md_path = out_dir / f"release_converge_{ts}.md"
    latest_json = out_dir / "latest.json"
    latest_md = out_dir / "latest.md"
    _write_json(json_path, report)
    _write_text(md_path, _build_markdown(report))
    _write_json(latest_json, report)
    _write_text(latest_md, md_path.read_text(encoding="utf-8"))

    print(f"[release-converge] json: {json_path}")
    print(f"[release-converge] md  : {md_path}")
    print(f"[release-converge] cleanup_status: {cleanup_status}")
    print(f"[release-converge] gate_status: {gate_status}")
    print(f"[release-converge] overall: {overall}")

    if overall == STATUS_PASS:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
