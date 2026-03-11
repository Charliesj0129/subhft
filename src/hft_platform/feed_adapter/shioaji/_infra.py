"""Infrastructure utilities for the Shioaji adapter.

Standalone functions for caching, rate limiting, metrics recording,
session locking, and thread liveness tracking.  Extracted from the
monolithic ``ShioajiClient`` to allow independent testing and reuse
across submodules.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from structlog import get_logger

from hft_platform.core import timebase
from hft_platform.feed_adapter.shioaji.signatures import detect_crash_signature

logger = get_logger("feed_adapter.infra")

# ---------------------------------------------------------------------------
# Prometheus label sanitisation
# ---------------------------------------------------------------------------


def sanitize_metric_label(value: Any, *, fallback: str) -> str:
    """Ensure Prometheus label values are always strings with stable cardinality."""
    if isinstance(value, str):
        text = value
    elif isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    elif isinstance(value, type):
        text = value.__name__
    else:
        name = getattr(value, "__name__", None)
        text = str(name) if name else type(value).__name__
    text = text.strip()
    if not text:
        return fallback
    if len(text) > 64:
        return text[:64]
    return text


# ---------------------------------------------------------------------------
# API latency recording
# ---------------------------------------------------------------------------


def record_api_latency(
    metrics: Any,
    last_latency_map: dict[str, float],
    op: str,
    start_ns: int,
    ok: bool = True,
) -> None:
    """Record API call latency and jitter to Prometheus metrics."""
    if not metrics:
        return
    now_ns = time.perf_counter_ns()
    try:
        start_ns_int = int(start_ns)
    except (TypeError, ValueError):
        start_ns_int = now_ns
    latency_ms = max(0.0, (now_ns - start_ns_int) / 1e6)
    op_label = sanitize_metric_label(op, fallback="unknown")
    result = "ok" if bool(ok) else "error"
    metrics.shioaji_api_latency_ms.labels(op=op_label, result=result).observe(latency_ms)
    last = last_latency_map.get(op_label)
    if last is not None:
        jitter = abs(latency_ms - last)
        metrics.shioaji_api_jitter_ms.labels(op=op_label).set(jitter)
        if hasattr(metrics, "shioaji_api_jitter_ms_hist"):
            metrics.shioaji_api_jitter_ms_hist.labels(op=op_label).observe(jitter)
    last_latency_map[op_label] = latency_ms
    if not ok:
        metrics.shioaji_api_errors_total.labels(op=op_label).inc()


# ---------------------------------------------------------------------------
# Crash signature recording
# ---------------------------------------------------------------------------


def record_crash_signature(metrics: Any, text: str | None, *, context: str) -> None:
    """Increment crash signature counter if a known pattern is detected."""
    if not metrics or not hasattr(metrics, "shioaji_crash_signature_total"):
        return
    signature = detect_crash_signature(text)
    if not signature:
        return
    try:
        metrics.shioaji_crash_signature_total.labels(signature=signature, context=context).inc()
    except Exception:
        return


# ---------------------------------------------------------------------------
# Timeout wrapper
# ---------------------------------------------------------------------------


def safe_call_with_timeout(
    op: str,
    fn: Callable[[], Any],
    timeout_s: float,
) -> tuple[bool, Any | None, Exception | None, bool]:
    """Run a blocking broker SDK call with timeout in a daemon thread.

    Returns ``(success, result, error, timed_out)``.
    """
    if timeout_s <= 0:
        try:
            return True, fn(), None, False
        except Exception as exc:
            return False, None, exc, False
    done = threading.Event()
    state: dict[str, Any] = {}

    def _worker() -> None:
        try:
            state["result"] = fn()
        except Exception as exc:  # pragma: no cover
            state["error"] = exc
        finally:
            done.set()

    worker = threading.Thread(target=_worker, name=f"shioaji-{op}-guard", daemon=True)
    worker.start()
    if not done.wait(timeout=max(0.1, timeout_s)):
        return False, None, TimeoutError(f"{op} timed out after {timeout_s:.1f}s"), True
    err = state.get("error")
    if err is not None:
        return False, None, err, False
    return True, state.get("result"), None, False


# ---------------------------------------------------------------------------
# Thread liveness tracking
# ---------------------------------------------------------------------------


def set_thread_alive_metric(metrics: Any, thread_name: str, alive: bool) -> None:
    """Set the ``shioaji_thread_alive`` gauge for *thread_name*."""
    if not metrics or not hasattr(metrics, "shioaji_thread_alive"):
        return
    try:
        metrics.shioaji_thread_alive.labels(thread=thread_name).set(1 if alive else 0)
    except Exception:
        return


# ---------------------------------------------------------------------------
# Quote pending metrics
# ---------------------------------------------------------------------------


def update_quote_pending_metrics(
    metrics: Any,
    pending_resubscribe: bool,
    pending_ts: float,
    stall_warn_s: float,
    stall_reported: bool,
    pending_reason: str | None,
) -> bool:
    """Update quote-pending age and stall metrics.

    Returns the (possibly updated) *stall_reported* flag so callers can
    persist it.
    """
    if not metrics:
        return stall_reported
    age_s = 0.0
    if pending_resubscribe and pending_ts > 0:
        age_s = max(0.0, timebase.now_s() - pending_ts)
    try:
        if hasattr(metrics, "shioaji_quote_pending_age_seconds"):
            metrics.shioaji_quote_pending_age_seconds.set(age_s)
        if (
            pending_resubscribe
            and age_s >= stall_warn_s
            and not stall_reported
            and hasattr(metrics, "shioaji_quote_pending_stall_total")
        ):
            reason = sanitize_metric_label(pending_reason or "unknown", fallback="unknown")
            metrics.shioaji_quote_pending_stall_total.labels(reason=reason).inc()
            stall_reported = True
            logger.warning(
                "Pending quote resubscribe appears stalled",
                reason=pending_reason,
                age_s=round(age_s, 2),
            )
    except Exception:
        pass
    return stall_reported


# ---------------------------------------------------------------------------
# Session lock management
# ---------------------------------------------------------------------------


def ensure_session_lock(
    enabled: bool,
    lock_fd: Any | None,
    lock_path: str,
    metrics: Any,
    fcntl_mod: ModuleType | None,
) -> tuple[bool, Any | None]:
    """Acquire the session lock file.

    Returns ``(acquired, lock_fd)``.  If already held or locking is
    disabled, returns ``(True, existing_fd)``.
    """
    if not enabled:
        return True, lock_fd
    if lock_fd is not None:
        return True, lock_fd
    new_fd = None
    try:
        p = Path(lock_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        new_fd = open(p, "a+", encoding="utf-8")  # noqa: SIM115
        if fcntl_mod is not None:
            fcntl_mod.flock(new_fd.fileno(), fcntl_mod.LOCK_EX | fcntl_mod.LOCK_NB)
        return True, new_fd
    except Exception as exc:
        if new_fd is not None:
            try:
                new_fd.close()
            except Exception:
                pass
        logger.warning(
            "Potential duplicate broker runtime detected: session lock unavailable",
            lock_path=lock_path,
            error=str(exc),
        )
        if metrics and hasattr(metrics, "shioaji_session_lock_conflicts_total"):
            try:
                metrics.shioaji_session_lock_conflicts_total.inc()
            except Exception:
                pass
        return False, None


def release_session_lock(
    lock_fd: Any | None,
    fcntl_mod: ModuleType | None,
) -> None:
    """Release the session lock file."""
    if lock_fd is None:
        return
    try:
        if fcntl_mod is not None:
            fcntl_mod.flock(lock_fd.fileno(), fcntl_mod.LOCK_UN)
    except Exception:
        pass
    try:
        lock_fd.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TTL-aware API response cache
# ---------------------------------------------------------------------------


def cache_get(
    cache: dict[str, tuple[float, Any]],
    lock: threading.Lock,
    key: str,
) -> Any | None:
    """Return cached value for *key*, or ``None`` if expired / missing."""
    now = timebase.now_s()
    with lock:
        entry = cache.get(key)
        if not entry:
            return None
        expires_at, value = entry
        if now >= expires_at:
            cache.pop(key, None)
            return None
        return value


def cache_set(
    cache: dict[str, tuple[float, Any]],
    lock: threading.Lock,
    max_size: int,
    key: str,
    ttl_s: float,
    value: Any,
) -> None:
    """Store *value* under *key* with a TTL of *ttl_s* seconds."""
    expires_at = timebase.now_s() + max(0.0, ttl_s)
    with lock:
        if len(cache) >= max_size:
            now = timebase.now_s()
            expired_keys = [k for k, (exp, _) in cache.items() if now >= exp]
            for k in expired_keys:
                del cache[k]
            if len(cache) >= max_size:
                oldest_key = min(cache.keys(), key=lambda k: cache[k][0])
                del cache[oldest_key]
        cache[key] = (expires_at, value)


# ---------------------------------------------------------------------------
# Per-operation rate limiter
# ---------------------------------------------------------------------------


def rate_limit_api(limiter: Any, op: str) -> bool:
    """Check and record a rate-limited API call.

    Returns ``True`` if the call is allowed, ``False`` if throttled.
    """
    if not limiter.check():
        logger.warning("API rate limit hit", op=op)
        return False
    limiter.record()
    return True
