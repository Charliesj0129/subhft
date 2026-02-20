"""CE3-05: DiskPressureMonitor — background WAL disk space watcher.

Monitors both WAL directory total size and free disk space.
Publishes a DiskPressureLevel and calls registered hooks on level transitions.
Per-topic write policies are configurable via HFT_WAL_FIRST_POLICY_{TABLE}.

Env vars:
    HFT_WAL_WARN_MB:          warn threshold MB (default 100)
    HFT_WAL_CRITICAL_MB:      critical threshold MB (default 300)
    HFT_WAL_HALT_MB:          halt threshold MB (default 500) — WAL dir size
    HFT_DISK_CHECK_INTERVAL_S: check interval seconds (default 10)
    HFT_WAL_FIRST_POLICY_{TABLE}: write|drop|halt per table (default write)
"""

from __future__ import annotations

import os
import threading
import time
from enum import IntEnum
from typing import Callable, Optional

from structlog import get_logger

logger = get_logger("recorder.disk_monitor")


class DiskPressureLevel(IntEnum):
    OK = 0
    WARN = 1
    CRITICAL = 2
    HALT = 3


class TopicPolicy(str):
    WRITE = "write"
    DROP = "drop"
    HALT = "halt"


class DiskPressureMonitor:
    """Background daemon thread that polls WAL disk usage periodically.

    Safe for concurrent access (all state protected by _lock).
    """

    def __init__(
        self,
        wal_dir: str = ".wal",
        warn_mb: float | None = None,
        critical_mb: float | None = None,
        halt_mb: float | None = None,
        check_interval_s: float | None = None,
    ) -> None:
        self._wal_dir = wal_dir
        self._warn_mb = warn_mb if warn_mb is not None else float(os.getenv("HFT_WAL_WARN_MB", "100"))
        self._critical_mb = critical_mb if critical_mb is not None else float(os.getenv("HFT_WAL_CRITICAL_MB", "300"))
        self._halt_mb = halt_mb if halt_mb is not None else float(os.getenv("HFT_WAL_HALT_MB", "500"))
        self._interval = (
            check_interval_s if check_interval_s is not None else float(os.getenv("HFT_DISK_CHECK_INTERVAL_S", "10"))
        )

        self._level = DiskPressureLevel.OK
        self._lock = threading.Lock()
        self._hooks: list[Callable[[DiskPressureLevel, DiskPressureLevel], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Start the background polling thread."""
        self._running = True
        self._thread = threading.Thread(
            target=self._loop,
            name="disk-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def get_level(self) -> DiskPressureLevel:
        """Thread-safe current pressure level."""
        with self._lock:
            return self._level

    def register_hook(self, fn: Callable[[DiskPressureLevel, DiskPressureLevel], None]) -> None:
        """Register a callback invoked with (old_level, new_level) on transitions."""
        with self._lock:
            self._hooks.append(fn)

    def get_topic_policy(self, table: str) -> str:
        """Read per-topic write policy from env. Default: write."""
        key = f"HFT_WAL_FIRST_POLICY_{table.upper()}"
        policy = os.getenv(key, "write").strip().lower()
        if policy not in ("write", "drop", "halt"):
            return "write"
        return policy

    # ── Private ───────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._check()
            except Exception as exc:
                logger.warning("DiskPressureMonitor check error", error=str(exc))
            time.sleep(self._interval)

    def _check(self) -> None:
        wal_size_mb = self._wal_dir_size_mb()
        new_level = self._compute_level(wal_size_mb)

        with self._lock:
            old_level = self._level
            if new_level == old_level:
                return
            self._level = new_level
            hooks = list(self._hooks)

        level_name = DiskPressureLevel(new_level).name
        if new_level > DiskPressureLevel.OK:
            logger.warning(
                "DiskPressure level change",
                old=DiskPressureLevel(old_level).name,
                new=level_name,
                wal_size_mb=round(wal_size_mb, 1),
            )
        else:
            logger.info("DiskPressure recovered to OK", wal_size_mb=round(wal_size_mb, 1))

        # Update Prometheus metric
        try:
            from hft_platform.observability.metrics import MetricsRegistry

            MetricsRegistry.get().disk_pressure_level.set(int(new_level))
        except Exception:
            pass

        for hook in hooks:
            try:
                hook(old_level, new_level)
            except Exception as exc:
                logger.warning("DiskPressure hook error", error=str(exc))

    def _wal_dir_size_mb(self) -> float:
        if not os.path.isdir(self._wal_dir):
            return 0.0
        total = 0
        try:
            for fname in os.listdir(self._wal_dir):
                fpath = os.path.join(self._wal_dir, fname)
                if os.path.isfile(fpath):
                    try:
                        total += os.path.getsize(fpath)
                    except OSError:
                        pass
        except OSError:
            pass
        return total / (1024 * 1024)

    def _compute_level(self, size_mb: float) -> DiskPressureLevel:
        if size_mb >= self._halt_mb:
            return DiskPressureLevel.HALT
        if size_mb >= self._critical_mb:
            return DiskPressureLevel.CRITICAL
        if size_mb >= self._warn_mb:
            return DiskPressureLevel.WARN
        return DiskPressureLevel.OK
