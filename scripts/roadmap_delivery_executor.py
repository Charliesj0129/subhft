#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

STATUS_PASS = "pass"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

WS_G_SKILLS: tuple[str, ...] = ("hft-strategy-dev", "rust_feature_engineering", "performance-profiling")
WS_H_SKILLS: tuple[str, ...] = ("hft-alpha-research", "validation-gate", "clickhouse-io")
WS_A_SKILLS: tuple[str, ...] = ("troubleshoot-metrics", "runtime-debug")
WS_B_SKILLS: tuple[str, ...] = ("clickhouse-io", "performance-profiling")
WS_C_SKILLS: tuple[str, ...] = ("validation-gate", "troubleshoot-metrics")
WS_F_SKILLS: tuple[str, ...] = ("deployment-patterns", "runtime-debug")
AGENT_ROLES: tuple[str, ...] = ("explorer", "worker", "default")

HOTPATH_MODULES: tuple[dict[str, str], ...] = (
    {
        "module": "strategy_runner",
        "layer": "strategy",
        "path": "src/hft_platform/strategy/runner.py",
        "benchmark_key": "normalize_bidask",
    },
    {
        "module": "risk_engine",
        "layer": "risk",
        "path": "src/hft_platform/risk/engine.py",
        "benchmark_key": "scale_book_pair_stats",
    },
    {
        "module": "order_adapter",
        "layer": "order",
        "path": "src/hft_platform/order/adapter.py",
        "benchmark_key": "position_update",
    },
    {
        "module": "execution_router",
        "layer": "execution",
        "path": "src/hft_platform/execution/router.py",
        "benchmark_key": "position_update",
    },
    {
        "module": "normalizer",
        "layer": "feed_adapter",
        "path": "src/hft_platform/feed_adapter/normalizer.py",
        "benchmark_key": "normalize_bidask",
    },
    {
        "module": "lob_engine",
        "layer": "feed_adapter",
        "path": "src/hft_platform/feed_adapter/lob_engine.py",
        "benchmark_key": "scale_book_pair_stats",
    },
)

HOTPATH_PRIORITY_WEIGHT: dict[str, int] = {
    "strategy": 30,
    "risk": 30,
    "order": 25,
    "execution": 25,
    "feed_adapter": 15,
}

PIPELINE_STAGES: tuple[tuple[str, str], ...] = (
    ("source inventory", "盤點 paper/tech/report 來源與 metadata 完整度"),
    ("metadata/dedup", "標準化欄位、來源分級、去重"),
    ("batch analysis", "topic clustering + citation graph + hypothesis queue"),
    ("promotion pre-check", "引用完整性/可重現性/資料時效 Gate"),
)

HOTSPOT_PATH_HINT: dict[str, str] = {
    "strategy_runner": "runner.py",
    "risk_engine": "engine.py",
    "order_adapter": "adapter.py",
    "execution_router": "router.py",
    "normalizer": "normalizer.py",
    "lob_engine": "lob_engine.py",
}

MODULE_LATENCY_KEYS: dict[str, tuple[str, ...]] = {
    "strategy_runner": ("strategy_noop_metrics_on_us_per_event",),
    "risk_engine": (
        "risk_run_approve_us_per_intent",
        "risk_run_reject_us_per_intent",
        "risk_evaluate_us_per_call",
    ),
    "order_adapter": ("gateway_process_envelope_obj_us_per_call",),
    "execution_router": ("gateway_process_envelope_typed_us_per_call",),
    "normalizer": ("normalizer_bidask_us", "market_data_callback_parse_us_per_event"),
    "lob_engine": ("lob_process_bidask_us", "feature_engine_lob_update_us_per_event"),
}

LAYER_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "strategy": ("normalizer", "lob_engine"),
    "risk": ("strategy_runner",),
    "order": ("risk_engine",),
    "execution": ("order_adapter",),
    "feed_adapter": (),
}

WS_A_METRICS: tuple[str, ...] = (
    "feed_reconnect_total",
    "feed_reconnect_timeout_total",
    "feed_reconnect_exception_total",
    "feed_gap_by_symbol_seconds",
    "shioaji_quote_callback_ingress_latency_ns",
    "shioaji_quote_callback_queue_dropped_total",
    "market_data_callback_parse_total",
    "quote_watchdog_recovery_attempts_total",
    "quote_schema_mismatch_total",
)

WS_B_METRICS: tuple[str, ...] = (
    "wal_backlog_files",
    "wal_drain_eta_seconds",
    "wal_replay_errors_total",
    "recorder_insert_retry_total",
    "recorder_failures_total",
)

