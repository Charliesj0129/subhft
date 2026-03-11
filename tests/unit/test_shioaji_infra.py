"""Tests for hft_platform.feed_adapter.shioaji._infra standalone utilities."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

from hft_platform.feed_adapter.shioaji._infra import (
    cache_get,
    cache_set,
    ensure_session_lock,
    rate_limit_api,
    record_api_latency,
    record_crash_signature,
    release_session_lock,
    safe_call_with_timeout,
    sanitize_metric_label,
    set_thread_alive_metric,
    update_quote_pending_metrics,
)


# ---------------------------------------------------------------------------
# sanitize_metric_label
# ---------------------------------------------------------------------------


class TestSanitizeMetricLabel:
    def test_string_passthrough(self) -> None:
        assert sanitize_metric_label("login", fallback="x") == "login"

    def test_empty_string_uses_fallback(self) -> None:
        assert sanitize_metric_label("", fallback="unknown") == "unknown"

    def test_whitespace_only_uses_fallback(self) -> None:
        assert sanitize_metric_label("   ", fallback="fb") == "fb"

    def test_bytes_decoded(self) -> None:
        assert sanitize_metric_label(b"hello", fallback="x") == "hello"

    def test_type_object_uses_name(self) -> None:
        assert sanitize_metric_label(int, fallback="x") == "int"

    def test_object_with_name_attr(self) -> None:
        def my_func() -> None:
            pass

        assert sanitize_metric_label(my_func, fallback="x") == "my_func"

    def test_truncation_at_64(self) -> None:
        long = "a" * 100
        result = sanitize_metric_label(long, fallback="x")
        assert len(result) == 64

    def test_object_without_name(self) -> None:
        obj = object()
        result = sanitize_metric_label(obj, fallback="x")
        assert result == "object"


# ---------------------------------------------------------------------------
# record_api_latency
# ---------------------------------------------------------------------------


class TestRecordApiLatency:
    def test_no_metrics_noop(self) -> None:
        # Should not raise
        record_api_latency(None, {}, "login", time.perf_counter_ns())

    def test_records_latency_ok(self) -> None:
        hist = MagicMock()
        gauge = MagicMock()
        counter = MagicMock()
        metrics = SimpleNamespace(
            shioaji_api_latency_ms=hist,
            shioaji_api_jitter_ms=gauge,
            shioaji_api_errors_total=counter,
        )
        last_map: dict[str, float] = {}
        start = time.perf_counter_ns()
        record_api_latency(metrics, last_map, "login", start, ok=True)
        hist.labels.assert_called_once_with(op="login", result="ok")
        hist.labels().observe.assert_called_once()
        assert "login" in last_map

    def test_records_jitter_on_second_call(self) -> None:
        hist = MagicMock()
        gauge = MagicMock()
        counter = MagicMock()
        metrics = SimpleNamespace(
            shioaji_api_latency_ms=hist,
            shioaji_api_jitter_ms=gauge,
            shioaji_api_errors_total=counter,
        )
        last_map: dict[str, float] = {"login": 5.0}
        start = time.perf_counter_ns()
        record_api_latency(metrics, last_map, "login", start, ok=True)
        gauge.labels.assert_called_with(op="login")
        gauge.labels().set.assert_called_once()

    def test_records_error(self) -> None:
        hist = MagicMock()
        gauge = MagicMock()
        counter = MagicMock()
        metrics = SimpleNamespace(
            shioaji_api_latency_ms=hist,
            shioaji_api_jitter_ms=gauge,
            shioaji_api_errors_total=counter,
        )
        record_api_latency(metrics, {}, "login", time.perf_counter_ns(), ok=False)
        counter.labels.assert_called_with(op="login")
        counter.labels().inc.assert_called_once()

    def test_invalid_start_ns(self) -> None:
        hist = MagicMock()
        metrics = SimpleNamespace(
            shioaji_api_latency_ms=hist,
            shioaji_api_jitter_ms=MagicMock(),
            shioaji_api_errors_total=MagicMock(),
        )
        # Should not raise even with invalid start_ns
        record_api_latency(metrics, {}, "op", "not_a_number", ok=True)  # type: ignore[arg-type]
        hist.labels().observe.assert_called_once()


# ---------------------------------------------------------------------------
# record_crash_signature
# ---------------------------------------------------------------------------


class TestRecordCrashSignature:
    def test_no_metrics_noop(self) -> None:
        record_crash_signature(None, "some error", context="test")

    def test_no_match_noop(self) -> None:
        counter = MagicMock()
        metrics = SimpleNamespace(shioaji_crash_signature_total=counter)
        record_crash_signature(metrics, "ordinary error", context="test")
        counter.labels.assert_not_called()

    def test_known_pattern_increments(self) -> None:
        counter = MagicMock()
        metrics = SimpleNamespace(shioaji_crash_signature_total=counter)
        record_crash_signature(
            metrics,
            "NoneType' object has no attribute 'subscribe",
            context="quote",
        )
        counter.labels.assert_called_once()
        counter.labels().inc.assert_called_once()


# ---------------------------------------------------------------------------
# safe_call_with_timeout
# ---------------------------------------------------------------------------


class TestSafeCallWithTimeout:
    def test_no_timeout_success(self) -> None:
        ok, result, err, timed_out = safe_call_with_timeout("op", lambda: 42, 0)
        assert ok is True
        assert result == 42
        assert err is None
        assert timed_out is False

    def test_no_timeout_exception(self) -> None:
        ok, result, err, timed_out = safe_call_with_timeout(
            "op", lambda: 1 / 0, 0
        )
        assert ok is False
        assert isinstance(err, ZeroDivisionError)
        assert timed_out is False

    def test_with_timeout_success(self) -> None:
        ok, result, err, timed_out = safe_call_with_timeout("op", lambda: 99, 5.0)
        assert ok is True
        assert result == 99
        assert err is None
        assert timed_out is False

    def test_with_timeout_times_out(self) -> None:
        import time as _time

        ok, result, err, timed_out = safe_call_with_timeout(
            "op", lambda: _time.sleep(10), 0.1
        )
        assert ok is False
        assert isinstance(err, TimeoutError)
        assert timed_out is True

    def test_negative_timeout_immediate(self) -> None:
        ok, result, err, timed_out = safe_call_with_timeout("op", lambda: "x", -1)
        assert ok is True
        assert result == "x"


# ---------------------------------------------------------------------------
# set_thread_alive_metric
# ---------------------------------------------------------------------------


class TestSetThreadAliveMetric:
    def test_no_metrics_noop(self) -> None:
        set_thread_alive_metric(None, "test", True)

    def test_sets_gauge(self) -> None:
        gauge = MagicMock()
        metrics = SimpleNamespace(shioaji_thread_alive=gauge)
        set_thread_alive_metric(metrics, "session_refresh", True)
        gauge.labels.assert_called_with(thread="session_refresh")
        gauge.labels().set.assert_called_with(1)

    def test_sets_zero_when_dead(self) -> None:
        gauge = MagicMock()
        metrics = SimpleNamespace(shioaji_thread_alive=gauge)
        set_thread_alive_metric(metrics, "watchdog", False)
        gauge.labels().set.assert_called_with(0)


# ---------------------------------------------------------------------------
# update_quote_pending_metrics
# ---------------------------------------------------------------------------


class TestUpdateQuotePendingMetrics:
    def test_no_metrics_returns_flag(self) -> None:
        result = update_quote_pending_metrics(None, True, 1.0, 10.0, False, "reason")
        assert result is False

    def test_no_pending_sets_zero_age(self) -> None:
        gauge = MagicMock()
        metrics = SimpleNamespace(shioaji_quote_pending_age_seconds=gauge)
        result = update_quote_pending_metrics(metrics, False, 0.0, 120.0, False, None)
        gauge.set.assert_called_with(0.0)
        assert result is False

    def test_stall_detection(self) -> None:
        gauge = MagicMock()
        stall_counter = MagicMock()
        metrics = SimpleNamespace(
            shioaji_quote_pending_age_seconds=gauge,
            shioaji_quote_pending_stall_total=stall_counter,
        )
        # pending_ts far in the past -> large age -> stall
        result = update_quote_pending_metrics(
            metrics, True, 0.001, 0.001, False, "test_reason"
        )
        assert result is True
        stall_counter.labels.assert_called_once()


# ---------------------------------------------------------------------------
# ensure_session_lock / release_session_lock
# ---------------------------------------------------------------------------


class TestSessionLock:
    def test_disabled_returns_true(self) -> None:
        ok, fd = ensure_session_lock(False, None, "/tmp/test.lock", None, None)
        assert ok is True
        assert fd is None

    def test_already_held(self) -> None:
        sentinel = object()
        ok, fd = ensure_session_lock(True, sentinel, "/tmp/test.lock", None, None)
        assert ok is True
        assert fd is sentinel

    def test_acquire_and_release(self, tmp_path) -> None:
        lock_path = str(tmp_path / "test.lock")
        ok, fd = ensure_session_lock(True, None, lock_path, None, None)
        assert ok is True
        assert fd is not None
        release_session_lock(fd, None)

    def test_release_none_noop(self) -> None:
        release_session_lock(None, None)  # Should not raise


# ---------------------------------------------------------------------------
# cache_get / cache_set
# ---------------------------------------------------------------------------


class TestCache:
    def test_get_missing_returns_none(self) -> None:
        cache: dict[str, tuple[float, object]] = {}
        lock = threading.Lock()
        assert cache_get(cache, lock, "missing") is None

    def test_set_and_get(self) -> None:
        cache: dict[str, tuple[float, object]] = {}
        lock = threading.Lock()
        cache_set(cache, lock, 100, "key1", 10.0, "value1")
        assert cache_get(cache, lock, "key1") == "value1"

    def test_expired_entry_returns_none(self) -> None:
        cache: dict[str, tuple[float, object]] = {}
        lock = threading.Lock()
        cache_set(cache, lock, 100, "key1", 0.0, "value1")
        # TTL=0 means expires immediately at next check
        # Force expiry by manipulating cache directly
        cache["key1"] = (0.0, "value1")
        assert cache_get(cache, lock, "key1") is None

    def test_eviction_at_max_size(self) -> None:
        cache: dict[str, tuple[float, object]] = {}
        lock = threading.Lock()
        max_size = 2
        cache_set(cache, lock, max_size, "a", 100.0, 1)
        cache_set(cache, lock, max_size, "b", 100.0, 2)
        # Third insert should evict something
        cache_set(cache, lock, max_size, "c", 100.0, 3)
        assert len(cache) <= max_size


# ---------------------------------------------------------------------------
# rate_limit_api
# ---------------------------------------------------------------------------


class TestRateLimitApi:
    def test_allowed(self) -> None:
        limiter = MagicMock()
        limiter.check.return_value = True
        assert rate_limit_api(limiter, "positions") is True
        limiter.record.assert_called_once()

    def test_throttled(self) -> None:
        limiter = MagicMock()
        limiter.check.return_value = False
        assert rate_limit_api(limiter, "positions") is False
        limiter.record.assert_not_called()
