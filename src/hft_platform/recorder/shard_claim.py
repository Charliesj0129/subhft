"""CE3-03: FileClaimRegistry — fcntl LOCK_EX sidecar claim protocol.

Architecture (D3):
- Creates {filename}.claim sidecar files in .wal/claims/.
- Acquires fcntl LOCK_EX | LOCK_NB on the sidecar file.
- On restart, recover_stale_claims() tries claim+release on all .claim files
  to detect and clear stale locks left by crashed processes.
- Works for current ThreadPoolExecutor AND future multi-process deployment.

Env vars:
    HFT_WAL_SHARD_CLAIM_ENABLED: enable sidecar locking (default 1)
"""
from __future__ import annotations

import fcntl
import os

from structlog import get_logger

logger = get_logger("recorder.shard_claim")


class FileClaimRegistry:
    """fcntl-based sidecar claim registry for WAL files.

    Thread/process safe: LOCK_EX | LOCK_NB on a .claim sidecar file.
    """

    def __init__(
        self,
        claim_dir: str = ".wal/claims",
        enabled: bool | None = None,
    ) -> None:
        _enabled = enabled if enabled is not None else (
            os.getenv("HFT_WAL_SHARD_CLAIM_ENABLED", "1").lower() not in {"0", "false", "no", "off"}
        )
        self._enabled = _enabled
        self._claim_dir = claim_dir
        if self._enabled:
            os.makedirs(claim_dir, exist_ok=True)
        # Map filename → open file descriptor (held for lock lifetime)
        self._held_fds: dict[str, int] = {}

    def try_claim(self, filename: str) -> bool:
        """Attempt to claim a WAL file for exclusive processing.

        Returns True if claim acquired, False if already claimed by another worker.
        """
        if not self._enabled:
            return True

        claim_path = self._claim_path(filename)
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_WRONLY, 0o600)
        except OSError as exc:
            logger.warning("Cannot open claim file", file=claim_path, error=str(exc))
            return False

        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._held_fds[filename] = fd
            return True
        except BlockingIOError:
            # Another process holds the lock
            os.close(fd)
            return False
        except OSError as exc:
            os.close(fd)
            logger.warning("flock failed on claim file", file=claim_path, error=str(exc))
            return False

    def release_claim(self, filename: str) -> None:
        """Release the exclusive lock and remove the sidecar file."""
        if not self._enabled:
            return

        fd = self._held_fds.pop(filename, None)
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass

        claim_path = self._claim_path(filename)
        try:
            os.unlink(claim_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to remove claim file", file=claim_path, error=str(exc))

    def recover_stale_claims(self) -> None:
        """On startup: detect and clear stale .claim files from crashed processes.

        A stale claim is a .claim file whose LOCK_EX can be acquired (no live holder).
        """
        if not self._enabled:
            return
        if not os.path.isdir(self._claim_dir):
            return

        recovered = 0
        try:
            for fname in os.listdir(self._claim_dir):
                if not fname.endswith(".claim"):
                    continue
                claim_path = os.path.join(self._claim_dir, fname)
                try:
                    fd = os.open(claim_path, os.O_WRONLY)
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        # No live holder — stale claim; remove it
                        fcntl.flock(fd, fcntl.LOCK_UN)
                        os.close(fd)
                        os.unlink(claim_path)
                        recovered += 1
                    except BlockingIOError:
                        # Live process holds it
                        os.close(fd)
                    except OSError:
                        os.close(fd)
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("recover_stale_claims failed", error=str(exc))

        if recovered:
            logger.info("Recovered stale WAL claims", count=recovered)

    def _claim_path(self, filename: str) -> str:
        base = os.path.basename(filename)
        return os.path.join(self._claim_dir, base + ".claim")
