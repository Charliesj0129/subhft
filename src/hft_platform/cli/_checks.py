"""Shared preflight / go-live check helpers."""

from __future__ import annotations

import os
import shutil
from typing import Any

from hft_platform.risk import kill_switch

_DEFAULT_KILL_SWITCH_PATH = kill_switch.DEFAULT_PATH


def check_kill_switch(*, active_detail: str = "ACTIVE") -> dict[str, Any]:
    active = kill_switch.is_active()
    return {"name": "kill_switch", "ok": not active, "detail": active_detail if active else "inactive"}


def check_wal_backlog(max_files: int = 100) -> dict[str, Any]:
    wal_dir = os.getenv("HFT_WAL_DIR", ".wal")
    if not os.path.isdir(wal_dir):
        return {"name": "wal_backlog", "ok": True, "detail": f"no wal dir: {wal_dir}"}
    count = len([f for f in os.listdir(wal_dir) if f.endswith(".wal")])
    ok = count <= max_files
    return {"name": "wal_backlog", "ok": ok, "detail": f"files={count} (max={max_files})"}


def check_disk_space(min_gb: float = 1.0) -> dict[str, Any]:
    usage = shutil.disk_usage(".")
    free_gb = usage.free / (1024**3)
    ok = free_gb >= min_gb
    return {"name": "disk_space", "ok": ok, "detail": f"free={free_gb:.1f}GB (min={min_gb:.1f}GB)"}