WS_C_METRICS: tuple[str, ...] = (
    "feature_quality_flags_total",
    "feature_shadow_parity_mismatch_total",
    "feature_shadow_parity_checks_total",
    "feature_plane_latency_ns",
    "market_data_callback_parse_total",
    "quote_schema_mismatch_total",
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


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


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _source_manifest(paths: list[Path]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for path in paths:
        if not path.exists() or not path.is_file():
            out.append(
                {
                    "path": str(path),
                    "exists": False,
                    "sha256": "",
                    "mtime": "",
                    "size_bytes": 0,
                }
            )
            continue
        stat = path.stat()
        out.append(
            {
                "path": str(path),
                "exists": True,
                "sha256": _sha256_file(path),
                "mtime": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc).isoformat(),
                "size_bytes": int(stat.st_size),
            }
        )
    return out


def _normalize_arxiv_id(value: str) -> str:
    text = str(value or "").strip()
    if "/abs/" in text:
        text = text.split("/abs/")[-1].strip()
    return re.sub(r"v\d+$", "", text)


def _normalize_title(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _parse_iso_dt(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _extract_defined_metrics(metrics_path: Path) -> set[str]:
    if not metrics_path.exists():
        return set()
    try:
        text = metrics_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(re.findall(r'(?:Counter|Gauge|Histogram)\(\s*"([a-zA-Z0-9_]+)"', text))


def _alert_metric_coverage(alert_rules_path: Path, metrics: tuple[str, ...]) -> dict[str, bool]:
    if not alert_rules_path.exists():
        return {metric: False for metric in metrics}
    try:
        text = alert_rules_path.read_text(encoding="utf-8")
    except OSError:
        return {metric: False for metric in metrics}
    return {metric: bool(re.search(rf"\b{re.escape(metric)}\b", text)) for metric in metrics}


def _task_for_ws(tasks: list[dict[str, str]], ws: str) -> dict[str, str]:
    for row in tasks:
        if str(row.get("ws") or "") == ws:
            return {
                "owner": str(row.get("owner") or ""),
                "deadline": str(row.get("deadline") or ""),
                "output": str(row.get("output") or ""),
                "acceptance": str(row.get("acceptance") or ""),
                "raw": str(row.get("raw") or ""),
            }
    return {"owner": "", "deadline": "", "output": "", "acceptance": "", "raw": ""}


# ---------------------------------------------------------------------------
# WS-G helpers
# ---------------------------------------------------------------------------


def _parse_benchmark_pairs(benchmark_path: Path) -> dict[str, dict[str, float]]:
    payload = _load_json(benchmark_path)
    rows = payload.get("benchmarks", []) if isinstance(payload, dict) else []
    py: dict[str, float] = {}
    rs: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name", ""))
        stats = row.get("stats", {})
        if not isinstance(stats, dict):
            continue
        mean_s = stats.get("mean")
        if not isinstance(mean_s, (int, float)):
            continue
        if name.startswith("test_bench_python_"):
            key = name.removeprefix("test_bench_python_")
            py[key] = float(mean_s)
        elif name.startswith("test_bench_rust_"):
            key = name.removeprefix("test_bench_rust_")
            rs[key] = float(mean_s)

    out: dict[str, dict[str, float]] = {}
    for key in sorted(set(py) | set(rs)):
        p = py.get(key)
        r = rs.get(key)
        if p is None and r is None:
            continue
        speedup = (p / r) if (p is not None and r is not None and r > 0) else None
        out[key] = {
            "python_mean_s": float(p) if p is not None else 0.0,
            "rust_mean_s": float(r) if r is not None else 0.0,
            "rust_speedup_vs_python": float(speedup) if speedup is not None else 0.0,
        }
    return out


def _extract_def_names(text: str, *, limit: int = 6) -> list[str]:
    names = re.findall(r"^def\s+([a-zA-Z_][a-zA-Z0-9_]*)\(", text, flags=re.M)
    return names[:limit]


def _load_hotspot_index(pyspy_path: Path) -> dict[str, float]:
    payload = _load_json(pyspy_path)
    rows = payload.get("aggregate_top_frames", []) if isinstance(payload, dict) else []
    out: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        frame = str(row.get("frame", ""))
        pct = row.get("max_pct")
        if not isinstance(pct, (int, float)):
            continue
        out[frame] = float(pct)
    return out


def _module_hotspot_pct(module_name: str, hotspots: dict[str, float]) -> float:
    hint = HOTSPOT_PATH_HINT.get(module_name, "")
    if not hint:
        return 0.0
    best = 0.0
    for frame, pct in hotspots.items():
        if hint in frame and pct > best:
            best = pct
    return best


def _load_perf_results(perf_snapshot_path: Path) -> dict[str, float]:
    payload = _load_json(perf_snapshot_path)
    rows = payload.get("results", []) if isinstance(payload, dict) else {}
    if not isinstance(rows, dict):
        return {}
    out: dict[str, float] = {}
    for key, value in rows.items():
        if isinstance(value, (int, float)):
            out[str(key)] = float(value)
    return out


def _load_stage_probe_means(stage_probe_path: Path) -> dict[str, float]:
    payload = _load_json(stage_probe_path)
    if not isinstance(payload, dict):
        return {}
    out: dict[str, float] = {}
    for key, row in payload.items():
        if not isinstance(row, dict):
            continue
        mean_v = row.get("mean")
        if isinstance(mean_v, (int, float)):
            out[str(key)] = float(mean_v)
    return out


def _module_latency_us(module_name: str, perf_results: dict[str, float], stage_probe: dict[str, float]) -> float:
    keys = MODULE_LATENCY_KEYS.get(module_name, ())
    values: list[float] = []
    for key in keys:
        if key in stage_probe:
            values.append(stage_probe[key])
        if key in perf_results:
            values.append(perf_results[key])
    if not values:
        return 0.0
    return max(values)


def _hotpath_rows(
    project_root: Path,
    benchmark_pairs: dict[str, dict[str, float]],
    hotspots: dict[str, float],
    perf_results: dict[str, float],
    stage_probe: dict[str, float],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    data_quality_flags: list[str] = []

    if not benchmark_pairs:
        data_quality_flags.append("benchmark_pairs_empty")
    if not hotspots:
        data_quality_flags.append("pyspy_hotspots_empty")

    for item in HOTPATH_MODULES:
        rel_path = item["path"]
        layer = item["layer"]
        path = project_root / rel_path
        exists = path.exists()

        has_rust_marker = False
        alloc_marker_count = 0
        def_names: list[str] = []
        if exists:
            text = path.read_text(encoding="utf-8")
            lower = text.lower()
            has_rust_marker = any(m in lower for m in ("rust", "ffi", "pyo3", "allow_threads"))
            alloc_marker_count = sum(lower.count(m) for m in ("append(", "dict(", "list(", "json.loads("))
            def_names = _extract_def_names(text)

        bench_key = item["benchmark_key"]
        bench = benchmark_pairs.get(bench_key, {})
        rust_speedup = float(bench.get("rust_speedup_vs_python", 0.0))
        cpu_hotspot_pct = _module_hotspot_pct(item["module"], hotspots)
        p95_us = _module_latency_us(item["module"], perf_results, stage_probe)

        score = HOTPATH_PRIORITY_WEIGHT.get(layer, 10)
        if not has_rust_marker:
            score += 25
        if rust_speedup > 0 and rust_speedup < 1.0:
            score += 20
        if alloc_marker_count >= 8:
            score += 10
        if cpu_hotspot_pct >= 10.0:
            score += 10
        if p95_us >= 20.0:
            score += 8

        priority = "P2"
        if score >= 65:
            priority = "P0"
        elif score >= 45:
            priority = "P1"

        if not exists:
            warnings.append(f"missing_module:{rel_path}")

        evidence_refs = [
            "outputs/research_maintenance/pyspy_triage.json",
            "outputs/perf_gate_latency_snapshot.clean.json",
            "outputs/latency_stage_probe_custom_nonorder.json",
            "tests/unit/test_rust_hotpath_parity.py",
            "tests/benchmark/perf_regression_gate.py",
        ]

        rows.append(
            {
                "row_id": f"{item['module']}::{layer}",
                "module": item["module"],
                "layer": layer,
                "path": rel_path,
                "exists": exists,
                "def_samples": def_names,
                "rust_marker": has_rust_marker,
                "alloc_marker_count": alloc_marker_count,
                "benchmark_key": bench_key,
                "benchmark": bench,
                "cpu_hotspot_pct": cpu_hotspot_pct,
                "p95_us": p95_us,
                "priority_score": score,
                "priority": priority,
                "evidence_refs": evidence_refs,
            }
        )

    rows.sort(key=lambda r: int(r["priority_score"]), reverse=True)
    return rows, warnings, data_quality_flags


def _build_cutover_backlog(rows: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for idx, row in enumerate(rows, start=1):
        module = str(row.get("module") or "")
        layer = str(row.get("layer") or "")
        priority = str(row.get("priority") or "P2")
        status = "planned"
        if priority == "P0":
            eta_days = 7
        elif priority == "P1":
            eta_days = 14
        else:
            eta_days = 21

        items.append(
            {
                "backlog_id": f"WSG-{idx:03d}",
                "module": module,
                "layer": layer,
                "priority": priority,
                "current_state": "python_hotpath",
                "target_state": "rust_kernel",
                "proposed_boundary": "typed_frame_zero_copy",
                "dependencies": list(LAYER_DEPENDENCIES.get(layer, ())),
                "acceptance_criteria": {
                    "parity": "int_x10000_contract_preserved",
                    "perf": "p95_latency_improve_at_least_20pct_vs_2026_03_baseline",
                    "ffi_alloc": "ffi_copy_ratio_le_5pct_and_alloc_tick_drop_ge_30pct",
                },
                "owner": "Rust Lead",
                "status": status,
                "eta_days": eta_days,
                "evidence_refs": list(row.get("evidence_refs") or []),
                "rollback_plan": "preserve_python_path_and_toggle_by_feature_flag",
            }
        )

    summary = {
        "total": len(items),
        "p0": len([x for x in items if x["priority"] == "P0"]),
        "p1": len([x for x in items if x["priority"] == "P1"]),
        "p2": len([x for x in items if x["priority"] == "P2"]),
        "blocked": len([x for x in items if x["status"] == "blocked"]),
        "ready": len([x for x in items if x["status"] == "planned"]),
    }
    return {
        "generated_at": _now_iso(),
        "skills": list(WS_G_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "flow": "profiling matrix -> kernel cutover -> CI parity/perf gate -> soak",
        "items": items,
        "summary": summary,
    }


def _render_cutover_backlog(cutover_payload: dict[str, Any]) -> str:
    items = cutover_payload.get("items", []) if isinstance(cutover_payload.get("items"), list) else []
    lines: list[str] = []
    lines.append("# WS-G Cutover Backlog")
    lines.append("")
    lines.append("Flow: `profiling matrix -> kernel cutover -> CI parity/perf gate -> soak`")
    lines.append("")
    lines.append("| backlog_id | module | layer | priority | owner | eta_days |")
    lines.append("|---|---|---|---|---|---:|")
    for row in items:
        lines.append(
            f"| `{row.get('backlog_id')}` | `{row.get('module')}` | `{row.get('layer')}` | "
            f"`{row.get('priority')}` | `{row.get('owner')}` | {row.get('eta_days')} |"
        )
    lines.append("")
    lines.append("## First Batch Recommendation")
    lines.append("")
    lines.append("1. strategy/risk: pure compute kernels + parity gate first.")
    lines.append("2. order/execution: integer math and state transitions kernelization.")
    lines.append("3. feed_adapter normalization path: enforce zero-copy/typed frame checks before promote.")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# WS-H helpers
# ---------------------------------------------------------------------------


def _has_header_line(text: str, key: str) -> bool:
    return re.search(rf"^{re.escape(key)}\s*", text, flags=re.M) is not None


def _scan_note_metadata(note_path: Path) -> dict[str, bool]:
    try:
        text = note_path.read_text(encoding="utf-8")
    except OSError:
        return {"ref": False, "arxiv": False, "authors": False, "published": False}
    return {
        "ref": _has_header_line(text, "ref:"),
        "arxiv": _has_header_line(text, "arxiv:"),
        "authors": _has_header_line(text, "Authors:"),
        "published": _has_header_line(text, "Published:"),
    }


def _sort_ref_keys(rows: dict[str, Any]) -> list[str]:
    numeric = sorted((k for k in rows if str(k).isdigit()), key=lambda x: int(str(x)))
    others = sorted((k for k in rows if not str(k).isdigit()), key=str)
    return [str(k) for k in numeric + others]


def _derive_source_type(row: dict[str, Any]) -> str:
    if str(row.get("arxiv_id", "")).strip():
        return "paper_arxiv"
    tags = [str(tag).lower() for tag in (row.get("tags") or []) if str(tag).strip()]
    if any("report" in tag for tag in tags):
        return "report"
    if any("tech" in tag for tag in tags):
        return "tech_article"
    return "unknown"


def _collect_run_metas(runs_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(runs_root.glob("*/meta.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        alpha_id = str(payload.get("alpha_id", "")).strip()
        run_id = str(payload.get("run_id", "")).strip()
        if not alpha_id or not run_id:
            continue
        gate_status = payload.get("gate_status", {})
        gate_c = bool((gate_status or {}).get("gate_c")) if isinstance(gate_status, dict) else False
        rows.append(
            {
                "alpha_id": alpha_id,
                "run_id": run_id,
                "timestamp": str(payload.get("timestamp", "")),
                "gate_c": gate_c,
                "path": str(path),
            }
        )
    return rows


def _collect_promotion_decisions(promotions_root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(promotions_root.glob("*/**/promotion_decision.json")):
        payload = _load_json(path)
        if not isinstance(payload, dict):
            continue
        alpha_id = str(payload.get("alpha_id", "")).strip()
        if not alpha_id:
            continue
        rows.append(
            {
                "alpha_id": alpha_id,
                "decision": str(payload.get("decision", "")).strip(),
                "timestamp": str(payload.get("timestamp", "")),
                "gate_d_passed": bool(payload.get("gate_d_passed", False)),
                "gate_e_passed": bool(payload.get("gate_e_passed", False)),
                "gate_f_passed": bool(payload.get("gate_f_passed", False)),
                "paper_governance_passed": bool(payload.get("paper_governance_passed", False)),
                "path": str(path),
            }
        )
    return rows


def _build_source_catalog(
    project_root: Path,
    index_path: Path,
    runs_root: Path,
    promotions_root: Path,
) -> dict[str, Any]:
    payload = _load_json(index_path)
    index = payload if isinstance(payload, dict) else {}

    sources: list[dict[str, Any]] = []
    notes: list[dict[str, Any]] = []

    type_count: dict[str, int] = {}
    dedup_groups: dict[str, list[str]] = {}

    for ref in _sort_ref_keys(index):
        raw = index.get(ref, {})
        if not isinstance(raw, dict):
            continue

        title = str(raw.get("title", "")).strip()
        arxiv_id = str(raw.get("arxiv_id", "")).strip()
        note_file = str(raw.get("note_file", "")).strip()
        note_path = (project_root / note_file).resolve() if note_file else None
        note_exists = bool(note_path and note_path.exists())

        checks = _scan_note_metadata(note_path) if note_exists and note_path else {
            "ref": False,
            "arxiv": False,
            "authors": False,
            "published": False,
        }
        note_complete = bool(checks["ref"] and checks["arxiv"] and checks["authors"] and checks["published"])

        source_type = _derive_source_type(raw)
        type_count[source_type] = type_count.get(source_type, 0) + 1

        dedup_key = _normalize_arxiv_id(arxiv_id)
        if not dedup_key:
            dedup_key = f"title::{_normalize_title(title)}"
        dedup_groups.setdefault(dedup_key, []).append(ref)

        source_row = {
            "paper_ref": ref,
            "title": title,
            "status": str(raw.get("status", "")).strip(),
            "arxiv_id": arxiv_id,
            "note_path": note_file,
            "note_exists": note_exists,
            "tags": [str(t) for t in (raw.get("tags") or []) if str(t).strip()],
            "alphas": [str(a) for a in (raw.get("alphas") or []) if str(a).strip()],
            "source_type": source_type,
            "dedup_key": dedup_key,
            "note_complete": note_complete,
        }
        sources.append(source_row)

        notes.append(
            {
                "paper_ref": ref,
                "note_path": note_file,
                "exists": note_exists,
                "has_ref": checks["ref"],
                "has_arxiv": checks["arxiv"],
                "has_authors": checks["authors"],
                "has_published": checks["published"],
                "complete": note_complete,
            }
        )

    runs = _collect_run_metas(runs_root)
    promotions = _collect_promotion_decisions(promotions_root)

    duplicate_groups = {k: v for k, v in dedup_groups.items() if len(v) > 1}
    duplicate_source_rows = sum(len(v) for v in duplicate_groups.values())

    source_total = len(sources)
    linked_source_total = len([row for row in sources if row["alphas"]])
    citation_complete_total = len([row for row in sources if row["note_complete"]])

    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "skills": list(WS_H_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "index_path": str(index_path),
        "runs_path": str(runs_root),
        "promotions_path": str(promotions_root),
        "summary": {
            "paper_total": source_total,
            "note_total": len(notes),
            "run_total": len(runs),
            "promotion_total": len(promotions),
            "linked_paper_total": linked_source_total,
            "citation_complete_total": citation_complete_total,
            "source_type_breakdown": type_count,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_source_rows": duplicate_source_rows,
        },
        "papers": sources,
        "notes": notes,
        "experiments": {
            "runs": runs,
            "promotions": promotions,
        },
        "dedup": {
            "groups": duplicate_groups,
        },
    }


def _quality_report_from_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    summary = catalog.get("summary", {}) if isinstance(catalog.get("summary"), dict) else {}
    papers = catalog.get("papers", []) if isinstance(catalog.get("papers"), list) else []
    notes = catalog.get("notes", []) if isinstance(catalog.get("notes"), list) else []
    experiments = catalog.get("experiments", {}) if isinstance(catalog.get("experiments"), dict) else {}
    runs = experiments.get("runs", []) if isinstance(experiments.get("runs"), list) else []
    promotions = experiments.get("promotions", []) if isinstance(experiments.get("promotions"), list) else []

    source_total = int(summary.get("paper_total") or len(papers) or 0)
    duplicate_rows = int(summary.get("duplicate_source_rows") or 0)
    citation_complete_total = int(summary.get("citation_complete_total") or 0)

    citation_completeness = (citation_complete_total / source_total) if source_total else 0.0
    dedup_hit_rate = (1.0 - (duplicate_rows / source_total)) if source_total else 0.0

    now = _now_utc()
    week_start = now - dt.timedelta(days=7)
    month_start = now - dt.timedelta(days=30)

    runs_week = 0
    for row in runs:
        ts = _parse_iso_dt(row.get("timestamp"))
        if ts and ts >= week_start:
            runs_week += 1

    promo_month: list[dict[str, Any]] = []
    for row in promotions:
        ts = _parse_iso_dt(row.get("timestamp"))
        if ts and ts >= month_start:
            promo_month.append(row)

    gate_precheck_pass = len(
        [
            row
            for row in promo_month
            if bool(row.get("gate_d_passed"))
            and bool(row.get("gate_e_passed"))
            and bool(row.get("paper_governance_passed"))
        ]
    )
    precheck_pass_rate = (gate_precheck_pass / len(promo_month)) if promo_month else 0.0

    missing_any = len([row for row in notes if not bool(row.get("complete"))])

    blocking_issues: list[str] = []
    if source_total == 0:
        blocking_issues.append("source_catalog_empty")

    if blocking_issues:
        status = "red"
    elif citation_completeness < 0.98 or dedup_hit_rate < 0.95 or (promo_month and precheck_pass_rate < 0.90):
        status = "yellow"
    else:
        status = "green"

    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "coverage": {
            "citation_completeness": {
                "ratio": citation_completeness,
                "complete": citation_complete_total,
                "total": source_total,
                "missing_any": missing_any,
            },
            "paper_linkage": {
                "linked": int(summary.get("linked_paper_total") or 0),
                "total": source_total,
                "ratio": (int(summary.get("linked_paper_total") or 0) / source_total) if source_total else 0.0,
            },
            "experiment_metadata": {
                "runs_total": len(runs),
                "runs_with_gate_c": len([row for row in runs if bool(row.get("gate_c"))]),
                "promotions_total": len(promotions),
            },
        },
        "dedup": {
            "hit_rate": dedup_hit_rate,
            "duplicate_source_rows": duplicate_rows,
            "duplicate_group_count": int(summary.get("duplicate_group_count") or 0),
        },
        "pipeline_quality": {
            "throughput_weekly_candidates": runs_week,
            "throughput_weekly_target": 50,
            "research_to_alpha_lead_time_median_days": None,
            "research_to_alpha_lead_time_target_days": 2,
        },
        "promotion_quality": {
            "precheck_pass_rate_monthly": precheck_pass_rate,
            "precheck_pass_count_monthly": gate_precheck_pass,
            "precheck_total_monthly": len(promo_month),
            "precheck_target": 0.90,
        },
        "blocking_issues": blocking_issues,
        "status": status,
    }


def _latest_by_alpha(rows: list[dict[str, Any]], key: str = "timestamp") -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        alpha = str(row.get("alpha_id") or "").strip()
        if not alpha:
            continue
        ts = _parse_iso_dt(row.get(key))
        prev = out.get(alpha)
        prev_ts = _parse_iso_dt(prev.get(key)) if prev else None
        if prev is None or (ts and (prev_ts is None or ts >= prev_ts)):
            out[alpha] = row
    return out


def _promotion_readiness(catalog: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
    papers = catalog.get("papers", []) if isinstance(catalog.get("papers"), list) else []
    experiments = catalog.get("experiments", {}) if isinstance(catalog.get("experiments"), dict) else {}
    runs = experiments.get("runs", []) if isinstance(experiments.get("runs"), list) else []
    promotions = experiments.get("promotions", []) if isinstance(experiments.get("promotions"), list) else []

    papers_by_alpha: dict[str, list[dict[str, Any]]] = {}
    for row in papers:
        if not isinstance(row, dict):
            continue
        for alpha_id in row.get("alphas", []):
            alpha = str(alpha_id).strip()
            if not alpha:
                continue
            papers_by_alpha.setdefault(alpha, []).append(row)

    latest_runs = _latest_by_alpha(runs)
    latest_promos = _latest_by_alpha(promotions)
    alpha_ids = sorted(set(papers_by_alpha) | set(latest_runs) | set(latest_promos))

    alpha_rows: list[dict[str, Any]] = []
    for alpha_id in alpha_ids:
        src_rows = papers_by_alpha.get(alpha_id, [])
        latest_run = latest_runs.get(alpha_id, {})
        latest_promo = latest_promos.get(alpha_id, {})

        gate_a = "pass" if src_rows else "fail"
        gate_b = "pass" if latest_run else "na"
        gate_c = "pass" if bool(latest_run.get("gate_c")) else ("fail" if latest_run else "na")
        gate_d = "pass" if bool(latest_promo.get("gate_d_passed")) else ("fail" if latest_promo else "na")
        gate_e = "pass" if bool(latest_promo.get("gate_e_passed")) else ("fail" if latest_promo else "na")
        gate_f = "pass" if bool(latest_promo.get("gate_f_passed")) else ("fail" if latest_promo else "na")

        fail_reasons: list[str] = []
        if gate_a != "pass":
            fail_reasons.append("missing_linked_sources")
        if gate_c == "fail":
            fail_reasons.append("gate_c_failed")
        if gate_d == "fail":
            fail_reasons.append("gate_d_failed")
        if gate_e == "fail":
            fail_reasons.append("gate_e_failed")
        if gate_f == "fail":
            fail_reasons.append("gate_f_failed")

        overall = "ready"
        for gate in (gate_a, gate_b, gate_c, gate_d, gate_e, gate_f):
            if gate != "pass":
                overall = "not_ready"
                break

        alpha_rows.append(
            {
                "alpha_id": alpha_id,
                "latest_run_id": str(latest_run.get("run_id", "")) or None,
                "latest_promotion_decision": str(latest_promo.get("decision", "")) or None,
                "gates": {
                    "A": gate_a,
                    "B": gate_b,
                    "C": gate_c,
                    "D": gate_d,
                    "E": gate_e,
                    "F": gate_f,
                },
                "overall": overall,
                "fail_reasons": fail_reasons,
            }
        )

    ready_count = len([row for row in alpha_rows if row["overall"] == "ready"])
    not_ready_count = len(alpha_rows) - ready_count
    quality_status = str(quality.get("status") or "red")

    overall_status = STATUS_PASS
    if not alpha_rows:
        overall_status = STATUS_FAIL
    elif quality_status == "red":
        overall_status = STATUS_FAIL
    elif not_ready_count > 0 or quality_status == "yellow":
        overall_status = STATUS_WARN

    recommendation = "ready" if overall_status == STATUS_PASS else "not_ready"

    return {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "criteria_version": "gates_A_to_F_v1",
        "skills": list(WS_H_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "alphas": alpha_rows,
        "summary": {
            "total": len(alpha_rows),
            "ready": ready_count,
            "not_ready": not_ready_count,
            "quality_status": quality_status,
        },
        "overall": overall_status,
        "recommendation": recommendation,
    }


def _render_factory_pipeline_md() -> str:
    lines: list[str] = []
    lines.append("# WS-H Factory Pipeline")
    lines.append("")
    lines.append("`source inventory -> metadata/dedup -> batch analysis -> hypothesis queue -> promotion pre-check`")
    lines.append("")
    lines.append("| stage | objective |")
    lines.append("|---|---|")
    for stage, objective in PIPELINE_STAGES:
        lines.append(f"| `{stage}` | {objective} |")
    lines.append("")
    lines.append("## KPI Definitions")
    lines.append("")
    lines.append("- throughput_weekly_target: `>= 50`")
    lines.append("- citation_completeness_target: `>= 0.98`")
    lines.append("- dedup_hit_rate_target: `>= 0.95`")
    lines.append("- research_to_alpha_median_lead_time_days: `<= 2`")
    lines.append("- precheck_monthly_pass_rate_target: `>= 0.90`")
    lines.append("")
    lines.append("## Promotion Pre-check Command Set")
    lines.append("")
    lines.append("1. `make research-audit`")
    lines.append("2. `make research-index`")
    lines.append("3. `uv run hft alpha validate <alpha_id>`")
    lines.append("4. `uv run hft alpha promote <alpha_id>`")
    lines.append("")
    return "\n".join(lines) + "\n"


def _parse_30d_tasks(roadmap_text: str) -> list[dict[str, str]]:
    lines = roadmap_text.splitlines()
    in_section = False
    tasks: list[dict[str, str]] = []
    for line in lines:
        if line.startswith("## 6."):
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section:
            continue
        m = re.match(r"^\d+\.\s+(.*)$", line.strip())
        if not m:
            continue
        body = m.group(1)
        ws = ""
        ws_match = re.search(r"\b(WS-[A-Z])\b", body)
        if ws_match:
            ws = ws_match.group(1)
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


def _build_ws_a_burnin_template(
    project_root: Path,
    *,
    tasks: list[dict[str, str]],
    metrics_defined: set[str],
    alert_rules_path: Path,
) -> tuple[dict[str, Any], str]:
    task = _task_for_ws(tasks, "WS-A")
    alert_coverage = _alert_metric_coverage(alert_rules_path, WS_A_METRICS)
    metrics = []
    missing: list[str] = []
    for metric in WS_A_METRICS:
        exists = metric in metrics_defined
        if not exists:
            missing.append(metric)
        metrics.append(
            {
                "metric": metric,
                "defined_in_metrics_py": exists,
                "covered_by_alert_rules": bool(alert_coverage.get(metric, False)),
            }
        )

    sources = [
        project_root / "docs/operations/cron-setup-remote.md",
        project_root / "docs/runbooks/old-pc-yearly-reliability.md",
        project_root / "scripts/soak_acceptance.py",
        project_root / "scripts/callback_latency_guard.py",
    ]
    status = STATUS_PASS
    if missing:
        status = _combine_status(status, STATUS_WARN)
    if not all(path.exists() for path in sources):
        status = _combine_status(status, STATUS_WARN)

    payload = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "ws": "WS-A",
        "skills": list(WS_A_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "task": task,
        "target_window_trading_days": 60,
        "acceptance_thresholds": {
            "reconnect_storm_signature": 0,
            "callback_crash_signature": 0,
            "first_quote_pass_ratio": 1.0,
            "reconnect_failure_ratio_max": 0.2,
            "watchdog_callback_reregister_max": 120,
        },
        "metric_catalog": metrics,
        "evidence_commands": [
            "python3 scripts/soak_acceptance.py daily --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --allow-warn-exit-zero",
            "python3 scripts/soak_acceptance.py weekly --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports",
            "python3 scripts/soak_acceptance.py canary --project-root . --prom-url http://localhost:9091 --output-dir outputs/soak_reports --window-days 10 --min-trading-days 5 --min-first-quote-pass-ratio 1.0 --max-reconnect-failure-ratio 0.2 --max-watchdog-callback-reregister 120 --allow-warn-exit-zero",
        ],
        "sources": _source_manifest(sources),
        "missing_metrics": missing,
        "status": status,
    }
    return payload, status


def _build_ws_b_mv_baseline_report(
    project_root: Path,
    *,
    tasks: list[dict[str, str]],
    metrics_defined: set[str],
    benchmark_pairs: dict[str, dict[str, float]],
) -> tuple[dict[str, Any], str]:
    task = _task_for_ws(tasks, "WS-B")
    metric_rows = [{"metric": metric, "defined_in_metrics_py": metric in metrics_defined} for metric in WS_B_METRICS]
    missing_metrics = [row["metric"] for row in metric_rows if not row["defined_in_metrics_py"]]

    required_paths = [
        project_root / "docs/runbooks/ch-mv-pressure-tuning.md",
        project_root / "config/monitoring/query_guard_suite_baseline.json",
        project_root / "scripts/ch_query_guard.py",
        project_root / "scripts/ch_query_guard_suite.py",
    ]
    missing_files = [str(path.relative_to(project_root)) for path in required_paths if not path.exists()]

    status = STATUS_PASS
    if missing_metrics or missing_files:
        status = _combine_status(status, STATUS_WARN)
    if not benchmark_pairs:
        status = _combine_status(status, STATUS_WARN)

    payload = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "ws": "WS-B",
        "skills": list(WS_B_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "task": task,
        "baseline_period": "2026-03",
        "metric_catalog": metric_rows,
        "benchmark_pairs": benchmark_pairs,
        "required_references": [str(path.relative_to(project_root)) for path in required_paths],
        "missing_references": missing_files,
        "missing_metrics": missing_metrics,
        "commands": [
            "make ch-query-guard-suite",
            "make ch-query-guard-check QUERY='SELECT ... LIMIT 100'",
            "make roadmap-delivery-check ALLOW_WARN=1",
        ],
        "status": status,
    }
    return payload, status


def _build_ws_c_quality_routing_report(
    project_root: Path,
    *,
    tasks: list[dict[str, str]],
    metrics_defined: set[str],
) -> tuple[dict[str, Any], str]:
    task = _task_for_ws(tasks, "WS-C")
    metric_rows = [{"metric": metric, "defined_in_metrics_py": metric in metrics_defined} for metric in WS_C_METRICS]
    missing_metrics = [row["metric"] for row in metric_rows if not row["defined_in_metrics_py"]]

    required_paths = [
        project_root / "docs/runbooks/feature-plane-operations.md",
        project_root / "docs/operations/env-vars-reference.md",
        project_root / "scripts/feature_canary_guard.py",
        project_root / "scripts/callback_latency_guard.py",
    ]
    missing_files = [str(path.relative_to(project_root)) for path in required_paths if not path.exists()]

    status = STATUS_PASS
    if missing_metrics or missing_files:
        status = _combine_status(status, STATUS_WARN)

    routing = {
        "primary_owner": task.get("owner") or "Data Steward",
        "backup_owner": "Ops Oncall",
        "triage_sla_minutes": 15,
        "close_loop": "alert -> triage -> remediation -> evidence attached to monthly review pack",
    }
    payload = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "ws": "WS-C",
        "skills": list(WS_C_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "task": task,
        "metric_catalog": metric_rows,
        "routing": routing,
        "commands": [
            "make feature-canary-report ALLOW_WARN=1",
            "make callback-latency-report ALLOW_WARN=1",
            "make reliability-monthly-pack",
        ],
        "required_references": [str(path.relative_to(project_root)) for path in required_paths],
        "missing_references": missing_files,
        "missing_metrics": missing_metrics,
        "status": status,
    }
    return payload, status


def _build_ws_f_monthly_review_flow(
    project_root: Path,
    *,
    tasks: list[dict[str, str]],
) -> tuple[dict[str, Any], str]:
    task = _task_for_ws(tasks, "WS-F")
    sections = [
        "soak",
        "backlog",
        "drift",
        "disk",
        "drill",
        "release_channel",
        "query_guard",
        "feature_canary",
        "callback_latency",
    ]
    required_paths = [
        project_root / "scripts/reliability_review_pack.py",
        project_root / "docs/operations/cron-setup-remote.md",
        project_root / "docs/operations/long-term-risk-register.md",
    ]
    missing_files = [str(path.relative_to(project_root)) for path in required_paths if not path.exists()]

    status = STATUS_PASS if not missing_files else STATUS_WARN
    payload = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "ws": "WS-F",
        "skills": list(WS_F_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "task": task,
        "monthly_review_sections": sections,
        "command": "make reliability-monthly-pack MONTH=<YYYY-MM> RUN_DRILL=0",
        "required_references": [str(path.relative_to(project_root)) for path in required_paths],
        "missing_references": missing_files,
        "status": status,
    }
    return payload, status


# ---------------------------------------------------------------------------
# CLI / main
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Execute TODO/ROADMAP deliverables for WS-A/B/C/F/G/H.")
    parser.add_argument("--project-root", default=".", help="Project root directory")
    parser.add_argument("--todo", default="docs/TODO.md", help="TODO markdown path")
    parser.add_argument("--roadmap", default="ROADMAP.md", help="ROADMAP markdown path")
    parser.add_argument("--benchmark", default="benchmark.json", help="pytest-benchmark JSON path")
    parser.add_argument(
        "--pyspy-triage",
        default="outputs/research_maintenance/pyspy_triage.json",
        help="pyspy triage JSON path",
    )
    parser.add_argument(
        "--perf-snapshot",
        default="outputs/perf_gate_latency_snapshot.clean.json",
        help="Performance snapshot JSON path",
    )
    parser.add_argument(
        "--stage-probe",
        default="outputs/latency_stage_probe_custom_nonorder.json",
        help="Stage latency probe JSON path",
    )
    parser.add_argument(
        "--paper-index",
        default="research/knowledge/paper_index.json",
        help="Research paper index JSON path",
    )
    parser.add_argument(
        "--runs-root",
        default="research/experiments/runs",
        help="Research runs root directory",
    )
    parser.add_argument(
        "--promotions-root",
        default="research/experiments/promotions",
        help="Research promotions root directory",
    )
    parser.add_argument("--output-dir", default="outputs/roadmap_execution", help="Output directory")
    parser.add_argument(
        "--allow-warn-exit-zero",
        action="store_true",
        help="Return exit 0 when overall status is warn",
    )
    return parser


def _write_latest(path: Path, content_path: Path) -> None:
    _write_text(path, content_path.read_text(encoding="utf-8"))


def _write_latest_json(path: Path, payload: dict[str, Any]) -> None:
    _write_json(path, payload)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    project_root = Path(args.project_root).resolve()
    todo_path = (project_root / str(args.todo)).resolve()
    roadmap_path = (project_root / str(args.roadmap)).resolve()
    benchmark_path = (project_root / str(args.benchmark)).resolve()
    pyspy_path = (project_root / str(args.pyspy_triage)).resolve()
    perf_snapshot_path = (project_root / str(args.perf_snapshot)).resolve()
    stage_probe_path = (project_root / str(args.stage_probe)).resolve()
    paper_index_path = (project_root / str(args.paper_index)).resolve()
    runs_root = (project_root / str(args.runs_root)).resolve()
    promotions_root = (project_root / str(args.promotions_root)).resolve()
    out_dir = (project_root / str(args.output_dir)).resolve()
    ts = _stamp()

    ws_a_dir = out_dir / "ws_a"
    ws_b_dir = out_dir / "ws_b"
    ws_c_dir = out_dir / "ws_c"
    ws_f_dir = out_dir / "ws_f"
    ws_g_dir = out_dir / "ws_g"
    ws_h_dir = out_dir / "ws_h"
    summary_dir = out_dir / "summary"

    todo_text = todo_path.read_text(encoding="utf-8") if todo_path.exists() else ""
    roadmap_text = roadmap_path.read_text(encoding="utf-8") if roadmap_path.exists() else ""
    tasks = _parse_30d_tasks(roadmap_text)

    benchmark_pairs = _parse_benchmark_pairs(benchmark_path) if benchmark_path.exists() else {}
    hotspots = _load_hotspot_index(pyspy_path) if pyspy_path.exists() else {}
    perf_results = _load_perf_results(perf_snapshot_path) if perf_snapshot_path.exists() else {}
    stage_probe = _load_stage_probe_means(stage_probe_path) if stage_probe_path.exists() else {}
    metrics_path = project_root / "src/hft_platform/observability/metrics.py"
    alert_rules_path = project_root / "config/monitoring/alerts/rules.yaml"
    metrics_defined = _extract_defined_metrics(metrics_path)

    hotpath_rows, ws_g_warnings, ws_g_data_quality_flags = _hotpath_rows(
        project_root,
        benchmark_pairs,
        hotspots,
        perf_results,
        stage_probe,
    )

    hotpath_matrix = {
        "schema_version": "1.0",
        "generated_at": _now_iso(),
        "skills": list(WS_G_SKILLS),
        "agent_roles": list(AGENT_ROLES),
        "todo_path": str(todo_path),
        "roadmap_path": str(roadmap_path),
        "rows": hotpath_rows,
        "warnings": ws_g_warnings,
        "data_quality_flags": ws_g_data_quality_flags,
        "parity_evidence": {
            "unit_parity_test_exists": (project_root / "tests/unit/test_rust_hotpath_parity.py").exists(),
            "benchmark_test_exists": (project_root / "tests/benchmark/perf_regression_gate.py").exists(),
        },
        "source_manifest": _source_manifest(
            [
                benchmark_path,
                pyspy_path,
                perf_snapshot_path,
                stage_probe_path,
            ]
        ),
    }

    cutover_backlog = _build_cutover_backlog(hotpath_rows)
    cutover_backlog_md = _render_cutover_backlog(cutover_backlog)

    source_catalog = _build_source_catalog(project_root, paper_index_path, runs_root, promotions_root)
    quality_report = _quality_report_from_catalog(source_catalog)
    factory_pipeline_md = _render_factory_pipeline_md()
    promotion_readiness = _promotion_readiness(source_catalog, quality_report)
    ws_a_burnin_template, ws_a_status = _build_ws_a_burnin_template(
        project_root,
        tasks=tasks,
        metrics_defined=metrics_defined,
        alert_rules_path=alert_rules_path,
    )
    ws_b_baseline_report, ws_b_status = _build_ws_b_mv_baseline_report(
        project_root,
        tasks=tasks,
        metrics_defined=metrics_defined,
        benchmark_pairs=benchmark_pairs,
    )
    ws_c_quality_routing, ws_c_status = _build_ws_c_quality_routing_report(
        project_root,
        tasks=tasks,
        metrics_defined=metrics_defined,
    )
    ws_f_monthly_flow, ws_f_status = _build_ws_f_monthly_review_flow(
        project_root,
        tasks=tasks,
    )

    ws_g_status = STATUS_PASS
    if ws_g_warnings or ws_g_data_quality_flags:
        ws_g_status = STATUS_WARN
    if not hotpath_rows:
        ws_g_status = STATUS_FAIL

    ws_h_status = STATUS_PASS
    q_status = str(quality_report.get("status") or "red")
    if str(promotion_readiness.get("overall") or STATUS_FAIL) == STATUS_FAIL or q_status == "red":
        ws_h_status = STATUS_FAIL
    elif q_status == "yellow" or str(promotion_readiness.get("overall") or STATUS_PASS) == STATUS_WARN:
        ws_h_status = STATUS_WARN

    summary_payload: dict[str, Any] = {
        "generated_at": _now_iso(),
        "skills": {
            "ws_a": list(WS_A_SKILLS),
            "ws_b": list(WS_B_SKILLS),
            "ws_c": list(WS_C_SKILLS),
            "ws_f": list(WS_F_SKILLS),
            "ws_g": list(WS_G_SKILLS),
            "ws_h": list(WS_H_SKILLS),
        },
        "agent_roles": list(AGENT_ROLES),
        "inputs": {
            "project_root": str(project_root),
            "todo": str(todo_path),
            "roadmap": str(roadmap_path),
            "benchmark": str(benchmark_path),
            "pyspy_triage": str(pyspy_path),
            "perf_snapshot": str(perf_snapshot_path),
            "stage_probe": str(stage_probe_path),
            "paper_index": str(paper_index_path),
            "runs_root": str(runs_root),
            "promotions_root": str(promotions_root),
        },
        "task_board": {
            "tasks": tasks,
            "task_count": len(tasks),
            "task_count_by_ws": {
                ws: len([row for row in tasks if row.get("ws") == ws]) for ws in sorted({row.get("ws", "") for row in tasks})
            },
        },
        "ws_a": {
            "status": ws_a_status,
            "deliverables": {
                "burn_in_template": "ws_a/latest_burn_in_template.json",
            },
        },
        "ws_b": {
            "status": ws_b_status,
            "deliverables": {
                "mv_baseline_report": "ws_b/latest_mv_baseline_report.json",
            },
        },
        "ws_c": {
            "status": ws_c_status,
            "deliverables": {
                "quality_routing_report": "ws_c/latest_quality_routing_report.json",
            },
        },
        "ws_f": {
            "status": ws_f_status,
            "deliverables": {
                "monthly_review_flow": "ws_f/latest_monthly_review_flow.json",
            },
        },
        "ws_g": {
            "status": ws_g_status,
            "deliverables": {
                "hotpath_matrix": "ws_g/latest_hotpath_matrix.json",
                "cutover_backlog_markdown": "ws_g/latest_cutover_backlog.md",
                "cutover_backlog_json": "ws_g/latest_cutover_backlog.json",
            },
        },
        "ws_h": {
            "status": ws_h_status,
            "deliverables": {
                "source_catalog": "ws_h/latest_source_catalog.json",
                "quality_report": "ws_h/latest_quality_report.json",
                "factory_pipeline": "ws_h/latest_factory_pipeline.md",
                "promotion_readiness": "ws_h/latest_promotion_readiness.json",
            },
        },
    }

    overall = STATUS_PASS
    overall = _combine_status(overall, ws_a_status)
    overall = _combine_status(overall, ws_b_status)
    overall = _combine_status(overall, ws_c_status)
    overall = _combine_status(overall, ws_f_status)
    overall = _combine_status(overall, ws_g_status)
    overall = _combine_status(overall, ws_h_status)
    summary_payload["result"] = {
        "overall": overall,
        "recommendation": ("go" if overall == STATUS_PASS else "go_with_warnings" if overall == STATUS_WARN else "block"),
    }
    summary_payload["todo_size_bytes"] = len(todo_text.encode("utf-8"))
    summary_payload["roadmap_size_bytes"] = len(roadmap_text.encode("utf-8"))

    ws_a_ts = ws_a_dir / f"burn_in_template_{ts}.json"
    ws_b_ts = ws_b_dir / f"mv_baseline_report_{ts}.json"
    ws_c_ts = ws_c_dir / f"quality_routing_report_{ts}.json"
    ws_f_ts = ws_f_dir / f"monthly_review_flow_{ts}.json"
    hotpath_ts = ws_g_dir / f"hotpath_matrix_{ts}.json"
    cutover_ts = ws_g_dir / f"cutover_backlog_{ts}.md"
    cutover_json_ts = ws_g_dir / f"cutover_backlog_{ts}.json"
    source_ts = ws_h_dir / f"source_catalog_{ts}.json"
    quality_ts = ws_h_dir / f"quality_report_{ts}.json"
    pipeline_ts = ws_h_dir / f"factory_pipeline_{ts}.md"
    readiness_ts = ws_h_dir / f"promotion_readiness_{ts}.json"
    summary_ts = summary_dir / f"roadmap_execution_{ts}.json"

    _write_json(ws_a_ts, ws_a_burnin_template)
    _write_json(ws_b_ts, ws_b_baseline_report)
    _write_json(ws_c_ts, ws_c_quality_routing)
    _write_json(ws_f_ts, ws_f_monthly_flow)
    _write_json(hotpath_ts, hotpath_matrix)
    _write_text(cutover_ts, cutover_backlog_md)
    _write_json(cutover_json_ts, cutover_backlog)
    _write_json(source_ts, source_catalog)
    _write_json(quality_ts, quality_report)
    _write_text(pipeline_ts, factory_pipeline_md)
    _write_json(readiness_ts, promotion_readiness)
    _write_json(summary_ts, summary_payload)

    _write_latest_json(ws_a_dir / "latest_burn_in_template.json", ws_a_burnin_template)
    _write_latest_json(ws_b_dir / "latest_mv_baseline_report.json", ws_b_baseline_report)
    _write_latest_json(ws_c_dir / "latest_quality_routing_report.json", ws_c_quality_routing)
    _write_latest_json(ws_f_dir / "latest_monthly_review_flow.json", ws_f_monthly_flow)
    _write_latest_json(ws_g_dir / "latest_hotpath_matrix.json", hotpath_matrix)
    _write_latest(ws_g_dir / "latest_cutover_backlog.md", cutover_ts)
    _write_latest_json(ws_g_dir / "latest_cutover_backlog.json", cutover_backlog)
    _write_latest_json(ws_h_dir / "latest_source_catalog.json", source_catalog)
    _write_latest_json(ws_h_dir / "latest_quality_report.json", quality_report)
    _write_latest(ws_h_dir / "latest_factory_pipeline.md", pipeline_ts)
    _write_latest_json(ws_h_dir / "latest_promotion_readiness.json", promotion_readiness)
    _write_latest_json(summary_dir / "latest.json", summary_payload)

    print(f"[roadmap-exec] summary: {summary_ts}")
    print(f"[roadmap-exec] overall: {overall}")

    if overall == STATUS_PASS:
        return 0
    if overall == STATUS_WARN and args.allow_warn_exit_zero:
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
