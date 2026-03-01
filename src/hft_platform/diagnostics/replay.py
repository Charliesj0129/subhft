from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


def load_traces(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    out: list[dict[str, Any]] = []
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def filter_traces(
    records: Iterable[dict[str, Any]], *, trace_id: str | None = None, stage: str | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        if trace_id and str(rec.get("trace_id", "")) != str(trace_id):
            continue
        if stage and str(rec.get("stage", "")) != str(stage):
            continue
        out.append(rec)
    return out


def summarize_trace(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    seq = list(records)
    by_stage: dict[str, int] = {}
    trace_ids: set[str] = set()
    for rec in seq:
        by_stage[str(rec.get("stage", ""))] = by_stage.get(str(rec.get("stage", "")), 0) + 1
        if rec.get("trace_id"):
            trace_ids.add(str(rec["trace_id"]))
    seq_sorted = sorted(seq, key=lambda r: int(r.get("ts_ns", 0) or 0))
    return {
        "count": len(seq_sorted),
        "trace_ids": sorted(trace_ids)[:20],
        "stages": by_stage,
        "first_ts_ns": int(seq_sorted[0].get("ts_ns", 0)) if seq_sorted else 0,
        "last_ts_ns": int(seq_sorted[-1].get("ts_ns", 0)) if seq_sorted else 0,
    }


def build_timeline(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    seq = sorted(list(records), key=lambda r: int(r.get("ts_ns", 0) or 0))
    summary = summarize_trace(seq)
    first_ts = int(summary.get("first_ts_ns", 0) or 0)
    rows: list[dict[str, Any]] = []
    for idx, rec in enumerate(seq):
        ts = int(rec.get("ts_ns", 0) or 0)
        rows.append(
            {
                "idx": idx,
                "ts_ns": ts,
                "t_rel_ms": ((ts - first_ts) / 1e6) if first_ts else 0.0,
                "trace_id": str(rec.get("trace_id", "") or ""),
                "stage": str(rec.get("stage", "") or ""),
                "payload": rec.get("payload", {}),
            }
        )
    return {"summary": summary, "timeline": rows}


def render_timeline_markdown(timeline_payload: dict[str, Any]) -> str:
    summary = dict(timeline_payload.get("summary") or {})
    rows = list(timeline_payload.get("timeline") or [])
    out = ["# Incident Timeline", ""]
    out.append(f"- count: `{summary.get('count', 0)}`")
    out.append(f"- trace_ids: `{', '.join(summary.get('trace_ids', [])[:5])}`")
    out.append(f"- first_ts_ns: `{summary.get('first_ts_ns', 0)}`")
    out.append(f"- last_ts_ns: `{summary.get('last_ts_ns', 0)}`")
    stages = summary.get("stages") or {}
    if stages:
        out.append("")
        out.append("## Stage Counts")
        for k in sorted(stages):
            out.append(f"- `{k}`: {stages[k]}")
    out.append("")
    out.append("## Timeline")
    out.append("")
    out.append("| idx | t_rel_ms | stage | trace_id | payload |")
    out.append("| ---: | ---: | --- | --- | --- |")
    for row in rows:
        payload = row.get("payload", {})
        payload_s = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(payload_s) > 180:
            payload_s = payload_s[:177] + "..."
        payload_s = payload_s.replace("|", "\\|")
        out.append(
            f"| {int(row.get('idx', 0))} | {float(row.get('t_rel_ms', 0.0)):.3f} | "
            f"{row.get('stage', '')} | {row.get('trace_id', '')} | `{payload_s}` |"
        )
    out.append("")
    return "\n".join(out)
