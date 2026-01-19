import asyncio
import json
import os
import time
from glob import glob

from structlog import get_logger

logger = get_logger("recorder.wal")


class WALWriter:
    def __init__(self, wal_dir: str):
        self.wal_dir = wal_dir
        os.makedirs(wal_dir, exist_ok=True)

    async def write(self, table: str, data: list):
        """Async append to local disk via thread pool."""
        ts = int(time.time_ns())
        filename = f"{self.wal_dir}/{table}_{ts}.jsonl"

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._write_sync, filename, data)
            logger.info("Wrote to WAL", table=table, count=len(data), file=filename)
        except Exception as e:
            logger.critical("WAL Write Failed!", error=str(e))

    def _write_sync(self, filename: str, data: list):
        """Blocking write function to be run in executor."""
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
