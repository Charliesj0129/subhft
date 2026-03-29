"""Health preflight CLI commands."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from hft_platform.cli._checks import check_disk_space, check_kill_switch, check_wal_backlog


def _check_clickhouse(timeout: float) -> dict[str, Any]:
    host = os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    port = os.getenv("HFT_CLICKHOUSE_HTTP_PORT", "8123")
    url = f"http://{host}:{port}/?query=SELECT+1"
    try:
        import urllib.request

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            ok = resp.status == 200
        return {"name": "clickhouse", "ok": ok, "detail": f"{host}:{port} status={resp.status}"}
    except Exception as exc:
        return {"name": "clickhouse", "ok": False, "detail": str(exc)}


def _check_checkpoint_recent(max_age_s: float = 300.0) -> dict[str, Any]:
    path = os.getenv("HFT_CHECKPOINT_PATH", ".runtime/position_checkpoint.json")
    if not os.path.exists(path):
        return {"name": "checkpoint_recent", "ok": False, "detail": f"not found: {path}"}
    age_s = time.time() - os.path.getmtime(path)
    ok = age_s <= max_age_s
    return {"name": "checkpoint_recent", "ok": ok, "detail": f"age={age_s:.0f}s (max={max_age_s:.0f}s)"}


def _check_prometheus(timeout: float) -> dict[str, Any]:
    host = os.getenv("HFT_PROMETHEUS_HOST", "localhost")
    port = os.getenv("HFT_PROMETHEUS_PORT", "9091")
    url = f"http://{host}:{port}/-/healthy"
    try:
        import urllib.request

        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            ok = resp.status == 200
        return {"name": "prometheus", "ok": ok, "detail": f"{host}:{port} status={resp.status}"}
    except Exception as exc:
        return {"name": "prometheus", "ok": False, "detail": str(exc)}


def _check_config_valid() -> dict[str, Any]:
    try:
        from hft_platform.config.loader import load_settings

        load_settings()
        return {"name": "config_valid", "ok": True, "detail": "loaded successfully"}
    except Exception as exc:
        return {"name": "config_valid", "ok": False, "detail": str(exc)}


def cmd_health_preflight(args) -> None:
    timeout = getattr(args, "timeout", 5.0)
    output_json = getattr(args, "json", False)
    checks = [
        _check_clickhouse(timeout),
        _check_checkpoint_recent(),
        check_wal_backlog(max_files=100),
        _check_prometheus(timeout),
        check_disk_space(min_gb=1.0),
        check_kill_switch(),
        _check_config_valid(),
    ]
    all_ok = all(c["ok"] for c in checks)
    if output_json:
        result = {"ok": all_ok, "checks": checks}
        print(json.dumps(result, indent=2))
    else:
        for c in checks:
            status = "PASS" if c["ok"] else "FAIL"
            print(f"  [{status}] {c['name']}: {c['detail']}")
        print()
        print(f"Overall: {'PASS' if all_ok else 'FAIL'}")
    sys.exit(0 if all_ok else 1)
