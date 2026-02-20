"""CE3-02: WALFirstWriter — strict WAL-only write path (no ClickHouse calls ever).

Architecture:
- Wraps WALBatchWriter.
- Checks DiskPressureMonitor.get_level() before each write.
- Per-topic policy (write|drop|halt) from DiskPressureMonitor.get_topic_policy().
- Returns False on HALT pressure or drop policy → caller records data_loss event.

Env vars:
    HFT_WAL_FIRST_POLICY_{TABLE}: write|drop|halt (default write)
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.recorder.disk_monitor import DiskPressureLevel, DiskPressureMonitor
from hft_platform.recorder.wal import WALBatchWriter

logger = get_logger("recorder.wal_first")


class WALFirstWriter:
    """Writes rows directly to WAL. Never calls ClickHouse."""

    def __init__(
        self,
        wal_batch_writer: WALBatchWriter,
        disk_monitor: DiskPressureMonitor,
    ) -> None:
        self._wal = wal_batch_writer
        self._disk = disk_monitor

    async def write(self, table: str, rows: list[dict[str, Any]]) -> bool:
        """Write rows to WAL.

        Returns True on success, False if data was dropped (HALT/drop policy).
        """
        level = self._disk.get_level()

        if level == DiskPressureLevel.HALT:
            logger.error(
                "WALFirstWriter HALT: disk pressure at HALT level, dropping rows",
                table=table,
                count=len(rows),
            )
            return False

        if level >= DiskPressureLevel.CRITICAL:
            policy = self._disk.get_topic_policy(table)
            if policy == "halt":
                logger.error(
                    "WALFirstWriter policy=halt on CRITICAL pressure, dropping rows",
                    table=table,
                    count=len(rows),
                )
                return False
            if policy == "drop":
                logger.warning(
                    "WALFirstWriter policy=drop on CRITICAL pressure, dropping rows",
                    table=table,
                    count=len(rows),
                )
                return False
            # policy == "write": fall through to normal write

        return await self._wal.add(table, rows)

    async def flush(self) -> None:
        """Force flush the underlying WAL batch buffer."""
        await self._wal.flush()
