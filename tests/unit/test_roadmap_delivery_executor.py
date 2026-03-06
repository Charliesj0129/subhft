from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "roadmap_delivery_executor.py"
    spec = importlib.util.spec_from_file_location("roadmap_delivery_executor", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _seed_hotpath_files(root: Path) -> None:
    _write(
        root / "src/hft_platform/strategy/runner.py",
        "def handle_event():\n    return 1\n",
    )
    _write(
        root / "src/hft_platform/risk/engine.py",
        "def check():\n    return 1\n",
    )
    _write(
        root / "src/hft_platform/order/adapter.py",
        "def send_order():\n    rust_bridge = True\n    return rust_bridge\n",
    )
    _write(
        root / "src/hft_platform/execution/router.py",
        "def route():\n    return 1\n",
    )
    _write(
        root / "src/hft_platform/feed_adapter/normalizer.py",
        "def normalize():\n    return 1\n",
    )
    _write(
        root / "src/hft_platform/feed_adapter/lob_engine.py",
        "def compute_book_stats():\n    return 1\n",
    )


def _seed_common_docs(root: Path, *, include_all_ws_tasks: bool = False) -> None:
    if include_all_ws_tasks:
        task_lines = (
            "1. WS-A 任務（Owner: Ops Oncall；截止: 2026-03-12；輸出: burn-in template；驗收: ok）。\n"
            "2. WS-B 任務（Owner: Tech Lead；截止: 2026-03-15；輸出: mv baseline report；驗收: ok）。\n"
            "3. WS-C 任務（Owner: Data Steward；截止: 2026-03-18；輸出: quality routing report；驗收: ok）。\n"
            "4. WS-F 任務（Owner: Ops Oncall；截止: 2026-03-20；輸出: monthly review flow；驗收: ok）。\n"
            "5. WS-G 任務（Owner: Rust Lead；截止: 2026-03-22；輸出: hotpath_matrix；驗收: ok）。\n"
            "6. WS-H 任務（Owner: Research Lead；截止: 2026-03-25；輸出: source_catalog；驗收: ok）。\n"
        )
    else:
        task_lines = (
            "1. WS-G 任務（Owner: Rust Lead；截止: 2026-03-22；輸出: hotpath_matrix；驗收: ok）。\n"
            "2. WS-H 任務（Owner: Research Lead；截止: 2026-03-25；輸出: source_catalog；驗收: ok）。\n"
        )

    _write(
        root / "docs/TODO.md",
        "# TODO\n### 1.4 熱路徑 Rust 化擴編（P0）\n### 2.4 研究與分析工廠擴容（P1）\n",
    )
    _write(
        root / "ROADMAP.md",
        f"# ROADMAP\n## 6. 接下來 30 天\n{task_lines}",
    )
    _write(
        root / "tests/unit/test_rust_hotpath_parity.py",
        "def test_placeholder():\n    assert True\n",
    )
    _write(
        root / "tests/benchmark/perf_regression_gate.py",
        "def test_placeholder():\n    assert True\n",
    )


def _seed_benchmark(root: Path) -> None:
    payload = {
        "benchmarks": [
            {"name": "test_bench_python_normalize_bidask", "stats": {"mean": 2.0e-5}},
            {"name": "test_bench_rust_normalize_bidask", "stats": {"mean": 2.5e-5}},
            {"name": "test_bench_python_scale_book_pair_stats", "stats": {"mean": 2.1e-5}},
            {"name": "test_bench_rust_scale_book_pair_stats", "stats": {"mean": 2.4e-5}},
            {"name": "test_bench_python_position_update", "stats": {"mean": 1.1e-6}},
            {"name": "test_bench_rust_position_update", "stats": {"mean": 1.3e-5}},
        ]
    }
    _write_json(root / "benchmark.json", payload)


def _seed_paper_index(root: Path) -> None:
    _write_json(
        root / "research/knowledge/paper_index.json",
        {
            "001": {
                "ref": "001",
                "title": "paper one",
                "note_file": "research/knowledge/notes/001.md",
                "status": "reviewed",
                "tags": ["microstructure"],
                "alphas": [],
                "arxiv_id": "",
            },
            "002": {
                "ref": "002",
                "title": "paper two",
                "note_file": "research/knowledge/notes/002.md",
                "status": "reviewed",
                "tags": ["report"],
                "alphas": ["alpha_demo"],
                "arxiv_id": "2408.03594",
            },
        },
    )
    _write(
        root / "research/knowledge/notes/001.md",
        "# n1\nref: 001\nAuthors: A\nPublished: 2024\n",
    )
    _write(
        root / "research/knowledge/notes/002.md",
        "# n2\nref: 002\narxiv: https://arxiv.org/abs/2408.03594\nAuthors: B\nPublished: 2025\n",
    )


def test_main_generates_wsg_wsh_artifacts(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_common_docs(tmp_path)
    _seed_hotpath_files(tmp_path)
    _seed_benchmark(tmp_path)
    _seed_paper_index(tmp_path)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--allow-warn-exit-zero",
        ]
    )
    assert rc == 0

    summary = json.loads((tmp_path / "outputs/roadmap_execution/summary/latest.json").read_text(encoding="utf-8"))
    assert summary["result"]["overall"] == "warn"
    assert summary["ws_g"]["status"] == "warn"
    assert summary["ws_h"]["status"] == "warn"
    assert summary["task_board"]["task_count_by_ws"]["WS-G"] == 1
    assert summary["task_board"]["task_count_by_ws"]["WS-H"] == 1

    ws_g_dir = tmp_path / "outputs/roadmap_execution/ws_g"
    ws_h_dir = tmp_path / "outputs/roadmap_execution/ws_h"
    assert (ws_g_dir / "latest_hotpath_matrix.json").exists()
    assert (ws_g_dir / "latest_cutover_backlog.md").exists()
    assert (ws_g_dir / "latest_cutover_backlog.json").exists()
    assert (ws_h_dir / "latest_source_catalog.json").exists()
    assert (ws_h_dir / "latest_quality_report.json").exists()
    assert (ws_h_dir / "latest_factory_pipeline.md").exists()
    assert (ws_h_dir / "latest_promotion_readiness.json").exists()

    hotpath = json.loads((ws_g_dir / "latest_hotpath_matrix.json").read_text(encoding="utf-8"))
    assert hotpath["schema_version"] == "1.0"
    assert hotpath["skills"] == ["hft-strategy-dev", "rust_feature_engineering", "performance-profiling"]
    assert hotpath["agent_roles"] == ["explorer", "worker", "default"]
    assert hotpath["rows"]
    assert "row_id" in hotpath["rows"][0]
    assert "cpu_hotspot_pct" in hotpath["rows"][0]
    assert hotpath["source_manifest"]

    cutover = json.loads((ws_g_dir / "latest_cutover_backlog.json").read_text(encoding="utf-8"))
    assert cutover["summary"]["total"] == len(cutover["items"])
    assert cutover["items"][0]["backlog_id"].startswith("WSG-")

    source_catalog = json.loads((ws_h_dir / "latest_source_catalog.json").read_text(encoding="utf-8"))
    assert source_catalog["schema_version"] == "1.0"
    assert source_catalog["summary"]["paper_total"] == 2
    assert source_catalog["summary"]["duplicate_group_count"] == 0

    quality = json.loads((ws_h_dir / "latest_quality_report.json").read_text(encoding="utf-8"))
    assert quality["status"] == "yellow"
    assert quality["coverage"]["citation_completeness"]["missing_any"] == 1

    readiness = json.loads((ws_h_dir / "latest_promotion_readiness.json").read_text(encoding="utf-8"))
    assert readiness["overall"] == "warn"
    assert readiness["summary"]["total"] == 1
    assert readiness["alphas"][0]["alpha_id"] == "alpha_demo"


