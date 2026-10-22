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
    assert hotpath["skills"] == ["hft-strategy", "hft-rust", "hft-latency-profiling"]
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


# --- unmeasured hot-path inputs are unknown, not zero ------------------------------------------

_ALL_UNKNOWN = ["cpu_hotspot_pct", "p95_us", "rust_speedup_vs_python"]
_FLAGS_ALL_EMPTY = ["benchmark_pairs_empty", "pyspy_hotspots_empty", "perf_snapshot_empty", "stage_probe_empty"]


def _row_for(rows: list[dict], module: str) -> dict:
    return next(row for row in rows if row["module"] == module)


def test_parse_benchmark_pairs_marks_a_missing_side_unknown(tmp_path: Path) -> None:
    mod = _load_module()
    _write_json(
        tmp_path / "bench.json",
        {
            "benchmarks": [
                {"name": "test_bench_python_only_py", "stats": {"mean": 2.0e-5}},
                {"name": "test_bench_rust_only_rs", "stats": {"mean": 3.0e-5}},
                {"name": "test_bench_python_both", "stats": {"mean": 4.0e-5}},
                {"name": "test_bench_rust_both", "stats": {"mean": 2.0e-5}},
                {"name": "test_bench_python_zero", "stats": {"mean": 4.0e-5}},
                {"name": "test_bench_rust_zero", "stats": {"mean": 0.0}},
            ]
        },
    )

    pairs = mod._parse_benchmark_pairs(tmp_path / "bench.json")

    assert pairs["only_py"] == {"python_mean_s": 2.0e-5, "rust_mean_s": None, "rust_speedup_vs_python": None}
    assert pairs["only_rs"] == {"python_mean_s": None, "rust_mean_s": 3.0e-5, "rust_speedup_vs_python": None}
    assert pairs["both"]["rust_speedup_vs_python"] == 2.0
    assert pairs["zero"]["rust_speedup_vs_python"] is None


def test_module_hotspot_pct_is_unknown_without_a_measurement() -> None:
    mod = _load_module()
    frames = {"src/hft_platform/feed_adapter/normalizer.py:normalize": 12.5}

    assert mod._module_hotspot_pct("normalizer", frames) == 12.5
    assert mod._module_hotspot_pct("normalizer", {}) is None
    assert mod._module_hotspot_pct("lob_engine", frames) is None
    assert mod._module_hotspot_pct("not_a_known_module", frames) is None


def test_module_latency_us_is_unknown_without_a_measurement() -> None:
    mod = _load_module()
    perf = {"normalizer_bidask_us": 9.0}
    probe = {"market_data_callback_parse_us_per_event": 14.0}

    assert mod._module_latency_us("normalizer", {}, {}) is None
    assert mod._module_latency_us("not_a_known_module", perf, probe) is None
    assert mod._module_latency_us("normalizer", perf, probe) == 14.0
    # a measured zero is a measurement, not a gap
    assert mod._module_latency_us("normalizer", {"normalizer_bidask_us": 0.0}, {}) == 0.0


