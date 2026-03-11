"""Centralized time utilities for consistent timestamp semantics."""

from __future__ import annotations

import datetime as dt
import os
import time
from typing import Any
from zoneinfo import ZoneInfo

from structlog import get_logger

logger = get_logger("core.timebase")

# Rust fast-path for coerce_ns (int/float branches)
_coerce_ns_int = None
_coerce_ns_float = None
_rust_coerce_loaded = False


def _load_rust_coerce():
    global _coerce_ns_int, _coerce_ns_float, _rust_coerce_loaded
    if _rust_coerce_loaded:
        return
    _rust_coerce_loaded = True
    if os.getenv("HFT_TIMEBASE_RUST_COERCE", "1").strip().lower() in {"0", "false", "no", "off"}:
        return
    try:
        from hft_platform.rust_core import coerce_ns_float, coerce_ns_int

        _coerce_ns_int = coerce_ns_int
        _coerce_ns_float = coerce_ns_float
    except ImportError:
        try:
            from rust_core import coerce_ns_float, coerce_ns_int

            _coerce_ns_int = coerce_ns_int
            _coerce_ns_float = coerce_ns_float
        except ImportError:
            pass


def _resolve_tz() -> tuple[str, dt.tzinfo]:
    tz_name = os.getenv("HFT_TS_TZ") or os.getenv("HFT_TS_ASSUME_TZ") or "Asia/Taipei"
    try:
        return tz_name, ZoneInfo(tz_name)
    except Exception as exc:
        logger.warning("Invalid HFT_TS_TZ, defaulting to UTC", tz=tz_name, error=str(exc))
        return tz_name, dt.timezone.utc


TZ_NAME, TZINFO = _resolve_tz()


def now_ns() -> int:
    """Wall-clock epoch time in nanoseconds (UTC)."""
    return time.time_ns()


def now_s() -> float:
    """Wall-clock epoch time in seconds (UTC)."""
    return time.time()


def monotonic_ns() -> int:
    """Monotonic time in nanoseconds for durations."""
    return time.monotonic_ns()


def perf_ns() -> int:
    """High-resolution monotonic clock in nanoseconds for profiling."""
    return time.perf_counter_ns()


def coerce_ns(ts_val: Any) -> int:
    """Coerce timestamp-like inputs into epoch nanoseconds.

    Rules:
    - datetime without tzinfo is assumed to be in HFT_TS_TZ.
    - ints/floats are interpreted by magnitude (s/ms/us/ns).
    - Uses Rust fast-path for int/float when available (HFT_TIMEBASE_RUST_COERCE=1).
    """
    if ts_val is None:
        return 0
    try:
        if hasattr(ts_val, "timestamp"):
            tzinfo = getattr(ts_val, "tzinfo", None)
            if tzinfo is None:
                ts_val = ts_val.replace(tzinfo=TZINFO)
            return int(ts_val.timestamp() * 1e9)
        _load_rust_coerce()
        if isinstance(ts_val, int):
            if _coerce_ns_int is not None:
                return _coerce_ns_int(ts_val)
            abs_ts = abs(float(ts_val))
            if abs_ts < 1e11:
                return ts_val * 1_000_000_000
            if abs_ts < 1e14:
                return ts_val * 1_000_000
            if abs_ts < 1e17:
                return ts_val * 1_000
            return ts_val
        if isinstance(ts_val, float):
            if _coerce_ns_float is not None:
                return _coerce_ns_float(ts_val)
            abs_ts = abs(float(ts_val))
            if abs_ts < 1e11:
                return int(ts_val * 1e9)
            if abs_ts < 1e14:
                return int(ts_val * 1e6)
            if abs_ts < 1e17:
                return int(ts_val * 1e3)
            return int(ts_val)
    except Exception:
        return 0
    return 0
