import os
from typing import Any

from hft_platform.core import timebase
from hft_platform.observability.metrics import MetricsRegistry


def _bool_env(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class LatencyRecorder:
    _instance: "LatencyRecorder | None" = None

    def __init__(self) -> None:
        self.enabled = _bool_env(os.getenv("HFT_LATENCY_TRACE", "0"))
        self.metrics_enabled = _bool_env(os.getenv("HFT_LATENCY_METRICS", "1"))
        try:
            sample_every = int(os.getenv("HFT_LATENCY_SAMPLE_EVERY", "100"))
        except ValueError:
            sample_every = 100
        self._sample_every = max(1, sample_every)
        self._counter = 0
        self._queue: Any | None = None
        self.metrics = MetricsRegistry.get()

    @classmethod
    def get(cls) -> "LatencyRecorder":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_for_tests(cls) -> None:
        cls._instance = None

    def configure(self, queue: Any | None) -> None:
        self._queue = queue

    def _should_sample(self) -> bool:
        if not self.enabled:
            return False
        if self._sample_every <= 1:
            return True
        self._counter = (self._counter + 1) % self._sample_every
        return self._counter == 0

    def record(
        self,
        stage: str,
        latency_ns: int,
        *,
        trace_id: str = "",
        symbol: str = "",
        strategy_id: str = "",
        ts_ns: int | None = None,
    ) -> None:
        if latency_ns < 0:
            return
        if self.metrics_enabled and self.metrics:
            try:
                self.metrics.pipeline_latency_ns.labels(stage=stage).observe(latency_ns)
            except Exception:
                pass

        if not self._should_sample():
            return

        if not self._queue:
            return

        if ts_ns is None:
            ts_ns = timebase.now_ns()

        payload = {
            "ingest_ts": int(ts_ns),
            "stage": stage,
            "latency_us": int(latency_ns / 1000),
            "trace_id": trace_id,
            "symbol": symbol or "",
            "strategy_id": strategy_id or "",
        }

        try:
            self._queue.put_nowait({"topic": "latency_spans", "data": payload})
        except Exception:
            pass
