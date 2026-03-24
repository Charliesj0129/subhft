"""File-based heartbeat for process health monitoring.

The engine writes to a heartbeat file every 30s. A cron watchdog
checks the file mtime — if stale (>90s), it restarts the service.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_PATH = "/tmp/hft-heartbeat"


def write_heartbeat(path: str = DEFAULT_HEARTBEAT_PATH) -> None:
    """Write current PID to heartbeat file and touch mtime. Never raises."""
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
        os.utime(path, None)
    except OSError:
        logger.warning("heartbeat_write_failed", path=path, exc_info=True)
