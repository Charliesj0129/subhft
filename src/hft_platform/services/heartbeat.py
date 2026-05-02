"""File-based heartbeat for process health monitoring.

The engine writes to a heartbeat file every 30s. A cron watchdog
checks the file mtime — if stale (>90s), it restarts the service.

P2-d/B108 (2026-04-27): default path moved off /tmp (world-writable on
most distros — bandit B108 hardcoded-tmp-path warning) to /var/run/hft/.
Operators can override via HFT_HEARTBEAT_PATH env var for environments
where /var/run/hft is unavailable (CI, dev shells, etc.). The Docker
runtime image runs as the unprivileged `hftuser` so /var/run/hft must
be either writable by uid 1000 or the env var must be set explicitly
in such environments.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger(__name__)

DEFAULT_HEARTBEAT_PATH: str = os.environ.get("HFT_HEARTBEAT_PATH", "/var/run/hft/heartbeat")


def write_heartbeat(path: str = DEFAULT_HEARTBEAT_PATH) -> None:
    """Write current PID to heartbeat file and touch mtime. Never raises."""
    try:
        with open(path, "w") as f:
            f.write(str(os.getpid()))
        os.utime(path, None)
    except OSError:
        logger.warning("heartbeat_write_failed", path=path, exc_info=True)
