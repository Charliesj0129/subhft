"""Go-live checklist CLI commands."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from hft_platform.cli._checks import check_disk_space, check_kill_switch, check_wal_backlog


def _check_position_checkpoint() -> dict[str, Any]:
    path = os.getenv("HFT_POSITION_CHECKPOINT_PATH") or os.getenv(
        "HFT_CHECKPOINT_PATH",
        ".runtime/position_checkpoint.json",
    )
    exists = os.path.exists(path)
    return {"name": "position_checkpoint", "ok": exists, "detail": f"found: {path}" if exists else f"missing: {path}"}


def _check_config_not_sim() -> dict[str, Any]:
    mode = os.getenv("HFT_MODE", "sim")
    ok = mode != "sim"
    return {"name": "config_not_sim", "ok": ok, "detail": f"HFT_MODE={mode}"}


def _check_alertmanager_config() -> dict[str, Any]:
    path = os.getenv("HFT_ALERTMANAGER_CONFIG", "config/monitoring/alerts/alertmanager.yml")
    exists = os.path.exists(path)
    return {"name": "alertmanager_config", "ok": exists, "detail": f"found: {path}" if exists else f"missing: {path}"}


_ALL_CHECKS: dict[str, Callable[[], dict[str, Any]]] = {
    "kill_switch": lambda: check_kill_switch(active_detail="ACTIVE - deactivate before go-live"),
    "position_checkpoint": _check_position_checkpoint,
    "wal_backlog": lambda: check_wal_backlog(max_files=50),
    "config_not_sim": _check_config_not_sim,
    "disk_space": lambda: check_disk_space(min_gb=2.0),
    "alertmanager_config": _check_alertmanager_config,
}


def cmd_golive_check(args) -> None:
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
