"""Persist ``DailyLossLimitValidator`` state across a restart.

Before this, the day's realized loss, the latched stop and the peak watermark
lived in memory only, so every restart re-granted the full daily budget and
released the stop (2026-10-22 16:44Z: a deploy restart cleared a daily-loss
HALT). The supervisor writes the validator's ``export_state()`` here whenever
it changes and the engine restores it at boot when it belongs to the same
trading day.

The default path is under ``.state/``, the engine's bind mount on the
production host. ``latch_published`` records that the daily-loss manual latch
(``risk/manual_latches.py``) was already written for the current stop, so a
restart after the operator released the latch does not write it again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from hft_platform.ops.runtime_state_store import atomic_write_json

DEFAULT_PATH = ".state/daily_loss_state.json"
PATH_ENV = "HFT_DAILY_LOSS_STATE_PATH"


def state_path() -> Path:
    return Path(os.getenv(PATH_ENV, DEFAULT_PATH))


def load(path: Path | None = None) -> dict[str, Any] | None:
    """The saved document, or None when there is none. Raises on a malformed file."""
    target = path or state_path()
    if not target.exists():
        return None
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("validator"), dict):
        raise ValueError(f"{target} is not a daily loss state document")
    return data


def save(document: dict[str, Any], path: Path | None = None) -> None:
    """Write durably (fsync, rename, fsync dir). Blocking; run it off the event loop."""
    atomic_write_json(path or state_path(), document)
