"""Go-live checklist CLI commands."""

from __future__ import annotations

import json
import os
import shutil
import sys
from typing import Any

from structlog import get_logger

logger = get_logger(__name__)

_DEFAULT_KILL_SWITCH_PATH = ".runtime/kill_switch"


def _check_kill_switch() -> dict[str, Any]:
    """Kill switch must not be active."""
    path = os.getenv("HFT_KILL_SWITCH_PATH", _DEFAULT_KILL_SWITCH_PATH)
    active = os.path.exists(path)
    return {"name": "kill_switch", "ok": not active, "detail": "ACTIVE — deactivate before go-live" if active else "inactive"}


def _check_position_checkpoint() -> dict[str, Any]:
    """Position checkpoint file must exist."""
    path = os.getenv("HFT_CHECKPOINT_PATH", ".runtime/position_checkpoint.json")
    exists = os.path.exists(path)
    return {"name": "position_checkpoint", "ok": exists, "detail": f"found: {path}" if exists else f"missing: {path}"}


def _check_wal_backlog(max_files: int = 50) -> dict[str, Any]:
    """WAL backlog should be manageable before go-live."""
    wal_dir = os.getenv("HFT_WAL_DIR", ".wal")
    if not os.path.isdir(wal_dir):
        return {"name": "wal_backlog", "ok": True, "detail": f"no wal dir: {wal_dir}"}
    count = len([f for f in os.listdir(wal_dir) if f.endswith(".wal")])
    ok = count <= max_files
    return {"name": "wal_backlog", "ok": ok, "detail": f"files={count} (max={max_files})"}


def _check_config_not_sim() -> dict[str, Any]:
    """HFT_MODE should not be sim for go-live."""
    mode = os.getenv("HFT_MODE", "sim")
    ok = mode != "sim"
    return {"name": "config_not_sim", "ok": ok, "detail": f"HFT_MODE={mode}"}


def _check_disk_space(min_gb: float = 2.0) -> dict[str, Any]:
    """Sufficient disk space for go-live."""
    usage = shutil.disk_usage(".")
    free_gb = usage.free / (1024 ** 3)
    ok = free_gb >= min_gb
    return {"name": "disk_space", "ok": ok, "detail": f"free={free_gb:.1f}GB (min={min_gb:.1f}GB)"}


def _check_alertmanager_config() -> dict[str, Any]:
    """Alertmanager config file must exist."""
    path = os.getenv("HFT_ALERTMANAGER_CONFIG", "config/alertmanager.yml")
    exists = os.path.exists(path)
    return {"name": "alertmanager_config", "ok": exists, "detail": f"found: {path}" if exists else f"missing: {path}"}


_ALL_CHECKS = {
    "kill_switch": _check_kill_switch,
    "position_checkpoint": _check_position_checkpoint,
    "wal_backlog": _check_wal_backlog,
    "config_not_sim": _check_config_not_sim,
    "disk_space": _check_disk_space,
    "alertmanager_config": _check_alertmanager_config,
}


def cmd_golive_check(args) -> None:
    """Run go-live checklist."""
    skip = set(getattr(args, "skip", []) or [])
    output_json = getattr(args, "json", False)

    checks: list[dict[str, Any]] = []
    for name, fn in _ALL_CHECKS.items():
        if name in skip:
            checks.append({"name": name, "ok": True, "detail": "SKIPPED"})
        else:
            checks.append(fn())

    all_ok = all(c["ok"] for c in checks)

    if output_json:
        result = {"ok": all_ok, "checks": checks}
        print(json.dumps(result, indent=2))
    else:
        for c in checks:
            status = "PASS" if c["ok"] else "FAIL"
            print(f"  [{status}] {c['name']}: {c['detail']}")
        print()
        print(f"Go-live: {'READY' if all_ok else 'NOT READY'}")

    sys.exit(0 if all_ok else 1)
