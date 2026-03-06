#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

REQUIRED_FIELDS: tuple[str, ...] = ("技能", "RACI", "Agent 角色", "KPI", "風險與緩解", "依賴", "Gate 證據")
TODO_TO_WS: dict[str, str] = {"1.4": "WS-G", "2.4": "WS-H"}
EXPECTED_SKILLS: dict[str, set[str]] = {
    "WS-G": {"hft-strategy-dev", "rust_feature_engineering", "performance-profiling"},
    "WS-H": {"hft-alpha-research", "validation-gate", "clickhouse-io"},
}
ROLE_DICTIONARY: set[str] = {
    "Rust Lead",
    "Tech Lead",
    "Strategy Owner",
    "Ops Oncall",
    "Research Lead",
    "Head of Research",
    "Data Steward",
    "Trading Runtime Owner",
}
REQUIRED_AGENT_ROLES: set[str] = {"explorer", "worker", "default"}
WS_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "WS-A": (
        "ws_a/latest_burn_in_template.json",
    ),
    "WS-B": (
        "ws_b/latest_mv_baseline_report.json",
    ),
    "WS-C": (
        "ws_c/latest_quality_routing_report.json",
    ),
    "WS-F": (
        "ws_f/latest_monthly_review_flow.json",
    ),
    "WS-G": (
        "ws_g/latest_hotpath_matrix.json",
        "ws_g/latest_cutover_backlog.md",
    ),
    "WS-H": (
        "ws_h/latest_source_catalog.json",
        "ws_h/latest_quality_report.json",
        "ws_h/latest_factory_pipeline.md",
        "ws_h/latest_promotion_readiness.json",
    ),
}


