from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "roadmap_delivery_guard.py"
    spec = importlib.util.spec_from_file_location("roadmap_delivery_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _seed_docs(tmp_path: Path, *, missing_gate_evidence: bool = False) -> tuple[Path, Path]:
    todo = tmp_path / "docs" / "TODO.md"
    roadmap = tmp_path / "ROADMAP.md"
    todo.parent.mkdir(parents=True, exist_ok=True)

    gate_line = "- Gate 證據：`hotpath_matrix`\n" if not missing_gate_evidence else ""
    todo.write_text(
        (
            "# TODO\n"
            "### 1.4 熱路徑 Rust 化擴編（P0）\n"
            "- 技能：`hft-strategy-dev`、`rust_feature_engineering`、`performance-profiling`\n"
            "- RACI：R=Rust Lead、A=Tech Lead、C=Strategy Owner、I=Ops Oncall\n"
            "- Agent 角色：`explorer` -> `worker` -> `default`\n"
            "- KPI：\n"
            "  - kpi-a\n"
            "- 風險與緩解：\n"
            "  - risk-a\n"
            "- 依賴：\n"
            "  - dep-a\n"
            f"{gate_line}"
            "### 2.4 研究與分析工廠擴容（P1）\n"
            "- 技能：`hft-alpha-research`、`validation-gate`、`clickhouse-io`\n"
            "- RACI：R=Research Lead、A=Head of Research、C=Data Steward、I=Trading Runtime Owner\n"
            "- Agent 角色：`explorer` -> `worker` -> `default`\n"
            "- KPI：\n"
            "  - kpi-b\n"
            "- 風險與緩解：\n"
            "  - risk-b\n"
            "- 依賴：\n"
            "  - dep-b\n"
            "- Gate 證據：`source_catalog`\n"
        ),
        encoding="utf-8",
    )

    roadmap.write_text(
        (
            "# ROADMAP\n"
            "### WS-G：熱路徑 Rust 化擴編\n"
            "- 技能：`hft-strategy-dev`、`rust_feature_engineering`、`performance-profiling`\n"
            "- RACI：R=Rust Lead、A=Tech Lead、C=Strategy Owner、I=Ops Oncall\n"
            "- Agent 角色：`explorer` -> `worker` -> `default`\n"
            "- KPI：\n"
            "  - a\n"
            "- 風險與緩解：\n"
            "  - a\n"
            "- 依賴：\n"
            "  - a\n"
            "- Gate 證據：`hotpath_matrix`\n"
            "### WS-H：研究與分析工廠擴大化\n"
            "- 技能：`hft-alpha-research`、`validation-gate`、`clickhouse-io`\n"
            "- RACI：R=Research Lead、A=Head of Research、C=Data Steward、I=Trading Runtime Owner\n"
            "- Agent 角色：`explorer` -> `worker` -> `default`\n"
            "- KPI：\n"
            "  - b\n"
            "- 風險與緩解：\n"
            "  - b\n"
            "- 依賴：\n"
            "  - b\n"
            "- Gate 證據：`source_catalog`\n"
            "## 6. 接下來 30 天\n"
            "1. WS-G 任務（Owner: Rust Lead；截止: 2026-03-22；輸出: hotpath_matrix；驗收: ok）。\n"
            "2. WS-H 任務（Owner: Research Lead；截止: 2026-03-25；輸出: source_catalog；驗收: ok）。\n"
        ),
        encoding="utf-8",
    )
    return todo, roadmap


def _seed_execution_artifacts(tmp_path: Path, *, stale: bool = False, missing_ws_h: bool = False) -> Path:
    execution_dir = tmp_path / "outputs" / "roadmap_execution"
    now = dt.datetime.now(dt.timezone.utc)
    generated = now - dt.timedelta(days=10) if stale else now
    ts = generated.isoformat()

    (execution_dir / "summary").mkdir(parents=True, exist_ok=True)
    (execution_dir / "ws_g").mkdir(parents=True, exist_ok=True)
    (execution_dir / "ws_h").mkdir(parents=True, exist_ok=True)

    (execution_dir / "summary" / "latest.json").write_text(
        json.dumps(
            {
                "generated_at": ts,
                "result": {"overall": "pass"},
                "ws_g": {"status": "pass"},
                "ws_h": {"status": "pass"},
            }
        ),
        encoding="utf-8",
    )
    (execution_dir / "ws_g" / "latest_hotpath_matrix.json").write_text(
        json.dumps({"generated_at": ts, "rows": []}),
        encoding="utf-8",
    )
    (execution_dir / "ws_g" / "latest_cutover_backlog.md").write_text("# ws-g\n", encoding="utf-8")

    if not missing_ws_h:
        (execution_dir / "ws_h" / "latest_source_catalog.json").write_text(
            json.dumps({"generated_at": ts, "source_count": 1}),
            encoding="utf-8",
        )
    (execution_dir / "ws_h" / "latest_quality_report.json").write_text(
        json.dumps({"generated_at": ts, "missing_any": 0}),
        encoding="utf-8",
    )
    (execution_dir / "ws_h" / "latest_factory_pipeline.md").write_text("# ws-h\n", encoding="utf-8")
    (execution_dir / "ws_h" / "latest_promotion_readiness.json").write_text(
        json.dumps({"generated_at": ts, "overall": "pass"}),
        encoding="utf-8",
    )
    return execution_dir


def test_main_pass_generates_board(tmp_path: Path):
    mod = _load_module()
    todo, roadmap = _seed_docs(tmp_path)
    execution_dir = _seed_execution_artifacts(tmp_path)
    out_dir = tmp_path / "outputs" / "roadmap_delivery"

    rc = mod.main(
        [
            "--todo",
            str(todo),
            "--roadmap",
            str(roadmap),
            "--execution-dir",
            str(execution_dir),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["result"]["overall"] == "pass"
    assert latest["execution_board"]["task_count"] == 2
    assert latest["execution_board"]["task_count_by_ws"]["WS-G"] == 1
    assert latest["execution_board"]["task_count_by_ws"]["WS-H"] == 1
    assert latest["execution_artifacts"]["enabled"] is True


def test_main_fails_when_required_field_missing(tmp_path: Path):
    mod = _load_module()
    todo, roadmap = _seed_docs(tmp_path, missing_gate_evidence=True)
    out_dir = tmp_path / "outputs" / "roadmap_delivery"

    rc = mod.main(
        [
            "--todo",
            str(todo),
            "--roadmap",
            str(roadmap),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 2

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["result"]["overall"] == "fail"
    failed_ids = [c["id"] for c in latest["result"]["checks"] if c["status"] == "fail"]
    assert "1.4_Gate 證據_present" in failed_ids


def test_main_fails_when_execution_artifact_missing(tmp_path: Path):
    mod = _load_module()
    todo, roadmap = _seed_docs(tmp_path)
    execution_dir = _seed_execution_artifacts(tmp_path, missing_ws_h=True)
    out_dir = tmp_path / "outputs" / "roadmap_delivery"

    rc = mod.main(
        [
            "--todo",
            str(todo),
            "--roadmap",
            str(roadmap),
            "--execution-dir",
            str(execution_dir),
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 2

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["result"]["overall"] == "fail"
    failed_ids = [c["id"] for c in latest["result"]["checks"] if c["status"] == "fail"]
    assert "WS-H_artifact_ws_h_latest_source_catalog.json_exists" in failed_ids


def test_main_warns_when_execution_artifact_is_stale(tmp_path: Path):
    mod = _load_module()
    todo, roadmap = _seed_docs(tmp_path)
    execution_dir = _seed_execution_artifacts(tmp_path, stale=True)
    out_dir = tmp_path / "outputs" / "roadmap_delivery"

    rc = mod.main(
        [
            "--todo",
            str(todo),
            "--roadmap",
            str(roadmap),
            "--execution-dir",
            str(execution_dir),
            "--max-artifact-age-hours",
            "24",
            "--allow-warn-exit-zero",
            "--output-dir",
            str(out_dir),
        ]
    )
    assert rc == 0

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["result"]["overall"] == "warn"
    warn_ids = [c["id"] for c in latest["result"]["checks"] if c["status"] == "warn"]
    assert "WS-G_artifact_ws_g_latest_hotpath_matrix.json_fresh" in warn_ids
