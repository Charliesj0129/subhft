from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "reliability_review_pack.py"
    spec = importlib.util.spec_from_file_location("reliability_review_pack", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _seed_minimal_monthly_inputs(
    tmp_path: Path,
    *,
    with_canary: bool = True,
    with_drift: bool = True,
    with_query_guard: bool = True,
    with_query_guard_suite: bool = True,
    with_feature_canary: bool = True,
    with_callback_latency: bool = True,
) -> dict[str, Path]:
    soak_dir = tmp_path / "outputs" / "soak_reports"
    deploy_dir = tmp_path / "outputs" / "deploy_guard"
    query_guard_dir = tmp_path / "outputs" / "query_guard"
    feature_canary_dir = tmp_path / "outputs" / "feature_canary"
    callback_latency_dir = tmp_path / "outputs" / "callback_latency"
    out_dir = tmp_path / "outputs" / "reliability" / "monthly"

    _write_json(
        soak_dir / "daily" / "2026-03-04.json",
        {
            "generated_at": "2026-03-04T22:23:24+08:00",
            "summary": {"overall": "pass"},
            "checks": [
                {"id": "wal_backlog_max_24h", "status": "pass", "value": 2},
            ],
        },
    )
    _write_json(
        soak_dir / "daily" / "2026-03-05.json",
        {
            "generated_at": "2026-03-05T22:23:24+08:00",
            "summary": {"overall": "pass"},
            "checks": [
                {"id": "wal_backlog_max_24h", "status": "pass", "value": 4},
            ],
        },
    )

    if with_canary:
        _write_json(
            soak_dir / "canary" / "canary_2026-03-05_2026-03-05.json",
            {
                "generated_at": "2026-03-05T16:30:00+08:00",
                "result": {"overall": "pass", "trading_days": 5},
            },
        )

    _write_json(
        soak_dir / "weekly" / "week_2026-03-01_2026-03-05.json",
        {
            "generated_at": "2026-03-05T16:20:00+08:00",
            "window_start": "2026-03-01",
            "window_end": "2026-03-05",
            "days": 5,
            "fail_days": 0,
            "warn_days": 0,
            "pass_days": 5,
        },
    )

    if with_drift:
        _write_json(
            deploy_dir / "checks" / "check_20260305T120000Z.json",
            {
                "generated_at": "2026-03-05T20:00:00+08:00",
                "result": {"overall": "pass"},
            },
        )

    _write_json(
        out_dir / "drill_checks" / "drill_20260305T120500Z.json",
        {
            "generated_at": "2026-03-05T20:05:00+08:00",
            "status": "pass",
            "exit_code": 0,
        },
    )

    if with_feature_canary:
        _write_json(
            feature_canary_dir / "feature_canary_20260305T120400Z.json",
            {
                "generated_at": "2026-03-05T20:04:00+08:00",
                "result": {"overall": "pass", "recommendation": "promote_canary_allowed", "checks": []},
            },
        )
    if with_callback_latency:
        _write_json(
            callback_latency_dir / "callback_latency_20260305T120450Z.json",
            {
                "generated_at": "2026-03-05T20:04:50+08:00",
                "result": {"overall": "pass", "recommendation": "callback_path_healthy", "checks": []},
            },
        )

    if with_query_guard:
        _write_json(
            query_guard_dir / "checks" / "check_20260305T120100Z.json",
            {
                "generated_at": "2026-03-05T20:01:00+08:00",
                "result": {"overall": "pass"},
            },
        )
        _write_json(
            query_guard_dir / "runs" / "run_20260305T120200Z.json",
            {
                "generated_at": "2026-03-05T20:02:00+08:00",
                "execution": {"allowed": True, "status": "pass", "exit_code": 0},
            },
        )
        if with_query_guard_suite:
            _write_json(
                query_guard_dir / "suites" / "suite_20260305T120300Z.json",
                {
                    "generated_at": "2026-03-05T20:03:00+08:00",
                    "result": {"overall": "pass", "query_count": 3},
                },
            )

    return {
        "soak_dir": soak_dir,
        "deploy_dir": deploy_dir,
        "query_guard_dir": query_guard_dir,
        "feature_canary_dir": feature_canary_dir,
        "callback_latency_dir": callback_latency_dir,
        "out_dir": out_dir,
    }


def test_parser_supports_monthly_args():
    mod = _load_module()
    parser = mod._build_parser()

    args = parser.parse_args(
        [
            "--month",
            "2026-03",
            "--run-drill-suite",
            "--disk-path",
            ".",
            "--min-query-guard-runs",
            "2",
            "--min-query-guard-suite-runs",
            "3",
            "--min-feature-canary-runs",
            "4",
            "--min-callback-latency-runs",
            "5",
        ]
    )
    assert args.month == "2026-03"
    assert args.run_drill_suite is True
    assert args.disk_path == ["."]
    assert args.min_query_guard_runs == 2
    assert args.min_query_guard_suite_runs == 3
    assert args.min_feature_canary_runs == 4
    assert args.min_callback_latency_runs == 5


def test_main_generates_monthly_pack(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
        ]
    )
    assert rc == 0

    reports = list(env["out_dir"].glob("monthly_2026-03_*.json"))
    assert len(reports) == 1

    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == "pass"
    assert payload["sections"]["backlog"]["peak"] == 4.0
    assert payload["sections"]["query_guard"]["runs_in_month"] == 1
    assert payload["sections"]["query_guard"]["suites_in_month"] == 1
    assert payload["sections"]["feature_canary"]["reports_in_month"] == 1
    assert payload["sections"]["callback_latency"]["reports_in_month"] == 1


def test_main_fails_when_canary_or_drift_missing(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path, with_canary=False, with_drift=False)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
        ]
    )
    assert rc == 2


def test_main_warns_when_query_guard_runs_below_threshold(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path, with_query_guard=False)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
            "--min-query-guard-runs",
            "1",
        ]
    )
    assert rc == 1


def test_main_warns_when_query_guard_suites_below_threshold(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path, with_query_guard_suite=False)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
            "--min-query-guard-runs",
            "1",
            "--min-query-guard-suite-runs",
            "1",
        ]
    )
    assert rc == 1


def test_main_warns_when_feature_canary_runs_below_threshold(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path, with_feature_canary=False)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
            "--min-feature-canary-runs",
            "1",
        ]
    )
    assert rc == 1


def test_main_warns_when_callback_latency_runs_below_threshold(tmp_path: Path):
    mod = _load_module()
    env = _seed_minimal_monthly_inputs(tmp_path, with_callback_latency=False)

    rc = mod.main(
        [
            "--project-root",
            str(tmp_path),
            "--soak-dir",
            str(env["soak_dir"]),
            "--deploy-dir",
            str(env["deploy_dir"]),
            "--query-guard-dir",
            str(env["query_guard_dir"]),
            "--feature-canary-dir",
            str(env["feature_canary_dir"]),
            "--callback-latency-dir",
            str(env["callback_latency_dir"]),
            "--output-dir",
            str(env["out_dir"]),
            "--month",
            "2026-03",
            "--disk-path",
            ".",
            "--min-disk-free-gb",
            "0",
            "--min-callback-latency-runs",
            "1",
        ]
    )
    assert rc == 1
