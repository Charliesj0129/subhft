from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "ch_query_guard_suite.py"
    spec = importlib.util.spec_from_file_location("ch_query_guard_suite", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_profile(path: Path, queries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "profile_id": "test_profile",
        "queries": queries,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_parser_accepts_basic_flags():
    mod = _load_module()
    parser = mod._build_parser()
    args = parser.parse_args(["--profile", "x.json", "--allow-warn-exit-zero", "--dry-run"])
    assert args.profile == "x.json"
    assert args.allow_warn_exit_zero is True
    assert args.dry_run is True


def test_main_generates_pass_suite_report(tmp_path: Path):
    mod = _load_module()
    profile = tmp_path / "profile.json"
    _write_profile(
        profile,
        [
            {"id": "q1", "sql": "SELECT 1 LIMIT 1"},
            {"id": "q2", "sql": "SELECT 2 LIMIT 1"},
        ],
    )

    def _fake_run_cmd(argv: list[str], timeout_s: int):  # noqa: ARG001
        if argv[2] == "check":
            return (
                0,
                "[query-guard] check json: outputs/query_guard/checks/check_x.json\n"
                "[query-guard] check md  : outputs/query_guard/checks/check_x.md\n",
                "",
            )
        if argv[2] == "run":
            return (
                0,
                "[query-guard] run json: outputs/query_guard/runs/run_x.json\n"
                "[query-guard] run md  : outputs/query_guard/runs/run_x.md\n",
                "",
            )
        raise AssertionError(argv)

    mod._run_cmd = _fake_run_cmd

    rc = mod.main(
        [
            "--profile",
            str(profile),
            "--output-dir",
            str(tmp_path / "out"),
            "--guard-script",
            "scripts/ch_query_guard.py",
            "--python-bin",
            "python3",
        ]
    )
    assert rc == 0

    reports = list((tmp_path / "out" / "suites").glob("suite_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == "pass"
    assert payload["result"]["check_status_counts"]["pass"] == 2
    assert payload["result"]["run_status_counts"]["pass"] == 2


def test_main_warn_exit_code_can_be_downgraded(tmp_path: Path):
    mod = _load_module()
    profile = tmp_path / "profile_warn.json"
    _write_profile(profile, [{"id": "q1", "sql": "SELECT count() FROM hft.market_data WHERE ingest_ts > 0"}])

    def _fake_run_cmd(argv: list[str], timeout_s: int):  # noqa: ARG001
        if argv[2] == "check":
            return (
                1,
                "[query-guard] check json: outputs/query_guard/checks/check_warn.json\n"
                "[query-guard] check md  : outputs/query_guard/checks/check_warn.md\n",
                "",
            )
        if argv[2] == "run":
            return (
                0,
                "[query-guard] run json: outputs/query_guard/runs/run_warn.json\n"
                "[query-guard] run md  : outputs/query_guard/runs/run_warn.md\n",
                "",
            )
        raise AssertionError(argv)

    mod._run_cmd = _fake_run_cmd

    rc_warn = mod.main(
        [
            "--profile",
            str(profile),
            "--output-dir",
            str(tmp_path / "out_warn"),
            "--guard-script",
            "scripts/ch_query_guard.py",
            "--python-bin",
            "python3",
        ]
    )
    assert rc_warn == 1

    rc_ok = mod.main(
        [
            "--profile",
            str(profile),
            "--output-dir",
            str(tmp_path / "out_warn_ok"),
            "--guard-script",
            "scripts/ch_query_guard.py",
            "--python-bin",
            "python3",
            "--allow-warn-exit-zero",
        ]
    )
    assert rc_ok == 0


def test_main_fails_and_skips_run_when_check_fails(tmp_path: Path):
    mod = _load_module()
    profile = tmp_path / "profile_fail.json"
    _write_profile(profile, [{"id": "q_fail", "sql": "DROP TABLE hft.market_data"}])
    calls = {"check": 0, "run": 0}

    def _fake_run_cmd(argv: list[str], timeout_s: int):  # noqa: ARG001
        if argv[2] == "check":
            calls["check"] += 1
            return (
                2,
                "[query-guard] check json: outputs/query_guard/checks/check_fail.json\n"
                "[query-guard] check md  : outputs/query_guard/checks/check_fail.md\n",
                "blocked",
            )
        if argv[2] == "run":
            calls["run"] += 1
            raise AssertionError("run stage should be skipped when check fails")
        raise AssertionError(argv)

    mod._run_cmd = _fake_run_cmd

    rc = mod.main(
        [
            "--profile",
            str(profile),
            "--output-dir",
            str(tmp_path / "out_fail"),
            "--guard-script",
            "scripts/ch_query_guard.py",
            "--python-bin",
            "python3",
        ]
    )
    assert rc == 2
    assert calls["check"] == 1
    assert calls["run"] == 0

    reports = list((tmp_path / "out_fail" / "suites").glob("suite_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["run_status_counts"]["skipped"] == 1
