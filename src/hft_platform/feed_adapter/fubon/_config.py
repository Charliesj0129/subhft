"""Fubon broker configuration constants."""

from __future__ import annotations

# Price scaling factor (platform convention: x10000)
PRICE_SCALE: int = 10_000

# Default exchange for Fubon symbols
DEFAULT_EXCHANGE: str = "TSE"

# Default user-defined tag for Fubon orders
DEFAULT_USER_DEF: str = "HFT"
