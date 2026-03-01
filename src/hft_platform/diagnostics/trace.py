from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hft_platform.utils.serialization import serialize


@dataclass(slots=True)
class DecisionTraceSampler:
    enabled: bool
    sample_every: int
    out_dir: str
    max_bytes_per_file: int
    _counter: int = 0
    _lock: threading.Lock | None = None

    def __post_init__(self) -> None:
        if self._lock is None:
            self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "DecisionTraceSampler":
        enabled = str(os.getenv("HFT_DIAG_TRACE_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"}
        try:
            sample_every = max(1, int(os.getenv("HFT_DIAG_TRACE_SAMPLE_EVERY", "100")))
        except ValueError:
            sample_every = 100
        out_dir = os.getenv("HFT_DIAG_TRACE_DIR", "outputs/decision_traces")
        try:
            max_bytes = max(1_000_000, int(os.getenv("HFT_DIAG_TRACE_MAX_BYTES", "25000000")))
        except ValueError:
            max_bytes = 25_000_000
        return cls(enabled=enabled, sample_every=sample_every, out_dir=out_dir, max_bytes_per_file=max_bytes)

    def emit(self, *, stage: str, trace_id: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        self._counter = (self._counter + 1) % self.sample_every
        if self._counter != 0:
            return
        record = {
            "ts_ns": time.time_ns(),
            "stage": str(stage),
            "trace_id": str(trace_id or ""),
            "payload": serialize(payload),
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        lock = self._lock
        if lock is None:
            return
        try:
            with lock:
                path = self._current_path()
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists() and path.stat().st_size >= self.max_bytes_per_file:
                    path = self._rollover_path(path)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            # diagnostics must never break hot path
            return

    def _current_path(self) -> Path:
        day = time.strftime("%Y%m%d", time.gmtime())
        return Path(self.out_dir) / f"{day}.jsonl"

    def _rollover_path(self, base: Path) -> Path:
        stem = base.stem
        parent = base.parent
        for i in range(1, 1000):
            cand = parent / f"{stem}.{i:03d}{base.suffix}"
            if not cand.exists() or cand.stat().st_size < self.max_bytes_per_file:
                return cand
        return parent / f"{stem}.overflow{base.suffix}"


_SAMPLER: DecisionTraceSampler | None = None


def get_trace_sampler() -> DecisionTraceSampler:
    global _SAMPLER
    if _SAMPLER is None:
        _SAMPLER = DecisionTraceSampler.from_env()
    return _SAMPLER
