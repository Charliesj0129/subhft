from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from hft_platform.core import timebase


def _metric_sample_value(metric: Any, sample_name: str, labels: dict[str, str] | None = None) -> float | None:
    labelset = labels or {}
    try:
        for metric_family in metric.collect():
            for sample in metric_family.samples:
                if sample.name == sample_name and sample.labels == labelset:
                    return float(sample.value)
    except Exception:
        return None
    return None


def _read_rss_bytes() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as status_file:
            for line in status_file:
                if not line.startswith("VmRSS:"):
                    continue
                parts = line.split()
                if len(parts) >= 2:
                    return int(parts[1]) * 1024
    except OSError:
        return 0
    return 0


@dataclass(slots=True)
class PlatformDegradeInputs:
    md_service: Any
    recorder: Any
    raw_queue: Any
    raw_exec_queue: Any
    recorder_queue: Any
    risk_queue: Any
    order_queue: Any
    intent_channel: Any | None = None
    api_queue: Any | None = None
    redis_client: Any | None = None
    redis_healthcheck: Callable[[], bool] | None = None
    metrics: Any | None = None
    feed_gap_threshold_s: float = 120.0
    reconnect_pending_threshold_s: float = 60.0
    reconnect_flap_budget: int = 5
    queue_depth_threshold: int = 5000
    rss_threshold_mb: int = 2048
    wal_backlog_files_threshold: int = 200
    rss_reader: Callable[[], int] = _read_rss_bytes

    def configure_thresholds(
        self,
        *,
        feed_gap_threshold_s: float,
        reconnect_pending_threshold_s: float,
        reconnect_flap_budget: int,
        queue_depth_threshold: int,
        rss_threshold_mb: int,
        wal_backlog_files_threshold: int,
    ) -> None:
        self.feed_gap_threshold_s = float(feed_gap_threshold_s)
        self.reconnect_pending_threshold_s = float(reconnect_pending_threshold_s)
        self.reconnect_flap_budget = int(reconnect_flap_budget)
        self.queue_depth_threshold = int(queue_depth_threshold)
        self.rss_threshold_mb = int(rss_threshold_mb)
        self.wal_backlog_files_threshold = int(wal_backlog_files_threshold)

    def bind_runtime_probes(
        self,
        *,
        redis_client: Any | None = None,
        redis_healthcheck: Callable[[], bool] | None = None,
    ) -> None:
        self.redis_client = redis_client
        self.redis_healthcheck = redis_healthcheck

    def reduce_only_reasons(self) -> list[str]:
        reasons: list[str] = []

        feed_gap_s = self._feed_gap_s()
        if feed_gap_s >= self.feed_gap_threshold_s:
            reasons.append("feed_reconnect_unhealthy")

        pending_since = getattr(self.md_service, "_pending_reconnect_since", None)
        if pending_since is not None and timebase.now_s() - float(pending_since) >= self.reconnect_pending_threshold_s:
            # Suppress during expected session gaps (e.g. 13:35-14:55 day→night
            # transition).  The reconnect is "pending" precisely because we are
            # outside the reconnect window — that is normal, not anomalous.
            within_fn = getattr(self.md_service, "within_reconnect_window", None)
            if within_fn is None or within_fn():
                reasons.append("feed_reconnect_pending")

        if self.reconnect_flap_budget > 0 and self._quote_flap_budget_exceeded():
            reasons.append("feed_reconnect_flapping")

        queue_depths = [
            self.raw_queue.qsize(),
            self.raw_exec_queue.qsize(),
            self.recorder_queue.qsize(),
            self.risk_queue.qsize(),
            self.order_queue.qsize(),
        ]
        if self.intent_channel is not None:
            queue_depths.append(self.intent_channel.qsize())
        if self.api_queue is not None:
            queue_depths.append(self.api_queue.qsize())
        queue_depth = max(queue_depths)
        if queue_depth >= self.queue_depth_threshold:
            reasons.append("queue_depth_exceeded")

        if self.rss_threshold_mb > 0:
            rss_bytes = self.rss_reader()
            if rss_bytes >= self.rss_threshold_mb * 1024 * 1024:
                reasons.append("rss_unhealthy")

        redis_healthy = self._redis_is_healthy()
        if redis_healthy is False:
            reasons.append("redis_unhealthy")

        if self.wal_backlog_files_threshold > 0:
            wal_backlog_files = self._wal_backlog_files()
            if wal_backlog_files is not None and wal_backlog_files >= self.wal_backlog_files_threshold:
                reasons.append("wal_backlog_unhealthy")

        recorder_state = self._recorder_state()
        if recorder_state == "DATA_LOSS":
            reasons.append("recorder_data_loss")
        elif recorder_state in {"DEGRADED", "CRITICAL"}:
            reasons.append("clickhouse_unhealthy")

        # Preserve first-cause ordering while preventing duplicates.
        return list(dict.fromkeys(reasons))

    def _feed_gap_s(self) -> float:
        fn = getattr(self.md_service, "get_max_feed_gap_s", None)
        if fn is None:
            return 0.0
        gap = fn()
        within_fn = getattr(self.md_service, "within_reconnect_window", None)
        if within_fn is not None and not within_fn():
            return 0.0
        return float(gap)

    def _quote_flap_budget_exceeded(self) -> bool:
        flap_events = getattr(self.md_service, "_quote_flap_events", None)
        flap_threshold = int(getattr(self.md_service, "_quote_flap_threshold", 0) or 0)
        flap_window_s = float(getattr(self.md_service, "_quote_flap_window_s", 0.0) or 0.0)
        if flap_events is None or flap_threshold <= 0 or flap_window_s <= 0:
            return False
        try:
            return len(flap_events) >= flap_threshold
        except Exception:
            return False

    def _redis_is_healthy(self) -> bool | None:
        if callable(self.redis_healthcheck):
            try:
                return bool(self.redis_healthcheck())
            except Exception:
                return False
        if self.redis_client is None:
            return None
        try:
            return bool(self.redis_client.ping())
        except Exception:
            return False

    def _wal_backlog_files(self) -> float | None:
        if hasattr(self.recorder, "wal_backlog_files"):
            try:
                return float(self.recorder.wal_backlog_files)
            except Exception:
                return None

        health_fn = getattr(self.recorder, "get_health", None)
        if callable(health_fn):
            try:
                health = health_fn()
            except Exception:
                health = None
            if isinstance(health, dict):
                if "wal_backlog_files" in health:
                    try:
                        return float(health["wal_backlog_files"])
                    except Exception:
                        return None
                event_counts = health.get("event_counts")
                if isinstance(event_counts, dict) and "wal_fallback" in event_counts:
                    try:
                        return float(event_counts["wal_fallback"])
                    except Exception:
                        return None

        if self.metrics is None:
            return None
        return _metric_sample_value(self.metrics.wal_backlog_files, "wal_backlog_files")

    def _recorder_state(self) -> str:
        health_fn = getattr(self.recorder, "get_health", None)
        if not callable(health_fn):
            return ""
        try:
            health = health_fn()
        except Exception:
            return ""
        if not isinstance(health, dict):
            return ""
        return str(health.get("state", "")).upper()