@dataclass(frozen=True)
class Block:
    ident: str
    title: str
    fields: dict[str, dict[str, Any]]


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
    board = payload.get("execution_board", {}) if isinstance(payload.get("execution_board"), dict) else {}
    tasks = board.get("tasks", []) if isinstance(board.get("tasks"), list) else []

    lines: list[str] = []
    lines.append("# Roadmap Delivery Guard Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append(f"- recommendation: `{result.get('recommendation')}`")
    lines.append("")
    lines.append("## Governance Checks")
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
    lines.append("## 30-Day Execution Board")
    lines.append("")
    lines.append("| ws | owner | deadline | output | acceptance |")
    lines.append("|---|---|---|---|---|")
    for task in tasks:
        if not isinstance(task, dict):
            continue
        lines.append(
            f"| `{task.get('ws')}` | `{task.get('owner')}` | `{task.get('deadline')}` | "
            f"{task.get('output')} | {task.get('acceptance')} |"
        )
    lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _extract_blocks(lines: list[str], pattern: re.Pattern[str]) -> dict[str, Block]:
    blocks: dict[str, Block] = {}
    current_id: str | None = None
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_id, current_title, current_lines
        if current_id is None:
            return
        blocks[current_id] = Block(ident=current_id, title=current_title, fields=_parse_fields(current_lines))
        current_id = None
        current_title = ""
        current_lines = []

    for line in lines:
        m = pattern.match(line)
        if m:
            flush()
            current_id = m.group("id").strip()
            current_title = m.group("title").strip()
            continue
        if current_id is not None:
            if line.startswith("### ") or line.startswith("## "):
                flush()
            else:
                current_lines.append(line.rstrip("\n"))
    flush()
    return blocks


def _parse_fields(lines: list[str]) -> dict[str, dict[str, Any]]:
    fields: dict[str, dict[str, Any]] = {}
    current: str | None = None
    for raw in lines:
        m = re.match(r"^- ([^：]+)：\s*(.*)$", raw.strip())
        if m:
            key = m.group(1).strip()
            fields[key] = {"value": m.group(2).strip(), "items": []}
            current = key
            continue
        if current and re.match(r"^  - ", raw):
            item = re.sub(r"^  -\s*", "", raw).strip()
            fields[current]["items"].append(item)
            continue
        if raw.strip().startswith("- "):
            current = None
    return fields


def _parse_skills(raw: str) -> list[str]:
    items = re.findall(r"`([^`]+)`", raw)
    if items:
        return [i.strip() for i in items if i.strip()]
    return [p.strip() for p in re.split(r"[、,]", raw) if p.strip()]


def _parse_raci(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in re.split(r"[、,]", raw):
        m = re.match(r"\s*([RACI])\s*=\s*(.+?)\s*$", part)
        if not m:
            continue
        out[m.group(1)] = m.group(2).strip()
    return out


def _parse_agent_roles(raw: str) -> list[str]:
    items = re.findall(r"`([^`]+)`", raw)
    if not items:
        items = re.findall(r"\b(explorer|worker|default)\b", raw)
    return [i.strip() for i in items if i.strip()]


def _parse_30d_tasks(roadmap_lines: list[str]) -> list[dict[str, Any]]:
    in_section = False
    tasks: list[dict[str, Any]] = []

    for raw in roadmap_lines:
        if raw.startswith("## 6."):
            in_section = True
            continue
        if in_section and raw.startswith("## "):
            break
        if not in_section:
            continue
        m = re.match(r"^\d+\.\s+(.*)$", raw.strip())
        if not m:
            continue
        body = m.group(1).strip()
        ws_match = re.search(r"\b(WS-[A-Z])\b", body)
        ws = ws_match.group(1) if ws_match else ""

        owner = ""
        deadline = ""
        output = ""
        acceptance = ""

        detail_match = re.search(r"（(.+)）", body)
        if detail_match:
            detail = detail_match.group(1)
            for part in detail.split("；"):
                p = part.strip()
                if p.startswith("Owner:"):
                    owner = p.split(":", 1)[1].strip()
                elif p.startswith("截止:"):
                    deadline = p.split(":", 1)[1].strip()
                elif p.startswith("輸出:"):
                    output = p.split(":", 1)[1].strip()
                elif p.startswith("驗收:"):
                    acceptance = p.split(":", 1)[1].strip()

        tasks.append(
            {
                "raw": body,
                "ws": ws,
                "owner": owner,
                "deadline": deadline,
                "output": output,
                "acceptance": acceptance,
            }
        )
    return tasks


def _parse_iso_date(value: str) -> bool:
    if not value:
        return False
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _parse_generated_at(value: str) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _artifact_age_hours(path: Path) -> float | None:
    try:
        if path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                generated_at = _parse_generated_at(str(payload.get("generated_at", "")))
                if generated_at is not None:
                    delta = _now_utc() - generated_at
                    return max(delta.total_seconds() / 3600.0, 0.0)
        mtime_utc = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.timezone.utc)
        delta = _now_utc() - mtime_utc
        return max(delta.total_seconds() / 3600.0, 0.0)
    except (OSError, ValueError, TypeError):
        return None


def _evaluate(
    todo_path: Path,
    roadmap_path: Path,
    *,
    execution_dir: Path | None = None,
    max_artifact_age_hours: float = 168.0,
) -> dict[str, Any]:
    todo_lines = todo_path.read_text(encoding="utf-8").splitlines()
    roadmap_lines = roadmap_path.read_text(encoding="utf-8").splitlines()

    todo_blocks = _extract_blocks(todo_lines, re.compile(r"^### (?P<id>\d+\.\d+)\s+(?P<title>.+)$"))
    roadmap_blocks = _extract_blocks(roadmap_lines, re.compile(r"^### (?P<id>WS-[A-Z])：(?P<title>.+)$"))
    tasks = _parse_30d_tasks(roadmap_lines)

    checks: list[dict[str, Any]] = []

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

    for todo_id, ws_id in TODO_TO_WS.items():
        tb = todo_blocks.get(todo_id)
        rb = roadmap_blocks.get(ws_id)
        add(
            f"{todo_id}_block_exists",
            tb is not None,
            severity="critical",
            expected="present",
            current=bool(tb),
            message=f"TODO block {todo_id} must exist",
        )
        add(
            f"{ws_id}_block_exists",
            rb is not None,
            severity="critical",
            expected="present",
            current=bool(rb),
            message=f"ROADMAP block {ws_id} must exist",
        )
        if tb is None or rb is None:
            continue

        for f in REQUIRED_FIELDS:
            add(
                f"{todo_id}_{f}_present",
                f in tb.fields,
                severity="critical",
                expected="present",
                current=sorted(tb.fields),
                message=f"TODO {todo_id} requires field {f}",
            )
            add(
                f"{ws_id}_{f}_present",
                f in rb.fields,
                severity="critical",
                expected="present",
                current=sorted(rb.fields),
                message=f"ROADMAP {ws_id} requires field {f}",
            )

        todo_skills = _parse_skills(tb.fields.get("技能", {}).get("value", ""))
        roadmap_skills = _parse_skills(rb.fields.get("技能", {}).get("value", ""))
        add(
            f"{ws_id}_skills_aligned",
            set(todo_skills) == set(roadmap_skills),
            severity="critical",
            expected=sorted(todo_skills),
            current=sorted(roadmap_skills),
            message=f"TODO {todo_id} and ROADMAP {ws_id} skills must align",
        )
        if ws_id in EXPECTED_SKILLS:
            add(
                f"{ws_id}_skills_expected",
                set(roadmap_skills) == EXPECTED_SKILLS[ws_id],
                severity="major",
                expected=sorted(EXPECTED_SKILLS[ws_id]),
                current=sorted(roadmap_skills),
                message=f"{ws_id} skills should match governance baseline",
                warn_only=True,
            )

        todo_raci = _parse_raci(tb.fields.get("RACI", {}).get("value", ""))
        roadmap_raci = _parse_raci(rb.fields.get("RACI", {}).get("value", ""))
        add(
            f"{ws_id}_raci_keys_complete",
            set(roadmap_raci) == {"R", "A", "C", "I"},
            severity="critical",
            expected=["R", "A", "C", "I"],
            current=sorted(roadmap_raci),
            message=f"{ws_id} RACI must contain R/A/C/I",
        )
        add(
            f"{ws_id}_raci_aligned",
            todo_raci == roadmap_raci,
            severity="critical",
            expected=todo_raci,
            current=roadmap_raci,
            message=f"TODO {todo_id} and ROADMAP {ws_id} RACI must align",
        )
        add(
            f"{ws_id}_raci_roles_in_dictionary",
            all(v in ROLE_DICTIONARY for v in roadmap_raci.values()),
            severity="major",
            expected=sorted(ROLE_DICTIONARY),
            current=sorted(set(roadmap_raci.values())),
            message=f"{ws_id} RACI roles should use fixed role dictionary",
            warn_only=True,
        )

        agent_roles = set(_parse_agent_roles(rb.fields.get("Agent 角色", {}).get("value", "")))
        add(
            f"{ws_id}_agent_roles_complete",
            REQUIRED_AGENT_ROLES.issubset(agent_roles),
            severity="critical",
            expected=sorted(REQUIRED_AGENT_ROLES),
            current=sorted(agent_roles),
            message=f"{ws_id} Agent roles must include explorer/worker/default",
        )

        ws_tasks = [t for t in tasks if t.get("ws") == ws_id]
        add(
            f"{ws_id}_30d_task_exists",
            bool(ws_tasks),
            severity="critical",
            expected="at least one 30-day task",
            current=len(ws_tasks),
            message=f"{ws_id} must have at least one task in 30-day action section",
        )

        for idx, task in enumerate(ws_tasks, start=1):
            owner = str(task.get("owner") or "")
            deadline = str(task.get("deadline") or "")
            output = str(task.get("output") or "")
            acceptance = str(task.get("acceptance") or "")
            add(
                f"{ws_id}_task{idx}_owner_present",
                bool(owner),
                severity="critical",
                expected="non-empty owner",
                current=owner,
                message=f"{ws_id} task {idx} must define owner",
            )
            add(
                f"{ws_id}_task{idx}_deadline_iso",
                _parse_iso_date(deadline),
                severity="critical",
                expected="YYYY-MM-DD",
                current=deadline,
                message=f"{ws_id} task {idx} deadline must be ISO date",
            )
            add(
                f"{ws_id}_task{idx}_output_present",
                bool(output),
                severity="critical",
                expected="non-empty output",
                current=output,
                message=f"{ws_id} task {idx} must define output",
            )
            add(
                f"{ws_id}_task{idx}_acceptance_present",
                bool(acceptance),
                severity="critical",
                expected="non-empty acceptance",
                current=acceptance,
                message=f"{ws_id} task {idx} must define acceptance",
            )
            if roadmap_raci.get("R"):
                add(
                    f"{ws_id}_task{idx}_owner_matches_R",
                    owner == roadmap_raci.get("R"),
                    severity="major",
                    expected=roadmap_raci.get("R"),
                    current=owner,
                    message=f"{ws_id} task {idx} owner should match RACI R",
                    warn_only=True,
                )

    task_ws_ids = sorted({str(task.get("ws") or "") for task in tasks if str(task.get("ws") or "")})

    artifact_summary: dict[str, Any] = {
        "enabled": execution_dir is not None,
        "execution_dir": str(execution_dir.resolve()) if execution_dir else "",
        "max_artifact_age_hours": float(max_artifact_age_hours),
        "artifacts": {},
    }
    if execution_dir is not None:
        add(
            "execution_dir_exists",
            execution_dir.exists() and execution_dir.is_dir(),
            severity="critical",
            expected="directory exists",
            current=str(execution_dir),
            message="Execution artifact directory must exist",
        )
        if execution_dir.exists() and execution_dir.is_dir():
            summary_path = execution_dir / "summary" / "latest.json"
            summary_exists = summary_path.exists()
            add(
                "execution_summary_exists",
                summary_exists,
                severity="critical",
                expected="present",
                current=str(summary_path),
                message="Execution summary artifact must exist",
            )
            if summary_exists:
                summary_payload = {}
                try:
                    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    summary_payload = {}

                summary_overall = str((summary_payload.get("result") or {}).get("overall", ""))
                add(
                    "execution_summary_not_fail",
                    summary_overall in {STATUS_PASS, STATUS_WARN},
                    severity="major",
                    expected=["pass", "warn"],
                    current=summary_overall,
                    message="Execution summary overall should not be fail",
                    warn_only=True,
                )
                for ws in task_ws_ids:
                    if ws not in WS_ARTIFACTS:
                        continue
                    ws_key = ws.lower().replace("-", "_")
                    ws_status = str((summary_payload.get(ws_key) or {}).get("status", ""))
                    add(
                        f"{ws}_execution_status_not_fail",
                        ws_status in {STATUS_PASS, STATUS_WARN},
                        severity="major",
                        expected=["pass", "warn"],
                        current=ws_status,
                        message=f"{ws} execution status should not be fail",
                        warn_only=True,
                    )

            for ws in task_ws_ids:
                rel_paths = WS_ARTIFACTS.get(ws, ())
                add(
                    f"{ws}_artifact_profile_defined",
                    bool(rel_paths),
                    severity="critical",
                    expected=sorted(WS_ARTIFACTS),
                    current=ws,
                    message=f"{ws} task must map to a known execution artifact set",
                )
                if not rel_paths:
                    continue
                ws_artifacts: dict[str, Any] = {}
                for rel in rel_paths:
                    artifact_path = execution_dir / rel
                    exists = artifact_path.exists()
                    add(
                        f"{ws}_artifact_{rel.replace('/', '_')}_exists",
                        exists,
                        severity="critical",
                        expected="present",
                        current=str(artifact_path),
                        message=f"{ws} artifact {rel} must exist",
                    )
                    row: dict[str, Any] = {
                        "path": str(artifact_path),
                        "exists": exists,
                    }
                    if exists:
                        age_h = _artifact_age_hours(artifact_path)
                        row["age_hours"] = age_h
                        age_ok = (age_h is not None) and (age_h <= float(max_artifact_age_hours))
                        add(
                            f"{ws}_artifact_{rel.replace('/', '_')}_fresh",
                            bool(age_ok),
                            severity="major",
                            expected=f"age_hours <= {max_artifact_age_hours}",
                            current=age_h,
                            message=f"{ws} artifact {rel} should be refreshed within age threshold",
                            warn_only=True,
                        )
                    ws_artifacts[rel] = row
                artifact_summary["artifacts"][ws] = ws_artifacts

    overall = STATUS_PASS
    for check in checks:
        overall = _combine_status(overall, str(check.get("status") or STATUS_PASS))
    recommendation = "go" if overall == STATUS_PASS else ("go_with_warnings" if overall == STATUS_WARN else "block")

    return {
        "result": {
            "overall": overall,
            "recommendation": recommendation,
            "checks": checks,
        },
        "governance": {
            "todo_blocks": sorted(todo_blocks.keys()),
            "roadmap_blocks": sorted(roadmap_blocks.keys()),
            "required_fields": list(REQUIRED_FIELDS),
        },
        "execution_board": {
            "tasks": tasks,
            "task_count": len(tasks),
            "task_count_by_ws": {
                ws: len([t for t in tasks if t.get("ws") == ws]) for ws in sorted({str(t.get("ws") or "") for t in tasks})
            },
        },
        "execution_artifacts": artifact_summary,
    }


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="TODO/ROADMAP delivery guard and execution board generator")
    p.add_argument("--todo", default="docs/TODO.md", help="TODO markdown path")
    p.add_argument("--roadmap", default="ROADMAP.md", help="ROADMAP markdown path")
    p.add_argument(
        "--execution-dir",
        default="",
        help="Optional execution artifact directory (e.g., outputs/roadmap_execution)",
    )
    p.add_argument(
        "--max-artifact-age-hours",
        type=float,
        default=168.0,
        help="Freshness threshold for execution artifacts (hours)",
    )
    p.add_argument("--output-dir", default="outputs/roadmap_delivery", help="Output directory")
    p.add_argument("--allow-warn-exit-zero", action="store_true", help="Exit 0 when overall status is warn")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    todo_path = Path(args.todo)
    roadmap_path = Path(args.roadmap)
    execution_dir = Path(args.execution_dir) if str(args.execution_dir).strip() else None
    output_dir = Path(args.output_dir)
    ts = _stamp()

    payload: dict[str, Any] = {
        "generated_at": _now_iso(),
        "todo_path": str(todo_path.resolve()),
        "roadmap_path": str(roadmap_path.resolve()),
        "execution_dir": str(execution_dir.resolve()) if execution_dir else "",
        "max_artifact_age_hours": float(args.max_artifact_age_hours),
    }

    if not todo_path.exists() or not roadmap_path.exists():
        payload["result"] = {
            "overall": STATUS_FAIL,
            "recommendation": "block",
            "checks": [
                {
                    "id": "input_paths_exist",
                    "status": STATUS_FAIL,
                    "severity": "critical",
                    "expected": "both markdown files exist",
                    "current": {"todo": str(todo_path), "roadmap": str(roadmap_path)},
                    "message": "TODO and ROADMAP paths must exist",
                }
            ],
        }
    else:
        payload.update(
            _evaluate(
                todo_path,
                roadmap_path,
                execution_dir=execution_dir,
                max_artifact_age_hours=float(args.max_artifact_age_hours),
            )
        )

    json_path = output_dir / f"roadmap_delivery_{ts}.json"
    md_path = output_dir / f"roadmap_delivery_{ts}.md"
    latest_json = output_dir / "latest.json"
    latest_md = output_dir / "latest.md"

    _write_json(json_path, payload)
    _write_markdown(md_path, payload)
    _write_json(latest_json, payload)
    latest_md.write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")

    overall = str(payload.get("result", {}).get("overall", STATUS_FAIL))
    print(f"[roadmap-delivery] json: {json_path}")
    print(f"[roadmap-delivery] md  : {md_path}")
    print(f"[roadmap-delivery] status: {overall}")

    if overall == STATUS_PASS:
        return 0
    if overall == STATUS_WARN and args.allow_warn_exit_zero:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
