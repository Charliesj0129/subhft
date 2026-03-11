"""Fubon account gateway — re-exports from account_gateway.py for backward compatibility."""

from __future__ import annotations

import warnings

warnings.warn(
    "Import FubonAccountGateway from hft_platform.feed_adapter.fubon.account_gateway instead",
    DeprecationWarning,
    stacklevel=2,
)

from hft_platform.feed_adapter.fubon.account_gateway import FubonAccountGateway

__all__ = ["FubonAccountGateway"]
