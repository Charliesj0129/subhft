from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "ch_query_guard.py"
    spec = importlib.util.spec_from_file_location("ch_query_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parser_supports_check_and_run():
    mod = _load_module()
    parser = mod._build_parser()

    c = parser.parse_args(["check", "--query", "SELECT 1"])
    assert c.command == "check"

    r = parser.parse_args(["run", "--query", "SELECT 1", "--dry-run"])
    assert r.command == "run"
    assert r.dry_run is True


def test_guard_blocks_mutating_query():
    mod = _load_module()
    out = mod._evaluate_sql_guard("DROP TABLE hft.market_data")

    assert out["overall"] == mod.STATUS_FAIL
    ids = {c["id"] for c in out["checks"]}
    assert "query_denied_keywords" in ids


def test_guard_blocks_large_table_full_scan_by_default():
    mod = _load_module()
    out = mod._evaluate_sql_guard("SELECT * FROM hft.market_data")

    assert out["overall"] == mod.STATUS_FAIL
    row = next(c for c in out["checks"] if c["id"] == "large_table_full_scan_guard")
    assert row["status"] == mod.STATUS_FAIL


def test_guard_downgrades_full_scan_when_override_enabled():
    mod = _load_module()
    out = mod._evaluate_sql_guard("SELECT * FROM hft.market_data", allow_full_scan=True)

    assert out["overall"] == mod.STATUS_WARN
    row = next(c for c in out["checks"] if c["id"] == "large_table_full_scan_guard")
    assert row["status"] == mod.STATUS_WARN


def test_build_clickhouse_command_contains_guard_limits():
    mod = _load_module()
    args = SimpleNamespace(
        container="clickhouse",
        host="localhost",
        port=9000,
        user="default",
        readonly=True,
        max_memory_usage=1024,
        max_threads=2,
        max_execution_time=15,
        max_result_rows=100,
        result_overflow_mode="break",
    )
    cmd = mod._build_clickhouse_command(args, "SELECT 1")

    assert "--readonly=1" in cmd
    assert "--max_memory_usage=1024" in cmd
    assert "--max_execution_time=15" in cmd


def test_check_returns_warn_and_writes_artifact(tmp_path: Path):
    mod = _load_module()
    args = SimpleNamespace(
        command="check",
        query="SELECT count() FROM hft.market_data WHERE ingest_ts > 0",
        query_file=None,
        output_dir=str(tmp_path / "out"),
        allow_full_scan=False,
        allow_warn_exit_zero=False,
    )

    rc = mod._run_check(args)
    assert rc == 1

    reports = list((tmp_path / "out" / "checks").glob("check_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == mod.STATUS_WARN


def test_run_blocks_fail_without_executing(tmp_path: Path):
    mod = _load_module()

    def _should_not_run(_argv, timeout_s=60):  # noqa: ARG001
        raise AssertionError("_run_cmd should not be called")

    mod._run_cmd = _should_not_run

    args = SimpleNamespace(
        command="run",
        query="DELETE FROM hft.market_data WHERE 1=1",
        query_file=None,
        output_dir=str(tmp_path / "out"),
        allow_full_scan=False,
        container="clickhouse",
        host="localhost",
        port=9000,
        user="default",
        readonly=True,
        max_memory_usage=1024,
        max_threads=2,
        max_execution_time=15,
        max_result_rows=100,
        result_overflow_mode="break",
        timeout_s=60,
        allow_warn_execute=False,
        dry_run=False,
    )

    rc = mod._run_execute(args)
    assert rc == 2


def test_run_dry_run_passes(tmp_path: Path):
    mod = _load_module()
    args = SimpleNamespace(
        command="run",
        query="SELECT count() FROM hft.market_data WHERE ingest_ts > 0 LIMIT 10",
        query_file=None,
        output_dir=str(tmp_path / "out"),
        allow_full_scan=False,
        container="clickhouse",
        host="localhost",
        port=9000,
        user="default",
        readonly=True,
        max_memory_usage=1024,
        max_threads=2,
        max_execution_time=15,
        max_result_rows=100,
        result_overflow_mode="break",
        timeout_s=60,
        allow_warn_execute=False,
        dry_run=True,
    )

    rc = mod._run_execute(args)
    assert rc == 0
