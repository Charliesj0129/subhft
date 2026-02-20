"""CE3-01: Recorder mode selection.

HFT_RECORDER_MODE=direct   (default) — current batcher/CH path
HFT_RECORDER_MODE=wal_first           — always write to WAL; CH ingestion via WALLoaderService

HFT_DISABLE_CLICKHOUSE=1 maps to wal_first for backward compatibility.
"""

import os
from enum import Enum


class RecorderMode(str, Enum):
    DIRECT = "direct"
    WAL_FIRST = "wal_first"


def get_recorder_mode() -> RecorderMode:
    """Read mode from environment, with backward-compat alias."""
    if os.getenv("HFT_DISABLE_CLICKHOUSE"):
        return RecorderMode.WAL_FIRST

    raw = os.getenv("HFT_RECORDER_MODE", "direct").strip().lower()
    try:
        return RecorderMode(raw)
    except ValueError:
        import structlog

        structlog.get_logger("recorder.mode").warning(
            "Unknown HFT_RECORDER_MODE value, falling back to direct",
            value=raw,
        )
        return RecorderMode.DIRECT