def test_hotpath_rows_mark_unmeasured_inputs_and_bound_the_score(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_hotpath_files(tmp_path)

    rows, _, _ = mod._hotpath_rows(tmp_path, {}, {}, {}, {})

    row = _row_for(rows, "strategy_runner")
    # layer weight 30 + 25 for no Rust marker; nothing else was measured
    assert row["priority_score"] == 55
    assert row["priority"] == "P1"
    assert row["unknown_inputs"] == _ALL_UNKNOWN
    assert row["priority_score_max"] == 55 + 20 + 10 + 8
    assert row["priority_max"] == "P0"
    assert row["cpu_hotspot_pct"] is None
    assert row["p95_us"] is None


def test_hotpath_rows_score_measured_inputs_and_report_no_unknowns(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_hotpath_files(tmp_path)
    pairs = {"normalize_bidask": {"python_mean_s": 2.0e-5, "rust_mean_s": 4.0e-5, "rust_speedup_vs_python": 0.5}}
    hotspots = {"src/hft_platform/strategy/runner.py:handle_event": 12.0}
    perf = {"strategy_noop_metrics_on_us_per_event": 25.0}

    rows, _, _ = mod._hotpath_rows(tmp_path, pairs, hotspots, perf, {})

    row = _row_for(rows, "strategy_runner")
    assert row["unknown_inputs"] == []
    assert row["priority_score"] == 30 + 25 + 20 + 10 + 8
    assert row["priority_score_max"] == row["priority_score"]
    assert row["priority"] == "P0"
    assert row["priority_max"] == "P0"
    assert row["cpu_hotspot_pct"] == 12.0
    assert row["p95_us"] == 25.0


def test_hotpath_rows_keep_a_measured_speedup_apart_from_the_unknown_ones(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_hotpath_files(tmp_path)
    pairs = {"normalize_bidask": {"python_mean_s": 4.0e-5, "rust_mean_s": 2.0e-5, "rust_speedup_vs_python": 2.0}}

    rows, _, _ = mod._hotpath_rows(tmp_path, pairs, {}, {}, {})

    row = _row_for(rows, "strategy_runner")
    assert row["unknown_inputs"] == ["cpu_hotspot_pct", "p95_us"]
    assert row["priority_score"] == 55
    assert row["priority_score_max"] == 55 + 10 + 8


def test_hotpath_rows_rank_a_row_with_unknown_inputs_above_an_equal_measured_one(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_hotpath_files(tmp_path)
    for rel in ("order/adapter.py", "execution/router.py"):
        _write(tmp_path / "src/hft_platform" / rel, "def f():\n    rust = 1\n    return rust\n")
    pairs = {"position_update": {"python_mean_s": 3.0e-5, "rust_mean_s": 1.0e-5, "rust_speedup_vs_python": 3.0}}
    hotspots = {"order/adapter.py:f": 1.0, "execution/router.py:f": 1.0}
    perf = {"gateway_process_envelope_obj_us_per_call": 5.0}

    rows, _, _ = mod._hotpath_rows(tmp_path, pairs, hotspots, perf, {})

    adapter = _row_for(rows, "order_adapter")
    router = _row_for(rows, "execution_router")
    assert adapter["priority_score"] == router["priority_score"] == 25
    assert adapter["unknown_inputs"] == []
    assert router["unknown_inputs"] == ["p95_us"]
    order = [row["module"] for row in rows]
    assert order.index("execution_router") < order.index("order_adapter")


def test_hotpath_rows_flag_empty_perf_snapshot_and_stage_probe(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_hotpath_files(tmp_path)
    pairs = {"normalize_bidask": {"python_mean_s": 4.0e-5, "rust_mean_s": 2.0e-5, "rust_speedup_vs_python": 2.0}}

    _, _, empty_flags = mod._hotpath_rows(tmp_path, {}, {}, {}, {})
    _, _, full_flags = mod._hotpath_rows(tmp_path, pairs, {"runner.py": 1.0}, {"k": 1.0}, {"k": 1.0})
    _, _, no_probe_flags = mod._hotpath_rows(tmp_path, pairs, {"runner.py": 1.0}, {"k": 1.0}, {})

    assert empty_flags == _FLAGS_ALL_EMPTY
    assert full_flags == []
    assert no_probe_flags == ["stage_probe_empty"]


def test_main_writes_null_for_unmeasured_hotpath_inputs(tmp_path: Path) -> None:
    mod = _load_module()
    _seed_common_docs(tmp_path)
    _seed_hotpath_files(tmp_path)
    _seed_paper_index(tmp_path)

    rc = mod.main(["--project-root", str(tmp_path), "--allow-warn-exit-zero"])
    assert rc == 0

    ws_g_dir = tmp_path / "outputs/roadmap_execution/ws_g"
    hotpath = json.loads((ws_g_dir / "latest_hotpath_matrix.json").read_text(encoding="utf-8"))
    assert hotpath["data_quality_flags"] == _FLAGS_ALL_EMPTY
    assert hotpath["rows"]
    for row in hotpath["rows"]:
        assert row["cpu_hotspot_pct"] is None
        assert row["p95_us"] is None
        assert row["unknown_inputs"] == _ALL_UNKNOWN
        assert row["priority_score_max"] > row["priority_score"]

    cutover = json.loads((ws_g_dir / "latest_cutover_backlog.json").read_text(encoding="utf-8"))
    for item in cutover["items"]:
        assert item["unknown_inputs"] == _ALL_UNKNOWN
        assert item["priority_max"] in {"P0", "P1", "P2"}
    markdown = (ws_g_dir / "latest_cutover_backlog.md").read_text(encoding="utf-8")
    table_rows = {line.split("`")[1]: line for line in markdown.splitlines() if line.startswith("| `WSG-")}
    assert len(table_rows) == len(cutover["items"])
    for item in cutover["items"]:
        line = table_rows[item["backlog_id"]]
        if item["priority_max"] != item["priority"]:
            assert f"(up to {item['priority_max']})" in line
        else:
            assert "(up to" not in line
    assert any(item["priority_max"] != item["priority"] for item in cutover["items"])

    summary = json.loads((tmp_path / "outputs/roadmap_execution/summary/latest.json").read_text(encoding="utf-8"))
    assert summary["ws_g"]["status"] == "warn"


# --- the arithmetic the change kept --------------------------------------------------------------


def test_priority_bands_start_at_45_and_65() -> None:
    mod = _load_module()

    assert [mod._priority_for(score) for score in (0, 44, 45, 64, 65, 200)] == ["P2", "P2", "P1", "P1", "P0", "P0"]


def test_each_measured_input_adds_its_points_from_its_threshold(tmp_path: Path) -> None:
    """The strategy_runner row starts at 55 (layer weight 30, no Rust marker 25). A speedup below 1.0 adds 20, a
    hotspot share of at least 10 adds 10, a latency of at least 20 us adds 8; the value on the line counts."""
    mod = _load_module()
    _seed_hotpath_files(tmp_path)

    def score(*, speedup=None, hotspot=None, p95=None) -> int:
        pairs = (
            {}
            if speedup is None
            else {"normalize_bidask": {"python_mean_s": 1.0, "rust_mean_s": 1.0, "rust_speedup_vs_python": speedup}}
        )
        hotspots = {} if hotspot is None else {"src/hft_platform/strategy/runner.py:handle_event": hotspot}
        perf = {} if p95 is None else {"strategy_noop_metrics_on_us_per_event": p95}
        rows, _, _ = mod._hotpath_rows(tmp_path, pairs, hotspots, perf, {})
        return _row_for(rows, "strategy_runner")["priority_score"]

    assert score() == 55
    assert [score(speedup=value) - 55 for value in (0.99, 1.0, 2.0)] == [20, 0, 0]
    assert [score(hotspot=value) - 55 for value in (10.0, 9.99)] == [10, 0]
    assert [score(p95=value) - 55 for value in (20.0, 19.99)] == [8, 0]


def test_module_hotspot_pct_is_the_largest_share_of_its_frames() -> None:
    mod = _load_module()
    frames = {
        "src/hft_platform/strategy/runner.py:a": 3.0,
        "src/hft_platform/strategy/runner.py:b": 12.0,
        "src/hft_platform/strategy/runner.py:c": 5.0,
        "src/hft_platform/risk/engine.py:d": 40.0,
    }

    assert mod._module_hotspot_pct("strategy_runner", frames) == 12.0


def test_cutover_backlog_shows_an_upper_bound_only_for_rows_that_have_one() -> None:
    mod = _load_module()
    unbounded = {
        "backlog_id": "WSG-001",
        "module": "a",
        "layer": "x",
        "priority": "P2",
        "priority_max": "P0",
        "owner": "o",
        "eta_days": 21,
    }
    measured = {**unbounded, "backlog_id": "WSG-002", "module": "b", "priority": "P1", "priority_max": "P1"}

    text = mod._render_cutover_backlog({"items": [unbounded, measured]})

    rows = {line.split("`")[1]: line for line in text.splitlines() if line.startswith("| `WSG-")}
    assert "`P2 (up to P0)`" in rows["WSG-001"]
    assert "(up to" not in rows["WSG-002"]
    assert "some inputs were not measured" in text
    assert "some inputs were not measured" not in mod._render_cutover_backlog({"items": [measured]})
