from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = ROOT / "research" / "knowledge" / "paper_index.json"
DEFAULT_ROOT_REPORTS = ROOT / "research" / "knowledge" / "reports" / "root_reports"


def _load_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            out[str(key)] = value
    return out


def _save_index(path: Path, index: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _normalize_arxiv_id(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    if "/abs/" in raw:
        raw = raw.split("/abs/")[-1].strip()
    return raw


def _extract_arxiv_from_note(text: str) -> str | None:
    m = re.search(r"https?://arxiv\.org/abs/([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", text, flags=re.I)
    if m:
        return m.group(1)
    m = re.search(r"\barXiv[:\s]+([0-9]{4}\.[0-9]{4,5}(?:v[0-9]+)?)", text, flags=re.I)
    if m:
        return m.group(1)
    return None


def _extract_authors_from_note(text: str) -> str | None:
    for pattern in (
        r"^Authors:\s*(.+)$",
        r"^[•\-\*]\s*\*\*作者\*\*：\s*(.+)$",
    ):
        m = re.search(pattern, text, flags=re.M)
        if m:
            value = m.group(1).strip()
            if value:
                return value
    return None


def _extract_published_from_note(text: str) -> str | None:
    m = re.search(r"^Published:\s*(.+)$", text, flags=re.M)
    if m:
        value = m.group(1).strip()
        if value:
            return value
    m = re.search(r"^[•\-\*]\s*\*\*年份\*\*：\s*(.+)$", text, flags=re.M)
    if m:
        value = m.group(1).strip()
        if value:
            return value
    return None


def _has_line(text: str, prefix: str) -> bool:
    return re.search(rf"^{re.escape(prefix)}\s*", text, flags=re.M) is not None


def _inject_metadata_block(
    *,
    text: str,
    ref: str,
    arxiv_id: str | None,
    authors: str | None,
    published: str | None,
) -> tuple[str, bool]:
    lines = text.splitlines()
    if not lines:
        return text, False
    has_ref = _has_line(text, "ref:")
    has_arxiv = _has_line(text, "arxiv:")
    has_authors = _has_line(text, "Authors:")
    has_published = _has_line(text, "Published:")
    if has_ref and has_arxiv and has_authors and has_published:
        return text, False

    inject_lines: list[str] = []
    if not has_ref:
        inject_lines.append(f"ref: {ref}")
    if not has_arxiv and arxiv_id:
        inject_lines.append(f"arxiv: https://arxiv.org/abs/{arxiv_id}")
    if not has_authors and authors:
        inject_lines.append(f"Authors: {authors}")
    if not has_published and published:
        inject_lines.append(f"Published: {published}")
    if not inject_lines:
        return text, False

    title_idx = 0
    for idx, line in enumerate(lines):
        if line.strip().startswith("# "):
            title_idx = idx
            break
    metadata_indices = [
        idx
        for idx, line in enumerate(lines[:40])
        if line.startswith("ref:")
        or line.startswith("arxiv:")
        or line.startswith("Authors:")
        or line.startswith("Published:")
    ]
    insert_at = (max(metadata_indices) + 1) if metadata_indices else (title_idx + 1)

    new_lines = lines[:insert_at] + inject_lines + lines[insert_at:]
    if insert_at + len(inject_lines) < len(new_lines):
        if new_lines[insert_at + len(inject_lines)].strip():
            new_lines.insert(insert_at + len(inject_lines), "")
    return ("\n".join(new_lines).rstrip() + "\n"), True


def _sorted_index_keys(index: dict[str, dict[str, Any]]) -> list[str]:
    numeric = sorted((key for key in index if key.isdigit()), key=lambda x: int(x))
    others = sorted(key for key in index if not key.isdigit())
    return numeric + others


def cmd_audit_note_citations(args: argparse.Namespace) -> int:
    index_path = Path(str(args.index))
    root = Path(str(args.project_root))
    index = _load_index(index_path)

    missing_arxiv = 0
    missing_authors = 0
    missing_published = 0
    missing_ref = 0
    missing_any = 0
    scanned = 0
    rows: list[dict[str, Any]] = []

    for key in _sorted_index_keys(index):
        row = index[key]
        note_file = str(row.get("note_file", "")).strip()
        if not note_file:
            continue
        note_path = (root / note_file).resolve()
        if not note_path.exists():
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1
        has_ref = _has_line(text, "ref:")
        has_arxiv = _has_line(text, "arxiv:")
        has_authors = _has_line(text, "Authors:")
        has_published = _has_line(text, "Published:")
        if not has_ref:
            missing_ref += 1
        if not has_arxiv:
            missing_arxiv += 1
        if not has_authors:
            missing_authors += 1
        if not has_published:
            missing_published += 1
        if not (has_ref and has_arxiv and has_authors and has_published):
            missing_any += 1
            if len(rows) < int(args.max_examples):
                rows.append(
                    {
                        "ref": key,
                        "note_file": note_file,
                        "missing": {
                            "ref": not has_ref,
                            "arxiv": not has_arxiv,
                            "authors": not has_authors,
                            "published": not has_published,
                        },
                    }
                )

    payload = {
        "scanned_notes": scanned,
        "missing_any": missing_any,
        "missing_ref": missing_ref,
        "missing_arxiv": missing_arxiv,
        "missing_authors": missing_authors,
        "missing_published": missing_published,
        "examples": rows,
    }
    if args.out:
        out = Path(str(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def cmd_backfill_note_citations(args: argparse.Namespace) -> int:
    index_path = Path(str(args.index))
    root = Path(str(args.project_root))
    index = _load_index(index_path)
    touched_notes = 0
    touched_index = 0
    scanned = 0
    examples: list[dict[str, Any]] = []
    limit = int(args.limit)

    for key in _sorted_index_keys(index):
        if limit > 0 and touched_notes >= limit:
            break
        row = index[key]
        note_file = str(row.get("note_file", "")).strip()
        if not note_file:
            continue
        note_path = (root / note_file).resolve()
        if not note_path.exists():
            continue
        try:
            text = note_path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1

        arxiv_id = _normalize_arxiv_id(str(row.get("arxiv_id", "")).strip())
        inferred_arxiv = _extract_arxiv_from_note(text)
        if not arxiv_id and inferred_arxiv:
            row["arxiv_id"] = inferred_arxiv
            arxiv_id = inferred_arxiv
            index[key] = row
            touched_index += 1

        authors = _extract_authors_from_note(text)
        published = _extract_published_from_note(text)
        updated_text, changed = _inject_metadata_block(
            text=text,
            ref=str(key),
            arxiv_id=arxiv_id or None,
            authors=authors,
            published=published,
        )
        if changed:
            touched_notes += 1
            if not bool(args.dry_run):
                note_path.write_text(updated_text, encoding="utf-8")
            if len(examples) < 20:
                examples.append({"ref": key, "note_file": note_file, "index_arxiv_id": arxiv_id or None})

    if touched_index > 0 and not bool(args.dry_run):
        _save_index(index_path, index)

    payload = {
        "scanned_notes": scanned,
        "touched_notes": touched_notes,
        "touched_index_rows": touched_index,
        "dry_run": bool(args.dry_run),
        "examples": examples,
    }
    if args.out:
        out = Path(str(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def _parse_title_frames(svg_text: str) -> tuple[int | None, list[tuple[str, int, float]]]:
    raw_titles = re.findall(r"<title>(.*?)</title>", svg_text, flags=re.S)
    total_samples: int | None = None
    frames: list[tuple[str, int, float]] = []
    for raw in raw_titles:
        text = re.sub(r"\s+", " ", raw.strip())
        match = re.match(r"^(?P<frame>.+) \((?P<samples>[0-9]+) samples, (?P<pct>[0-9]+(?:\.[0-9]+)?)%\)$", text)
        if match is None:
            continue
        frame = match.group("frame").strip()
        samples = int(match.group("samples"))
        pct = float(match.group("pct"))
        if frame == "all":
            total_samples = samples
            continue
        frames.append((frame, samples, pct))
    return total_samples, frames


def _frame_hint(frame: str) -> str:
    text = frame.lower()
    if "lob_engine.py" in text:
        return "Optimize LOB hot path (Rust kernel or tighter Python loop)."
    if "prometheus_client" in text or "metrics.py" in text:
        return "Cache metrics labels/counters and reduce per-event label calls."
    if "importlib" in text or "yaml/" in text:
        return "Move import/config parsing out of runtime loop; warm cache on startup."
    if "normalizer.py" in text:
        return "Cache normalization lookups and avoid repeated parsing."
    return "Profile callsite and reduce allocations in this frame."


def cmd_triage_pyspy(args: argparse.Namespace) -> int:
    root_reports = Path(str(args.root_reports))
    top = max(1, int(args.top))
    pattern = str(args.pattern)
    svg_files = sorted(root_reports.glob(pattern))
    aggregate: dict[str, dict[str, Any]] = {}
    per_file: list[dict[str, Any]] = []

    for svg_path in svg_files:
        try:
            text = svg_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        total_samples, frames = _parse_title_frames(text)
        frames_sorted = sorted(frames, key=lambda item: item[1], reverse=True)
        top_rows = frames_sorted[:top]
        per_file.append(
            {
                "file": str(svg_path),
                "total_samples": total_samples,
                "top_frames": [
                    {"frame": frame, "samples": samples, "pct": pct, "hint": _frame_hint(frame)}
                    for frame, samples, pct in top_rows
                ],
            }
        )
        for frame, samples, pct in top_rows:
            row = aggregate.setdefault(frame, {"frame": frame, "samples": 0, "max_pct": 0.0, "file_hits": 0})
            row["samples"] = int(row["samples"]) + int(samples)
            row["max_pct"] = max(float(row["max_pct"]), float(pct))
            row["file_hits"] = int(row["file_hits"]) + 1

    aggregate_rows = sorted(aggregate.values(), key=lambda item: int(item["samples"]), reverse=True)[:top]
    for row in aggregate_rows:
        row["hint"] = _frame_hint(str(row["frame"]))

    payload = {
        "root_reports": str(root_reports),
        "scanned_svg_files": len(svg_files),
        "top": top,
        "aggregate_top_frames": aggregate_rows,
        "per_file": per_file,
    }

    if args.out:
        out = Path(str(args.out))
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    if args.md:
        md_path = Path(str(args.md))
        md_path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Pyspy Hotspot Triage",
            "",
            f"- scanned_svg_files: {len(svg_files)}",
            f"- top: {top}",
            "",
            "## Aggregate Hotspots",
            "",
            "| Rank | Frame | Samples | Max % | File Hits | Hint |",
            "|---|---|---:|---:|---:|---|",
        ]
        for idx, row in enumerate(aggregate_rows, 1):
            lines.append(
                f"| {idx} | `{row['frame']}` | {row['samples']} | {row['max_pct']:.2f} | "
                f"{row['file_hits']} | {row['hint']} |"
            )
        lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Research maintenance helpers for citation and profiling debt.")
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit-note-citations", help="Audit citation metadata completeness in knowledge notes.")
    audit.add_argument("--index", default=str(DEFAULT_INDEX))
    audit.add_argument("--project-root", default=str(ROOT))
    audit.add_argument("--max-examples", type=int, default=20)
    audit.add_argument("--out", default=None)
    audit.set_defaults(func=cmd_audit_note_citations)

    backfill = sub.add_parser(
        "backfill-note-citations",
        help="Backfill normalized citation headers into note files using existing metadata.",
    )
    backfill.add_argument("--index", default=str(DEFAULT_INDEX))
    backfill.add_argument("--project-root", default=str(ROOT))
    backfill.add_argument("--limit", type=int, default=0, help="Max notes to modify (0 means no limit).")
    backfill.add_argument("--dry-run", action="store_true")
    backfill.add_argument("--out", default=None)
    backfill.set_defaults(func=cmd_backfill_note_citations)

    triage = sub.add_parser("triage-pyspy", help="Parse pyspy flamegraph SVG and rank hotspots.")
    triage.add_argument("--root-reports", default=str(DEFAULT_ROOT_REPORTS))
    triage.add_argument("--pattern", default="pyspy*.svg")
    triage.add_argument("--top", type=int, default=20)
    triage.add_argument("--out", default=None)
    triage.add_argument("--md", default=None, help="Optional markdown report output path.")
    triage.set_defaults(func=cmd_triage_pyspy)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
