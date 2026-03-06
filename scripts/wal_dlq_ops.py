#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat()


def _stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append("# WAL DLQ Ops Report")
    lines.append("")
    lines.append(f"- generated_at: `{payload.get('generated_at')}`")
    lines.append(f"- command: `{payload.get('command')}`")
    lines.append(f"- overall: `{payload.get('overall')}`")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(payload.get("result"), indent=2, ensure_ascii=False))
    lines.append("```")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _artifact_paths(output_dir: Path, prefix: str) -> tuple[Path, Path]:
    name = f"{prefix}_{_stamp()}"
    return output_dir / f"{name}.json", output_dir / f"{name}.md"


def _scan_dlq(dlq_dir: Path) -> dict[str, Any]:
    if not dlq_dir.exists():
        return {
            "exists": False,
            "files": 0,
            "bytes": 0,
            "oldest_file": None,
            "oldest_age_hours": None,
            "newest_file": None,
            "newest_age_hours": None,
            "files_by_table": {},
        }

    rows: list[dict[str, Any]] = []
    table_counts: dict[str, int] = {}
    now = dt.datetime.now(dt.timezone.utc).timestamp()

    for p in sorted(dlq_dir.glob("*.jsonl")):
        try:
            st = p.stat()
        except OSError:
            continue
        age_h = max(0.0, (now - st.st_mtime) / 3600.0)
        rows.append({"name": p.name, "size": int(st.st_size), "age_hours": age_h, "mtime": st.st_mtime})
        table = p.stem.rsplit("_", 1)[0] if "_" in p.stem else p.stem
        table_counts[table] = table_counts.get(table, 0) + 1

    rows.sort(key=lambda x: x["mtime"])
    oldest = rows[0] if rows else None
    newest = rows[-1] if rows else None
    total_bytes = sum(int(r["size"]) for r in rows)

    return {
        "exists": True,
        "files": len(rows),
        "bytes": total_bytes,
        "oldest_file": oldest["name"] if oldest else None,
        "oldest_age_hours": round(float(oldest["age_hours"]), 3) if oldest else None,
        "newest_file": newest["name"] if newest else None,
        "newest_age_hours": round(float(newest["age_hours"]), 3) if newest else None,
        "files_by_table": table_counts,
    }


def _load_loader_cls():
    from hft_platform.recorder.loader import WALLoaderService

    return WALLoaderService


def _build_loader(args: argparse.Namespace):
    wal_loader_cls = _load_loader_cls()
    return wal_loader_cls(
        wal_dir=str(args.wal_dir),
        archive_dir=str(args.archive_dir),
        ch_host=str(args.ch_host),
        ch_port=int(args.ch_port),
    )


def _run_status(args: argparse.Namespace) -> int:
    dlq = _scan_dlq(Path(args.wal_dir) / "dlq")
    overall = STATUS_PASS if int(dlq.get("files", 0)) == 0 else STATUS_WARN

    payload = {
        "generated_at": _now_iso(),
        "command": "status",
        "overall": overall,
        "result": {
            "wal_dir": str(Path(args.wal_dir).resolve()),
            "dlq_dir": str((Path(args.wal_dir) / "dlq").resolve()),
            "dlq": dlq,
        },
    }

    out_dir = Path(args.output_dir) / "status"
    json_path, md_path = _artifact_paths(out_dir, "dlq_status")
    _write_json(json_path, payload)
    _write_markdown(md_path, payload)

    print(f"[wal-dlq] status json: {json_path}")
    print(f"[wal-dlq] status md  : {md_path}")
    print(f"[wal-dlq] files      : {dlq.get('files', 0)}")

    if overall == STATUS_WARN and not bool(args.allow_warn_exit_zero):
        return 1
    return 0


