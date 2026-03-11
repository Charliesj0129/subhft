"""Centralized Prometheus metric accessors for Shioaji submodules.

Instead of every submodule calling ``MetricsRegistry.get()`` and accessing
metrics by attribute name, this bridge provides typed accessors with
pre-cached ``.labels()`` children to avoid repeated dict lookups on the
hot path.
"""

from __future__ import annotations

from typing import Any

from structlog import get_logger

from hft_platform.observability.metrics import MetricsRegistry

logger = get_logger("feed_adapter.metrics")

__all__ = ["ShioajiMetricsBridge"]


class ShioajiMetricsBridge:
    """Thin facade over ``MetricsRegistry`` for Shioaji-specific metrics.

    Caches ``.labels()`` children so callers can do::

        bridge.api_latency("login", "ok").observe(42.0)

    without paying the label-dict lookup every tick.

    The bridge is intentionally *not* frozen/slotted so it can be created
    once and shared across submodules while lazily caching children.
    """

    __slots__ = ("_registry", "_label_cache")

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        self._registry: MetricsRegistry = registry or MetricsRegistry.get()
        self._label_cache: dict[tuple[str, tuple[tuple[str, str], ...]], Any] = {}

    # ------------------------------------------------------------------
    # Cached label lookup helper
    # ------------------------------------------------------------------

    def _child(self, metric_name: str, **labels: str) -> Any:
        """Return a cached child metric for the given labels."""
        key = (metric_name, tuple(sorted(labels.items())))
        cached = self._label_cache.get(key)
        if cached is not None:
            return cached
        metric = getattr(self._registry, metric_name, None)
        if metric is None:
            return None
        child = metric.labels(**labels)
        self._label_cache[key] = child
        return child

    # ------------------------------------------------------------------
    # API latency / errors
    # ------------------------------------------------------------------

    def api_latency(self, op: str, result: str) -> Any:
        """``shioaji_api_latency_ms.labels(op=op, result=result)``."""
        return self._child("shioaji_api_latency_ms", op=op, result=result)

    def api_errors(self, op: str) -> Any:
        """``shioaji_api_errors_total.labels(op=op)``."""
        return self._child("shioaji_api_errors_total", op=op)

    def api_jitter(self, op: str) -> Any:
        """``shioaji_api_jitter_ms.labels(op=op)``."""
        return self._child("shioaji_api_jitter_ms", op=op)

    def api_jitter_hist(self, op: str) -> Any:
        """``shioaji_api_jitter_ms_hist.labels(op=op)``."""
        return self._child("shioaji_api_jitter_ms_hist", op=op)

    # ------------------------------------------------------------------
    # Quote routing / callback
    # ------------------------------------------------------------------

    def quote_route(self, result: str) -> Any:
        """``shioaji_quote_route_total.labels(result=result)``."""
        return self._child("shioaji_quote_route_total", result=result)

    def quote_callback_ingress_latency(self) -> Any:
        """``shioaji_quote_callback_ingress_latency_ns`` (no labels)."""
        return getattr(self._registry, "shioaji_quote_callback_ingress_latency_ns", None)

    def quote_callback_queue_depth(self) -> Any:
        """``shioaji_quote_callback_queue_depth`` (no labels)."""
        return getattr(self._registry, "shioaji_quote_callback_queue_depth", None)

    def quote_callback_queue_dropped(self) -> Any:
        """``shioaji_quote_callback_queue_dropped_total`` (no labels)."""
        return getattr(self._registry, "shioaji_quote_callback_queue_dropped_total", None)

    # ------------------------------------------------------------------
    # Thread liveness
    # ------------------------------------------------------------------

    def thread_alive(self, thread: str) -> Any:
        """``shioaji_thread_alive.labels(thread=thread)``."""
        return self._child("shioaji_thread_alive", thread=thread)

    # ------------------------------------------------------------------
    # Quote pending / stall
    # ------------------------------------------------------------------

    def quote_pending_age(self) -> Any:
        """``shioaji_quote_pending_age_seconds`` (no labels)."""
        return getattr(self._registry, "shioaji_quote_pending_age_seconds", None)

    def quote_pending_stall(self, reason: str) -> Any:
        """``shioaji_quote_pending_stall_total.labels(reason=reason)``."""
        return self._child("shioaji_quote_pending_stall_total", reason=reason)

    # ------------------------------------------------------------------
    # Session / Login
    # ------------------------------------------------------------------

    def session_lock_conflicts(self) -> Any:
        """``shioaji_session_lock_conflicts_total`` (no labels)."""
        return getattr(self._registry, "shioaji_session_lock_conflicts_total", None)

    def login_fail(self, reason: str) -> Any:
        """``shioaji_login_fail_total.labels(reason=reason)``."""
        return self._child("shioaji_login_fail_total", reason=reason)

    def crash_signature(self, signature: str, context: str) -> Any:
        """``shioaji_crash_signature_total.labels(...)``."""
        return self._child("shioaji_crash_signature_total", signature=signature, context=context)

    # ------------------------------------------------------------------
    # Keep-alive / Contract
    # ------------------------------------------------------------------

    def keepalive_failures(self) -> Any:
        """``shioaji_keepalive_failures_total`` (no labels)."""
        return getattr(self._registry, "shioaji_keepalive_failures_total", None)

    def contract_lookup_errors(self, code: str) -> Any:
        """``shioaji_contract_lookup_errors_total.labels(code=code)``."""
        return self._child("shioaji_contract_lookup_errors_total", code=code)

    # ------------------------------------------------------------------
    # Feed-level metrics (used by reconnect/resubscribe paths)
    # ------------------------------------------------------------------

    def feed_reconnect(self, result: str) -> Any:
        """``feed_reconnect_total.labels(result=result)``."""
        return self._child("feed_reconnect_total", result=result)

    def feed_resubscribe(self, result: str) -> Any:
        """``feed_resubscribe_total.labels(result=result)``."""
        return self._child("feed_resubscribe_total", result=result)

    # ------------------------------------------------------------------
    # Raw registry access (escape hatch)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> MetricsRegistry:
        """Direct access to the underlying ``MetricsRegistry``."""
        return self._registry
