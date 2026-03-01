"""CE2-09: Gateway active/standby leader lease (file-lock prototype).

Prototype scope:
- Single-host leader election via fcntl LOCK_EX on a lease file.
- Non-blocking acquire; standby remains passive.
- Heartbeat JSON is written for observability / debugging.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from typing import Any

from structlog import get_logger

logger = get_logger("gateway.leader_lease")


class FileLeaderLease:
    """fcntl-based leader lease for active/standby GatewayService.

    The process holding LOCK_EX on ``lease_path`` is the leader and is allowed
    to dispatch broker commands. Standby keeps trying to acquire without
    blocking the event loop.
    """

    def __init__(
        self,
        lease_path: str = ".state/gateway_leader.lock",
        enabled: bool | None = None,
        owner_id: str | None = None,
    ) -> None:
        self.enabled = (
            enabled
            if enabled is not None
            else (os.getenv("HFT_GATEWAY_HA_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"})
        )
        self.lease_path = lease_path
        self.owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}"
        self._fd: int | None = None
        self._is_leader = False
        if self.enabled:
            lease_dir = os.path.dirname(self.lease_path) or "."
            os.makedirs(lease_dir, exist_ok=True)

    def is_leader(self) -> bool:
        return (not self.enabled) or self._is_leader

    def tick(self) -> bool:
        """Acquire or renew the lease. Returns leader state after the tick."""
        if not self.enabled:
            self._is_leader = True
            return True

        if self._fd is None:
            try:
                self._fd = os.open(self.lease_path, os.O_CREAT | os.O_RDWR, 0o600)
            except OSError as exc:
                logger.warning("Leader lease open failed", path=self.lease_path, error=str(exc))
                self._is_leader = False
                return False

        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._is_leader = True
            self._write_heartbeat()
            return True
        except BlockingIOError:
            self._is_leader = False
            return False
        except OSError as exc:
            logger.warning("Leader lease flock failed", path=self.lease_path, error=str(exc))
            self._is_leader = False
            return False

    def release(self) -> None:
        if not self.enabled:
            self._is_leader = False
            return
        fd = self._fd
        self._fd = None
        self._is_leader = False
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "is_leader": bool(self._is_leader),
            "lease_path": self.lease_path,
            "owner_id": self.owner_id,
        }

    def _write_heartbeat(self) -> None:
        fd = self._fd
        if fd is None:
            return
        payload = {
            "owner_id": self.owner_id,
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "ts_ns": time.time_ns(),
        }
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            os.fsync(fd)
        except OSError:
            # Heartbeat failure should not crash gateway processing.
            pass
