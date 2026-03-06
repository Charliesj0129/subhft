from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path


def _load_module():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "soak_acceptance.py"
    spec = importlib.util.spec_from_file_location("soak_acceptance", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_compose_json_supports_lines_and_array():
    mod = _load_module()
    lines = '{"Service":"hft-engine","State":"running"}\n{"Service":"redis","State":"running"}\n'
    parsed_lines = mod._parse_compose_json(lines)
    assert len(parsed_lines) == 2
    assert parsed_lines[0]["Service"] == "hft-engine"

    arr = '[{"Service":"hft-engine","State":"running"}]'
    parsed_arr = mod._parse_compose_json(arr)
    assert len(parsed_arr) == 1
    assert parsed_arr[0]["Service"] == "hft-engine"


def test_rule_status_maps_severity_to_fail_or_warn():
    mod = _load_module()
    critical = mod.Rule("a", "critical", "up", "ge", 1.0, "critical", "must be up")
    warning = mod.Rule("b", "warning", "up", "ge", 1.0, "warning", "prefer up")

    status_c, _ = mod._status_from_rule(critical, 0.0, None)
    status_w, _ = mod._status_from_rule(warning, 0.0, None)
    status_u, _ = mod._status_from_rule(warning, None, None)

    assert status_c == mod.STATUS_FAIL
    assert status_w == mod.STATUS_WARN
    assert status_u == mod.STATUS_UNKNOWN


def test_daily_rules_use_uptime_ratio_not_min_over_time():
    mod = _load_module()
    rules = mod._daily_rules(expect_trading_day=False, reconnect_window="30m")
    by_id = {r.check_id: r for r in rules}

    assert "execution_gateway_alive_5m" in by_id
    assert "execution_router_alive_5m" in by_id
    assert "execution_gateway_uptime_ratio_24h" in by_id
    assert "execution_router_uptime_ratio_24h" in by_id
    assert "execution_gateway_alive_24h" not in by_id
    assert "execution_router_alive_24h" not in by_id
    assert by_id["execution_gateway_uptime_ratio_24h"].expr.startswith("avg_over_time(")
    assert by_id["execution_router_uptime_ratio_24h"].expr.startswith("avg_over_time(")
    assert "recorder_insert_failed_ratio_24h" in by_id
    assert "recorder_insert_retry_ratio_24h" in by_id
    assert "recorder_insert_batches_total" in by_id["recorder_insert_failed_ratio_24h"].expr
    assert "recorder_insert_batches_total" in by_id["recorder_insert_retry_ratio_24h"].expr
    assert "[30m]" in by_id["feed_reconnect_gap_24h"].expr
    assert "[30m]" in by_id["feed_reconnect_symbol_gap_24h"].expr
    assert "quote_watchdog_callback_reregister_24h" in by_id
    assert "[30m]" in by_id["quote_watchdog_callback_reregister_24h"].expr


def test_parse_uptime_seconds_parses_common_compose_formats():
    mod = _load_module()
    assert mod._parse_uptime_seconds("Up 6 minutes (healthy)") == 360
    assert mod._parse_uptime_seconds("Up 2 hours (healthy)") == 7200
    assert mod._parse_uptime_seconds("Up 1 day, 3 hours") == (24 + 3) * 3600
    assert mod._parse_uptime_seconds("Exited (1) 2 minutes ago") is None


def test_derive_reconnect_window_caps_by_engine_uptime():
    mod = _load_module()
    services = [
        {"Service": "redis", "Status": "Up 3 hours"},
        {"Service": "hft-engine", "Status": "Up 7 minutes (healthy)"},
    ]
    window, uptime_s = mod._derive_reconnect_window(services)
    assert uptime_s == 420
    assert window == "15m"


def test_canary_window_passes_when_thresholds_met():
    mod = _load_module()
    rows = [
        (
            dt.date(2026, 3, 2),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_PASS, "value": 5},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_PASS, "value": 0.03},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_PASS,
                        "value": 2,
                    },
                ],
            },
        ),
        (
            dt.date(2026, 3, 3),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_PASS, "value": 3},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_PASS, "value": 0.06},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_PASS,
                        "value": 1,
                    },
                ],
            },
        ),
    ]
    out = mod._evaluate_canary_window(
        rows,
        min_trading_days=2,
        min_first_quote_pass_ratio=1.0,
        max_reconnect_failure_ratio=0.2,
        max_watchdog_callback_reregister=10.0,
    )
    assert out["overall"] == mod.STATUS_PASS
    assert out["trading_days"] == 2
    assert out["first_quote_pass_days"] == 2
    assert out["reconnect_failure_ratio_max"] == 0.06
    assert out["watchdog_callback_reregister_max"] == 2


def test_canary_window_fails_when_first_quote_or_reconnect_ratio_violates():
    mod = _load_module()
    rows = [
        (
            dt.date(2026, 3, 2),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_WARN, "value": 0},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_WARN, "value": 0.35},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_WARN,
                        "value": 150,
                    },
                ],
            },
        ),
        (
            dt.date(2026, 3, 3),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_PASS, "value": 4},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_PASS, "value": 0.02},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_PASS,
                        "value": 3,
                    },
                ],
            },
        ),
    ]
    out = mod._evaluate_canary_window(
        rows,
        min_trading_days=2,
        min_first_quote_pass_ratio=1.0,
        max_reconnect_failure_ratio=0.2,
        max_watchdog_callback_reregister=100.0,
    )
    assert out["overall"] == mod.STATUS_FAIL
    assert out["first_quote_pass_ratio"] < 1.0
    assert out["reconnect_failure_ratio_max"] == 0.35
    assert out["watchdog_callback_reregister_max"] == 150
    assert out["reasons"]


def test_canary_window_fails_when_watchdog_reregister_exceeds_threshold():
    mod = _load_module()
    rows = [
        (
            dt.date(2026, 3, 2),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_PASS, "value": 5},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_PASS, "value": 0.02},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_WARN,
                        "value": 250,
                    },
                ],
            },
        ),
        (
            dt.date(2026, 3, 3),
            {
                "expect_trading_day": True,
                "checks": [
                    {"id": "feed_first_quote_24h", "status": mod.STATUS_PASS, "value": 6},
                    {"id": "feed_reconnect_failure_ratio_24h", "status": mod.STATUS_PASS, "value": 0.03},
                    {
                        "id": "quote_watchdog_callback_reregister_24h",
                        "status": mod.STATUS_PASS,
                        "value": 2,
                    },
                ],
            },
        ),
    ]
    out = mod._evaluate_canary_window(
        rows,
        min_trading_days=2,
        min_first_quote_pass_ratio=1.0,
        max_reconnect_failure_ratio=0.2,
        max_watchdog_callback_reregister=100.0,
    )
    assert out["overall"] == mod.STATUS_FAIL
    assert out["watchdog_callback_reregister_max"] == 250
    assert any("watchdog callback_reregister max" in reason for reason in out["reasons"])


def test_parser_supports_canary_command():
    mod = _load_module()
    parser = mod._build_parser()
    args = parser.parse_args(["canary", "--window-days", "7", "--min-trading-days", "3"])
    assert args.command == "canary"
    assert args.window_days == 7
    assert args.min_trading_days == 3
    assert args.max_watchdog_callback_reregister == 120.0