def test_main_fails_when_source_catalog_empty(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_common_docs(tmp_path)
    _seed_hotpath_files(tmp_path)
    _seed_benchmark(tmp_path)
    _write_json(tmp_path / "research/knowledge/paper_index.json", {})

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 2

    summary = json.loads((tmp_path / "outputs/roadmap_execution/summary/latest.json").read_text(encoding="utf-8"))
    assert summary["result"]["overall"] == "fail"
    assert summary["ws_h"]["status"] == "fail"


def test_main_generates_ws_a_b_c_f_artifacts_when_tasks_present(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_common_docs(tmp_path, include_all_ws_tasks=True)
    _seed_hotpath_files(tmp_path)
    _seed_benchmark(tmp_path)
    _seed_paper_index(tmp_path)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--allow-warn-exit-zero",
        ]
    )
    assert rc == 0

    summary = json.loads((tmp_path / "outputs/roadmap_execution/summary/latest.json").read_text(encoding="utf-8"))
    for ws in ("WS-A", "WS-B", "WS-C", "WS-F", "WS-G", "WS-H"):
        assert summary["task_board"]["task_count_by_ws"][ws] == 1

    assert summary["ws_a"]["status"] in {"pass", "warn"}
    assert summary["ws_b"]["status"] in {"pass", "warn"}
    assert summary["ws_c"]["status"] in {"pass", "warn"}
    assert summary["ws_f"]["status"] in {"pass", "warn"}

    ws_a_dir = tmp_path / "outputs/roadmap_execution/ws_a"
    ws_b_dir = tmp_path / "outputs/roadmap_execution/ws_b"
    ws_c_dir = tmp_path / "outputs/roadmap_execution/ws_c"
    ws_f_dir = tmp_path / "outputs/roadmap_execution/ws_f"
    assert (ws_a_dir / "latest_burn_in_template.json").exists()
    assert (ws_b_dir / "latest_mv_baseline_report.json").exists()
    assert (ws_c_dir / "latest_quality_routing_report.json").exists()
    assert (ws_f_dir / "latest_monthly_review_flow.json").exists()
