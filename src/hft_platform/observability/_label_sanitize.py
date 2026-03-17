"""Sanitize Prometheus label values to cap cardinality.

Prevents unbounded exception_type labels from causing Prometheus OOM.
"""

from __future__ import annotations

__all__ = ["sanitize_exception_type"]

_KNOWN_EXC_TYPES: frozenset[str] = frozenset(
    {
        "ValueError",
        "TypeError",
        "KeyError",
        "IndexError",
        "AttributeError",
        "RuntimeError",
        "ZeroDivisionError",
        "OverflowError",
        "StopIteration",
        "ConnectionError",
        "TimeoutError",
        "OSError",
        "IOError",
    }
)


def sanitize_exception_type(exc: BaseException) -> str:
    """Return exception class name if known, else 'other'."""
    name = type(exc).__name__
    return name if name in _KNOWN_EXC_TYPES else "other"
