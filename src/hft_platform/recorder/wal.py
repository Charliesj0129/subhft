import asyncio
import fcntl
import json
import os
import tempfile
from glob import glob

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("recorder.wal")


class WALWriter:
    def __init__(self, wal_dir: str):
        self.wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)
        self._lock_fd = None

    async def write(self, table: str, data: list):
        """Async append to local disk via thread pool with atomic write."""
        ts = int(timebase.now_ns())
        filename = f"{self.wal_dir}/{table}_{ts}.jsonl"

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_sync_atomic, filename, data)
            logger.info("Wrote to WAL", table=table, count=len(data), file=filename)
        except Exception as e:
            logger.critical("WAL Write Failed!", error=str(e))

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
                        f.write(json.dumps(row) + "\n")
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
                f.write(json.dumps(row) + "\n")


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
                            data.append(json.loads(line))

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
