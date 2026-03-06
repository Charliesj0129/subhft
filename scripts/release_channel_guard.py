#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import sys
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


def _safe_name(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_"}:
            out.append(ch)
        else:
            out.append("_")
    return "".join(out) or "unnamed"


def _combine_status(current: str, incoming: str) -> str:
    order = {STATUS_PASS: 0, STATUS_WARN: 1, STATUS_FAIL: 2}
    if order.get(incoming, 0) > order.get(current, 0):
        return incoming
    return current


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _parse_iso_datetime(raw: Any) -> dt.datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _age_hours(generated_at: Any, now_utc: dt.datetime) -> float | None:
    parsed = _parse_iso_datetime(generated_at)
    if parsed is None:
        return None
    delta = now_utc - parsed
    return delta.total_seconds() / 3600.0


def _find_latest(pattern: str) -> Path | None:
    candidates = sorted(Path(p) for p in glob.glob(pattern))
    if not candidates:
        return None
    return candidates[-1]


def _resolve_manifest(output_dir: Path, change_id: str, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    pattern = str((output_dir / "pre_sync").resolve() / f"{change_id}_*" / "manifest.json")
    return _find_latest(pattern)


def _resolve_canary_report(soak_dir: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    pattern = str((soak_dir / "canary" / "canary_*.json").resolve())
    return _find_latest(pattern)


def _resolve_drift_report(output_dir: Path, explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    pattern = str((output_dir / "checks" / "check_*.json").resolve())
    return _find_latest(pattern)


def _write_gate_markdown(payload: dict[str, Any], path: Path) -> None:
    result = payload.get("result", {}) if isinstance(payload.get("result"), dict) else {}
    checks = result.get("checks", []) if isinstance(result.get("checks"), list) else []

    lines: list[str] = []
    lines.append("# Release Channel Gate Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- change_id: `{payload.get('change_id')}`")
    lines.append(f"- transition: `{payload.get('from_channel')} -> {payload.get('to_channel')}`")
    lines.append(f"- overall: `{result.get('overall')}`")
    lines.append(f"- recommendation: `{result.get('recommendation')}`")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    evidence = payload.get("evidence", {}) if isinstance(payload.get("evidence"), dict) else {}
    lines.append(f"- manifest: `{evidence.get('manifest_path')}`")
    lines.append(f"- canary_report: `{evidence.get('canary_report_path')}`")
    lines.append(f"- drift_report: `{evidence.get('drift_report_path')}`")
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

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _evaluate_gate(
    *,
    change_id: str,
    output_dir: Path,
    soak_dir: Path,
    manifest_path: Path | None,
    canary_report_path: Path | None,
    drift_report_path: Path | None,
    min_trading_days: int,
    max_report_age_hours: float,
    allow_canary_warn: bool,
    allow_drift_warn: bool,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        cid: str,
        ok: bool,
        *,
        severity: str,
        expected: Any,
        current: Any,
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
                "expected": expected,
                "current": current,
                "message": message,
            }
        )

    now_utc = _now_utc()

    manifest_obj: dict[str, Any] = {}
    if manifest_path and manifest_path.exists():
        try:
            manifest_obj = _read_json(manifest_path)
        except Exception as exc:
            add(
                "manifest_json_parse",
                False,
                severity="critical",
                expected="valid json",
                current=str(exc),
                message="failed to parse pre-sync manifest json",
            )

    add(
        "manifest_exists",
        bool(manifest_path and manifest_path.exists()),
        severity="critical",
        expected="manifest json exists",
        current=str(manifest_path) if manifest_path else None,
        message="pre-sync manifest is required for channel promotion",
    )

    if manifest_obj:
        add(
            "manifest_change_id_match",
            str(manifest_obj.get("change_id") or "") == change_id,
            severity="critical",
            expected=change_id,
            current=manifest_obj.get("change_id"),
            message="manifest change_id must match requested change",
        )

        required_fields = ["backup_tar", "rollback_script", "template"]
        missing_fields = [k for k in required_fields if not str(manifest_obj.get(k) or "").strip()]
        add(
            "manifest_required_fields",
            not missing_fields,
            severity="critical",
            expected=required_fields,
            current=missing_fields,
            message="manifest must include backup/rollback/template fields",
        )

        artifact_dir_raw = str(manifest_obj.get("artifact_dir") or "").strip()
        if artifact_dir_raw:
            artifact_dir = Path(artifact_dir_raw)
            if not artifact_dir.is_absolute() and manifest_path is not None:
                artifact_dir = (manifest_path.parent / artifact_dir).resolve()
        elif manifest_path is not None:
            artifact_dir = manifest_path.parent
        else:
            artifact_dir = output_dir

        required_files = [
            manifest_obj.get("backup_tar"),
            manifest_obj.get("rollback_script"),
            manifest_obj.get("template"),
        ]
        missing_artifacts: list[str] = []
        for name in required_files:
            if not isinstance(name, str) or not name.strip():
                continue
            if not (artifact_dir / name).exists():
                missing_artifacts.append(name)

        add(
            "manifest_artifacts_exist",
            not missing_artifacts,
            severity="critical",
            expected="backup/rollback/template files exist",
            current=missing_artifacts,
            message="manifest artifact files must exist",
        )

        manifest_age = _age_hours(manifest_obj.get("generated_at"), now_utc)
        add(
            "manifest_freshness",
            manifest_age is not None and manifest_age <= max_report_age_hours,
            severity="warning",
            expected=f"<= {max_report_age_hours}h",
            current=None if manifest_age is None else round(manifest_age, 3),
            message="pre-sync manifest should be recent for the release window",
        )

    canary_obj: dict[str, Any] = {}
    if canary_report_path and canary_report_path.exists():
        try:
            canary_obj = _read_json(canary_report_path)
        except Exception as exc:
            add(
                "canary_json_parse",
                False,
                severity="critical",
                expected="valid json",
                current=str(exc),
                message="failed to parse canary report json",
            )

    add(
        "canary_report_exists",
        bool(canary_report_path and canary_report_path.exists()),
        severity="critical",
        expected="canary report json exists",
        current=str(canary_report_path) if canary_report_path else None,
        message="canary acceptance report is required",
    )

    if canary_obj:
        result = canary_obj.get("result", {}) if isinstance(canary_obj.get("result"), dict) else {}
        canary_overall = str(result.get("overall") or "")
        allow_statuses = {STATUS_PASS}
        if allow_canary_warn:
            allow_statuses.add(STATUS_WARN)

        add(
            "canary_overall",
            canary_overall == STATUS_PASS,
            severity="critical",
            expected=STATUS_PASS,
            current=canary_overall,
            message="canary overall must be pass before stable promotion",
            allow=allow_canary_warn and canary_overall == STATUS_WARN,
        )

        trading_days = result.get("trading_days")
        add(
            "canary_min_trading_days",
            isinstance(trading_days, int) and trading_days >= min_trading_days,
            severity="critical",
            expected=f">= {min_trading_days}",
            current=trading_days,
            message="insufficient canary trading days",
        )

        canary_age = _age_hours(canary_obj.get("generated_at"), now_utc)
        add(
            "canary_freshness",
            canary_age is not None and canary_age <= max_report_age_hours,
            severity="warning",
            expected=f"<= {max_report_age_hours}h",
            current=None if canary_age is None else round(canary_age, 3),
            message="canary report should be recent for promotion",
        )

        if canary_overall not in allow_statuses:
            # Keep an explicit guard so the decision payload records accepted statuses.
            add(
                "canary_allowed_status_set",
                False,
                severity="critical",
                expected=sorted(allow_statuses),
                current=canary_overall,
                message="canary status is outside allowed policy",
            )
        else:
            add(
                "canary_allowed_status_set",
                True,
                severity="warning",
                expected=sorted(allow_statuses),
                current=canary_overall,
                message="canary status is within allowed policy",
            )

    drift_obj: dict[str, Any] = {}
    if drift_report_path and drift_report_path.exists():
        try:
            drift_obj = _read_json(drift_report_path)
        except Exception as exc:
            add(
                "drift_json_parse",
                False,
                severity="critical",
                expected="valid json",
                current=str(exc),
                message="failed to parse drift check json",
            )

    add(
        "drift_report_exists",
        bool(drift_report_path and drift_report_path.exists()),
        severity="critical",
        expected="drift report json exists",
        current=str(drift_report_path) if drift_report_path else None,
        message="drift check report is required",
    )

    if drift_obj:
        result = drift_obj.get("result", {}) if isinstance(drift_obj.get("result"), dict) else {}
        drift_overall = str(result.get("overall") or "")
        add(
            "drift_overall",
            drift_overall == STATUS_PASS,
            severity="critical",
            expected=STATUS_PASS,
            current=drift_overall,
            message="drift overall must be pass before stable promotion",
            allow=allow_drift_warn and drift_overall == STATUS_WARN,
        )

        drift_age = _age_hours(drift_obj.get("generated_at"), now_utc)
        add(
            "drift_freshness",
            drift_age is not None and drift_age <= max_report_age_hours,
            severity="warning",
            expected=f"<= {max_report_age_hours}h",
            current=None if drift_age is None else round(drift_age, 3),
            message="drift check should be recent for promotion",
        )

    overall = STATUS_PASS
    for check in checks:
        if not isinstance(check, dict):
            continue
        overall = _combine_status(overall, str(check.get("status") or STATUS_WARN))

    return {
        "generated_at": _now_iso(),
        "change_id": change_id,
        "from_channel": "canary",
        "to_channel": "stable",
        "thresholds": {
            "min_trading_days": min_trading_days,
            "max_report_age_hours": max_report_age_hours,
            "allow_canary_warn": allow_canary_warn,
            "allow_drift_warn": allow_drift_warn,
        },
        "evidence": {
            "manifest_path": str(manifest_path.resolve()) if manifest_path else None,
            "canary_report_path": str(canary_report_path.resolve()) if canary_report_path else None,
            "drift_report_path": str(drift_report_path.resolve()) if drift_report_path else None,
        },
        "result": {
            "overall": overall,
            "recommendation": "promote" if overall == STATUS_PASS else "hold",
            "checks": checks,
        },
    }


def _emit_gate_artifacts(report: dict[str, Any], output_dir: Path) -> tuple[Path, Path]:
    safe_change = _safe_name(str(report.get("change_id") or "unknown"))
    name = f"release_gate_{_stamp()}_{safe_change}"
    decision_dir = output_dir / "release_channel" / "decisions"
    json_path = decision_dir / f"{name}.json"
    md_path = decision_dir / f"{name}.md"
    _write_json(json_path, report)
    _write_gate_markdown(report, md_path)
    return json_path, md_path


def _gate_common(args: argparse.Namespace) -> tuple[dict[str, Any], Path, Path, int]:
    change_id = str(args.change_id).strip()
    if not change_id:
        raise ValueError("change-id is required")

    output_dir = Path(args.output_dir)
    soak_dir = Path(args.soak_dir)

    manifest = _resolve_manifest(output_dir, change_id, args.manifest)
    canary_report = _resolve_canary_report(soak_dir, args.canary_report)
    drift_report = _resolve_drift_report(output_dir, args.drift_report)

    report = _evaluate_gate(
        change_id=change_id,
        output_dir=output_dir,
        soak_dir=soak_dir,
        manifest_path=manifest,
        canary_report_path=canary_report,
        drift_report_path=drift_report,
        min_trading_days=int(args.min_trading_days),
        max_report_age_hours=float(args.max_report_age_hours),
        allow_canary_warn=bool(args.allow_canary_warn),
        allow_drift_warn=bool(args.allow_drift_warn),
    )

    json_path, md_path = _emit_gate_artifacts(report, output_dir)

    result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
    overall = str(result.get("overall") or STATUS_FAIL)
    if overall == STATUS_FAIL:
        rc = 2
    elif overall == STATUS_WARN:
        rc = 1
    else:
        rc = 0

    return report, json_path, md_path, rc


def _run_gate(args: argparse.Namespace) -> int:
    try:
        report, json_path, md_path, rc = _gate_common(args)
    except ValueError as exc:
        print(f"[release] {exc}")
        return 2

    print(f"[release] gate json: {json_path}")
    print(f"[release] gate md  : {md_path}")
    print(f"[release] overall  : {report.get('result', {}).get('overall')}")

    if rc == 1 and args.allow_warn_exit_zero:
        return 0
    return rc


def _run_promote(args: argparse.Namespace) -> int:
    try:
        report, json_path, md_path, rc = _gate_common(args)
    except ValueError as exc:
        print(f"[release] {exc}")
        return 2

    print(f"[release] gate json: {json_path}")
    print(f"[release] gate md  : {md_path}")
    print(f"[release] overall  : {report.get('result', {}).get('overall')}")

    if not args.apply:
        return rc

    result = report.get("result", {}) if isinstance(report.get("result"), dict) else {}
    if result.get("overall") != STATUS_PASS:
        print("[release] promotion blocked: gate overall must be pass when --apply is used")
        return 2

    output_dir = Path(args.output_dir)
    safe_change = _safe_name(str(report.get("change_id") or "unknown"))
    name = f"stable_{_stamp()}_{safe_change}"
    promotion_path = output_dir / "release_channel" / "promotions" / f"{name}.json"

    payload = {
        "promoted_at": _now_iso(),
        "change_id": report.get("change_id"),
        "actor": args.actor,
        "from_channel": report.get("from_channel"),
        "to_channel": report.get("to_channel"),
        "gate_decision_path": str(json_path.resolve()),
        "gate_decision_md": str(md_path.resolve()),
        "evidence": report.get("evidence"),
        "result": "promoted",
    }
    _write_json(promotion_path, payload)

    print(f"[release] promotion record: {promotion_path}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Release channel guard (canary -> stable)")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project-root", default=".", help="Reserved for interface parity")
        p.add_argument("--output-dir", default="outputs/deploy_guard", help="deploy guard output directory")
        p.add_argument("--soak-dir", default="outputs/soak_reports", help="soak reports output directory")
        p.add_argument("--change-id", required=True, help="change id (e.g. CHG-20260305-02)")
        p.add_argument("--manifest", default=None, help="override pre-sync manifest path")
        p.add_argument("--canary-report", default=None, help="override canary report json path")
        p.add_argument("--drift-report", default=None, help="override drift check json path")
        p.add_argument("--min-trading-days", type=int, default=5, help="minimum canary trading days")
        p.add_argument(
            "--max-report-age-hours",
            type=float,
            default=72.0,
            help="maximum age for evidence reports",
        )
        p.add_argument(
            "--allow-canary-warn",
            action="store_true",
            help="downgrade canary overall=warn from fail to warn",
        )
        p.add_argument(
            "--allow-drift-warn",
            action="store_true",
            help="downgrade drift overall=warn from fail to warn",
        )

    gate = sub.add_parser("gate", help="evaluate canary->stable promotion gate")
    add_common(gate)
    gate.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="exit zero when gate overall is warn",
    )

    promote = sub.add_parser("promote", help="run gate and optionally write stable promotion record")
    add_common(promote)
    promote.add_argument("--apply", action="store_true", help="write stable promotion record if gate passes")
    promote.add_argument("--actor", default="ops", help="operator name for promotion record")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "gate":
        return _run_gate(args)
    if args.command == "promote":
        return _run_promote(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