def _run_replay(args: argparse.Namespace) -> int:
    max_files = int(args.max_files) if args.max_files else None
    if bool(args.dry_run):
        # Dry-run should not require importing loader/clickhouse deps.
        dlq_dir = Path(args.wal_dir) / "dlq"
        files = sorted(dlq_dir.glob("*.jsonl")) if dlq_dir.exists() else []
        if isinstance(max_files, int) and max_files > 0:
            files = files[:max_files]
        table_counts: dict[str, int] = {}
        for p in files:
            table = p.stem.rsplit("_", 1)[0] if "_" in p.stem else p.stem
            table_counts[table] = table_counts.get(table, 0) + 1
        result = {
            "replayed": len(files),
            "skipped": 0,
            "failed": 0,
            "errors": [],
            "selected": len(files),
            "max_files": max_files,
            "mode": "dry_run_preview",
            "files_by_table": table_counts,
        }
    else:
        try:
            loader = _build_loader(args)
            loader.connect()
            result = loader.replay_dlq(dry_run=False, max_files=max_files)
        except Exception as exc:
            result = {
                "replayed": 0,
                "skipped": 0,
                "failed": 1,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "selected": 0,
                "max_files": max_files,
            }

    errors = result.get("errors", [])
    failed = int(result.get("failed", 0))
    overall = STATUS_PASS if failed == 0 and not errors else STATUS_FAIL
    if bool(args.dry_run) and overall == STATUS_PASS:
        overall = STATUS_WARN

    payload = {
        "generated_at": _now_iso(),
        "command": "replay",
        "overall": overall,
        "result": {
            "wal_dir": str(Path(args.wal_dir).resolve()),
            "archive_dir": str(Path(args.archive_dir).resolve()),
            "clickhouse": {"host": args.ch_host, "port": int(args.ch_port)},
            "dry_run": bool(args.dry_run),
            "max_files": int(args.max_files) if args.max_files else None,
            "replay_result": result,
        },
    }

    out_dir = Path(args.output_dir) / "replay"
    json_path, md_path = _artifact_paths(out_dir, "dlq_replay")
    _write_json(json_path, payload)
    _write_markdown(md_path, payload)

    print(f"[wal-dlq] replay json: {json_path}")
    print(f"[wal-dlq] replay md  : {md_path}")
    print(f"[wal-dlq] replayed   : {result.get('replayed', 0)}")
    print(f"[wal-dlq] failed     : {result.get('failed', 0)}")

    if overall == STATUS_FAIL:
        return 2
    if overall == STATUS_WARN and not bool(args.allow_warn_exit_zero):
        return 1
    return 0


def _run_cleanup_tmp(args: argparse.Namespace) -> int:
    wal_dir = Path(args.wal_dir)
    if not wal_dir.exists():
        print(f"[wal-dlq] wal dir not found: {wal_dir}")
        return 2

    min_age_seconds = float(args.min_age_seconds)
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    deleted: list[str] = []
    skipped: list[str] = []

    for p in sorted(wal_dir.glob("*.tmp")):
        try:
            st = p.stat()
        except OSError:
            continue
        age_s = now - st.st_mtime
        if age_s < min_age_seconds:
            skipped.append(p.name)
            continue
        if not args.dry_run:
            try:
                p.unlink()
            except OSError:
                skipped.append(p.name)
                continue
        deleted.append(p.name)

    payload = {
        "generated_at": _now_iso(),
        "command": "cleanup-tmp",
        "overall": STATUS_PASS,
        "result": {
            "wal_dir": str(wal_dir.resolve()),
            "dry_run": bool(args.dry_run),
            "min_age_seconds": min_age_seconds,
            "deleted_count": len(deleted),
            "skipped_count": len(skipped),
            "deleted_files": deleted,
            "skipped_files": skipped,
        },
    }

    out_dir = Path(args.output_dir) / "cleanup_tmp"
    json_path, md_path = _artifact_paths(out_dir, "dlq_cleanup_tmp")
    _write_json(json_path, payload)
    _write_markdown(md_path, payload)

    print(f"[wal-dlq] cleanup json: {json_path}")
    print(f"[wal-dlq] cleanup md  : {md_path}")
    print(f"[wal-dlq] deleted     : {len(deleted)}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WAL DLQ operations helper")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--wal-dir", default=".wal", help="WAL directory")
        p.add_argument("--archive-dir", default=".wal/archive", help="WAL archive directory")
        p.add_argument("--ch-host", default=os.getenv("HFT_CLICKHOUSE_HOST", "clickhouse"), help="ClickHouse host")
        p.add_argument(
            "--ch-port",
            type=int,
            default=int(os.getenv("HFT_CLICKHOUSE_PORT", "9000")),
            help="ClickHouse native port",
        )
        p.add_argument("--output-dir", default="outputs/wal_dlq", help="Artifact output directory")

    status = sub.add_parser("status", help="Report DLQ file count/size/age")
    add_common(status)
    status.add_argument("--allow-warn-exit-zero", action="store_true", help="Exit 0 even when DLQ files exist")

    replay = sub.add_parser("replay", help="Replay DLQ files into ClickHouse")
    add_common(replay)
    replay.add_argument("--dry-run", action="store_true", help="Preview replay without inserts/moves")
    replay.add_argument("--max-files", type=int, default=0, help="Max DLQ files to process (0=all)")
    replay.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Exit 0 when dry-run produces warn overall",
    )

    cleanup = sub.add_parser("cleanup-tmp", help="Cleanup orphan manifest .tmp files in WAL dir")
    add_common(cleanup)
    cleanup.add_argument("--dry-run", action="store_true", help="Preview only")
    cleanup.add_argument(
        "--min-age-seconds",
        type=float,
        default=300.0,
        help="Only cleanup .tmp files older than this age",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "status":
        return _run_status(args)
    if args.command == "replay":
        return _run_replay(args)
    if args.command == "cleanup-tmp":
        return _run_cleanup_tmp(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    sys.exit(main())
