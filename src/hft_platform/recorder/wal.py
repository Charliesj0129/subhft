import asyncio
import fcntl
import os
import tempfile
import time
from glob import glob

from structlog import get_logger

try:
    import orjson

    def _dumps(obj: object) -> str:
        return orjson.dumps(obj).decode()

    _loads = orjson.loads
except ImportError:
    import json

    _dumps = json.dumps  # type: ignore[assignment]
    _loads = json.loads

from hft_platform.core import timebase

logger = get_logger("recorder.wal")


class WALWriter:
    def __init__(self, wal_dir: str):
        self.wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)
        self._lock_fd = None
        # Disk space circuit breaker (EC-4)
        self._disk_min_mb = float(os.getenv("HFT_WAL_DISK_MIN_MB", "500"))
        self._disk_full = False
        self._disk_check_interval_s = 60.0
        self._last_disk_check_ts = 0.0
        self._disk_full_count = 0

    def _check_disk_space(self) -> bool:
        """Check available disk space; return True if sufficient."""
        now = time.monotonic()
        if now - self._last_disk_check_ts < self._disk_check_interval_s:
            return not self._disk_full
        self._last_disk_check_ts = now
        try:
            stat = os.statvfs(self.wal_dir)
            avail_mb = (stat.f_bavail * stat.f_frsize) / (1024 * 1024)
            if avail_mb < self._disk_min_mb:
                if not self._disk_full:
                    logger.critical(
                        "WAL disk space below threshold, activating circuit breaker",
                        avail_mb=round(avail_mb, 1),
                        threshold_mb=self._disk_min_mb,
                    )
                self._disk_full = True
                return False
            if self._disk_full:
                logger.info(
                    "WAL disk space recovered, deactivating circuit breaker",
                    avail_mb=round(avail_mb, 1),
                )
            self._disk_full = False
            return True
        except OSError:
            return True  # Fail open if statvfs unavailable

    async def write(self, table: str, data: list) -> bool:
        """Async append to local disk via thread pool with atomic write.

        Returns True if written, False if skipped (disk full).
        """
        if not self._check_disk_space():
            self._disk_full_count += len(data)
            logger.warning(
                "WAL write skipped - disk full circuit breaker active",
                table=table,
                rows_skipped=len(data),
                total_skipped=self._disk_full_count,
            )
            return False

        ts = int(timebase.now_ns())
        filename = f"{self.wal_dir}/{table}_{ts}.jsonl"

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_sync_atomic, filename, data)
            logger.info("Wrote to WAL", table=table, count=len(data), file=filename)
            return True
        except Exception as e:
            logger.critical("WAL Write Failed!", error=str(e))
            return False

    def _write_sync_atomic(self, filename: str, data: list):
        """
        Atomic write: write to temp file, then rename.
        This prevents partial reads by the loader.
        """
        # Write to temp file in same directory (for atomic rename)
        dir_path = os.path.dirname(filename)
        fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=dir_path)
        try:
            with os.fdopen(fd, "w") as f:
                # Acquire exclusive lock during write
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    for row in data:
                        f.write(_dumps(row) + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            # Atomic rename (on POSIX systems)
            os.rename(tmp_path, filename)
            # fsync directory to ensure rename is durable on disk
            dir_fd = os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            # Clean up temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def _write_sync(self, filename: str, data: list):
        """Legacy blocking write (kept for compatibility)."""
        with open(filename, "w") as f:
            for row in data:
                f.write(_dumps(row) + "\n")


class WALReplayer:
    def __init__(self, wal_dir: str, sender_func):
        self.wal_dir = wal_dir
        self.sender_func = sender_func  # Async function(table, data) -> bool

    async def replay(self):
        """Scans directory and attempts to replay files."""
        files = sorted(glob(f"{self.wal_dir}/*.jsonl"))
        if not files:
            return

        logger.info("Found WAL files", count=len(files))

        for fpath in files:
            # Parse table name from filename provided it matches table_timestamp.jsonl
            fname = os.path.basename(fpath)
            table = fname.rsplit("_", 1)[0]

            try:
                data = []
                with open(fpath, "r") as f:
                    for line in f:
                        if line.strip():
                            data.append(_loads(line))

                if data:
                    success = await self.sender_func(table, data)
                    if success:
                        os.remove(fpath)
                        logger.info("Replayed and deleted WAL", file=fname)
                    else:
                        logger.warning("Replay failed, keeping WAL", file=fname)
                        break  # Stop on first failure to preserve order?
                else:
                    # Empty file
                    os.remove(fpath)
            except Exception as e:
                logger.error("Corrupt WAL file", file=fname, error=str(e))
                # Move to corrupt dir?
