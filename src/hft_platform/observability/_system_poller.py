"""Background daemon thread for system metrics (CPU/memory).

Moves psutil calls off the async event loop to comply with Async Law.
Pattern follows DiskPressureMonitor (recorder/disk_monitor.py).
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hft_platform.observability.metrics import MetricsRegistry

__all__ = ["SystemPoller"]


class SystemPoller:
    """Polls psutil CPU/memory in a daemon thread, updates Prometheus gauges."""

    __slots__ = ("_metrics", "_interval_s", "_running", "_thread")

    def __init__(self, metrics: MetricsRegistry, interval_s: float = 5.0) -> None:
        self._metrics: Any = metrics
        self._interval_s = max(1.0, interval_s)
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="system-poller")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        try:
            import psutil
        except ImportError:
            return
        cpu_gauge = getattr(self._metrics, "system_cpu_usage", None)
        mem_gauge = getattr(self._metrics, "system_memory_usage", None)
        while self._running:
            try:
                if cpu_gauge is not None:
                    cpu_gauge.set(psutil.cpu_percent())
                if mem_gauge is not None:
                    mem_gauge.set(psutil.virtual_memory().percent)
            except Exception as _exc:  # noqa: BLE001
                pass
            time.sleep(self._interval_s)
