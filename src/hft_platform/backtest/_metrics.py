"""WU-12: Optional Prometheus metrics for backtest runs.

Feature-flagged via HFT_BACKTEST_METRICS (default off).
"""

from __future__ import annotations

import os

_ENABLED = os.environ.get("HFT_BACKTEST_METRICS", "0") == "1"

# Lazy-init metrics to avoid import-time Prometheus dependency when disabled
_metrics_initialized = False
_ticks_total = None
_tick_overhead_us = None
_fills_total = None
_run_duration_seconds = None


def _ensure_metrics() -> None:
    """Initialize Prometheus metrics on first use."""
    global _metrics_initialized, _ticks_total, _tick_overhead_us, _fills_total, _run_duration_seconds

    if _metrics_initialized:
        return
    _metrics_initialized = True

    try:
        from prometheus_client import Counter, Histogram

        _ticks_total = Counter(
            "hft_backtest_ticks_processed_total",
            "Total ticks processed in backtest",
            ["symbol", "mode"],
        )
        _tick_overhead_us = Histogram(
            "hft_backtest_tick_overhead_us",
            "Per-tick overhead in microseconds",
            ["mode"],
            buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
        )
        _fills_total = Counter(
            "hft_backtest_fills_total",
            "Total fills in backtest",
            ["symbol", "side"],
        )
        _run_duration_seconds = Histogram(
            "hft_backtest_run_duration_seconds",
            "Total backtest run duration",
            ["mode"],
            buckets=[0.1, 0.5, 1, 5, 10, 30, 60, 120, 300],
        )
    except ImportError:
        pass


def is_enabled() -> bool:
    """Check if backtest metrics are enabled."""
    return _ENABLED


def record_tick(symbol: str, mode: str) -> None:
    """Record one tick processed."""
    if not _ENABLED:
        return
    _ensure_metrics()
    if _ticks_total is not None:
        _ticks_total.labels(symbol=symbol, mode=mode).inc()


def record_tick_overhead(mode: str, overhead_us: float) -> None:
    """Record per-tick overhead in microseconds."""
    if not _ENABLED:
        return
    _ensure_metrics()
    if _tick_overhead_us is not None:
        _tick_overhead_us.labels(mode=mode).observe(overhead_us)


def record_fill(symbol: str, side: str) -> None:
    """Record a fill event."""
    if not _ENABLED:
        return
    _ensure_metrics()
    if _fills_total is not None:
        _fills_total.labels(symbol=symbol, side=side).inc()


def record_run_duration(mode: str, duration_seconds: float) -> None:
    """Record total backtest run duration."""
    if not _ENABLED:
        return
    _ensure_metrics()
    if _run_duration_seconds is not None:
        _run_duration_seconds.labels(mode=mode).observe(duration_seconds)
