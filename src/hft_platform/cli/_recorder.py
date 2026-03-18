"""Recorder status CLI command."""

from __future__ import annotations

import argparse
import os


def cmd_recorder_status(args: argparse.Namespace) -> None:
    import time
    import urllib.request

    wal_dir: str = getattr(args, "wal_dir", None) or os.getenv("HFT_WAL_DIR", "data/wal")  # type: ignore[assignment]
    ck_host = getattr(args, "ck_host", None) or os.getenv("HFT_CLICKHOUSE_HOST", "localhost")
    ck_port = int(os.getenv("HFT_CLICKHOUSE_PORT", "8123"))
    recorder_mode = os.getenv("HFT_RECORDER_MODE", "direct")
    batcher_max = os.getenv("HFT_BATCHER_MAX_BUFFER", "2000")
    wal_batch_max = os.getenv("HFT_WAL_BATCH_MAX_ROWS", "500")
    wal_disk_min_mb = os.getenv("HFT_WAL_DISK_MIN_MB", "500")
    wal_pressure_policy = os.getenv("HFT_WAL_DISK_PRESSURE_POLICY", "drop")

    # WAL backlog scan
    wal_files: list[tuple[str, float, int]] = []  # (name, mtime, size)
    try:
        with os.scandir(wal_dir) as it:
            for entry in it:
                if entry.name.endswith(".wal") and entry.is_file():
                    st = entry.stat()
                    wal_files.append((entry.name, st.st_mtime, st.st_size))
    except FileNotFoundError:
        pass

    now = time.time()
    wal_count = len(wal_files)
    wal_total_bytes = sum(s for _, _, s in wal_files)
    oldest_age_s: float | None = None
    if wal_files:
        oldest_mtime = min(m for _, m, _ in wal_files)
        oldest_age_s = now - oldest_mtime

    def _fmt_bytes(b: int) -> str:
        if b >= 1024 * 1024:
            return f"{b / 1024 / 1024:.1f} MB"
        if b >= 1024:
            return f"{b / 1024:.1f} KB"
        return f"{b} B"

    # Disk free
    disk_free_str = "unknown"
    try:
        st_vfs = os.statvfs(wal_dir if os.path.exists(wal_dir) else ".")
        free_mb = st_vfs.f_frsize * st_vfs.f_bavail / 1024 / 1024
        if free_mb >= 1024:
            disk_free_str = f"{free_mb / 1024:.1f} GB"
        else:
            disk_free_str = f"{free_mb:.0f} MB"
    except Exception:
        pass

    # WAL guard status
    try:
        guard_threshold_mb = int(wal_disk_min_mb)
        disk_free_mb_val = st_vfs.f_frsize * st_vfs.f_bavail / 1024 / 1024  # noqa: F821
        guard_active = disk_free_mb_val < guard_threshold_mb
        guard_str = "ACTIVE" if guard_active else "OFF"
    except Exception:
        guard_str = "unknown"

    if oldest_age_s is not None:
        backlog_str = f"{wal_count} files (oldest: {oldest_age_s:.0f}s ago, total: {_fmt_bytes(wal_total_bytes)})"
    else:
        backlog_str = f"{wal_count} files"

    # ClickHouse reachability
    ck_status = "unreachable"
    try:
        resp = urllib.request.urlopen(f"http://{ck_host}:{ck_port}/ping", timeout=2.0)
        if resp.status == 200:
            ck_status = "ok"
    except Exception:
        pass

    ck_pool = os.getenv("HFT_CLICKHOUSE_POOL_SIZE", "8")

    print("WAL Status:")
    print(f"  Mode:        {recorder_mode} (HFT_RECORDER_MODE={recorder_mode})")
    print(f"  Backlog:     {backlog_str}")
    print(f"  Disk guard:  {wal_disk_min_mb} MB min (policy={wal_pressure_policy}, free={disk_free_str}) — {guard_str}")
    print()
    print("ClickHouse:")
    print(f"  Status:      {ck_status} ({ck_host}:{ck_port})")
    print()
    print("Config:")
    print(f"  Batcher:     {batcher_max} rows/table | WAL batch: {wal_batch_max} rows | CK pool: {ck_pool} threads")
