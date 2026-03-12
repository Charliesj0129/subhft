"""Fubon quote runtime — re-exports from quote_runtime.py for backward compatibility."""

from __future__ import annotations

import warnings

warnings.warn(
    "Import FubonQuoteRuntime from hft_platform.feed_adapter.fubon.quote_runtime instead",
    DeprecationWarning,
    stacklevel=2,
)

from hft_platform.feed_adapter.fubon.quote_runtime import FubonQuoteRuntime  # noqa: E402

__all__ = ["FubonQuoteRuntime"]
