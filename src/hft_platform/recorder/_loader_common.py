"""Shared utilities for loader sub-modules.

Centralises JSON codec selection and the ClickHouse price-scale factor so
that every helper module uses the same implementation without circular
imports.
"""

from __future__ import annotations

import os

from structlog import get_logger

from hft_platform.core import timebase

logger = get_logger("wal_loader")

# ---------------------------------------------------------------------------
# JSON codec: prefer orjson, fallback to stdlib json
# ---------------------------------------------------------------------------
try:
    import orjson

    def _dumps(obj: object) -> str:
        return orjson.dumps(obj).decode()

    _loads = orjson.loads
except ImportError:
    import json

    _dumps = json.dumps  # type: ignore[assignment]
    _loads = json.loads  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# ClickHouse price scale constant
# ---------------------------------------------------------------------------
PRICE_SCALE: int = 1_000_000


def _to_scaled(val: float | int | None) -> int:
    """Convert a float/int price to ClickHouse scaled-int representation."""
    if val is None:
        return 0
    return int(round(float(val) * PRICE_SCALE))


# ---------------------------------------------------------------------------
# Retry defaults
# ---------------------------------------------------------------------------
DEFAULT_INSERT_MAX_RETRIES: int = 3
DEFAULT_INSERT_BASE_DELAY_S: float = 0.5
DEFAULT_INSERT_MAX_BACKOFF_S: float = 5.0

# ---------------------------------------------------------------------------
# Timestamp validation
# ---------------------------------------------------------------------------
try:
    _TS_MAX_FUTURE_NS: int = int(float(os.getenv("HFT_TS_MAX_FUTURE_S", "5")) * 1e9)
except Exception as _exc:
    logger.warning(
        "Failed to parse HFT_TS_MAX_FUTURE_S, timestamp validation disabled",
        error=str(_exc),
        env_value=os.getenv("HFT_TS_MAX_FUTURE_S"),
    )
    _TS_MAX_FUTURE_NS = 0


__all__ = [
    "logger",
    "_dumps",
    "_loads",
    "PRICE_SCALE",
    "_to_scaled",
    "DEFAULT_INSERT_MAX_RETRIES",
    "DEFAULT_INSERT_BASE_DELAY_S",
    "DEFAULT_INSERT_MAX_BACKOFF_S",
    "_TS_MAX_FUTURE_NS",
    "timebase",
]
