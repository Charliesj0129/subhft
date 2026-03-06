from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "callback_latency_guard.py"
    spec = importlib.util.spec_from_file_location("callback_latency_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _default_args():
    return SimpleNamespace(
        window="30m",
        max_callback_ingress_p99_ns=100_000.0,
        max_callback_queue_dropped=0.0,
        max_callback_queue_depth_p99=512.0,
        max_parse_fallback_ratio=0.1,
        max_parse_miss=0.0,
        min_parse_samples=1.0,
    )


def test_parser_supports_threshold_overrides():
    mod = _load_module()
    parser = mod._build_parser()
    args = parser.parse_args(
        [
            "--window",
            "10m",
            "--max-callback-ingress-p99-ns",
            "50000",
            "--max-parse-fallback-ratio",
            "0.05",
        ]
    )
    assert args.window == "10m"
    assert args.max_callback_ingress_p99_ns == 50000
    assert args.max_parse_fallback_ratio == 0.05


def test_evaluate_rules_fails_on_ingress_latency():
    mod = _load_module()
    rules = mod._build_rules(_default_args())
    values = {
        "callback_ingress_latency_p99_ns": 200_000.0,
        "callback_queue_dropped_increase": 0.0,
        "callback_queue_depth_p99": 50.0,
        "callback_parse_fallback_ratio": 0.01,
        "callback_parse_miss_increase": 0.0,
        "callback_parse_samples_increase": 100.0,
    }

    def _query(expr: str):
        for rule in rules:
            if rule.expr == expr:
                return values[rule.check_id], None
        return None, "missing"

    result = mod._evaluate_rules(rules, _query)
    assert result["overall"] == mod.STATUS_FAIL
    assert result["recommendation"] == "block_canary_and_rollback_callback_changes"


def test_main_writes_report_and_passes(tmp_path: Path):
    mod = _load_module()

    def _ok_query(_prom_url: str, expr: str, timeout_s: int = 8):  # noqa: ARG001
        if "shioaji_quote_callback_ingress_latency_ns_bucket" in expr:
            return 20_000.0, None
        if "callback_queue_depth" in expr:
            return 32.0, None
        if 'result=~"fast|fallback|miss"' in expr:
            return 300.0, None
        return 0.0, None

    mod._query_prom = _ok_query
    out_dir = tmp_path / "out"
    rc = mod.main(["--prom-url", "http://example.test:9090", "--output-dir", str(out_dir)])
    assert rc == 0

    reports = list(out_dir.glob("callback_latency_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == mod.STATUS_PASS
