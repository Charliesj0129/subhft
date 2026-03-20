"""Market data ingestion helpers: payload extraction, normalization dispatch, constants.

Private module — imported only by ``market_data.py``.
"""

from __future__ import annotations

import os
from enum import Enum
from typing import Any

from hft_platform.feature.engine import (
    QUALITY_FLAG_GAP,
    QUALITY_FLAG_OUT_OF_ORDER,
    QUALITY_FLAG_PARTIAL,
    QUALITY_FLAG_STALE_INPUT,
    QUALITY_FLAG_STATE_RESET,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MD_PRICE_FIELDS = (
    "close",
    "price",
    "bid_price",
    "ask_price",
    "bid_volume",
    "ask_volume",
    "buy_price",
    "sell_price",
)
_MD_TIME_FIELDS = ("ts", "datetime")
_MD_CODE_FIELDS = ("code", "symbol")
_MD_NESTED_FIELDS = ("tick", "bidask")
_FEATURE_QUALITY_FLAG_LABELS = (
    (QUALITY_FLAG_GAP, "gap"),
    (QUALITY_FLAG_STATE_RESET, "state_reset"),
    (QUALITY_FLAG_STALE_INPUT, "stale_input"),
    (QUALITY_FLAG_OUT_OF_ORDER, "out_of_order"),
    (QUALITY_FLAG_PARTIAL, "partial"),
)


# ---------------------------------------------------------------------------
# Feed state
# ---------------------------------------------------------------------------


class FeedState(Enum):
    INIT = "INIT"
    CONNECTING = "CONNECTING"
    SNAPSHOTTING = "SNAPSHOTTING"
    CONNECTED = "CONNECTED"
    DISCONNECTED = "DISCONNECTED"
    RECOVERING = "RECOVERING"


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def env_int(name: str, default: int) -> int:
    """Read an env var as a positive int, clamped to >= 1."""
    try:
        return max(1, int(os.getenv(name, str(default))))
    except Exception:
        return max(1, int(default))


def obs_policy() -> str:
    """Return the observability policy: ``minimal``, ``balanced``, or ``debug``."""
    policy = os.getenv("HFT_OBS_POLICY", "balanced").strip().lower()
    if policy not in {"minimal", "balanced", "debug"}:
        return "balanced"
    return policy


def get_trace_sampler():
    """Lazy-load the diagnostics trace sampler (returns ``None`` on failure)."""
    try:
        from hft_platform.diagnostics.trace import get_trace_sampler as _get

        return _get()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Payload inspection / extraction
# ---------------------------------------------------------------------------


def looks_like_md(obj: object) -> bool:
    """Heuristic: does *obj* look like a market-data payload?"""
    if obj is None:
        return False
    if isinstance(obj, dict):
        keys = obj.keys()
        if "code" in keys or "symbol" in keys:
            return True
        if (
            "bid_price" in keys
            or "ask_price" in keys
            or "close" in keys
            or "price" in keys
            or "bid_volume" in keys
            or "ask_volume" in keys
            or "buy_price" in keys
            or "sell_price" in keys
        ):
            return True
        return "ts" in keys or "datetime" in keys
    has_code = getattr(obj, "code", None) is not None or getattr(obj, "symbol", None) is not None
    has_price = (
        hasattr(obj, "bid_price")
        or hasattr(obj, "ask_price")
        or hasattr(obj, "close")
        or hasattr(obj, "price")
        or hasattr(obj, "bid_volume")
        or hasattr(obj, "ask_volume")
    )
    has_time = hasattr(obj, "ts") or hasattr(obj, "datetime")
    return bool(has_price or (has_code and (has_price or has_time)))


def unwrap_md(obj: object) -> object:
    """Unwrap nested ``tick`` / ``bidask`` wrappers."""
    if obj is None:
        return obj
    if isinstance(obj, dict):
        tick = obj.get("tick")
        if looks_like_md(tick):
            return tick
        bidask = obj.get("bidask")
        if looks_like_md(bidask):
            return bidask
        return obj
    tick = getattr(obj, "tick", None)
    if looks_like_md(tick):
        return tick
    bidask = getattr(obj, "bidask", None)
    if looks_like_md(bidask):
        return bidask
    return obj


def summarize_md(obj: object) -> dict[str, Any]:
    """Build a small debug summary of a market-data payload."""
    if obj is None:
        return {}
    nested: dict[str, str]
    if isinstance(obj, dict):
        keys = list(obj.keys())
        present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if k in obj]
        nested = {k: type(obj.get(k)).__name__ for k in _MD_NESTED_FIELDS if k in obj}
        return {"keys": keys[:20], "present": present, "nested": nested}
    present = [k for k in (*_MD_CODE_FIELDS, *_MD_PRICE_FIELDS, *_MD_TIME_FIELDS) if hasattr(obj, k)]
    nested = {}
    for k in _MD_NESTED_FIELDS:
        if hasattr(obj, k):
            nested[k] = type(getattr(obj, k)).__name__
    return {"attrs": present, "nested": nested}


def try_fast_extract_callback_payload(*args: Any, **kwargs: Any) -> tuple[object | None, object | None]:
    """Attempt to extract ``(exchange, msg)`` from a broker callback's args/kwargs."""
    exchange = kwargs.get("exchange")

    for key in ("quote", "tick", "bidask", "data", "msg"):
        if key not in kwargs:
            continue
        candidate = unwrap_md(kwargs[key])
        if looks_like_md(candidate):
            return exchange, candidate

    argc = len(args)
    if argc == 2:
        a0, a1 = args
        msg = unwrap_md(a1)
        if looks_like_md(msg):
            if exchange is None and (isinstance(a0, str) or hasattr(a0, "name")):
                exchange = a0
            return exchange, msg
        msg = unwrap_md(a0)
        if looks_like_md(msg):
            if exchange is None and (isinstance(a1, str) or hasattr(a1, "name")):
                exchange = a1
            return exchange, msg
    elif argc == 1:
        msg = unwrap_md(args[0])
        if looks_like_md(msg):
            return exchange, msg
    elif argc >= 3:
        for candidate in (args[-1], args[-2], args[0]):
            msg = unwrap_md(candidate)
            if looks_like_md(msg):
                if exchange is None and argc > 0 and (isinstance(args[0], str) or hasattr(args[0], "name")):
                    exchange = args[0]
                return exchange, msg

    return exchange, None
