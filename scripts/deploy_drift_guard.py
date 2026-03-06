#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shlex
import socket
import subprocess
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


@dataclass(frozen=True)
class CmdResult:
    code: int
    out: str
    err: str


DEFAULT_INCLUDE_PATHS = [
    "docker-compose.yml",
    "docker-stack.yml",
    "config",
    ".env",
    ".env.example",
    "Makefile",
]
DEFAULT_ENV_PREFIXES = ["HFT_", "CLICKHOUSE_", "REDIS_", "SHIOAJI_", "SYMBOLS_"]
DEFAULT_RUNTIME_SERVICES = ["hft-engine", "wal-loader", "hft-monitor", "prometheus", "clickhouse", "redis"]


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_text(text: str) -> str:
    return _sha256_bytes(text.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _run_cmd(argv: list[str], cwd: Path | None = None, timeout_s: int = 20) -> CmdResult:
    proc = subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return CmdResult(code=proc.returncode, out=proc.stdout, err=proc.stderr)


def _parse_compose_ps_json(output: str) -> list[dict[str, Any]]:
    payload = output.strip()
    if not payload:
        return []
    if payload.startswith("["):
        data = json.loads(payload)
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        return []

    rows: list[dict[str, Any]] = []
    for line in payload.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _collect_included_files(project_root: Path, includes: Iterable[str]) -> tuple[list[Path], list[str]]:
    files: list[Path] = []
    missing: list[str] = []

    for raw in includes:
        inc = str(raw).strip()
        if not inc:
            continue
        target = (project_root / inc).resolve()
        if not target.exists():
            missing.append(inc)
            continue
        if target.is_file():
            files.append(target)
            continue

        if target.is_dir():
            for p in sorted(target.rglob("*")):
                if p.is_file():
                    files.append(p)
            continue

        missing.append(inc)

    # De-duplicate while preserving sorted deterministic order.
    uniq = sorted(set(files), key=lambda p: str(p))
    return uniq, sorted(set(missing))


def _file_entries(project_root: Path, files: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for p in files:
        rel = p.relative_to(project_root)
        rows.append(
            {
                "path": rel.as_posix(),
                "size": p.stat().st_size,
                "sha256": _sha256_file(p),
            }
        )
    return rows


def _combined_hash(entries: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for row in entries:
        parts.append(f"{row['path']}:{row['sha256']}")
    return _sha256_text("\n".join(parts)) if parts else _sha256_text("")


def _git_snapshot(project_root: Path) -> dict[str, Any]:
    probe = _run_cmd(["git", "rev-parse", "--is-inside-work-tree"], cwd=project_root)
    if probe.code != 0 or probe.out.strip() != "true":
        return {
            "available": False,
            "error": (probe.err.strip() or probe.out.strip() or "not a git repository"),
        }

    head = _run_cmd(["git", "rev-parse", "HEAD"], cwd=project_root)
    branch = _run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=project_root)
    status = _run_cmd(["git", "status", "--porcelain=v1", "-uall"], cwd=project_root)
    lsfiles = _run_cmd(["git", "ls-files", "-s"], cwd=project_root)

    status_lines = [line for line in status.out.splitlines() if line.strip()]
    tracked_dirty = [line for line in status_lines if not line.startswith("?? ")]
    untracked = [line for line in status_lines if line.startswith("?? ")]

    return {
        "available": True,
        "head": head.out.strip(),
        "branch": branch.out.strip(),
        "status_lines": status_lines,
        "tracked_dirty_count": len(tracked_dirty),
        "untracked_count": len(untracked),
        "status_sha256": _sha256_text("\n".join(status_lines)),
        "index_sha256": _sha256_text(lsfiles.out.strip()),
    }


def _normalize_env(env_list: Iterable[str], prefixes: list[str]) -> dict[str, str]:
    env_map: dict[str, str] = {}
    for item in env_list:
        if "=" not in item:
            continue
        key, val = item.split("=", 1)
        if prefixes and not any(key.startswith(prefix) for prefix in prefixes):
            continue
        env_map[key] = val
    return {k: env_map[k] for k in sorted(env_map.keys())}


def _compose_snapshot(project_root: Path, prefixes: list[str], services: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "errors": [],
        "ps": [],
        "config_sha256": None,
        "runtime_env": {},
    }

    ps = _run_cmd(["docker", "compose", "ps", "--format", "json"], cwd=project_root)
    if ps.code != 0:
        out["errors"].append(ps.err.strip() or ps.out.strip() or f"docker compose ps exit={ps.code}")
        return out

    try:
        rows = _parse_compose_ps_json(ps.out)
    except Exception as exc:  # pragma: no cover - defensive guard
        out["errors"].append(f"parse compose ps failed: {exc}")
        rows = []

    out["ps"] = rows

    cfg = _run_cmd(["docker", "compose", "config", "--no-interpolate"], cwd=project_root)
    if cfg.code != 0:
        cfg = _run_cmd(["docker", "compose", "config"], cwd=project_root)
    if cfg.code == 0:
        out["config_sha256"] = _sha256_text(cfg.out)
    else:
        out["errors"].append(cfg.err.strip() or cfg.out.strip() or f"docker compose config exit={cfg.code}")

    allow = {x for x in services if x}
    for row in rows:
        service = str(row.get("Service") or "").strip()
        container = str(row.get("Name") or service).strip()
        state = str(row.get("State") or "unknown")
        if allow and service not in allow:
            continue

        ins = _run_cmd(["docker", "inspect", container], cwd=project_root)
        if ins.code != 0:
            out["errors"].append(f"docker inspect {container}: {ins.err.strip() or ins.out.strip()}")
            continue
        try:
            payload = json.loads(ins.out)
            obj = payload[0] if isinstance(payload, list) and payload else {}
        except Exception as exc:
            out["errors"].append(f"docker inspect parse {container}: {exc}")
            continue

        env_list = obj.get("Config", {}).get("Env", [])
        env_map = _normalize_env(env_list if isinstance(env_list, list) else [], prefixes)
        digest = _sha256_text("\n".join(f"{k}={v}" for k, v in env_map.items()))
        out["runtime_env"][service] = {
            "container": container,
            "state": state,
            "image": obj.get("Config", {}).get("Image") or "",
            "env": env_map,
            "env_sha256": digest,
        }

    return out


def _build_snapshot(
    project_root: Path,
    includes: list[str],
    prefixes: list[str],
    services: list[str],
    label: str | None = None,
) -> dict[str, Any]:
    files, missing = _collect_included_files(project_root, includes)
    entries = _file_entries(project_root, files)

    return {
        "generated_at": _now_iso(),
        "label": label,
        "host": socket.gethostname(),
        "project_root": str(project_root.resolve()),
        "includes": list(includes),
        "env_prefixes": list(prefixes),
        "services": list(services),
        "git": _git_snapshot(project_root),
        "files": {
            "count": len(entries),
            "missing_includes": missing,
            "combined_sha256": _combined_hash(entries),
            "entries": entries,
        },
        "compose": _compose_snapshot(project_root, prefixes, services),
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _compare_snapshots(
    baseline: dict[str, Any],
    current: dict[str, Any],
    *,
    allow_dirty_worktree: bool = False,
    allow_runtime_env_diff: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        cid: str,
        ok: bool,
        *,
        severity: str,
        baseline_val: Any,
        current_val: Any,
        message: str,
        allow: bool = False,
    ) -> None:
        if ok:
            status = STATUS_PASS
        elif allow:
            status = STATUS_WARN
        else:
            status = STATUS_FAIL
        checks.append(
            {
                "id": cid,
                "status": status,
                "severity": severity,
                "baseline": baseline_val,
                "current": current_val,
                "message": message,
            }
        )

    bgit = baseline.get("git", {}) if isinstance(baseline.get("git"), dict) else {}
    cgit = current.get("git", {}) if isinstance(current.get("git"), dict) else {}

    add(
        "git_available",
        bool(bgit.get("available")) and bool(cgit.get("available")),
        severity="critical",
        baseline_val=bgit.get("available"),
        current_val=cgit.get("available"),
        message="both baseline and current snapshots must be inside a git repository",
    )
    add(
        "git_head_same",
        str(bgit.get("head") or "") == str(cgit.get("head") or ""),
        severity="critical",
        baseline_val=bgit.get("head"),
        current_val=cgit.get("head"),
        message="HEAD commit drift detected",
    )
    add(
        "git_branch_same",
        str(bgit.get("branch") or "") == str(cgit.get("branch") or ""),
        severity="warning",
        baseline_val=bgit.get("branch"),
        current_val=cgit.get("branch"),
        message="branch drift detected",
    )

    dirty = int(cgit.get("tracked_dirty_count", 0)) + int(cgit.get("untracked_count", 0))
    add(
        "git_worktree_clean",
        dirty == 0,
        severity="critical",
        baseline_val={
            "tracked_dirty_count": bgit.get("tracked_dirty_count"),
            "untracked_count": bgit.get("untracked_count"),
        },
        current_val={
            "tracked_dirty_count": cgit.get("tracked_dirty_count"),
            "untracked_count": cgit.get("untracked_count"),
        },
        message="working tree drift detected",
        allow=allow_dirty_worktree,
    )

    bfiles = baseline.get("files", {}) if isinstance(baseline.get("files"), dict) else {}
    cfiles = current.get("files", {}) if isinstance(current.get("files"), dict) else {}
    add(
        "config_hash_same",
        str(bfiles.get("combined_sha256") or "") == str(cfiles.get("combined_sha256") or ""),
        severity="critical",
        baseline_val=bfiles.get("combined_sha256"),
        current_val=cfiles.get("combined_sha256"),
        message="config/layout hash drift detected",
    )

    bcompose = baseline.get("compose", {}) if isinstance(baseline.get("compose"), dict) else {}
    ccompose = current.get("compose", {}) if isinstance(current.get("compose"), dict) else {}
    bcfg = bcompose.get("config_sha256")
    ccfg = ccompose.get("config_sha256")
    add(
        "compose_config_hash_same",
        bool(bcfg) and bool(ccfg) and str(bcfg) == str(ccfg),
        severity="warning",
        baseline_val=bcfg,
        current_val=ccfg,
        message="docker compose resolved config drift or unavailable compose snapshot",
    )

    bruntime = bcompose.get("runtime_env", {}) if isinstance(bcompose.get("runtime_env"), dict) else {}
    cruntime = ccompose.get("runtime_env", {}) if isinstance(ccompose.get("runtime_env"), dict) else {}
    all_services = sorted(set(bruntime.keys()) | set(cruntime.keys()))
    runtime_diff: dict[str, Any] = {}
    for svc in all_services:
        bsvc = bruntime.get(svc)
        csvc = cruntime.get(svc)
        if not isinstance(bsvc, dict) or not isinstance(csvc, dict):
            runtime_diff[svc] = {"baseline": bsvc, "current": csvc, "reason": "service_missing"}
            continue
        if str(bsvc.get("env_sha256") or "") != str(csvc.get("env_sha256") or ""):
            runtime_diff[svc] = {
                "baseline_env_sha256": bsvc.get("env_sha256"),
                "current_env_sha256": csvc.get("env_sha256"),
                "reason": "env_diff",
            }
            continue
        if str(bsvc.get("image") or "") != str(csvc.get("image") or ""):
            runtime_diff[svc] = {
                "baseline_image": bsvc.get("image"),
                "current_image": csvc.get("image"),
                "reason": "image_diff",
            }

    add(
        "runtime_env_same",
        not runtime_diff,
        severity="critical",
        baseline_val={k: (v or {}).get("env_sha256") if isinstance(v, dict) else None for k, v in bruntime.items()},
        current_val={k: (v or {}).get("env_sha256") if isinstance(v, dict) else None for k, v in cruntime.items()},
        message="runtime env/image drift detected",
        allow=allow_runtime_env_diff,
    )

    overall = STATUS_PASS
    for c in checks:
        overall = _combine_status(overall, str(c.get("status") or STATUS_WARN))

    return {
        "overall": overall,
        "checks": checks,
        "runtime_diff": runtime_diff,
        "generated_at": _now_iso(),
    }


def _write_check_markdown(report: dict[str, Any], path: Path) -> None:
    lines: list[str] = []
    lines.append("# Deployment Drift Check Report")
    lines.append("")
    lines.append(f"- generated_at: `{report.get('generated_at')}`")
    lines.append(f"- overall: `{report.get('overall')}`")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| id | status | severity | message |")
    lines.append("|---|---|---|---|")
    for c in report.get("checks", []):
        lines.append(
            f"| `{c.get('id')}` | `{c.get('status')}` | `{c.get('severity')}` | {c.get('message')} |"
        )
    lines.append("")
    lines.append("## Runtime Drift")
    lines.append("")
    runtime_diff = report.get("runtime_diff") or {}
    if runtime_diff:
        lines.append("```json")
        lines.append(json.dumps(runtime_diff, indent=2, ensure_ascii=False))
        lines.append("```")
    else:
        lines.append("No runtime service env/image drift detected.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _create_backup_archive(project_root: Path, files: list[Path], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for p in files:
            arc = p.relative_to(project_root).as_posix()
            tar.add(p, arcname=arc, recursive=False)


def _write_rollback_script(path: Path, backup_tar: Path) -> None:
    script = f"""#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${{1:-$(pwd)}}"
SCRIPT_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
BACKUP_TAR="${{SCRIPT_DIR}}/{backup_tar.name}"

if [ ! -f "${{BACKUP_TAR}}" ]; then
  echo "backup archive not found: ${{BACKUP_TAR}}" >&2
  exit 2
fi

echo "[rollback] restoring files from ${{BACKUP_TAR}} into ${{PROJECT_ROOT}}"
tar -xzf "${{BACKUP_TAR}}" -C "${{PROJECT_ROOT}}"

echo "[rollback] file restore complete"
echo "[rollback] optional git rollback: git checkout <rollback-tag-or-branch>"
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _write_pre_sync_template(path: Path, change_id: str, rollback_tag: str, rollback_branch: str) -> None:
    content = f"""# Pre-Sync Change Template

- change_id: `{change_id}`
- generated_at: `{_now_iso()}`
- rollback_tag: `{rollback_tag}`
- rollback_branch: `{rollback_branch}`

## What

## Why

## Risk

## Validation
- `docker compose ps`
- `docker compose logs --tail=200 hft-engine`
- `curl -fsS http://localhost:9090/metrics | head`
- `uv run hft recorder status`

## Rollback Plan
1. Execute `rollback.sh <project_root>`
2. Optional git rollback: `git checkout {rollback_tag}`
3. Re-run validation checklist
"""
    path.write_text(content, encoding="utf-8")


def _run_snapshot(args: argparse.Namespace) -> int:
    project_root = Path(args.project_root).resolve()
    output_dir = Path(args.output_dir)
    includes = args.include_path if args.include_path else list(DEFAULT_INCLUDE_PATHS)
    prefixes = args.env_prefix if args.env_prefix else list(DEFAULT_ENV_PREFIXES)
    services = args.service if args.service else list(DEFAULT_RUNTIME_SERVICES)

    snapshot = _build_snapshot(project_root, includes, prefixes, services, label=args.label)
    filename = f"snapshot_{_stamp()}"
    if args.label:
        filename += f"_{args.label}"
    json_path = output_dir / "snapshots" / f"{filename}.json"
    _write_json(json_path, snapshot)

    print(f"[drift] snapshot: {json_path}")
    print(f"[drift] git head: {snapshot.get('git', {}).get('head')}")
    return 0


def _run_check(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    if not baseline_path.exists():
        print(f"[drift] baseline not found: {baseline_path}")
        return 2

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))

    project_root = Path(args.project_root).resolve()
    output_dir = Path(args.output_dir)
    includes = args.include_path if args.include_path else list(DEFAULT_INCLUDE_PATHS)
    prefixes = args.env_prefix if args.env_prefix else list(DEFAULT_ENV_PREFIXES)
    services = args.service if args.service else list(DEFAULT_RUNTIME_SERVICES)

    current = _build_snapshot(project_root, includes, prefixes, services, label="current_check")
    report = _compare_snapshots(
        baseline,
        current,
        allow_dirty_worktree=bool(args.allow_dirty_worktree),
        allow_runtime_env_diff=bool(args.allow_runtime_env_diff),
    )

    check_name = f"check_{_stamp()}"
    json_path = output_dir / "checks" / f"{check_name}.json"
    md_path = output_dir / "checks" / f"{check_name}.md"
    payload = {
        "generated_at": _now_iso(),
        "baseline_path": str(baseline_path.resolve()),
        "baseline": {
            "generated_at": baseline.get("generated_at"),
            "label": baseline.get("label"),
            "git_head": (baseline.get("git") or {}).get("head"),
        },
        "current": {
            "generated_at": current.get("generated_at"),
            "label": current.get("label"),
            "git_head": (current.get("git") or {}).get("head"),
        },
        "result": report,
    }
    _write_json(json_path, payload)
    _write_check_markdown(report, md_path)

    print(f"[drift] check json: {json_path}")
    print(f"[drift] check md  : {md_path}")
    print(f"[drift] overall   : {report['overall']}")

    if report["overall"] == STATUS_FAIL:
        return 2
    if report["overall"] == STATUS_WARN and not args.allow_warn_exit_zero:
        return 1
    return 0


def _run_prepare(args: argparse.Namespace) -> int:
    change_id = str(args.change_id).strip()
    if not change_id:
        print("[drift] change-id is required")
        return 2

    project_root = Path(args.project_root).resolve()
    output_dir = Path(args.output_dir)
    stamp = _stamp()

    includes = args.include_path if args.include_path else list(DEFAULT_INCLUDE_PATHS)
    prefixes = args.env_prefix if args.env_prefix else list(DEFAULT_ENV_PREFIXES)
    services = args.service if args.service else list(DEFAULT_RUNTIME_SERVICES)

    artifact_dir = output_dir / "pre_sync" / f"{change_id}_{stamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    snapshot = _build_snapshot(project_root, includes, prefixes, services, label=f"pre_sync_{change_id}")
    snapshot_path = artifact_dir / "pre_sync_snapshot.json"
    _write_json(snapshot_path, snapshot)

    files, missing = _collect_included_files(project_root, includes)
    backup_tar = artifact_dir / f"backup_{change_id}.tar.gz"
    _create_backup_archive(project_root, files, backup_tar)

    rollback_tag = args.rollback_tag or f"rollback/{change_id}/{stamp}"
    rollback_branch = args.rollback_branch or f"rollback/{change_id}"

    rollback_script = artifact_dir / "rollback.sh"
    _write_rollback_script(rollback_script, backup_tar)

    template_path = artifact_dir / "change_template.md"
    _write_pre_sync_template(template_path, change_id, rollback_tag, rollback_branch)

    created_refs: dict[str, Any] = {"created": False, "errors": []}
    if args.create_git_ref:
        tag_cmd = _run_cmd(
            [
                "git",
                "tag",
                "-a",
                rollback_tag,
                "-m",
                f"pre-sync rollback tag for {change_id} ({stamp})",
            ],
            cwd=project_root,
        )
        branch_cmd = _run_cmd(["git", "branch", rollback_branch], cwd=project_root)
        created_refs["created"] = tag_cmd.code == 0 and branch_cmd.code == 0
        if tag_cmd.code != 0:
            created_refs["errors"].append(tag_cmd.err.strip() or tag_cmd.out.strip())
        if branch_cmd.code != 0:
            created_refs["errors"].append(branch_cmd.err.strip() or branch_cmd.out.strip())

    manifest = {
        "generated_at": _now_iso(),
        "change_id": change_id,
        "artifact_dir": str(artifact_dir.resolve()),
        "project_root": str(project_root),
        "backup_tar": backup_tar.name,
        "rollback_script": rollback_script.name,
        "template": template_path.name,
        "rollback_tag": rollback_tag,
        "rollback_branch": rollback_branch,
        "missing_includes": missing,
        "tracked_files_count": len(files),
        "created_git_refs": created_refs,
        "suggested_commands": {
            "create_tag": f"git tag -a {shlex.quote(rollback_tag)} -m 'pre-sync rollback tag for {change_id}'",
            "create_branch": f"git branch {shlex.quote(rollback_branch)}",
        },
    }
    _write_json(artifact_dir / "manifest.json", manifest)

    print(f"[drift] pre-sync artifact: {artifact_dir}")
    print(f"[drift] backup archive   : {backup_tar}")
    print(f"[drift] rollback script  : {rollback_script}")
    if missing:
        print(f"[drift] missing includes : {', '.join(missing)}")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deployment drift checker and pre-sync artifact helper")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project-root", default=".", help="Project root")
        p.add_argument("--output-dir", default="outputs/deploy_guard", help="Output directory")
        p.add_argument(
            "--include-path",
            action="append",
            default=[],
            help="Include path for hash/backup snapshot (repeatable)",
        )
        p.add_argument(
            "--env-prefix",
            action="append",
            default=[],
            help="Runtime env key prefix to snapshot (repeatable)",
        )
        p.add_argument(
            "--service",
            action="append",
            default=[],
            help="Runtime service name allowlist (repeatable)",
        )

    snapshot = sub.add_parser("snapshot", help="Create a deployment drift baseline snapshot")
    add_common(snapshot)
    snapshot.add_argument("--label", default=None, help="Snapshot label")

    check = sub.add_parser("check", help="Compare current state with baseline snapshot")
    add_common(check)
    check.add_argument("--baseline", required=True, help="Baseline snapshot json path")
    check.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help="Treat worktree drift as warning instead of fail",
    )
    check.add_argument(
        "--allow-runtime-env-diff",
        action="store_true",
        help="Treat runtime env/image drift as warning instead of fail",
    )
    check.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit 0 when overall status is warn",
    )

    prepare = sub.add_parser("prepare", help="Generate pre-sync backup + rollback template artifact")
    add_common(prepare)
    prepare.add_argument("--change-id", required=True, help="Change request id (e.g. CHG-20260305-01)")
    prepare.add_argument("--rollback-tag", default=None, help="Override rollback tag")
    prepare.add_argument("--rollback-branch", default=None, help="Override rollback branch")
    prepare.add_argument(
        "--create-git-ref",
        action="store_true",
        help="Actually create rollback tag and branch",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "snapshot":
        return _run_snapshot(args)
    if args.command == "check":
        return _run_check(args)
    if args.command == "prepare":
        return _run_prepare(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
