from __future__ import annotations

from typing import Final

# Known crash/abort precursor signatures observed in production logs.
# Keep patterns lowercase for case-insensitive matching.
CRASH_SIGNATURE_PATTERNS: Final[tuple[tuple[str, str], ...]] = (
    ("none_subscribe", "nonetype' object has no attribute 'subscribe"),
    ("none_callable", "nonetype' object is not callable"),
    ("thread_lock_callable", "_thread.lock' object is not callable"),
    ("datetime_parse_hour", "unexpected end of string while parsing hour"),
    ("pybind11_error_already_set", "pybind11::error_already_set"),
)


def detect_crash_signature(text: str | None) -> str | None:
    if not text:
        return None
    payload = str(text).lower()
    for signature, needle in CRASH_SIGNATURE_PATTERNS:
        if needle in payload:
            return signature
    return None

