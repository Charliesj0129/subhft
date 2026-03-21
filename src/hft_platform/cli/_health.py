"""Health preflight CLI commands."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from typing import Any

from structlog import get_logger

logger = get_logger(__name__)
_DEFAULT_KILL_SWITCH_PATH = ".runtime/kill_switch"


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


def _check_wal_backlog(max_files: int = 100) -> dict[str, Any]:
    wal_dir = os.getenv("HFT_WAL_DIR", ".wal")
    if not os.path.isdir(wal_dir):
        return {"name": "wal_backlog", "ok": True, "detail": f"no wal dir: {wal_dir}"}
    count = len([f for f in os.listdir(wal_dir) if f.endswith(".wal")])
    ok = count <= max_files
    return {"name": "wal_backlog", "ok": ok, "detail": f"files={count} (max={max_files})"}


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


def _check_disk_space(min_gb: float = 1.0) -> dict[str, Any]:
    usage = shutil.disk_usage(".")
    free_gb = usage.free / (1024**3)
    ok = free_gb >= min_gb
    return {"name": "disk_space", "ok": ok, "detail": f"free={free_gb:.1f}GB (min={min_gb:.1f}GB)"}


def _check_kill_switch() -> dict[str, Any]:
    path = os.getenv("HFT_KILL_SWITCH_PATH", _DEFAULT_KILL_SWITCH_PATH)
    active = os.path.exists(path)
    return {"name": "kill_switch", "ok": not active, "detail": "ACTIVE" if active else "inactive"}


def _check_config_valid() -> dict[str, Any]:
    try:
        from hft_platform.config.loader import load_config

        load_config()
        return {"name": "config_valid", "ok": True, "detail": "loaded successfully"}
    except Exception as exc:
        return {"name": "config_valid", "ok": False, "detail": str(exc)}


def cmd_health_preflight(args) -> None:
    timeout = getattr(args, "timeout", 5.0)
    output_json = getattr(args, "json", False)
    checks = [
        _check_clickhouse(timeout),
        _check_checkpoint_recent(),
        _check_wal_backlog(),
        _check_prometheus(timeout),
        _check_disk_space(),
        _check_kill_switch(),
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
