from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "feature_canary_guard.py"
    spec = importlib.util.spec_from_file_location("feature_canary_guard", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _default_args():
    return SimpleNamespace(
        window="1h",
        max_shadow_mismatch=0.0,
        max_gap_flags=0.0,
        max_out_of_order_flags=0.0,
        max_partial_flags=0.0,
        max_latency_p99_ns=50_000.0,
        max_update_error_ratio=0.01,
        min_shadow_checks=1.0,
    )


def test_parser_supports_threshold_overrides():
    mod = _load_module()
    parser = mod._build_parser()
    args = parser.parse_args(["--window", "30m", "--max-latency-p99-ns", "60000", "--min-shadow-checks", "10"])
    assert args.window == "30m"
    assert args.max_latency_p99_ns == 60000
    assert args.min_shadow_checks == 10


def test_evaluate_rules_fails_on_shadow_mismatch():
    mod = _load_module()
    rules = mod._build_rules(_default_args())
    values = {
        "feature_shadow_mismatch_increase": 3.0,
        "feature_quality_gap_increase": 0.0,
        "feature_quality_out_of_order_increase": 0.0,
        "feature_quality_partial_increase": 0.0,
        "feature_plane_latency_p99_ns": 10_000.0,
        "feature_plane_update_error_ratio": 0.0,
        "feature_shadow_checks_increase": 50.0,
    }

    def _query(expr: str):
        for rule in rules:
            if rule.expr == expr:
                return values[rule.check_id], None
        return None, "missing"

    result = mod._evaluate_rules(rules, _query)
    assert result["overall"] == mod.STATUS_FAIL
    assert result["recommendation"] == "rollback_or_disable_feature_canary"


def test_main_writes_report_and_passes(tmp_path: Path):
    mod = _load_module()

    def _ok_query(_prom_url: str, expr: str, timeout_s: int = 8):  # noqa: ARG001
        if "feature_shadow_parity_checks_total" in expr:
            return 20.0, None
        if "feature_plane_latency_ns_bucket" in expr:
            return 20_000.0, None
        return 0.0, None

    mod._query_prom = _ok_query
    out_dir = tmp_path / "out"
    rc = mod.main(["--prom-url", "http://example.test:9090", "--output-dir", str(out_dir)])
    assert rc == 0

    reports = list(out_dir.glob("feature_canary_*.json"))
    assert len(reports) == 1
    payload = json.loads(reports[0].read_text(encoding="utf-8"))
    assert payload["result"]["overall"] == mod.STATUS_PASS
