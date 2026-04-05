"""Branded NewType aliases for compile-time safety on financial values.

These are zero-cost at runtime (erased by Python), but provide MyPy-level
guards against accidentally mixing raw ``int`` with scaled price/PnL values.

Convention: all prices and PnL values in the platform are ``int`` scaled by x10000.
"""

from typing import NewType

# Scaled integer price (x10000). E.g. 100.50 NTD → 1_005_000
ScaledPrice = NewType("ScaledPrice", int)

# Scaled integer PnL (x10000).
ScaledPnl = NewType("ScaledPnl", int)

# Scaled integer fee/tax (x10000).
ScaledFee = NewType("ScaledFee", int)

# Price scale factor: 1 NTD point = 10,000 in scaled representation.
PLATFORM_SCALE: int = 10_000

__all__ = ["ScaledPrice", "ScaledPnl", "ScaledFee", "PLATFORM_SCALE"]
