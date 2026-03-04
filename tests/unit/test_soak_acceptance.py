from __future__ import annotations

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
    assert "[30m]" in by_id["feed_reconnect_gap_24h"].expr
    assert "[30m]" in by_id["feed_reconnect_symbol_gap_24h"].expr


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
